"use client";

import {
  Activity,
  ArrowLeft,
  Brain,
  CheckCircle2,
  Clock,
  Database,
  FileText,
  MessageSquare,
  Route,
  Sparkles,
  Wrench,
  Zap
} from "lucide-react";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

const CONFIGURED_API_URL = process.env.NEXT_PUBLIC_JARVIS_API_URL ?? "http://localhost:8000";

type TraceMessage = {
  id: string;
  role: string;
  content: string;
  created_at: string;
  metadata?: Record<string, unknown>;
};

type TraceNode = {
  id: string;
  kind: string;
  title: string;
  summary?: string;
  payload?: Record<string, unknown>;
};

type TracePayload = {
  conversation: {
    id: string;
    title: string;
    created_at: string;
    updated_at: string;
    message_count: number;
  };
  input: TraceMessage | null;
  output: TraceMessage;
  duration_ms?: number | null;
  events: {
    type: string;
    title: string;
    content?: string | null;
    payload?: Record<string, unknown>;
  }[];
  nodes: TraceNode[];
  edges: { from: string; to: string }[];
  disclosure: string;
};

function apiUrl() {
  if (typeof window === "undefined") {
    return CONFIGURED_API_URL.replace(/\/$/, "");
  }
  try {
    const configured = new URL(CONFIGURED_API_URL);
    const pageHost = window.location.hostname;
    const localHosts = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
    if (localHosts.has(configured.hostname) && !localHosts.has(pageHost)) {
      return `${window.location.protocol}//${pageHost}:8000`;
    }
    return configured.toString().replace(/\/$/, "");
  } catch {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
}

function formatDuration(ms?: number | null) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "n/a";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 10000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 1000)} s`;
}

function trimText(value: string | undefined | null, maxChars = 1800) {
  const text = String(value ?? "").trim();
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars).trimEnd()}...`;
}

function eventLabel(kind: string) {
  const labels: Record<string, string> = {
    input: "вход",
    output: "выход",
    tool_call: "инструмент",
    thought: "мысль",
    task_kernel: "маршрут",
    memory: "память",
    approval: "допуск",
    mission: "миссия",
    mission_step: "шаг",
    assistant_done: "ответ"
  };
  return labels[kind] ?? kind.replace(/_/g, " ");
}

function nodeIcon(kind: string): ReactNode {
  if (kind === "input") return <MessageSquare size={18} />;
  if (kind === "output") return <CheckCircle2 size={18} />;
  if (kind === "tool_call") return <Wrench size={18} />;
  if (kind === "task_kernel") return <Route size={18} />;
  if (kind === "memory") return <Database size={18} />;
  if (kind === "assistant_done") return <Sparkles size={18} />;
  if (kind === "thought") return <Brain size={18} />;
  return <Activity size={18} />;
}

function payloadPreview(payload?: Record<string, unknown>) {
  if (!payload || Object.keys(payload).length === 0) return null;
  const important = ["route", "intent", "query", "tool", "ok", "source", "finish_reason", "tool_steps"];
  const lines = important
    .filter((key) => key in payload)
    .map((key) => `${key}: ${String(payload[key])}`);
  if (lines.length > 0) return lines.join("\n");
  return JSON.stringify(payload, null, 2).slice(0, 700);
}

export default function TracePage() {
  const params = useParams<{ messageId: string }>();
  const messageId = Array.isArray(params.messageId) ? params.messageId[0] : params.messageId;
  const [trace, setTrace] = useState<TracePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    async function loadTrace() {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(
          `${apiUrl()}/api/agent/trace/message/${encodeURIComponent(messageId)}`,
          { cache: "no-store" }
        );
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const payload = (await response.json()) as TracePayload;
        if (alive) setTrace(payload);
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : "Не удалось загрузить трассу");
      } finally {
        if (alive) setLoading(false);
      }
    }
    void loadTrace();
    return () => {
      alive = false;
    };
  }, [messageId]);

  const visibleNodes = useMemo(() => trace?.nodes ?? [], [trace]);

  return (
    <main className="tracePage">
      <header className="traceTopbar">
        <a className="traceBack" href="/">
          <ArrowLeft size={17} />
          <span>Command Center</span>
        </a>
        <div>
          <p>Схема мышления</p>
          <h1>{trace?.conversation.title ?? "Трасса ответа"}</h1>
        </div>
        <div className="traceStatus">
          <Clock size={16} />
          <span>{formatDuration(trace?.duration_ms)}</span>
        </div>
      </header>

      {loading && <div className="traceNotice">Загружаю цепочку прохождения мысли...</div>}
      {error && <div className="traceNotice error">Трасса недоступна: {error}</div>}

      {trace && (
        <>
          <section className="traceIOGrid">
            <div className="traceTextPanel">
              <div className="tracePanelHeader">
                <MessageSquare size={17} />
                <span>Входящий текст</span>
              </div>
              <pre>{trimText(trace.input?.content, 2400) || "Входное сообщение не найдено"}</pre>
            </div>
            <div className="traceTextPanel output">
              <div className="tracePanelHeader">
                <FileText size={17} />
                <span>Ответ LLM</span>
              </div>
              <pre>{trimText(trace.output.content, 2400)}</pre>
            </div>
          </section>

          <section className="traceSignalSection">
            <div className="traceSectionHead">
              <div>
                <p>Observable runtime trace</p>
                <h2>Как сигнал прошёл через JARVIS</h2>
              </div>
              <span>{visibleNodes.length} узлов</span>
            </div>
            <div className="traceRail" style={{ "--trace-node-count": visibleNodes.length } as CSSProperties}>
              {visibleNodes.map((node, index) => {
                const preview = payloadPreview(node.payload);
                return (
                  <div className={`traceNode ${node.kind}`} key={node.id}>
                    {index < visibleNodes.length - 1 && <span className="traceWire" aria-hidden="true" />}
                    <div className="traceNodeIcon">
                      <span className="tracePulse" aria-hidden="true" />
                      {nodeIcon(node.kind)}
                    </div>
                    <div className="traceNodeBody">
                      <span>{eventLabel(node.kind)}</span>
                      <h3>{node.title}</h3>
                      {node.summary && <p>{node.summary}</p>}
                      {preview && <pre>{preview}</pre>}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="traceMetaGrid">
            <div>
              <Brain size={17} />
              <span>{trace.disclosure}</span>
            </div>
            <div>
              <Zap size={17} />
              <span>Связей сигнала: {trace.edges.length}</span>
            </div>
            <div>
              <Activity size={17} />
              <span>Событий runtime: {trace.events.length}</span>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
