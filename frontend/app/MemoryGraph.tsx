"use client";

import {
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import { Download, FileText, Search, X } from "lucide-react";

import type { MemoryGraphNode, MemoryVault } from "./page";

// --- simulation tuning -------------------------------------------------------
const K_REPEL = 5200; // Coulomb charge (node-node repulsion)
const K_SPRING = 0.022; // Hooke link stiffness
const K_GRAVITY = 0.009; // pull toward the world centre so the graph stays framed
const DAMPING = 0.85;
const MIN_D2 = 120; // clamp repulsion at very short range
const MIN_ALPHA = 0.02; // freeze the sim below this "temperature"
const COOL = 0.986;
const REHEAT = 0.6;
const WORLD_W = 900;
const WORLD_H = 560;

const REST_BY_KIND: Record<string, number> = {
  namespace: 62,
  tag: 54,
  link: 74,
  mentions: 96,
  "co-source": 84,
  "co-day": 108,
  "same-content": 72
};

const HUB_KINDS = new Set(["namespace", "folder", "daybucket"]);

const KIND_LABELS: Record<string, string> = {
  memory: "Память",
  document: "Документы",
  namespace: "Разделы",
  tag: "Теги",
  link: "Ссылки",
  folder: "Папки",
  daybucket: "Даты"
};

type SimNode = {
  id: string;
  label: string;
  kind: string;
  degree: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
  dragging: boolean;
  pinned: boolean;
  data: MemoryGraphNode;
  el: SVGGElement | null;
};

type ViewBox = { x: number; y: number; w: number; h: number };

function restLength(kind: string): number {
  return REST_BY_KIND[kind] ?? 82;
}

function nodeRadius(degree: number, kind: string): number {
  const base = 4.5 + Math.sqrt(Math.max(0, degree)) * 2.3;
  const bonus = HUB_KINDS.has(kind) ? 3 : kind === "document" ? 1.5 : 0;
  return Math.max(4.5, Math.min(base + bonus, 22));
}

function hashAngle(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) | 0;
  return ((h >>> 0) % 3600) / 3600 * Math.PI * 2;
}

function formatSize(bytes?: number | null): string {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

export function MemoryGraph({ vault }: { vault: MemoryVault }) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const simRef = useRef<Map<string, SimNode>>(new Map());
  const edgeElsRef = useRef<Array<SVGLineElement | null>>([]);
  const alphaRef = useRef<number>(0);
  const rafRef = useRef<number | null>(null);
  const vbRef = useRef<ViewBox>({ x: -WORLD_W / 2, y: -WORLD_H / 2, w: WORLD_W, h: WORLD_H });

  const [vb, setVbState] = useState<ViewBox>(vbRef.current);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set());
  // A real vault can hold thousands of notes; the hand-rolled O(N^2) sim + SVG DOM stay
  // smooth by rendering a budget of the most-connected nodes (hubs + documents first).
  const [renderCap, setRenderCap] = useState(600);

  const setViewBox = useCallback((next: ViewBox) => {
    vbRef.current = next;
    setVbState(next);
  }, []);

  const kindsPresent = useMemo(() => {
    const order = ["memory", "document", "namespace", "tag", "folder", "daybucket", "link"];
    const present = new Set(vault.nodes.map((n) => n.kind));
    return order.filter((k) => present.has(k));
  }, [vault.nodes]);

  // Filtered graph: kind filters REMOVE nodes from the sim; search only highlights.
  // When the vault is large, keep a budget of the most meaningful nodes: hubs and
  // documents first, then the highest-degree memories/tags.
  const { nodes, edges, totalNodes, truncated } = useMemo(() => {
    const byKind = vault.nodes.filter((n) => !hiddenKinds.has(n.kind));
    let keep = byKind;
    let truncated = false;
    if (byKind.length > renderCap) {
      truncated = true;
      const priority = (n: MemoryGraphNode) =>
        HUB_KINDS.has(n.kind) ? 3 : n.kind === "document" ? 2 : 1;
      keep = [...byKind]
        .sort((a, b) => {
          const pa = priority(a);
          const pb = priority(b);
          if (pa !== pb) return pb - pa;
          return (b.degree ?? 0) - (a.degree ?? 0);
        })
        .slice(0, renderCap);
    }
    const keepIds = new Set(keep.map((n) => n.id));
    const keptEdges = vault.edges.filter(
      (e) => keepIds.has(e.source) && keepIds.has(e.target)
    );
    return { nodes: keep, edges: keptEdges, totalNodes: byKind.length, truncated };
  }, [vault.nodes, vault.edges, hiddenKinds, renderCap]);

  const noteById = useMemo(() => {
    const map = new Map<string, MemoryVault["notes"][number]>();
    for (const note of vault.notes) {
      const key = note.id || note.path;
      if (key) map.set(key, note);
    }
    return map;
  }, [vault.notes]);

  const adjacency = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const e of edges) {
      if (!map.has(e.source)) map.set(e.source, new Set());
      if (!map.has(e.target)) map.set(e.target, new Set());
      map.get(e.source)!.add(e.target);
      map.get(e.target)!.add(e.source);
    }
    return map;
  }, [edges]);

  const searchLc = search.trim().toLowerCase();
  const searchMatch = useMemo(() => {
    if (!searchLc) return null;
    const hits = new Set<string>();
    for (const n of nodes) {
      const tags = (n.tags || []).join(" ").toLowerCase();
      if (n.label.toLowerCase().includes(searchLc) || tags.includes(searchLc)) hits.add(n.id);
    }
    return hits;
  }, [nodes, searchLc]);

  // Highlight set = hovered/selected node + its direct neighbours.
  const focusId = hoverId ?? selectedId;
  const highlight = useMemo(() => {
    if (!focusId) return null;
    const set = new Set<string>([focusId]);
    for (const nb of adjacency.get(focusId) ?? []) set.add(nb);
    return set;
  }, [focusId, adjacency]);

  const reheat = useCallback(() => {
    alphaRef.current = Math.max(alphaRef.current, REHEAT);
    if (rafRef.current == null) startLoop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Rebuild the sim whenever the filtered node/edge set changes, keeping positions.
  useEffect(() => {
    const prev = simRef.current;
    const next = new Map<string, SimNode>();
    nodes.forEach((node, index) => {
      const existing = prev.get(node.id);
      if (existing) {
        existing.degree = node.degree ?? 0;
        existing.data = node;
        existing.label = node.label;
        next.set(node.id, existing);
      } else {
        const angle = hashAngle(node.id);
        const radius = 40 + (index % 40) * 6;
        next.set(node.id, {
          id: node.id,
          label: node.label,
          kind: node.kind,
          degree: node.degree ?? 0,
          x: Math.cos(angle) * radius,
          y: Math.sin(angle) * radius,
          vx: 0,
          vy: 0,
          dragging: false,
          pinned: false,
          data: node,
          el: null
        });
      }
    });
    simRef.current = next;
    edgeElsRef.current = new Array(edges.length).fill(null);
    alphaRef.current = 1;
    startLoop();
    return stopLoop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges]);

  const stopLoop = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const startLoop = useCallback(() => {
    if (rafRef.current != null) return;
    const step = () => {
      const sim = simRef.current;
      const arr = Array.from(sim.values());
      const n = arr.length;
      // Coulomb repulsion (all pairs).
      for (let i = 0; i < n; i += 1) {
        const a = arr[i];
        for (let j = i + 1; j < n; j += 1) {
          const b = arr[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < MIN_D2) {
            d2 = MIN_D2;
            if (dx === 0 && dy === 0) {
              dx = (i - j) * 0.5 + 0.1;
              dy = (j - i) * 0.5 + 0.1;
            }
          }
          const f = K_REPEL / d2;
          const dist = Math.sqrt(d2);
          const fx = (dx / dist) * f;
          const fy = (dy / dist) * f;
          a.vx += fx;
          a.vy += fy;
          b.vx -= fx;
          b.vy -= fy;
        }
      }
      // Link springs.
      for (const e of edges) {
        const a = sim.get(e.source);
        const b = sim.get(e.target);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const force = (dist - restLength(e.kind)) * K_SPRING;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
      // Gravity + integrate.
      for (const node of arr) {
        if (node.dragging || node.pinned) {
          node.vx = 0;
          node.vy = 0;
        } else {
          node.vx = (node.vx - node.x * K_GRAVITY) * DAMPING;
          node.vy = (node.vy - node.y * K_GRAVITY) * DAMPING;
          node.x += node.vx * alphaRef.current;
          node.y += node.vy * alphaRef.current;
        }
        if (node.el) node.el.setAttribute("transform", `translate(${node.x.toFixed(2)} ${node.y.toFixed(2)})`);
      }
      // Position edges.
      edges.forEach((e, index) => {
        const line = edgeElsRef.current[index];
        if (!line) return;
        const a = sim.get(e.source);
        const b = sim.get(e.target);
        if (!a || !b) return;
        line.setAttribute("x1", a.x.toFixed(2));
        line.setAttribute("y1", a.y.toFixed(2));
        line.setAttribute("x2", b.x.toFixed(2));
        line.setAttribute("y2", b.y.toFixed(2));
      });
      alphaRef.current *= COOL;
      if (alphaRef.current > MIN_ALPHA && !document.hidden) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        rafRef.current = null;
      }
    };
    rafRef.current = requestAnimationFrame(step);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edges]);

  // Resume the sim when the tab becomes visible again.
  useEffect(() => {
    const onVisible = () => {
      if (!document.hidden && alphaRef.current > MIN_ALPHA) startLoop();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [startLoop]);

  const screenToWorld = useCallback((clientX: number, clientY: number) => {
    const rect = svgRef.current?.getBoundingClientRect();
    const view = vbRef.current;
    if (!rect || rect.width === 0) return { x: 0, y: 0 };
    return {
      x: view.x + ((clientX - rect.left) / rect.width) * view.w,
      y: view.y + ((clientY - rect.top) / rect.height) * view.h
    };
  }, []);

  // Background pan.
  const beginPan = useCallback(
    (event: ReactPointerEvent<SVGSVGElement>) => {
      if (event.target !== svgRef.current) return; // only when the empty canvas is grabbed
      const start = { x: event.clientX, y: event.clientY };
      const base = { ...vbRef.current };
      const rect = svgRef.current?.getBoundingClientRect();
      const scaleX = rect ? base.w / rect.width : 1;
      const scaleY = rect ? base.h / rect.height : 1;
      const onMove = (moveEvent: PointerEvent) => {
        setViewBox({
          ...base,
          x: base.x - (moveEvent.clientX - start.x) * scaleX,
          y: base.y - (moveEvent.clientY - start.y) * scaleY
        });
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
    },
    [setViewBox]
  );

  // Node drag.
  const beginNodeDrag = useCallback(
    (event: ReactPointerEvent<SVGGElement>, id: string) => {
      event.stopPropagation();
      const node = simRef.current.get(id);
      if (!node) return;
      node.dragging = true;
      reheat();
      const onMove = (moveEvent: PointerEvent) => {
        const world = screenToWorld(moveEvent.clientX, moveEvent.clientY);
        const target = simRef.current.get(id);
        if (target) {
          target.x = world.x;
          target.y = world.y;
          target.vx = 0;
          target.vy = 0;
        }
        alphaRef.current = Math.max(alphaRef.current, REHEAT);
        if (rafRef.current == null) startLoop();
      };
      const onUp = () => {
        const target = simRef.current.get(id);
        if (target) target.dragging = false;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
    },
    [reheat, screenToWorld, startLoop]
  );

  // Native wheel listener (passive: false) so zoom can preventDefault the page scroll —
  // React's synthetic onWheel is passive and would let the panel scroll under the cursor.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return undefined;
    const handler = (event: WheelEvent) => {
      event.preventDefault();
      const view = vbRef.current;
      const factor = event.deltaY > 0 ? 1.12 : 0.89;
      const newW = Math.max(WORLD_W * 0.2, Math.min(view.w * factor, WORLD_W * 4));
      const newH = newW * (view.h / view.w);
      const focus = screenToWorld(event.clientX, event.clientY);
      setViewBox({
        w: newW,
        h: newH,
        x: focus.x - (focus.x - view.x) * (newW / view.w),
        y: focus.y - (focus.y - view.y) * (newH / view.h)
      });
    };
    svg.addEventListener("wheel", handler, { passive: false });
    return () => svg.removeEventListener("wheel", handler);
  }, [screenToWorld, setViewBox]);

  const fitView = useCallback(() => {
    const arr = Array.from(simRef.current.values());
    if (!arr.length) {
      setViewBox({ x: -WORLD_W / 2, y: -WORLD_H / 2, w: WORLD_W, h: WORLD_H });
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of arr) {
      minX = Math.min(minX, node.x);
      minY = Math.min(minY, node.y);
      maxX = Math.max(maxX, node.x);
      maxY = Math.max(maxY, node.y);
    }
    const pad = 60;
    const w = Math.max(maxX - minX + pad * 2, 200);
    const h = Math.max(maxY - minY + pad * 2, 160);
    setViewBox({ x: minX - pad, y: minY - pad, w, h });
  }, [setViewBox]);

  // Auto-fit once after the initial layout settles.
  useEffect(() => {
    const timer = window.setTimeout(fitView, 900);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vault.stats.nodes]);

  const toggleKind = (kind: string) => {
    setHiddenKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const selectedNode = selectedId ? vault.nodes.find((n) => n.id === selectedId) ?? null : null;

  const nodeClass = (node: MemoryGraphNode) => {
    const classes = [`mg-node`, `mg-${node.kind}`];
    if (HUB_KINDS.has(node.kind)) classes.push("mg-hub");
    if (node.id === selectedId) classes.push("mg-selected");
    if (highlight) classes.push(highlight.has(node.id) ? "mg-hi" : "mg-dim");
    if (searchMatch) classes.push(searchMatch.has(node.id) ? "mg-match" : "mg-nomatch");
    return classes.join(" ");
  };

  const edgeClass = (edge: { source: string; target: string; kind: string }) => {
    const classes = ["mg-edge", `mg-edge-${edge.kind}`];
    if (highlight) {
      const on = highlight.has(edge.source) && highlight.has(edge.target);
      classes.push(on ? "mg-hi" : "mg-dim");
    }
    return classes.join(" ");
  };

  if (!vault.nodes.length) {
    return (
      <div className="mg-empty">
        Граф пуст — пока нет ни записей памяти, ни документов. Сохраните заметку или загрузите файл.
      </div>
    );
  }

  return (
    <div className="mg-wrap">
      <div className="mg-toolbar">
        <label className="mg-search">
          <Search size={14} />
          <input
            value={search}
            placeholder="Поиск по узлам…"
            onChange={(event) => {
              setSearch(event.target.value);
            }}
          />
          {search && (
            <button type="button" onClick={() => setSearch("")} aria-label="Очистить">
              <X size={13} />
            </button>
          )}
        </label>
        <div className="mg-chips">
          {kindsPresent.map((kind) => (
            <button
              key={kind}
              type="button"
              className={`mg-chip mg-chip-${kind} ${hiddenKinds.has(kind) ? "off" : ""}`}
              onClick={() => toggleKind(kind)}
            >
              <span className="mg-chip-dot" />
              {KIND_LABELS[kind] ?? kind}
            </button>
          ))}
        </div>
        <select
          className="mg-cap"
          value={renderCap}
          onChange={(event) => setRenderCap(Number(event.target.value))}
          title="Сколько узлов показывать"
        >
          <option value={300}>300 узлов</option>
          <option value={600}>600 узлов</option>
          <option value={1200}>1200 узлов</option>
          <option value={100000}>все узлы</option>
        </select>
        <button type="button" className="mg-fit" onClick={fitView}>
          Сброс вида
        </button>
      </div>

      <div className="mg-stage">
        <svg
          ref={svgRef}
          className="mg-canvas"
          viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
          onPointerDown={beginPan}
        >
          <g className="mg-edges">
            {edges.map((edge, index) => (
              <line
                key={`${edge.source}::${edge.target}::${edge.kind}::${index}`}
                ref={(el) => {
                  edgeElsRef.current[index] = el;
                }}
                className={edgeClass(edge)}
              />
            ))}
          </g>
          <g className="mg-nodes">
            {nodes.map((node) => {
              const radius = nodeRadius(node.degree ?? 0, node.kind);
              return (
                <g
                  key={node.id}
                  className={nodeClass(node)}
                  ref={(el) => {
                    const sim = simRef.current.get(node.id);
                    if (sim) sim.el = el;
                  }}
                  onPointerDown={(event) => beginNodeDrag(event, node.id)}
                  onPointerEnter={() => setHoverId(node.id)}
                  onPointerLeave={() => setHoverId((current) => (current === node.id ? null : current))}
                  onClick={(event) => {
                    event.stopPropagation();
                    setSelectedId(node.id);
                  }}
                >
                  {node.kind === "document" && (
                    <rect
                      className="mg-doc-back"
                      x={-radius}
                      y={-radius}
                      width={radius * 2}
                      height={radius * 2}
                      rx={2.5}
                    />
                  )}
                  <circle className="mg-dot" r={radius} />
                  <text className="mg-label" x={radius + 3} y={3.5}>
                    {node.label.length > 24 ? `${node.label.slice(0, 23)}…` : node.label}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>

        {selectedNode && (
          <aside className="mg-detail">
            <header>
              <span className={`mg-detail-kind mg-${selectedNode.kind}`}>
                {selectedNode.kind === "document" ? <FileText size={13} /> : null}
                {KIND_LABELS[selectedNode.kind] ?? selectedNode.kind}
              </span>
              <button type="button" onClick={() => setSelectedId(null)} aria-label="Закрыть">
                <X size={14} />
              </button>
            </header>
            <h4>{selectedNode.label}</h4>
            <DetailBody
              node={selectedNode}
              note={noteById.get(selectedNode.id)}
              backlinks={vault.backlinks[selectedNode.label] ?? vault.backlinks[selectedNode.id] ?? []}
              neighbours={Array.from(adjacency.get(selectedNode.id) ?? []).length}
            />
          </aside>
        )}
      </div>

      <div className="mg-footer">
        {truncated ? (
          <span className="mg-warn">
            показано {nodes.length} из {totalNodes} узлов
          </span>
        ) : (
          <span>{nodes.length} узлов</span>
        )}
        <span>{edges.length} связей</span>
        <span>{vault.stats.documents ?? 0} документов</span>
        <span className="mg-hint">колесо — зум · фон — панорама · узел — детали</span>
      </div>
    </div>
  );
}

function DetailBody({
  node,
  note,
  backlinks,
  neighbours
}: {
  node: MemoryGraphNode;
  note?: MemoryVault["notes"][number];
  backlinks: string[];
  neighbours: number;
}) {
  if (node.kind === "document") {
    return (
      <dl className="mg-meta">
        <div>
          <dt>Тип</dt>
          <dd>{node.mime || "—"}</dd>
        </div>
        <div>
          <dt>Размер</dt>
          <dd>{formatSize(node.size)}</dd>
        </div>
        <div>
          <dt>Загружен</dt>
          <dd>{(node.created_at || "").slice(0, 10) || "—"}</dd>
        </div>
        <div>
          <dt>Статус</dt>
          <dd>{node.status || "—"}</dd>
        </div>
        <div>
          <dt>Фрагментов</dt>
          <dd>{node.chunk_count ?? 0}</dd>
        </div>
        <div>
          <dt>Связей</dt>
          <dd>{neighbours}</dd>
        </div>
        {node.doc_id && (
          <a
            className="mg-download"
            href={`/jarvis-api/api/files/${encodeURIComponent(node.doc_id)}/download`}
          >
            <Download size={13} /> Скачать
          </a>
        )}
      </dl>
    );
  }
  return (
    <div className="mg-meta">
      {node.namespace && (
        <div>
          <dt>Раздел</dt>
          <dd>{node.namespace}</dd>
        </div>
      )}
      {node.tags && node.tags.length > 0 && (
        <div>
          <dt>Теги</dt>
          <dd>{node.tags.join(", ")}</dd>
        </div>
      )}
      <div>
        <dt>Связей</dt>
        <dd>{neighbours}</dd>
      </div>
      {note?.content && <p className="mg-snippet">{note.content.slice(0, 240)}</p>}
      {backlinks.length > 0 && (
        <div>
          <dt>Обратные ссылки</dt>
          <dd>{backlinks.length}</dd>
        </div>
      )}
    </div>
  );
}
