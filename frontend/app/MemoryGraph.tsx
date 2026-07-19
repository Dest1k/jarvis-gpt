"use client";

import {
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState
} from "react";
import {
  Download,
  FileText,
  Focus,
  RefreshCw,
  Search,
  Settings2,
  X,
  ZoomIn,
  ZoomOut
} from "lucide-react";

import type { MemoryGraphNode, MemoryVault } from "./page";
import {
  DEFAULT_MEMORY_GRAPH_NODES,
  MAX_MEMORY_GRAPH_NODES,
  clampMemoryGraphCap,
  selectMemoryGraph
} from "../lib/memory-graph.mjs";

// --- simulation tuning (defaults; operator can tweak in the settings rail) --
const DEFAULT_REPEL = 5200;
const DEFAULT_SPRING = 0.022;
const DEFAULT_GRAVITY = 0.009;
const DAMPING = 0.86;
const MIN_D2 = 120;
const MIN_ALPHA = 0.02;
const COOL = 0.985;
const REHEAT = 0.55;
const WORLD_W = 900;
const WORLD_H = 560;
const MIN_ZOOM_SCALE = 0.12;
const MAX_ZOOM_SCALE = 10;
/** Hard cap so O(N^2) repulsion cannot fling nodes into infinity. */
const MAX_SPEED = 28;
const MAX_COORD = 2800;
/** Re-frame the camera this often while the layout is still settling. */
const FOLLOW_EVERY_FRAMES = 18;

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

const EDGE_KIND_LABELS: Record<string, string> = {
  namespace: "Раздел",
  tag: "Тег",
  link: "Ссылка",
  mentions: "Упоминание",
  "co-source": "Общий источник",
  "co-day": "Один день",
  "same-content": "Одинаковый текст",
  folder: "Папка"
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
type GraphEdge = MemoryVault["edges"][number];

function edgeKey(edge: GraphEdge, index: number): string {
  return `${edge.source}\u0000${edge.target}\u0000${edge.kind}\u0000${index}`;
}

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
  return (((h >>> 0) % 3600) / 3600) * Math.PI * 2;
}

function formatSize(bytes?: number | null): string {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

/**
 * Map a screen point into SVG user space under default
 * preserveAspectRatio="xMidYMid meet". Manual width/height scaling without
 * letterboxing made zoom/pan jump content off-canvas.
 */
function clientToWorld(
  svg: SVGSVGElement,
  clientX: number,
  clientY: number,
  view: ViewBox
): { x: number; y: number } {
  const rect = svg.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0 || view.w <= 0 || view.h <= 0) {
    return { x: view.x + view.w / 2, y: view.y + view.h / 2 };
  }
  const scale = Math.min(rect.width / view.w, rect.height / view.h);
  const offsetX = (rect.width - view.w * scale) / 2;
  const offsetY = (rect.height - view.h * scale) / 2;
  return {
    x: view.x + (clientX - rect.left - offsetX) / scale,
    y: view.y + (clientY - rect.top - offsetY) / scale
  };
}

function meetScale(svg: SVGSVGElement, view: ViewBox): number {
  const rect = svg.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0 || view.w <= 0 || view.h <= 0) return 1;
  return Math.min(rect.width / view.w, rect.height / view.h);
}

function clampViewBox(view: ViewBox, content?: { w: number; h: number }): ViewBox {
  const baseW = content?.w && content.w > 0 ? content.w : WORLD_W;
  const baseH = content?.h && content.h > 0 ? content.h : WORLD_H;
  const aspect = view.h > 0 && view.w > 0 ? view.h / view.w : baseH / baseW;
  const minW = Math.max(80, baseW * MIN_ZOOM_SCALE);
  const maxW = Math.max(minW + 40, baseW * MAX_ZOOM_SCALE);
  const w = Math.max(minW, Math.min(view.w, maxW));
  const h = Math.max(60, w * aspect);
  if (!Number.isFinite(w) || !Number.isFinite(h) || !Number.isFinite(view.x) || !Number.isFinite(view.y)) {
    return { x: -WORLD_W / 2, y: -WORLD_H / 2, w: WORLD_W, h: WORLD_H };
  }
  return { x: view.x, y: view.y, w, h };
}

export function MemoryGraph({
  vault,
  onRefresh,
  busy = false
}: {
  vault: MemoryVault;
  onRefresh?: () => void;
  busy?: boolean;
}) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const simRef = useRef<Map<string, SimNode>>(new Map());
  const simCacheRef = useRef<Map<string, SimNode>>(new Map());
  const nodeElsRef = useRef<Map<string, SVGGElement>>(new Map());
  const edgeElsRef = useRef<Map<string, SVGLineElement>>(new Map());
  const topologyRef = useRef<{ edges: GraphEdge[]; edgeKeys: string[] }>({
    edges: [],
    edgeKeys: []
  });
  const alphaRef = useRef<number>(0);
  const rafRef = useRef<number | null>(null);
  const contentBoundsRef = useRef({ w: WORLD_W, h: WORLD_H });
  const vbRef = useRef<ViewBox>({ x: -WORLD_W / 2, y: -WORLD_H / 2, w: WORLD_W, h: WORLD_H });
  const physicsRef = useRef({ repel: DEFAULT_REPEL, spring: DEFAULT_SPRING, gravity: DEFAULT_GRAVITY });
  /** When true, camera tracks content while physics settles. User pan/zoom disables it. */
  const cameraFollowRef = useRef(true);
  const followFrameRef = useRef(0);
  const fitViewRef = useRef<() => void>(() => undefined);

  const [vb, setVbState] = useState<ViewBox>(vbRef.current);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set());
  const [hiddenEdgeKinds, setHiddenEdgeKinds] = useState<Set<string>>(new Set());
  const [renderCap, setRenderCap] = useState(DEFAULT_MEMORY_GRAPH_NODES);
  const [labelsAlways, setLabelsAlways] = useState(false);
  const [physicsRunning, setPhysicsRunning] = useState(true);
  const [repel, setRepel] = useState(DEFAULT_REPEL);
  const [spring, setSpring] = useState(DEFAULT_SPRING);
  const [gravity, setGravity] = useState(DEFAULT_GRAVITY);
  const [settingsOpen, setSettingsOpen] = useState(true);

  physicsRef.current = { repel, spring, gravity };

  const setViewBox = useCallback((next: ViewBox, options?: { fromUser?: boolean }) => {
    if (options?.fromUser) cameraFollowRef.current = false;
    const clamped = clampViewBox(next, contentBoundsRef.current);
    vbRef.current = clamped;
    setVbState(clamped);
  }, []);

  const kindsPresent = useMemo(() => {
    const order = ["memory", "document", "namespace", "tag", "folder", "daybucket", "link"];
    const present = new Set(vault.nodes.map((n) => n.kind));
    return order.filter((k) => present.has(k));
  }, [vault.nodes]);

  const edgeKindsPresent = useMemo(() => {
    const present = new Set(vault.edges.map((e) => e.kind));
    return Array.from(present).sort();
  }, [vault.edges]);

  const graph = useMemo(
    () =>
      selectMemoryGraph({
        nodes: vault.nodes,
        edges: vault.edges.filter((edge) => !hiddenEdgeKinds.has(edge.kind)),
        hiddenKinds,
        renderCap,
        search
      }),
    [vault.nodes, vault.edges, hiddenKinds, hiddenEdgeKinds, renderCap, search]
  );
  const { nodes, edges, totalNodes, truncated } = graph;
  const renderedEdgeKeys = useMemo(
    () => edges.map((edge, index) => edgeKey(edge, index)),
    [edges]
  );

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

  const searchMatch = search.trim() ? graph.searchMatchIds : null;
  const focusId = hoverId ?? selectedId;
  const highlight = useMemo(() => {
    if (!focusId) return null;
    const set = new Set<string>([focusId]);
    for (const nb of adjacency.get(focusId) ?? []) set.add(nb);
    return set;
  }, [focusId, adjacency]);

  const stopLoop = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const startLoop = useCallback(() => {
    if (rafRef.current != null) return;
    if (!physicsRunning) return;
    const step = () => {
      if (!physicsRunning) {
        rafRef.current = null;
        return;
      }
      const sim = simRef.current;
      const topology = topologyRef.current;
      const phys = physicsRef.current;
      const arr = Array.from(sim.values());
      const n = arr.length;
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
          const f = phys.repel / d2;
          const dist = Math.sqrt(d2);
          const fx = (dx / dist) * f;
          const fy = (dy / dist) * f;
          a.vx += fx;
          a.vy += fy;
          b.vx -= fx;
          b.vy -= fy;
        }
      }
      for (const e of topology.edges) {
        const a = sim.get(e.source);
        const b = sim.get(e.target);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const force = (dist - restLength(e.kind)) * phys.spring;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
      for (const node of arr) {
        if (node.dragging || node.pinned) {
          node.vx = 0;
          node.vy = 0;
        } else {
          node.vx = (node.vx - node.x * phys.gravity) * DAMPING;
          node.vy = (node.vy - node.y * phys.gravity) * DAMPING;
          // Velocity / coordinate clamps: without them N² repulsion flings the
          // graph outside the fitted viewBox ~1–2s after start (nodes "vanish").
          if (node.vx > MAX_SPEED) node.vx = MAX_SPEED;
          else if (node.vx < -MAX_SPEED) node.vx = -MAX_SPEED;
          if (node.vy > MAX_SPEED) node.vy = MAX_SPEED;
          else if (node.vy < -MAX_SPEED) node.vy = -MAX_SPEED;
          node.x += node.vx * alphaRef.current;
          node.y += node.vy * alphaRef.current;
          if (node.x > MAX_COORD) node.x = MAX_COORD;
          else if (node.x < -MAX_COORD) node.x = -MAX_COORD;
          if (node.y > MAX_COORD) node.y = MAX_COORD;
          else if (node.y < -MAX_COORD) node.y = -MAX_COORD;
        }
        if (node.el) {
          node.el.setAttribute(
            "transform",
            `translate(${node.x.toFixed(2)} ${node.y.toFixed(2)})`
          );
        }
      }
      topology.edges.forEach((e, index) => {
        const line = edgeElsRef.current.get(topology.edgeKeys[index]);
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
      followFrameRef.current += 1;
      // Keep the camera on the moving layout until the user takes over.
      if (
        cameraFollowRef.current &&
        followFrameRef.current % FOLLOW_EVERY_FRAMES === 0
      ) {
        fitViewRef.current();
      }
      if (alphaRef.current > MIN_ALPHA && !document.hidden && physicsRunning) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        rafRef.current = null;
        if (cameraFollowRef.current) fitViewRef.current();
      }
    };
    rafRef.current = requestAnimationFrame(step);
  }, [physicsRunning]);

  const reheat = useCallback(() => {
    if (!physicsRunning) return;
    alphaRef.current = Math.max(alphaRef.current, REHEAT);
    if (rafRef.current == null) startLoop();
  }, [physicsRunning, startLoop]);

  useLayoutEffect(() => {
    stopLoop();
    const cache = simCacheRef.current;
    const next = new Map<string, SimNode>();
    nodes.forEach((node, index) => {
      let simNode = cache.get(node.id);
      if (simNode) {
        simNode.degree = node.degree ?? 0;
        simNode.data = node;
        simNode.label = node.label;
        simNode.kind = node.kind;
      } else {
        const angle = hashAngle(node.id);
        const radius = 40 + (index % 40) * 6;
        simNode = {
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
        };
        cache.set(node.id, simNode);
      }
      simNode.el = nodeElsRef.current.get(node.id) ?? null;
      if (simNode.el) {
        simNode.el.setAttribute(
          "transform",
          `translate(${simNode.x.toFixed(2)} ${simNode.y.toFixed(2)})`
        );
      }
      next.set(node.id, simNode);
    });
    simRef.current = next;
    topologyRef.current = { edges, edgeKeys: renderedEdgeKeys };

    edges.forEach((edge, index) => {
      const line = edgeElsRef.current.get(renderedEdgeKeys[index]);
      const source = next.get(edge.source);
      const target = next.get(edge.target);
      if (!line || !source || !target) return;
      line.setAttribute("x1", source.x.toFixed(2));
      line.setAttribute("y1", source.y.toFixed(2));
      line.setAttribute("x2", target.x.toFixed(2));
      line.setAttribute("y2", target.y.toFixed(2));
    });

    alphaRef.current = physicsRunning ? 1 : 0;
    if (physicsRunning) startLoop();
    return stopLoop;
  }, [edges, nodes, physicsRunning, renderedEdgeKeys, startLoop, stopLoop]);

  useEffect(() => {
    const onVisible = () => {
      if (!document.hidden && alphaRef.current > MIN_ALPHA && physicsRunning) startLoop();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [physicsRunning, startLoop]);

  const screenToWorld = useCallback((clientX: number, clientY: number) => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    return clientToWorld(svg, clientX, clientY, vbRef.current);
  }, []);

  const beginPan = useCallback(
    (event: ReactPointerEvent<SVGSVGElement>) => {
      // Allow pan from empty canvas / edge group, not from node groups.
      const target = event.target as Element | null;
      if (target?.closest?.(".mg-node")) return;
      const svg = svgRef.current;
      if (!svg) return;
      const start = { x: event.clientX, y: event.clientY };
      const base = { ...vbRef.current };
      const scale = meetScale(svg, base) || 1;
      const onMove = (moveEvent: PointerEvent) => {
        setViewBox(
          {
            ...base,
            x: base.x - (moveEvent.clientX - start.x) / scale,
            y: base.y - (moveEvent.clientY - start.y) / scale
          },
          { fromUser: true }
        );
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
          if (target.el) {
            target.el.setAttribute(
              "transform",
              `translate(${target.x.toFixed(2)} ${target.y.toFixed(2)})`
            );
          }
        }
        // Keep edges attached while dragging even if the sim loop is cold.
        topologyRef.current.edges.forEach((edge, index) => {
          if (edge.source !== id && edge.target !== id) return;
          const line = edgeElsRef.current.get(topologyRef.current.edgeKeys[index]);
          const a = simRef.current.get(edge.source);
          const b = simRef.current.get(edge.target);
          if (!line || !a || !b) return;
          line.setAttribute("x1", a.x.toFixed(2));
          line.setAttribute("y1", a.y.toFixed(2));
          line.setAttribute("x2", b.x.toFixed(2));
          line.setAttribute("y2", b.y.toFixed(2));
        });
        if (physicsRunning) {
          alphaRef.current = Math.max(alphaRef.current, REHEAT);
          if (rafRef.current == null) startLoop();
        }
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
    [physicsRunning, reheat, screenToWorld, startLoop]
  );

  const zoomAt = useCallback(
    (clientX: number, clientY: number, factor: number, fromUser = true) => {
      const view = vbRef.current;
      if (view.w <= 0 || view.h <= 0) return;
      const focus = screenToWorld(clientX, clientY);
      const newW = view.w * factor;
      const newH = view.h * factor;
      setViewBox(
        {
          w: newW,
          h: newH,
          x: focus.x - (focus.x - view.x) * (newW / view.w),
          y: focus.y - (focus.y - view.y) * (newH / view.h)
        },
        { fromUser }
      );
    },
    [screenToWorld, setViewBox]
  );

  // Native wheel (passive: false) — React onWheel is passive and cannot prevent page scroll.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return undefined;
    const handler = (event: WheelEvent) => {
      event.preventDefault();
      const factor = event.deltaY > 0 ? 1.12 : 0.89;
      zoomAt(event.clientX, event.clientY, factor);
    };
    svg.addEventListener("wheel", handler, { passive: false });
    return () => svg.removeEventListener("wheel", handler);
  }, [zoomAt]);

  const fitView = useCallback(
    (opts?: { userRequest?: boolean }) => {
      if (opts?.userRequest) cameraFollowRef.current = true;
      const arr = Array.from(simRef.current.values());
      if (!arr.length) {
        contentBoundsRef.current = { w: WORLD_W, h: WORLD_H };
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
      if (!Number.isFinite(minX) || !Number.isFinite(maxX)) return;
      const pad = 90;
      const rawW = Math.max(maxX - minX + pad * 2, 280);
      const rawH = Math.max(maxY - minY + pad * 2, 200);
      contentBoundsRef.current = { w: rawW, h: rawH };
      // Match canvas aspect so meet-letterboxing stays minimal after fit.
      const stage = stageRef.current?.getBoundingClientRect();
      const aspect =
        stage && stage.width > 40 && stage.height > 40
          ? stage.height / stage.width
          : WORLD_H / WORLD_W;
      let w = rawW;
      let h = rawW * aspect;
      if (h < rawH) {
        h = rawH;
        w = rawH / aspect;
      }
      const cx = (minX + maxX) / 2;
      const cy = (minY + maxY) / 2;
      // fromUser:false — automatic re-frame must not disable camera follow.
      setViewBox({ x: cx - w / 2, y: cy - h / 2, w, h });
    },
    [setViewBox]
  );
  fitViewRef.current = () => fitView();

  // Initial frame after topology changes: follow camera + reheat layout.
  useEffect(() => {
    cameraFollowRef.current = true;
    followFrameRef.current = 0;
    const kick = window.setTimeout(() => fitView(), 50);
    return () => window.clearTimeout(kick);
  }, [vault.stats.nodes, nodes.length, fitView]);

  // When the stage size changes, keep content framed if the user hasn't taken over.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => {
      if (cameraFollowRef.current) fitView();
      else setViewBox({ ...vbRef.current });
    });
    observer.observe(stage);
    return () => observer.disconnect();
  }, [fitView, setViewBox]);

  const toggleKind = (kind: string) => {
    setHiddenKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const toggleEdgeKind = (kind: string) => {
    setHiddenEdgeKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const selectedNode = selectedId ? vault.nodes.find((n) => n.id === selectedId) ?? null : null;

  const nodeClass = (node: MemoryGraphNode) => {
    const classes = ["mg-node", `mg-${node.kind}`];
    if (HUB_KINDS.has(node.kind)) classes.push("mg-hub");
    if (labelsAlways) classes.push("mg-labels-on");
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
      <div className="mg-page mg-empty-page">
        <div className="mg-empty">
          Граф пуст — пока нет ни записей памяти, ни документов. Сохраните заметку или загрузите
          файл.
        </div>
      </div>
    );
  }

  return (
    <div className={`mg-page ${settingsOpen ? "mg-settings-open" : "mg-settings-closed"}`}>
      <aside className="mg-settings" aria-label="Настройки графа">
        <header className="mg-settings-head">
          <div>
            <p className="mg-settings-eyebrow">Граф памяти</p>
            <h2>Настройки</h2>
          </div>
          <button
            type="button"
            className="mg-icon-btn"
            onClick={() => setSettingsOpen(false)}
            aria-label="Скрыть настройки"
            title="Скрыть"
          >
            <X size={16} />
          </button>
        </header>

        <section className="mg-settings-block">
          <h3>Обзор</h3>
          <dl className="mg-stats">
            <div>
              <dt>Узлов в vault</dt>
              <dd>{vault.stats.nodes ?? vault.nodes.length}</dd>
            </div>
            <div>
              <dt>Связей в vault</dt>
              <dd>{vault.stats.edges ?? vault.edges.length}</dd>
            </div>
            <div>
              <dt>На экране</dt>
              <dd>
                {nodes.length} / {edges.length}
              </dd>
            </div>
            <div>
              <dt>Документы</dt>
              <dd>{vault.stats.documents ?? 0}</dd>
            </div>
            <div>
              <dt>Заметки</dt>
              <dd>{vault.stats.notes ?? 0}</dd>
            </div>
          </dl>
          {onRefresh ? (
            <button
              type="button"
              className="mg-settings-action"
              onClick={onRefresh}
              disabled={busy}
            >
              <RefreshCw size={14} className={busy ? "spin" : undefined} />
              Обновить vault
            </button>
          ) : null}
        </section>

        <section className="mg-settings-block">
          <h3>Поиск</h3>
          <label className="mg-search mg-search-block">
            <Search size={14} />
            <input
              value={search}
              placeholder="Метка, путь, тег, id…"
              onChange={(event) => setSearch(event.target.value)}
            />
            {search ? (
              <button type="button" onClick={() => setSearch("")} aria-label="Очистить">
                <X size={13} />
              </button>
            ) : null}
          </label>
          {searchMatch ? (
            <p className="mg-settings-hint">Совпадений: {searchMatch.size}</p>
          ) : null}
        </section>

        <section className="mg-settings-block">
          <h3>Типы узлов</h3>
          <div className="mg-chips mg-chips-stack">
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
        </section>

        {edgeKindsPresent.length > 0 ? (
          <section className="mg-settings-block">
            <h3>Типы связей</h3>
            <div className="mg-chips mg-chips-stack">
              {edgeKindsPresent.map((kind) => (
                <button
                  key={kind}
                  type="button"
                  className={`mg-chip ${hiddenEdgeKinds.has(kind) ? "off" : ""}`}
                  onClick={() => toggleEdgeKind(kind)}
                >
                  <span className="mg-chip-dot" />
                  {EDGE_KIND_LABELS[kind] ?? kind}
                </button>
              ))}
            </div>
          </section>
        ) : null}

        <section className="mg-settings-block">
          <h3>Отображение</h3>
          <label className="mg-field">
            <span>Лимит узлов</span>
            <select
              value={renderCap}
              onChange={(event) => setRenderCap(clampMemoryGraphCap(event.target.value))}
            >
              <option value={300}>300</option>
              <option value={600}>600</option>
              <option value={900}>900</option>
              <option value={MAX_MEMORY_GRAPH_NODES}>{MAX_MEMORY_GRAPH_NODES}</option>
            </select>
          </label>
          <label className="mg-toggle">
            <input
              type="checkbox"
              checked={labelsAlways}
              onChange={(event) => setLabelsAlways(event.target.checked)}
            />
            <span>Всегда показывать подписи</span>
          </label>
          {truncated ? (
            <p className="mg-settings-hint mg-warn">
              Показано {nodes.length} из {totalNodes} — поднимите лимит или сузьте фильтры.
            </p>
          ) : null}
        </section>

        <section className="mg-settings-block">
          <h3>Физика раскладки</h3>
          <label className="mg-toggle">
            <input
              type="checkbox"
              checked={physicsRunning}
              onChange={(event) => {
                const on = event.target.checked;
                setPhysicsRunning(on);
                if (on) {
                  alphaRef.current = REHEAT;
                  startLoop();
                } else {
                  stopLoop();
                }
              }}
            />
            <span>Симуляция включена</span>
          </label>
          <label className="mg-field">
            <span>Отталкивание · {repel}</span>
            <input
              type="range"
              min={1000}
              max={12000}
              step={100}
              value={repel}
              onChange={(event) => {
                setRepel(Number(event.target.value));
                reheat();
              }}
            />
          </label>
          <label className="mg-field">
            <span>Пружины · {spring.toFixed(3)}</span>
            <input
              type="range"
              min={0.005}
              max={0.08}
              step={0.001}
              value={spring}
              onChange={(event) => {
                setSpring(Number(event.target.value));
                reheat();
              }}
            />
          </label>
          <label className="mg-field">
            <span>Гравитация · {gravity.toFixed(3)}</span>
            <input
              type="range"
              min={0}
              max={0.04}
              step={0.001}
              value={gravity}
              onChange={(event) => {
                setGravity(Number(event.target.value));
                reheat();
              }}
            />
          </label>
          <button type="button" className="mg-settings-action" onClick={() => reheat()}>
            Перезапустить раскладку
          </button>
        </section>

        <section className="mg-settings-block">
          <h3>Камера</h3>
          <div className="mg-settings-actions">
            <button
              type="button"
              className="mg-settings-action"
              onClick={() => fitView({ userRequest: true })}
            >
              <Focus size={14} /> Вписать граф
            </button>
            <button
              type="button"
              className="mg-settings-action"
              onClick={() => {
                const svg = svgRef.current;
                if (!svg) return;
                const rect = svg.getBoundingClientRect();
                zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 0.85);
              }}
            >
              <ZoomIn size={14} /> Приблизить
            </button>
            <button
              type="button"
              className="mg-settings-action"
              onClick={() => {
                const svg = svgRef.current;
                if (!svg) return;
                const rect = svg.getBoundingClientRect();
                zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1.18);
              }}
            >
              <ZoomOut size={14} /> Отдалить
            </button>
          </div>
          <p className="mg-settings-hint">
            Колесо — зум к курсору · пустой фон — панорама · узел — детали · перетаскивание
            закрепляет позицию.
          </p>
        </section>
      </aside>

      <div className="mg-main">
        <div className="mg-main-bar">
          {!settingsOpen ? (
            <button
              type="button"
              className="mg-icon-btn"
              onClick={() => setSettingsOpen(true)}
              aria-label="Показать настройки"
              title="Настройки"
            >
              <Settings2 size={16} />
            </button>
          ) : null}
          <div className="mg-main-title">
            <h2>Граф связей vault</h2>
            <span>
              {nodes.length} узлов · {edges.length} связей
              {truncated ? ` · из ${totalNodes}` : ""}
            </span>
          </div>
          <div className="mg-main-actions">
            <button
              type="button"
              className="mg-fit"
              onClick={() => fitView({ userRequest: true })}
            >
              <Focus size={13} /> Вписать
            </button>
          </div>
        </div>

        <div className="mg-stage" ref={stageRef}>
          <svg
            ref={svgRef}
            className="mg-canvas"
            viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
            preserveAspectRatio="xMidYMid meet"
            onPointerDown={beginPan}
          >
            <g className="mg-edges">
              {edges.map((edge, index) => (
                <line
                  key={renderedEdgeKeys[index]}
                  ref={(el) => {
                    const key = renderedEdgeKeys[index];
                    if (el) edgeElsRef.current.set(key, el);
                    else edgeElsRef.current.delete(key);
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
                      if (el) nodeElsRef.current.set(node.id, el);
                      else nodeElsRef.current.delete(node.id);
                      const sim = simRef.current.get(node.id);
                      if (sim) {
                        sim.el = el;
                        if (el) {
                          el.setAttribute(
                            "transform",
                            `translate(${sim.x.toFixed(2)} ${sim.y.toFixed(2)})`
                          );
                        }
                      }
                    }}
                    onPointerDown={(event) => beginNodeDrag(event, node.id)}
                    onPointerEnter={() => setHoverId(node.id)}
                    onPointerLeave={() =>
                      setHoverId((current) => (current === node.id ? null : current))
                    }
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
                      {node.label.length > 28 ? `${node.label.slice(0, 27)}…` : node.label}
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
                backlinks={
                  vault.backlinks[selectedNode.label] ?? vault.backlinks[selectedNode.id] ?? []
                }
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
