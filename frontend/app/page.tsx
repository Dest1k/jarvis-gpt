"use client";

import {
  Activity,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Database,
  FileText,
  History,
  Loader2,
  MessageSquare,
  Play,
  RefreshCw,
  Save,
  Search,
  Send,
  Server,
  ShieldAlert,
  Sparkles,
  Upload,
  Wrench
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
  notes?: string | null;
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

type MemoryItem = {
  id: string;
  namespace: string;
  content: string;
  tags: string[];
  importance: number;
  rank?: number | null;
};

type FileItem = {
  id: string;
  name: string;
  source_path?: string | null;
  stored_path: string;
  mime_type: string;
  size: number;
  sha256: string;
  status: string;
  error?: string | null;
  chunk_count: number;
  created_at: string;
  updated_at: string;
};

type FileChunkHit = {
  file_id: string;
  file_name: string;
  chunk_id: string;
  position: number;
  content: string;
  created_at: string;
  rank?: number | null;
};

type AuditEntry = {
  id: string;
  ts: string;
  actor: string;
  action: string;
  target_type: string;
  target_id?: string | null;
  summary: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
};

type ToolInfo = {
  name: string;
  description: string;
  category: string;
  danger_level: "safe" | "review" | "danger";
};

type MissionExecution = {
  mission: Mission;
  task: MissionTask | null;
  result: {
    tool: string;
    ok: boolean;
    summary: string;
    data: Record<string, unknown>;
  };
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
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [fileHits, setFileHits] = useState<FileChunkHit[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [input, setInput] = useState("");
  const [memoryDraft, setMemoryDraft] = useState("");
  const [fileQuery, setFileQuery] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
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
      const [statusData, missionData, toolData, memoryData, fileData, auditData] =
        await Promise.all([
          api<RuntimeStatus>("/api/status"),
          api<Mission[]>("/api/missions"),
          api<ToolInfo[]>("/api/tools"),
          api<MemoryItem[]>("/api/memory?limit=8"),
          api<FileItem[]>("/api/files?limit=8"),
          api<AuditEntry[]>("/api/audit?limit=8")
        ]);
      setStatus(statusData);
      setMissions(missionData);
      setTools(toolData);
      setMemories(memoryData);
      setFiles(fileData);
      setAudit(auditData);
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

  async function executeNextMissionStep(missionId: string) {
    setBusy(true);
    try {
      const response = await api<MissionExecution>(`/api/missions/${missionId}/execute-next`, {
        method: "POST",
        body: "{}"
      });
      setMissions((current) =>
        current.map((mission) => (mission.id === response.mission.id ? response.mission : mission))
      );
      setLines((current) => [
        ...current,
        {
          role: "system",
          content: `${response.result.summary}${response.task ? `\n${response.task.title}` : ""}`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Mission step failed");
    } finally {
      setBusy(false);
    }
  }

  async function updateTaskStatus(missionId: string, taskId: string, statusValue: string) {
    setBusy(true);
    try {
      const updated = await api<MissionTask>(`/api/missions/${missionId}/tasks/${taskId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: statusValue })
      });
      setMissions((current) =>
        current.map((mission) =>
          mission.id === missionId
            ? {
                ...mission,
                tasks: mission.tasks.map((task) => (task.id === taskId ? updated : task))
              }
            : mission
        )
      );
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Task update failed");
    } finally {
      setBusy(false);
    }
  }

  async function saveMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = memoryDraft.trim();
    if (!content || busy) return;
    setBusy(true);
    try {
      const saved = await api<MemoryItem>("/api/memory", {
        method: "POST",
        body: JSON.stringify({
          content,
          namespace: "operator",
          tags: ["manual"],
          importance: 0.65
        })
      });
      setMemoryDraft("");
      setMemories((current) => [saved, ...current].slice(0, 8));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Memory save failed");
    } finally {
      setBusy(false);
    }
  }

  async function uploadSelectedFile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFile || busy) return;
    const form = event.currentTarget;
    const formData = new FormData();
    formData.append("file", selectedFile);
    setBusy(true);
    try {
      const response = await fetch(`${API_URL}/api/files/upload`, {
        method: "POST",
        body: formData
      });
      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText}`);
      }
      const result = (await response.json()) as { file: FileItem; chunks_indexed: number };
      setSelectedFile(null);
      form.reset();
      setFiles((current) => [result.file, ...current].slice(0, 8));
      setLines((current) => [
        ...current,
        {
          role: "system",
          content: `Файл загружен: ${result.file.name} (${result.chunks_indexed} chunks)`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "File upload failed");
    } finally {
      setBusy(false);
    }
  }

  async function searchFileChunks(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = fileQuery.trim();
    if (!query) {
      setFileHits([]);
      return;
    }
    setBusy(true);
    try {
      const hits = await api<FileChunkHit[]>(
        `/api/files/search?q=${encodeURIComponent(query)}&limit=6`
      );
      setFileHits(hits);
    } catch (err) {
      setError(err instanceof Error ? err.message : "File search failed");
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
        <IconButton label="Файлы">
          <FileText size={20} />
        </IconButton>
        <IconButton label="Диагностика">
          <Activity size={20} />
        </IconButton>
        <IconButton label="Audit">
          <History size={20} />
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
            icon={<FileText size={19} />}
            label="Файлы"
            value={`${counters.files ?? 0}`}
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
                      <div>
                        <h3>{mission.title}</h3>
                        <div className="progressTrack" aria-label="Mission progress">
                          <span style={{ width: `${Math.round(mission.progress * 100)}%` }} />
                        </div>
                      </div>
                    </div>
                    <div className="taskList">
                      {mission.tasks.slice(0, 6).map((task) => (
                        <div className={`taskRow ${task.status}`} key={task.id}>
                          <span>{task.position}</span>
                          <p>{task.title}</p>
                          <div className="taskControls">
                            <button
                              type="button"
                              title="Done"
                              aria-label="Done"
                              disabled={busy || task.status === "done"}
                              onClick={() => updateTaskStatus(mission.id, task.id, "done")}
                            >
                              <ClipboardCheck size={14} />
                            </button>
                            <button
                              type="button"
                              title="Blocked"
                              aria-label="Blocked"
                              disabled={busy || task.status === "blocked"}
                              onClick={() => updateTaskStatus(mission.id, task.id, "blocked")}
                            >
                              <ShieldAlert size={14} />
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="missionActions">
                      <button
                        className="iconText compact"
                        type="button"
                        onClick={() => executeNextMissionStep(mission.id)}
                        disabled={busy || mission.status === "done"}
                      >
                        <Play size={15} />
                        <span>Следующий шаг</span>
                      </button>
                      <small>{Math.round(mission.progress * 100)}%</small>
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

            <div className="panelHeader lower">
              <h2>Инструменты</h2>
              <span>{tools.length}</span>
            </div>
            <div className="toolList">
              {tools.slice(0, 8).map((tool) => (
                <div className="toolRow" key={tool.name}>
                  <Wrench size={14} />
                  <strong>{tool.name}</strong>
                  <span>{tool.category}</span>
                </div>
              ))}
            </div>

            <div className="panelHeader lower">
              <h2>Файлы</h2>
              <span>{files.length}</span>
            </div>
            <form className="fileForm" onSubmit={uploadSelectedFile}>
              <input
                type="file"
                aria-label="Файл"
                onChange={(event) => setSelectedFile(event.currentTarget.files?.[0] ?? null)}
              />
              <button type="submit" disabled={busy || !selectedFile} title="Загрузить">
                {busy && selectedFile ? <Loader2 className="spin" size={15} /> : <Upload size={15} />}
              </button>
            </form>
            <form className="fileSearchForm" onSubmit={searchFileChunks}>
              <input
                value={fileQuery}
                onChange={(event) => setFileQuery(event.target.value)}
                placeholder="Поиск по файлам"
                aria-label="Поиск по файлам"
              />
              <button type="submit" disabled={busy || !fileQuery.trim()} title="Найти">
                <Search size={15} />
              </button>
            </form>
            <div className="fileList">
              {files.slice(0, 5).map((file) => (
                <div className={`fileRow ${file.status}`} key={file.id}>
                  <FileText size={14} />
                  <strong>{file.name}</strong>
                  <span>{file.chunk_count}</span>
                  <small>{formatBytes(file.size)}</small>
                </div>
              ))}
            </div>
            {fileHits.length > 0 && (
              <div className="fileMatches">
                {fileHits.slice(0, 4).map((hit) => (
                  <article className="fileMatch" key={hit.chunk_id}>
                    <div>
                      <strong>{hit.file_name}</strong>
                      <span>#{hit.position}</span>
                    </div>
                    <p>{hit.content}</p>
                  </article>
                ))}
              </div>
            )}

            <div className="panelHeader lower">
              <h2>Память</h2>
              <span>{memories.length}</span>
            </div>
            <form className="memoryForm" onSubmit={saveMemory}>
              <input
                value={memoryDraft}
                onChange={(event) => setMemoryDraft(event.target.value)}
                placeholder="Новая запись"
                aria-label="Новая запись памяти"
              />
              <button type="submit" disabled={busy || !memoryDraft.trim()} title="Сохранить">
                <Save size={15} />
              </button>
            </form>
            <div className="memoryList">
              {memories.slice(0, 5).map((memory) => (
                <article className="memoryRow" key={memory.id}>
                  <strong>{memory.namespace}</strong>
                  <p>{memory.content}</p>
                </article>
              ))}
            </div>

            <div className="panelHeader lower">
              <h2>Audit</h2>
              <span>{audit.length}</span>
            </div>
            <div className="auditList">
              {audit.slice(0, 6).map((entry) => (
                <div className="auditRow" key={entry.id}>
                  <History size={14} />
                  <strong>{entry.action}</strong>
                  <p>{entry.summary}</p>
                  <span>{entry.ts.slice(11, 19)}</span>
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

function formatBytes(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}
