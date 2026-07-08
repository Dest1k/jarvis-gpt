"use client";

import {
  Activity,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Cpu,
  Database,
  FileText,
  Gauge,
  Globe,
  History,
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
  Terminal,
  Zap,
  Upload,
  Wrench
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";

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
  relevance?: number | null;
  snippet?: string | null;
  matched_terms?: string[];
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
  relevance?: number | null;
  snippet?: string | null;
  matched_terms?: string[];
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
  stdout?: string;
  stderr?: string;
  command?: string[];
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

type RuntimePreferences = {
  operator_name: string;
  communication_style: "concise" | "balanced" | "detailed";
  daily_briefing: boolean;
  voice_reply: boolean;
  preferred_profile: "gemma4-turbo" | "gemma4-mono";
  quiet_hours: string;
  working_roots: string[];
};

type AutonomyPolicy = {
  mode: "safe" | "balanced" | "operator";
  allow_safe_tools: boolean;
  allow_review_tools: boolean;
  allow_danger_tools: boolean;
  allow_background_learning: boolean;
  allow_self_healing_suggestions: boolean;
  approval_required_for: string[];
  max_autonomous_steps: number;
  resource_guard: Record<string, number>;
};

type DailyBriefing = {
  ts: string;
  operator_name: string;
  profile: string;
  home: string;
  headline: string;
  focus: string[];
  risks: string[];
  suggestions: string[];
  pending_approvals: number;
  policy_mode: string;
  counters: Record<string, number>;
  resources: Record<string, unknown>;
  recent_events: RuntimeEvent[];
};

type SelfHealReport = {
  ts: string;
  ok: boolean;
  summary: string;
  issues: { check: string; status: "warn" | "error"; message: string }[];
  actions: { id: string; label: string; kind: "safe" | "approval"; risk: string; reason: string }[];
};

type BenchmarkReport = {
  ts: string;
  profile: string;
  summary: string;
  metrics: Record<string, number>;
  telemetry: Record<string, unknown>;
  dispatcher: Record<string, unknown>;
  llm: Record<string, unknown>;
  recommendations: string[];
  history: { ts?: string; summary?: string; total_ms?: number; llm_ok?: boolean }[];
};

type BrowserPolicy = {
  mode: "approval-only" | "local-safe" | "locked";
  allow_localhost: boolean;
  allowed_hosts: string[];
  blocked_schemes: string[];
  require_approval_for_external: boolean;
  max_urls_per_action: number;
};

type DockerPolicy = {
  allowed_prefixes: string[];
  allowed_containers: string[];
  max_log_tail: number;
  include_stopped: boolean;
};

type DockerContainers = {
  ok: boolean;
  summary: string;
  policy: DockerPolicy;
  containers: { name?: string; status?: string; image?: string; allowed?: boolean }[];
  error?: string | null;
};

type AutonomyJob = {
  id: string;
  title: string;
  kind: "diagnostics" | "learning.tick" | "self_heal" | "benchmark";
  status: "enabled" | "paused" | "done";
  cadence: string;
  budget: { max_runs?: number; max_minutes?: number };
  run_count: number;
  last_run_at?: string | null;
  last_result?: Record<string, unknown>;
};

type Routine = {
  id: string;
  title: string;
  description: string;
  steps: string[];
};

type RoutineRun = {
  routine: Routine;
  ok: boolean;
  summary: string;
  results: { ok: boolean; summary: string }[];
};

type DirectoryIngestResult = {
  root: string;
  files_seen: number;
  files_indexed: number;
  files_failed: number;
};

type CommandTab =
  | "chat"
  | "runtime"
  | "models"
  | "memory"
  | "files"
  | "diagnostics"
  | "resources"
  | "audit";

type ToolInfo = {
  name: string;
  description: string;
  category: string;
  danger_level: "safe" | "review" | "danger";
};

type ToolRunResult = {
  tool: string;
  ok: boolean;
  summary: string;
  data: Record<string, unknown>;
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
  const [preferences, setPreferences] = useState<RuntimePreferences | null>(null);
  const [preferenceDraft, setPreferenceDraft] = useState({
    operator_name: "",
    communication_style: "concise" as RuntimePreferences["communication_style"],
    quiet_hours: ""
  });
  const [autonomyPolicy, setAutonomyPolicy] = useState<AutonomyPolicy | null>(null);
  const [briefing, setBriefing] = useState<DailyBriefing | null>(null);
  const [selfHealReport, setSelfHealReport] = useState<SelfHealReport | null>(null);
  const [benchmarkReport, setBenchmarkReport] = useState<BenchmarkReport | null>(null);
  const [browserPolicy, setBrowserPolicy] = useState<BrowserPolicy | null>(null);
  const [dockerPolicy, setDockerPolicy] = useState<DockerPolicy | null>(null);
  const [dockerContainers, setDockerContainers] = useState<DockerContainers | null>(null);
  const [autonomyJobs, setAutonomyJobs] = useState<AutonomyJob[]>([]);
  const [routines, setRoutines] = useState<Routine[]>([]);
  const [routineRun, setRoutineRun] = useState<RoutineRun | null>(null);
  const [directoryDraft, setDirectoryDraft] = useState("D:\\jarvis");
  const [directoryIngest, setDirectoryIngest] = useState<DirectoryIngestResult | null>(null);
  const [activeTab, setActiveTab] = useState<CommandTab>("chat");
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [input, setInput] = useState("");
  const [voiceAvailable, setVoiceAvailable] = useState(false);
  const [voiceState, setVoiceState] = useState<VoiceState>("idle");
  const [voiceInterim, setVoiceInterim] = useState("");
  const [memoryDraft, setMemoryDraft] = useState("");
  const [fileQuery, setFileQuery] = useState("");
  const [hostCommandDraft, setHostCommandDraft] = useState("");
  const [webUrlDraft, setWebUrlDraft] = useState("");
  const [webFetchResult, setWebFetchResult] = useState<ToolRunResult | null>(null);
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
        preferencesData,
        autonomyPolicyData,
        briefingData,
        browserPolicyData,
        dockerPolicyData,
        dockerContainersData,
        autonomyJobData,
        routineData,
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
          api<RuntimePreferences>("/api/preferences"),
          api<AutonomyPolicy>("/api/autonomy/policy"),
          api<DailyBriefing>("/api/briefing"),
          api<BrowserPolicy>("/api/browser/policy"),
          api<DockerPolicy>("/api/docker/policy"),
          api<DockerContainers>("/api/docker/containers"),
          api<AutonomyJob[]>("/api/autonomy/jobs"),
          api<Routine[]>("/api/routines"),
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
      setPreferences(preferencesData);
      setPreferenceDraft({
        operator_name: preferencesData.operator_name,
        communication_style: preferencesData.communication_style,
        quiet_hours: preferencesData.quiet_hours
      });
      setAutonomyPolicy(autonomyPolicyData);
      setBriefing(briefingData);
      setBrowserPolicy(browserPolicyData);
      setDockerPolicy(dockerPolicyData);
      setDockerContainers(dockerContainersData);
      setAutonomyJobs(autonomyJobData);
      setRoutines(routineData);
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
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
    navigator.serviceWorker.register("/sw.js").catch(() => undefined);
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
  const activeApprovals = useMemo(
    () =>
      approvals.filter(
        (approval) => approval.status === "pending" || approval.status === "approved"
      ),
    [approvals]
  );
  const llmCheck = useMemo(
    () => diagnostics.find((check) => check.name === "llm.router"),
    [diagnostics]
  );
  const llmReady = llmCheck?.status === "ok";
  const dispatcherPhase = dispatcher?.container_status?.status ?? (dispatcher?.port_open ? "port open" : "offline");
  const activeTabTitle: Record<CommandTab, string> = {
    chat: "Command Hub",
    runtime: "Runtime",
    models: "Models",
    memory: "Memory",
    files: "Files",
    diagnostics: "Diagnostics",
    resources: "Resources",
    audit: "Audit"
  };

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

  async function submitChat() {
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

  async function sendChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await submitChat();
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    void submitChat();
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

  async function runSelfHeal() {
    setBusy(true);
    try {
      const report = await api<SelfHealReport>("/api/self-heal", {
        method: "POST",
        body: "{}"
      });
      setSelfHealReport(report);
      setLines((current) => [
        ...current,
        { role: "system", content: `Self-heal: ${report.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Self-heal scan failed");
    } finally {
      setBusy(false);
    }
  }

  async function runBenchmark() {
    setBusy(true);
    try {
      const report = await api<BenchmarkReport>("/api/benchmark", {
        method: "POST",
        body: "{}"
      });
      setBenchmarkReport(report);
      setLines((current) => [
        ...current,
        { role: "system", content: `Benchmark: ${report.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Benchmark failed");
    } finally {
      setBusy(false);
    }
  }

  async function updatePolicyMode(mode: AutonomyPolicy["mode"]) {
    setBusy(true);
    try {
      const updated = await api<AutonomyPolicy>("/api/autonomy/policy", {
        method: "PATCH",
        body: JSON.stringify({ mode })
      });
      setAutonomyPolicy(updated);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Policy update failed");
    } finally {
      setBusy(false);
    }
  }

  async function savePreferences(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      const updated = await api<RuntimePreferences>("/api/preferences", {
        method: "PATCH",
        body: JSON.stringify(preferenceDraft)
      });
      setPreferences(updated);
      setPreferenceDraft({
        operator_name: updated.operator_name,
        communication_style: updated.communication_style,
        quiet_hours: updated.quiet_hours
      });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preferences save failed");
    } finally {
      setBusy(false);
    }
  }

  async function updateBrowserPolicyMode(mode: BrowserPolicy["mode"]) {
    setBusy(true);
    try {
      const updated = await api<BrowserPolicy>("/api/browser/policy", {
        method: "PATCH",
        body: JSON.stringify({ mode })
      });
      setBrowserPolicy(updated);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Browser policy update failed");
    } finally {
      setBusy(false);
    }
  }

  async function updateDockerTail(maxLogTail: number) {
    setBusy(true);
    try {
      const updated = await api<DockerPolicy>("/api/docker/policy", {
        method: "PATCH",
        body: JSON.stringify({ max_log_tail: maxLogTail })
      });
      setDockerPolicy(updated);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Docker policy update failed");
    } finally {
      setBusy(false);
    }
  }

  async function createAutonomyJob(kind: AutonomyJob["kind"]) {
    setBusy(true);
    try {
      const created = await api<AutonomyJob>("/api/autonomy/jobs", {
        method: "POST",
        body: JSON.stringify({
          title: kind,
          kind,
          cadence: "manual",
          budget: { max_runs: 3, max_minutes: 10 }
        })
      });
      setAutonomyJobs((current) => [created, ...current].slice(0, 10));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Autonomy job create failed");
    } finally {
      setBusy(false);
    }
  }

  async function runAutonomyJob(jobId: string) {
    setBusy(true);
    try {
      const result = await api<{ job: AutonomyJob; summary: string }>(
        `/api/autonomy/jobs/${jobId}/run`,
        { method: "POST", body: "{}" }
      );
      setAutonomyJobs((current) =>
        current.map((job) => (job.id === result.job.id ? result.job : job))
      );
      setLines((current) => [
        ...current,
        { role: "system", content: `Autonomy job: ${result.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Autonomy job run failed");
    } finally {
      setBusy(false);
    }
  }

  async function runRoutine(routineId: string) {
    setBusy(true);
    try {
      const result = await api<RoutineRun>(`/api/routines/${routineId}/run`, {
        method: "POST",
        body: "{}"
      });
      setRoutineRun(result);
      setLines((current) => [
        ...current,
        { role: "system", content: `Routine: ${result.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Routine run failed");
    } finally {
      setBusy(false);
    }
  }

  async function ingestDirectory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const path = directoryDraft.trim();
    if (!path || busy) return;
    setBusy(true);
    try {
      const result = await api<DirectoryIngestResult>("/api/files/ingest-directory", {
        method: "POST",
        body: JSON.stringify({ path, max_files: 80 })
      });
      setDirectoryIngest(result);
      setLines((current) => [
        ...current,
        {
          role: "system",
          content: `Directory ingest: ${result.files_indexed}/${result.files_seen} indexed`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Directory ingest failed");
    } finally {
      setBusy(false);
    }
  }

  async function runDispatcherAction(action: "start" | "stop" | "logs") {
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
          content: [result.summary, result.stdout || result.stderr].filter(Boolean).join("\n")
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

  async function requestHostCommandApproval(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const command = hostCommandDraft.trim();
    if (!command || busy) return;
    setBusy(true);
    try {
      const approval = await api<ApprovalItem>("/api/approvals", {
        method: "POST",
        body: JSON.stringify({
          title: "Host command",
          description: command,
          requested_action: "tool.run",
          risk: "danger",
          payload: {
            tool: "host.bridge.execute",
            arguments: { command }
          }
        })
      });
      setHostCommandDraft("");
      setApprovals((current) => [approval, ...current].slice(0, 8));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Host command approval failed");
    } finally {
      setBusy(false);
    }
  }

  async function runWebFetch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = webUrlDraft.trim();
    if (!url || busy) return;
    setBusy(true);
    try {
      const result = await api<ToolRunResult>("/api/tools/web.fetch/run", {
        method: "POST",
        body: JSON.stringify({
          arguments: { url, max_chars: 3000 }
        })
      });
      const text = typeof result.data.text === "string" ? result.data.text : "";
      setWebFetchResult(result);
      setLines((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: [result.summary, text.slice(0, 900)].filter(Boolean).join("\n")
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Web fetch failed");
    } finally {
      setBusy(false);
    }
  }

  const webFetchText = typeof webFetchResult?.data.text === "string" ? webFetchResult.data.text : "";
  const webFetchStatus =
    typeof webFetchResult?.data.status_code === "number" ? webFetchResult.data.status_code : null;

  return (
    <main className="shell">
      <aside className="rail" aria-label="Навигация">
        <div className="brandMark">
          <Brain size={22} />
        </div>
        <IconButton active={activeTab === "chat"} label="Диалог" tab="chat" onSelect={setActiveTab}>
          <MessageSquare size={20} />
        </IconButton>
        <IconButton
          active={activeTab === "runtime"}
          label="Runtime"
          tab="runtime"
          onSelect={setActiveTab}
        >
          <Server size={20} />
        </IconButton>
        <IconButton
          active={activeTab === "models"}
          label="Модели"
          tab="models"
          onSelect={setActiveTab}
        >
          <Cpu size={20} />
        </IconButton>
        <IconButton
          active={activeTab === "memory"}
          label="Память"
          tab="memory"
          onSelect={setActiveTab}
        >
          <Database size={20} />
        </IconButton>
        <IconButton
          active={activeTab === "files"}
          label="Файлы"
          tab="files"
          onSelect={setActiveTab}
        >
          <FileText size={20} />
        </IconButton>
        <IconButton
          active={activeTab === "diagnostics"}
          label="Диагностика"
          tab="diagnostics"
          onSelect={setActiveTab}
        >
          <Activity size={20} />
        </IconButton>
        <IconButton
          active={activeTab === "resources"}
          label="Ресурсы"
          tab="resources"
          onSelect={setActiveTab}
        >
          <Gauge size={20} />
        </IconButton>
        <IconButton active={activeTab === "audit"} label="Audit" tab="audit" onSelect={setActiveTab}>
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

        <section className="statusGrid" id="runtime" aria-label="Сводка runtime">
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
          <section className="chatPanel" id="dialog" aria-label="Диалог">
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
                onKeyDown={handleComposerKeyDown}
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

          <section className="opsPanel" aria-label={activeTabTitle[activeTab]}>
            {activeTab === "chat" && (
              <>
                <div className="panelHeader">
                  <h2>Command Hub</h2>
                  <span>{llmReady ? "ready" : "loading"}</span>
                </div>
                <div className="vitalsPanel">
                  <article className={`vitalHero ${llmReady ? "ok" : "warn"}`}>
                    <Sparkles size={18} />
                    <div>
                      <strong>{llmReady ? "LLM ready" : "LLM warming"}</strong>
                      <p>{llmCheck?.message ?? dispatcherPhase}</p>
                    </div>
                    <span>{dispatcher?.port_open ? "8001" : "off"}</span>
                  </article>
                  <div className="vitalGrid">
                    <div>
                      <span>Backend</span>
                      <strong>{status ? "online" : "offline"}</strong>
                    </div>
                    <div>
                      <span>Bridge</span>
                      <strong>{hostBridge?.port_open ? "online" : "offline"}</strong>
                    </div>
                    <div>
                      <span>Dispatcher</span>
                      <strong>{dispatcherPhase}</strong>
                    </div>
                    <div>
                      <span>GPU</span>
                      <strong>{telemetry?.gpu.available ? "online" : "offline"}</strong>
                    </div>
                  </div>
                </div>
              </>
            )}

            {(activeTab === "chat" || activeTab === "runtime") && (
              <>
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
              <span>{activeApprovals.length}</span>
            </div>
            <div className="approvalList">
              {activeApprovals.length === 0 ? (
                <div className="emptyState compact">No pending gates</div>
              ) : (
                activeApprovals.slice(0, 5).map((approval) => (
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
              <h2>Briefing</h2>
              <span>{briefing?.policy_mode ?? autonomyPolicy?.mode ?? "..."}</span>
            </div>
            <div className="briefingPanel">
              <div className="briefingHero">
                <Brain size={16} />
                <strong>{briefing?.headline ?? "Runtime snapshot pending"}</strong>
                <span>{briefing?.operator_name ?? preferences?.operator_name ?? "operator"}</span>
              </div>
              <div className="briefingList">
                {(briefing?.focus ?? []).slice(0, 4).map((item) => (
                  <p key={item}>{item}</p>
                ))}
              </div>
              <div className="briefingList suggestions">
                {(briefing?.suggestions ?? []).slice(0, 4).map((item) => (
                  <p key={item}>{item}</p>
                ))}
              </div>
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
              </>
            )}

            {(activeTab === "chat" || activeTab === "diagnostics" || activeTab === "runtime") && (
              <>
            <div className={`panelHeader ${activeTab === "chat" ? "lower" : ""}`} id="health">
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
            <div className="selfHealPanel">
              <button
                className="iconText compact full"
                type="button"
                onClick={runSelfHeal}
                disabled={busy}
              >
                {busy ? <Loader2 className="spin" size={15} /> : <Activity size={15} />}
                <span>Self-heal scan</span>
              </button>
              {selfHealReport && (
                <article className={`selfHealReport ${selfHealReport.ok ? "ok" : "warn"}`}>
                  <div>
                    <strong>{selfHealReport.summary}</strong>
                    <span>{selfHealReport.actions.length} actions</span>
                  </div>
                  {selfHealReport.actions.slice(0, 3).map((action) => (
                    <p key={action.id}>
                      {action.label}: {action.reason}
                    </p>
                  ))}
                </article>
              )}
            </div>
              </>
            )}

            {(activeTab === "models" || activeTab === "runtime") && (
              <>
            <div className={`panelHeader ${activeTab === "runtime" ? "lower" : ""}`} id="models">
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
                    <button
                      type="button"
                      title="Dispatcher logs"
                      aria-label="Dispatcher logs"
                      disabled={dispatcherBusy || !dispatcher.container_status?.exists}
                      onClick={() => runDispatcherAction("logs")}
                    >
                      <FileText size={14} />
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
              </>
            )}

            {(activeTab === "resources" || activeTab === "runtime") && (
              <>
            <div className={`panelHeader ${activeTab === "runtime" ? "lower" : ""}`} id="resources">
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
              <form className="hostCommandForm" onSubmit={requestHostCommandApproval}>
                <input
                  aria-label="Host command"
                  value={hostCommandDraft}
                  onChange={(event) => setHostCommandDraft(event.target.value)}
                  placeholder="Get-Date"
                />
                <button
                  type="submit"
                  disabled={busy || !hostCommandDraft.trim()}
                  title="Request approval"
                  aria-label="Request approval"
                >
                  <Terminal size={15} />
                </button>
              </form>
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
              </>
            )}

            {(activeTab === "diagnostics" || activeTab === "runtime") && (
              <>
            <div className={`panelHeader ${activeTab === "runtime" ? "lower" : ""}`}>
              <h2>Autonomy Policy</h2>
              <span>{autonomyPolicy?.max_autonomous_steps ?? 0} steps</span>
            </div>
            <div className="policyPanel">
              <div className="segmentedControl" role="group" aria-label="Autonomy policy mode">
                {(["safe", "balanced", "operator"] as AutonomyPolicy["mode"][]).map((mode) => (
                  <button
                    className={autonomyPolicy?.mode === mode ? "active" : ""}
                    disabled={busy}
                    key={mode}
                    onClick={() => updatePolicyMode(mode)}
                    type="button"
                  >
                    {mode}
                  </button>
                ))}
              </div>
              <div className="policyFlags">
                <span className={autonomyPolicy?.allow_review_tools ? "on" : ""}>review</span>
                <span className={autonomyPolicy?.allow_danger_tools ? "on" : ""}>danger</span>
                <span className={autonomyPolicy?.allow_background_learning ? "on" : ""}>
                  learning
                </span>
                <span>
                  {Math.round((autonomyPolicy?.resource_guard.max_gpu_memory_ratio ?? 0) * 100)}%
                  GPU
                </span>
              </div>
            </div>

            <div className="panelHeader lower">
              <h2>Autonomy Jobs</h2>
              <span>{autonomyJobs.length}</span>
            </div>
            <div className="jobsPanel">
              <div className="quickActions">
                {(["diagnostics", "self_heal", "benchmark"] as AutonomyJob["kind"][]).map(
                  (kind) => (
                    <button
                      className="iconText compact"
                      disabled={busy}
                      key={kind}
                      onClick={() => createAutonomyJob(kind)}
                      type="button"
                    >
                      <Brain size={14} />
                      <span>{kind}</span>
                    </button>
                  )
                )}
              </div>
              {autonomyJobs.slice(0, 4).map((job) => (
                <div className={`jobRow ${job.status}`} key={job.id}>
                  <Brain size={14} />
                  <strong>{job.title}</strong>
                  <span>{job.run_count}/{job.budget.max_runs ?? 1}</span>
                  <button
                    type="button"
                    disabled={busy || job.status !== "enabled"}
                    title="Run job"
                    aria-label="Run job"
                    onClick={() => runAutonomyJob(job.id)}
                  >
                    <Play size={14} />
                  </button>
                </div>
              ))}
            </div>

            <div className="panelHeader lower">
              <h2>Routines</h2>
              <span>{routines.length}</span>
            </div>
            <div className="routinePanel">
              {routines.map((routine) => (
                <button
                  className="routineRow"
                  disabled={busy}
                  key={routine.id}
                  onClick={() => runRoutine(routine.id)}
                  type="button"
                >
                  <Play size={14} />
                  <strong>{routine.title}</strong>
                  <span>{routine.steps.length}</span>
                </button>
              ))}
              {routineRun && (
                <article className={`routineResult ${routineRun.ok ? "ok" : "warn"}`}>
                  <strong>{routineRun.summary}</strong>
                  <p>{routineRun.results.map((item) => item.summary).join(" | ")}</p>
                </article>
              )}
            </div>

            <div className="panelHeader lower">
              <h2>Performance</h2>
              <span>
                {typeof benchmarkReport?.metrics.total_ms === "number"
                  ? `${Math.round(benchmarkReport.metrics.total_ms)} ms`
                  : "ready"}
              </span>
            </div>
            <div className="benchmarkPanel">
              <button
                className="iconText compact full"
                type="button"
                onClick={runBenchmark}
                disabled={busy}
              >
                {busy ? <Loader2 className="spin" size={15} /> : <Gauge size={15} />}
                <span>Benchmark</span>
              </button>
              {benchmarkReport && (
                <article className="benchmarkReport">
                  <strong>{benchmarkReport.summary}</strong>
                  <div className="metricRows">
                    <span>storage {Math.round(benchmarkReport.metrics.storage_ping_ms ?? 0)} ms</span>
                    <span>LLM {Math.round(benchmarkReport.metrics.llm_health_ms ?? 0)} ms</span>
                    <span>
                      {benchmarkReport.dispatcher.port_open ? "dispatcher on" : "dispatcher off"}
                    </span>
                  </div>
                  {benchmarkReport.recommendations.slice(0, 3).map((item) => (
                    <p key={item}>{item}</p>
                  ))}
                </article>
              )}
            </div>

            <div className="panelHeader lower">
              <h2>Browser Policy</h2>
              <span>{browserPolicy?.mode ?? "..."}</span>
            </div>
            <div className="policyPanel">
              <div className="segmentedControl" role="group" aria-label="Browser policy mode">
                {(["approval-only", "local-safe", "locked"] as BrowserPolicy["mode"][]).map(
                  (mode) => (
                    <button
                      className={browserPolicy?.mode === mode ? "active" : ""}
                      disabled={busy}
                      key={mode}
                      onClick={() => updateBrowserPolicyMode(mode)}
                      type="button"
                    >
                      {mode}
                    </button>
                  )
                )}
              </div>
              <div className="policyFlags">
                <span className={browserPolicy?.allow_localhost ? "on" : ""}>localhost</span>
                <span>{browserPolicy?.max_urls_per_action ?? 0} tabs</span>
                <span>{browserPolicy?.allowed_hosts.slice(0, 2).join(", ")}</span>
              </div>
            </div>

            <div className="panelHeader lower">
              <h2>Docker Fleet</h2>
              <span>{dockerContainers?.containers.length ?? 0}</span>
            </div>
            <div className="dockerPanel">
              <div className="policyFlags">
                <button
                  className="iconText compact"
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    updateDockerTail(Math.min((dockerPolicy?.max_log_tail ?? 200) + 50, 1000))
                  }
                >
                  <FileText size={14} />
                  <span>{dockerPolicy?.max_log_tail ?? 200} logs</span>
                </button>
                <span>{dockerPolicy?.allowed_prefixes.slice(0, 2).join(", ")}</span>
              </div>
              {(dockerContainers?.containers ?? []).slice(0, 4).map((container) => (
                <div
                  className={`dockerRow ${container.allowed ? "allowed" : ""}`}
                  key={container.name}
                >
                  <Server size={14} />
                  <strong>{container.name}</strong>
                  <span>{container.allowed ? "allowed" : "blocked"}</span>
                  <small>{container.status ?? container.image}</small>
                </div>
              ))}
            </div>

            <div className="panelHeader lower">
              <h2>Инструменты</h2>
              <span>{tools.length}</span>
            </div>
            <form className="webFetchForm" onSubmit={runWebFetch}>
              <input
                aria-label="Web URL"
                value={webUrlDraft}
                onChange={(event) => setWebUrlDraft(event.target.value)}
                placeholder="https://example.com"
              />
              <button
                type="submit"
                disabled={busy || !webUrlDraft.trim()}
                title="Fetch"
                aria-label="Fetch"
              >
                <Globe size={15} />
              </button>
            </form>
            {webFetchResult && (
              <article className={`webFetchResult ${webFetchResult.ok ? "ok" : "warn"}`}>
                <div>
                  <Globe size={14} />
                  <strong>{webFetchResult.summary}</strong>
                  <span>{webFetchStatus ?? webFetchResult.tool}</span>
                </div>
                <p>{webFetchText || webFetchResult.summary}</p>
              </article>
            )}
            <div className="toolList">
              {tools.slice(0, 8).map((tool) => (
                <div className="toolRow" key={tool.name}>
                  <Wrench size={14} />
                  <strong>{tool.name}</strong>
                  <span>{tool.category}</span>
                </div>
              ))}
            </div>
              </>
            )}

            {activeTab === "files" && (
              <>
            <div className="panelHeader" id="files">
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
            <form className="directoryForm" onSubmit={ingestDirectory}>
              <input
                value={directoryDraft}
                onChange={(event) => setDirectoryDraft(event.target.value)}
                placeholder="D:\\jarvis"
                aria-label="Directory to ingest"
              />
              <button
                type="submit"
                disabled={busy || !directoryDraft.trim()}
                title="Index directory"
                aria-label="Index directory"
              >
                <Database size={15} />
              </button>
            </form>
            {directoryIngest && (
              <article className="directoryResult">
                <strong>{directoryIngest.root}</strong>
                <span>
                  {directoryIngest.files_indexed}/{directoryIngest.files_seen} indexed
                </span>
              </article>
            )}
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
                      <span>{Math.round((hit.relevance ?? 0) * 100)}%</span>
                    </div>
                    <p>{hit.snippet ?? hit.content}</p>
                  </article>
                ))}
              </div>
            )}
              </>
            )}

            {activeTab === "memory" && (
              <>
            <div className="panelHeader">
              <h2>Preferences</h2>
              <span>{preferences?.communication_style ?? "concise"}</span>
            </div>
            <form className="preferencesForm" onSubmit={savePreferences}>
              <input
                value={preferenceDraft.operator_name}
                onChange={(event) =>
                  setPreferenceDraft((current) => ({
                    ...current,
                    operator_name: event.target.value
                  }))
                }
                placeholder="Operator"
                aria-label="Operator name"
              />
              <select
                value={preferenceDraft.communication_style}
                onChange={(event) =>
                  setPreferenceDraft((current) => ({
                    ...current,
                    communication_style: event.target.value as RuntimePreferences["communication_style"]
                  }))
                }
                aria-label="Communication style"
              >
                <option value="concise">concise</option>
                <option value="balanced">balanced</option>
                <option value="detailed">detailed</option>
              </select>
              <input
                value={preferenceDraft.quiet_hours}
                onChange={(event) =>
                  setPreferenceDraft((current) => ({
                    ...current,
                    quiet_hours: event.target.value
                  }))
                }
                placeholder="quiet hours"
                aria-label="Quiet hours"
              />
              <button
                type="submit"
                disabled={busy}
                title="Save preferences"
                aria-label="Save preferences"
              >
                {busy ? <Loader2 className="spin" size={15} /> : <Save size={15} />}
              </button>
            </form>

            <div className="panelHeader lower" id="memory">
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
                  <p>{memory.snippet ?? memory.content}</p>
                </article>
              ))}
            </div>
              </>
            )}

            {activeTab === "audit" && (
              <>
            <div className="panelHeader" id="audit">
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
              </>
            )}
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

function IconButton({
  active,
  children,
  label,
  tab,
  onSelect
}: {
  active: boolean;
  children: ReactNode;
  label: string;
  tab: CommandTab;
  onSelect: (tab: CommandTab) => void;
}) {
  return (
    <button
      className={`railButton ${active ? "active" : ""}`}
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active}
      onClick={() => onSelect(tab)}
    >
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
