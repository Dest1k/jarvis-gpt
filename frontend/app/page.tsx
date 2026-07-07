"use client";

import {
  Activity,
  Brain,
  CheckCircle2,
  Database,
  Loader2,
  MessageSquare,
  Play,
  RefreshCw,
  Send,
  Server,
  ShieldAlert,
  Sparkles
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

const API_URL = process.env.NEXT_PUBLIC_JARVIS_API_URL ?? "http://localhost:8000";

type RuntimeStatus = {
  settings: {
    home: string;
    profile: { name: string; title: string; description: string; eager_mode: boolean };
    paths: Record<string, string>;
    llm: { enabled: boolean; base_url: string; model: string };
  };
  counters: Record<string, number>;
  health: DiagnosticCheck[];
  recent_events: RuntimeEvent[];
};

type RuntimeEvent = {
  id: string;
  ts: string;
  level: string;
  kind: string;
  title: string;
  payload: Record<string, unknown>;
};

type DiagnosticCheck = {
  name: string;
  status: "ok" | "warn" | "error";
  message: string;
  details: Record<string, unknown>;
};

type MissionTask = {
  id: string;
  title: string;
  status: string;
  position: number;
};

type Mission = {
  id: string;
  title: string;
  goal: string;
  status: string;
  progress: number;
  tasks: MissionTask[];
};

type ChatLine = {
  role: "user" | "assistant" | "system";
  content: string;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export default function CommandCenter() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [diagnostics, setDiagnostics] = useState<DiagnosticCheck[]>([]);
  const [input, setInput] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [lines, setLines] = useState<ChatLine[]>([
    {
      role: "system",
      content: "JARVIS GPT Command Center готов к подключению."
    }
  ]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [statusData, missionData] = await Promise.all([
        api<RuntimeStatus>("/api/status"),
        api<Mission[]>("/api/missions")
      ]);
      setStatus(statusData);
      setMissions(missionData);
      if (statusData.health.length) {
        setDiagnostics(statusData.health);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Backend недоступен");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const counters = useMemo(() => status?.counters ?? {}, [status]);

  async function sendChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = input.trim();
    if (!message || busy) return;
    setBusy(true);
    setInput("");
    setLines((current) => [...current, { role: "user", content: message }]);
    try {
      const response = await api<{
        conversation_id: string;
        answer: string;
        mission_id?: string;
      }>("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          message,
          conversation_id: conversationId,
          mode: "auto"
        })
      });
      setConversationId(response.conversation_id);
      setLines((current) => [...current, { role: "assistant", content: response.answer }]);
      await refresh();
    } catch (err) {
      setLines((current) => [
        ...current,
        {
          role: "assistant",
          content: err instanceof Error ? `Backend error: ${err.message}` : "Backend error"
        }
      ]);
    } finally {
      setBusy(false);
    }
  }

  async function runDiagnostics() {
    setBusy(true);
    try {
      const result = await api<{ checks: DiagnosticCheck[] }>("/api/diagnostics", {
        method: "POST",
        body: "{}"
      });
      setDiagnostics(result.checks);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Диагностика не ответила");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="shell">
      <aside className="rail" aria-label="Навигация">
        <div className="brandMark">
          <Brain size={22} />
        </div>
        <IconButton label="Диалог">
          <MessageSquare size={20} />
        </IconButton>
        <IconButton label="Runtime">
          <Server size={20} />
        </IconButton>
        <IconButton label="Память">
          <Database size={20} />
        </IconButton>
        <IconButton label="Диагностика">
          <Activity size={20} />
        </IconButton>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">JARVIS GPT</p>
            <h1>Command Center</h1>
          </div>
          <div className="topActions">
            <button className="iconText" type="button" onClick={refresh} disabled={busy}>
              <RefreshCw size={17} />
              <span>Обновить</span>
            </button>
            <button className="primary" type="button" onClick={runDiagnostics} disabled={busy}>
              {busy ? <Loader2 className="spin" size={17} /> : <Play size={17} />}
              <span>Диагностика</span>
            </button>
          </div>
        </header>

        {error && (
          <div className="notice" role="status">
            <ShieldAlert size={18} />
            <span>{error}</span>
          </div>
        )}

        <section className="statusGrid" aria-label="Сводка runtime">
          <StatusTile
            icon={<Server size={19} />}
            label="Профиль"
            value={status?.settings.profile.name ?? "offline"}
            tone={status ? "ok" : "warn"}
          />
          <StatusTile
            icon={<Sparkles size={19} />}
            label="LLM"
            value={status?.settings.llm.enabled ? status.settings.llm.model : "disabled"}
            tone={status?.settings.llm.enabled ? "ok" : "warn"}
          />
          <StatusTile
            icon={<Database size={19} />}
            label="Память"
            value={`${counters.memories ?? 0}`}
            tone="neutral"
          />
          <StatusTile
            icon={<Activity size={19} />}
            label="Миссии"
            value={`${counters.missions ?? 0}`}
            tone="neutral"
          />
        </section>

        <section className="mainGrid">
          <section className="chatPanel" aria-label="Диалог">
            <div className="panelHeader">
              <h2>Диалог</h2>
              <span>{conversationId ? "active" : "new"}</span>
            </div>
            <div className="transcript">
              {lines.map((line, index) => (
                <article className={`bubble ${line.role}`} key={`${line.role}-${index}`}>
                  <span>{line.role}</span>
                  <p>{line.content}</p>
                </article>
              ))}
            </div>
            <form className="composer" onSubmit={sendChat}>
              <textarea
                aria-label="Сообщение"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="JARVIS, оформи это как mission plan..."
                rows={3}
              />
              <button className="sendButton" type="submit" disabled={busy || !input.trim()}>
                {busy ? <Loader2 className="spin" size={19} /> : <Send size={19} />}
              </button>
            </form>
          </section>

          <section className="opsPanel" aria-label="Операции">
            <div className="panelHeader">
              <h2>Миссии</h2>
              <span>{missions.length}</span>
            </div>
            <div className="missionList">
              {missions.length === 0 ? (
                <div className="emptyState">Нет активных mission plans</div>
              ) : (
                missions.slice(0, 5).map((mission) => (
                  <article className="missionItem" key={mission.id}>
                    <div className="missionTitle">
                      <CheckCircle2 size={17} />
                      <h3>{mission.title}</h3>
                    </div>
                    <div className="taskList">
                      {mission.tasks.slice(0, 4).map((task) => (
                        <div className="taskRow" key={task.id}>
                          <span>{task.position}</span>
                          <p>{task.title}</p>
                        </div>
                      ))}
                    </div>
                  </article>
                ))
              )}
            </div>

            <div className="panelHeader lower">
              <h2>Здоровье</h2>
              <span>{diagnostics.length}</span>
            </div>
            <div className="healthList">
              {diagnostics.slice(0, 8).map((check) => (
                <div className={`healthRow ${check.status}`} key={check.name}>
                  <span className="dot" />
                  <strong>{check.name}</strong>
                  <p>{check.message}</p>
                </div>
              ))}
            </div>
          </section>
        </section>

        <footer className="runtimeLine">
          <span>{status?.settings.home ?? "D:\\jarvis"}</span>
          <span>{status?.settings.llm.base_url ?? API_URL}</span>
        </footer>
      </section>
    </main>
  );
}

function IconButton({ children, label }: { children: ReactNode; label: string }) {
  return (
    <button className="railButton" type="button" title={label} aria-label={label}>
      {children}
    </button>
  );
}

function StatusTile({
  icon,
  label,
  value,
  tone
}: {
  icon: ReactNode;
  label: string;
  value: string;
  tone: "ok" | "warn" | "neutral";
}) {
  return (
    <article className={`statusTile ${tone}`}>
      <div>{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}
