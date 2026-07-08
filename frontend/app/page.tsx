"use client";

import {
  Activity,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Cpu,
  Database,
  FileText,
  History,
  Gauge,
  Loader2,
  Mic,
  MicOff,
  MessageSquare,
  Play,
  RefreshCw,
  Save,
  Search,
  Send,
  Server,
  ShieldAlert,
  Sparkles,
  Square,
  Zap,
  Upload,
  Wrench
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

const API_URL = process.env.NEXT_PUBLIC_JARVIS_API_URL ?? "http://localhost:8000";

type RuntimeStatus = {
  settings: {
    home: string;
    profile: { name: string; title: string; description: string; eager_mode: boolean };
    paths: Record<string, string>;
    llm: {
      enabled: boolean;
      base_url: string;
      model: string;
      max_tokens?: number;
      timeout_sec?: number;
    };
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
  id?: string;
  role: "user" | "assistant" | "system";
  content: string;
};

type ConversationItem = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
};

type MessageItem = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
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

type ApprovalItem = {
  id: string;
  created_at: string;
  updated_at: string;
  status: string;
  risk: string;
  title: string;
  description: string;
  requested_action: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
};

type ApprovalExecution = {
  approval: ApprovalItem;
  ok: boolean;
  summary: string;
  data: Record<string, unknown>;
};

type ModelArtifact = {
  id: string;
  path: string;
  exists: boolean;
  active: boolean;
  size_bytes: number;
  shard_count: number;
  model_type?: string | null;
  dtype?: string | null;
  quantization?: string | null;
};

type ModelCatalog = {
  root: string;
  active_profile: string;
  active_model: ModelArtifact;
  models: ModelArtifact[];
  dispatcher: {
    base_url: string;
    served_model_name: string;
    model_path: string;
    docker_model_path: string;
    env: Record<string, string>;
  };
};

type DispatcherStatus = {
  service: string;
  container: string;
  docker_available: boolean;
  port: number;
  port_open: boolean;
  base_url: string;
  model: string;
  active_model: ModelArtifact;
  container_status?: { exists?: boolean; status?: string } | null;
};

type DispatcherAction = {
  ok: boolean;
  summary: string;
  status: DispatcherStatus;
};

type TelemetrySnapshot = {
  ts: string;
  host: { hostname: string; platform: string; cpu_count: number };
  memory: { total?: number | null; available?: number | null; used?: number | null; used_ratio?: number | null };
  disks: { path: string; total: number; used: number; free: number; used_ratio: number }[];
  gpu: { available: boolean; gpus?: { name: string; memory_used_ratio?: number | null; utilization_gpu?: number | null; temperature_c?: number | null }[]; error?: string };
  docker: { available: boolean; containers?: { name?: string; status?: string }[]; error?: string };
  performance: Record<string, unknown>;
};

type HostBridgeStatus = {
  port_open: boolean;
  token_available: boolean;
  script_available: boolean;
  start_command: string;
};

type AutonomyStatus = {
  enabled: boolean;
  running_tasks: string[];
  telemetry_interval_sec: number;
  learning_interval_sec: number;
  last_telemetry_at?: string | null;
  last_learning_at?: string | null;
  last_error?: string | null;
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

type ChatStreamItem = {
  type: "meta" | "event" | "delta" | "done" | "error";
  content?: string;
  conversation_id?: string;
  answer?: string;
  error?: string;
};

type VoiceState = "idle" | "listening";

type SpeechRecognitionAlternativeLike = {
  transcript: string;
};

type SpeechRecognitionResultLike = {
  isFinal: boolean;
  0?: SpeechRecognitionAlternativeLike;
};

type SpeechRecognitionResultListLike = {
  length: number;
  [index: number]: SpeechRecognitionResultLike;
};

type SpeechRecognitionEventLike = {
  results: SpeechRecognitionResultListLike;
};

type SpeechRecognitionLike = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  start: () => void;
  stop: () => void;
};

type SpeechRecognitionConstructorLike = new () => SpeechRecognitionLike;

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

async function streamApi(
  path: string,
  body: Record<string, unknown>,
  onItem: (item: ChatStreamItem) => void
) {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  if (!response.body) {
    throw new Error("Streaming response has no body");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = drainStreamBuffer(buffer, onItem);
  }
  buffer += decoder.decode();
  drainStreamBuffer(`${buffer}\n`, onItem);
}

function drainStreamBuffer(
  buffer: string,
  onItem: (item: ChatStreamItem) => void
) {
  const lines = buffer.split("\n");
  const rest = lines.pop() ?? "";
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    onItem(JSON.parse(trimmed) as ChatStreamItem);
  }
  return rest;
}

function clampMaxTokens(value: number) {
  if (!Number.isFinite(value)) return 512;
  return Math.max(64, Math.min(8192, Math.round(value)));
}

function speechRecognitionConstructor() {
  if (typeof window === "undefined") return null;
  const speechWindow = window as typeof window & {
    SpeechRecognition?: SpeechRecognitionConstructorLike;
    webkitSpeechRecognition?: SpeechRecognitionConstructorLike;
  };
  return speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition ?? null;
}

export default function CommandCenter() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [diagnostics, setDiagnostics] = useState<DiagnosticCheck[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [fileHits, setFileHits] = useState<FileChunkHit[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null);
  const [dispatcher, setDispatcher] = useState<DispatcherStatus | null>(null);
  const [telemetry, setTelemetry] = useState<TelemetrySnapshot | null>(null);
  const [hostBridge, setHostBridge] = useState<HostBridgeStatus | null>(null);
  const [autonomy, setAutonomy] = useState<AutonomyStatus | null>(null);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [input, setInput] = useState("");
  const [voiceAvailable, setVoiceAvailable] = useState(false);
  const [voiceState, setVoiceState] = useState<VoiceState>("idle");
  const [voiceInterim, setVoiceInterim] = useState("");
  const [memoryDraft, setMemoryDraft] = useState("");
  const [fileQuery, setFileQuery] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [maxTokens, setMaxTokens] = useState(512);
  const [lines, setLines] = useState<ChatLine[]>([
    {
      id: "system-boot",
      role: "system",
      content: "JARVIS GPT Command Center готов к подключению."
    }
  ]);
  const [busy, setBusy] = useState(false);
  const [chatBusy, setChatBusy] = useState(false);
  const [dispatcherBusy, setDispatcherBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const voiceBaseInputRef = useRef("");

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [
        statusData,
        conversationData,
        missionData,
        toolData,
        memoryData,
        fileData,
        auditData,
        modelData,
        dispatcherData,
        telemetryData,
        hostBridgeData,
        autonomyData,
        approvalData
      ] = await Promise.all([
          api<RuntimeStatus>("/api/status"),
          api<ConversationItem[]>("/api/conversations?limit=8"),
          api<Mission[]>("/api/missions"),
          api<ToolInfo[]>("/api/tools"),
          api<MemoryItem[]>("/api/memory?limit=8"),
          api<FileItem[]>("/api/files?limit=8"),
          api<AuditEntry[]>("/api/audit?limit=8"),
          api<ModelCatalog>("/api/models"),
          api<DispatcherStatus>("/api/dispatcher"),
          api<TelemetrySnapshot>("/api/telemetry"),
          api<HostBridgeStatus>("/api/host-bridge"),
          api<AutonomyStatus>("/api/autonomy"),
          api<ApprovalItem[]>("/api/approvals?limit=8")
        ]);
      setStatus(statusData);
      setConversations(conversationData);
      setMissions(missionData);
      setTools(toolData);
      setMemories(memoryData);
      setFiles(fileData);
      setAudit(auditData);
      setModelCatalog(modelData);
      setDispatcher(dispatcherData);
      setTelemetry(telemetryData);
      setHostBridge(hostBridgeData);
      setAutonomy(autonomyData);
      setApprovals(approvalData);
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

  useEffect(() => {
    setVoiceAvailable(Boolean(speechRecognitionConstructor()));
  }, []);

  useEffect(() => {
    return () => {
      const recognition = recognitionRef.current;
      if (!recognition) return;
      recognition.onend = null;
      recognition.onerror = null;
      recognition.onresult = null;
      recognition.stop();
      recognitionRef.current = null;
    };
  }, []);

  const counters = useMemo(() => status?.counters ?? {}, [status]);

  function startVoiceInput() {
    const Recognition = speechRecognitionConstructor();
    if (!Recognition) {
      setError("Voice input is not available in this browser");
      return;
    }
    if (voiceState === "listening") return;

    const recognition = new Recognition();
    recognition.lang = "ru-RU";
    recognition.continuous = false;
    recognition.interimResults = true;
    voiceBaseInputRef.current = input.trim();
    recognitionRef.current = recognition;
    setVoiceInterim("");
    setVoiceState("listening");

    recognition.onresult = (event) => {
      let transcript = "";
      let interim = "";
      for (let index = 0; index < event.results.length; index += 1) {
        const result = event.results[index];
        const text = result[0]?.transcript ?? "";
        transcript += text;
        if (!result.isFinal) {
          interim += text;
        }
      }
      setVoiceInterim(interim.trim());
      setInput([voiceBaseInputRef.current, transcript.trim()].filter(Boolean).join(" "));
    };
    recognition.onerror = () => {
      setVoiceState("idle");
      setVoiceInterim("");
      recognitionRef.current = null;
      setError("Voice input stopped with an error");
    };
    recognition.onend = () => {
      setVoiceState("idle");
      setVoiceInterim("");
      recognitionRef.current = null;
    };
    try {
      recognition.start();
    } catch (err) {
      setVoiceState("idle");
      setVoiceInterim("");
      recognitionRef.current = null;
      setError(err instanceof Error ? err.message : "Voice input could not start");
    }
  }

  function stopVoiceInput() {
    recognitionRef.current?.stop();
    recognitionRef.current = null;
    setVoiceState("idle");
    setVoiceInterim("");
  }

  async function sendChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = input.trim();
    if (!message || chatBusy) return;
    const userId = crypto.randomUUID();
    const assistantId = crypto.randomUUID();
    let receivedDelta = false;
    setChatBusy(true);
    setInput("");
    setLines((current) => [
      ...current,
      { id: userId, role: "user", content: message },
      { id: assistantId, role: "assistant", content: "" }
    ]);
    try {
      await streamApi(
        "/api/chat/stream",
        {
          message,
          conversation_id: conversationId,
          max_tokens: maxTokens,
          mode: "auto"
        },
        (item) => {
          if (item.type === "meta" && item.conversation_id) {
            setConversationId(item.conversation_id);
          }
          if (item.type === "delta" && item.content) {
            receivedDelta = true;
            setLines((current) =>
              current.map((line) =>
                line.id === assistantId
                  ? { ...line, content: `${line.content}${item.content}` }
                  : line
              )
            );
          }
          if (item.type === "done") {
            if (item.conversation_id) {
              setConversationId(item.conversation_id);
            }
            if (!receivedDelta && item.answer) {
              setLines((current) =>
                current.map((line) =>
                  line.id === assistantId ? { ...line, content: item.answer ?? "" } : line
                )
              );
            }
          }
          if (item.type === "error") {
            setLines((current) =>
              current.map((line) =>
                line.id === assistantId
                  ? { ...line, content: item.error ?? "Streaming error" }
                  : line
              )
            );
          }
        }
      );
      await refresh();
    } catch (err) {
      setLines((current) =>
        current.map((line) =>
          line.id === assistantId
            ? {
                ...line,
                content: err instanceof Error ? `Backend error: ${err.message}` : "Backend error"
              }
            : line
        )
      );
    } finally {
      setChatBusy(false);
    }
  }

  async function loadConversation(id: string) {
    setBusy(true);
    try {
      const messages = await api<MessageItem[]>(`/api/conversations/${id}/messages?limit=120`);
      setConversationId(id);
      setLines(
        messages
          .filter((message) => ["user", "assistant", "system"].includes(message.role))
          .map((message) => ({
            id: message.id,
            role: message.role,
            content: message.content
          }))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Conversation load failed");
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

  async function runDispatcherAction(action: "start" | "stop") {
    if (dispatcherBusy) return;
    setDispatcherBusy(true);
    try {
      const result = await api<DispatcherAction>(`/api/dispatcher/${action}`, {
        method: "POST",
        body: "{}"
      });
      setDispatcher(result.status);
      setLines((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: result.summary
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dispatcher action failed");
    } finally {
      setDispatcherBusy(false);
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

  async function updateApprovalStatus(approvalId: string, statusValue: string) {
    setBusy(true);
    try {
      const updated = await api<ApprovalItem>(`/api/approvals/${approvalId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: statusValue, result: { source: "command-center" } })
      });
      setApprovals((current) =>
        current.map((approval) => (approval.id === approvalId ? updated : approval))
      );
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approval update failed");
    } finally {
      setBusy(false);
    }
  }

  async function executeApproval(approvalId: string) {
    setBusy(true);
    try {
      const result = await api<ApprovalExecution>(`/api/approvals/${approvalId}/execute`, {
        method: "POST",
        body: "{}"
      });
      setApprovals((current) =>
        current.map((approval) => (approval.id === approvalId ? result.approval : approval))
      );
      setLines((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: result.summary
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approval execution failed");
    } finally {
      setBusy(false);
    }
  }

  async function runLearningTick() {
    setBusy(true);
    try {
      const result = await api<{ lesson_count: number }>("/api/learning/tick", {
        method: "POST",
        body: "{}"
      });
      setLines((current) => [
        ...current,
        { role: "system", content: `Learning tick: ${result.lesson_count} lessons saved` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Learning tick failed");
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
        <IconButton label="Модели">
          <Cpu size={20} />
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
        <IconButton label="Ресурсы">
          <Gauge size={20} />
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
            value={modelCatalog?.active_model.id ?? status?.settings.llm.model ?? "disabled"}
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
          <StatusTile
            icon={<Gauge size={19} />}
            label="GPU"
            value={telemetry?.gpu.available ? `${telemetry.gpu.gpus?.length ?? 0}` : "offline"}
            tone={telemetry?.gpu.available ? "ok" : "warn"}
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
                <article className={`bubble ${line.role}`} key={line.id ?? `${line.role}-${index}`}>
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
              <div className="composerSide">
                <input
                  aria-label="Max tokens"
                  className="tokenInput"
                  min={64}
                  max={8192}
                  step={64}
                  type="number"
                  value={maxTokens}
                  onChange={(event) => setMaxTokens(clampMaxTokens(Number(event.target.value)))}
                />
                <button
                  className={`voiceButton ${voiceState === "listening" ? "active" : ""}`}
                  disabled={!voiceAvailable}
                  onClick={voiceState === "listening" ? stopVoiceInput : startVoiceInput}
                  title={voiceState === "listening" ? "Stop voice input" : "Start voice input"}
                  type="button"
                  aria-label={voiceState === "listening" ? "Stop voice input" : "Start voice input"}
                >
                  {voiceState === "listening" ? <MicOff size={16} /> : <Mic size={16} />}
                </button>
                <span className="srOnly" aria-live="polite">
                  {voiceState === "listening" ? voiceInterim || "Listening" : ""}
                </span>
                <button className="sendButton" type="submit" disabled={chatBusy || !input.trim()}>
                  {chatBusy ? <Loader2 className="spin" size={19} /> : <Send size={19} />}
                </button>
              </div>
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
              <h2>Approvals</h2>
              <span>{approvals.filter((approval) => approval.status === "pending").length}</span>
            </div>
            <div className="approvalList">
              {approvals.length === 0 ? (
                <div className="emptyState compact">No pending gates</div>
              ) : (
                approvals.slice(0, 5).map((approval) => (
                  <article className={`approvalRow ${approval.status}`} key={approval.id}>
                    <ShieldAlert size={15} />
                    <div>
                      <strong>{approval.title}</strong>
                      <p>{approval.description}</p>
                    </div>
                    <span>{approval.risk}</span>
                    <div className="approvalControls">
                      <button
                        type="button"
                        title="Approve"
                        aria-label="Approve"
                        disabled={busy || approval.status !== "pending"}
                        onClick={() => updateApprovalStatus(approval.id, "approved")}
                      >
                        <CheckCircle2 size={14} />
                      </button>
                      <button
                        type="button"
                        title="Reject"
                        aria-label="Reject"
                        disabled={busy || approval.status !== "pending"}
                        onClick={() => updateApprovalStatus(approval.id, "rejected")}
                      >
                        <ShieldAlert size={14} />
                      </button>
                      <button
                        type="button"
                        title="Execute"
                        aria-label="Execute"
                        disabled={busy || approval.status !== "approved"}
                        onClick={() => executeApproval(approval.id)}
                      >
                        <Play size={14} />
                      </button>
                    </div>
                  </article>
                ))
              )}
            </div>

            <div className="panelHeader lower">
              <h2>Dialog History</h2>
              <span>{conversations.length}</span>
            </div>
            <div className="conversationList">
              {conversations.length === 0 ? (
                <div className="emptyState compact">No saved dialogs</div>
              ) : (
                conversations.slice(0, 8).map((conversation) => (
                  <button
                    className={`conversationRow ${
                      conversation.id === conversationId ? "active" : ""
                    }`}
                    disabled={busy}
                    key={conversation.id}
                    onClick={() => loadConversation(conversation.id)}
                    type="button"
                  >
                    <MessageSquare size={14} />
                    <strong>{conversation.title}</strong>
                    <span>{conversation.message_count}</span>
                  </button>
                ))
              )}
            </div>

            <div className="panelHeader lower">
              <h2>Health</h2>
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
              <h2>Модели</h2>
              <span>{modelCatalog?.models.length ?? 0}</span>
            </div>
            <div className="modelList">
              {dispatcher && (
                <div className={`dispatcherRow ${dispatcher.port_open ? "online" : ""}`}>
                  <Server size={14} />
                  <strong>{dispatcher.model}</strong>
                  <span>{dispatcher.port_open ? "online" : "offline"}</span>
                  <small>{dispatcher.container_status?.status ?? "port 8001"}</small>
                  <div className="dispatcherControls">
                    <button
                      type="button"
                      title="Start dispatcher"
                      aria-label="Start dispatcher"
                      disabled={dispatcherBusy || dispatcher.port_open}
                      onClick={() => runDispatcherAction("start")}
                    >
                      {dispatcherBusy ? <Loader2 className="spin" size={14} /> : <Play size={14} />}
                    </button>
                    <button
                      type="button"
                      title="Stop dispatcher"
                      aria-label="Stop dispatcher"
                      disabled={dispatcherBusy || !dispatcher.container_status?.exists}
                      onClick={() => runDispatcherAction("stop")}
                    >
                      <Square size={14} />
                    </button>
                  </div>
                </div>
              )}
              {modelCatalog ? (
                modelCatalog.models.slice(0, 5).map((model) => (
                  <div className={`modelRow ${model.active ? "active" : ""}`} key={model.id}>
                    <Cpu size={14} />
                    <strong>{model.id}</strong>
                    <span>{model.quantization ?? model.dtype ?? model.model_type ?? "model"}</span>
                    <small>{formatBytes(model.size_bytes)}</small>
                  </div>
                ))
              ) : (
                <div className="emptyState compact">Model catalog offline</div>
              )}
            </div>

            <div className="panelHeader lower">
              <h2>Ресурсы</h2>
              <span>{telemetry?.host.cpu_count ?? 0} CPU</span>
            </div>
            <div className="resourceList">
              <div className="resourceRow">
                <Gauge size={14} />
                <strong>RAM</strong>
                <div className="meter">
                  <span style={{ width: `${ratioPercent(telemetry?.memory.used_ratio)}%` }} />
                </div>
                <small>{ratioLabel(telemetry?.memory.used_ratio)}</small>
              </div>
              <div className="resourceRow">
                <Zap size={14} />
                <strong>GPU</strong>
                <div className="meter">
                  <span
                    style={{
                      width: `${ratioPercent(telemetry?.gpu.gpus?.[0]?.memory_used_ratio)}%`
                    }}
                  />
                </div>
                <small>
                  {telemetry?.gpu.available
                    ? `${Math.round(telemetry.gpu.gpus?.[0]?.utilization_gpu ?? 0)}% util`
                    : "offline"}
                </small>
              </div>
              <div className={`bridgeRow ${hostBridge?.port_open ? "online" : ""}`}>
                <Server size={14} />
                <strong>Host bridge</strong>
                <span>{hostBridge?.port_open ? "online" : "offline"}</span>
                <small>{hostBridge?.token_available ? "token" : "no token"}</small>
              </div>
              <div className={`bridgeRow ${autonomy?.enabled ? "online" : ""}`}>
                <Brain size={14} />
                <strong>Autonomy</strong>
                <span>{autonomy?.enabled ? "on" : "off"}</span>
                <small>{autonomy?.running_tasks.length ?? 0} tasks</small>
              </div>
              <button
                className="iconText compact full"
                type="button"
                onClick={runLearningTick}
                disabled={busy}
              >
                <Brain size={15} />
                <span>Learning tick</span>
              </button>
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

function ratioPercent(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value * 100)));
}

function ratioLabel(value?: number | null) {
  return `${ratioPercent(value)}%`;
}
