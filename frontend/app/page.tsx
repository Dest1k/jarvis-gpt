"use client";

import {
  Activity,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Cpu,
  Database,
  Download,
  FileText,
  Gauge,
  Globe,
  GripHorizontal,
  History,
  Loader2,
  Mic,
  MicOff,
  MessageSquare,
  Paperclip,
  Plus,
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
  Trash2,
  Zap,
  Upload,
  Wrench,
  X
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from "react";

const CONFIGURED_API_URL = process.env.NEXT_PUBLIC_JARVIS_API_URL ?? "http://localhost:8000";
const CHAT_WINDOWS_KEY = "jarvis-gpt.chatWindows.v1";
const CHAT_SETTINGS_KEY = "jarvis-gpt.chatSettings.v1";
const DEFAULT_CHAT_HEIGHT = 620;
const DEFAULT_MAX_TOKENS = 2048;
const DEFAULT_CHAT_WINDOW_ID = "chat-default";
const BOOT_MESSAGE = "Центр управления JARVIS GPT готов к подключению.";
const LIVE_TELEMETRY_INTERVAL_MS = 1000;
const BACKGROUND_TELEMETRY_INTERVAL_MS = 3000;
const VITAL_STATUS_INTERVAL_MS = 3000;

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

type ChatAttachment = {
  id: string;
  name: string;
  mime_type?: string | null;
  size?: number | null;
  url?: string | null;
};

type ChatLine = {
  id?: string;
  role: "user" | "assistant" | "system";
  content: string;
  attachments?: ChatAttachment[];
  durationMs?: number | null;
  pending?: boolean;
  startedAt?: number | null;
};

type ChatWindow = {
  id: string;
  title: string;
  conversationId: string | null;
  input: string;
  lines: ChatLine[];
  createdAt: number;
};

type StoredChatWindows = {
  activeId?: string;
  windows?: ChatWindow[];
};

type StoredChatSettings = {
  activeTab?: CommandTab;
  chatSideTab?: ChatSideTab;
  chatHeight?: number;
  maxTokens?: number;
  thinkingEnabled?: boolean;
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
  metadata?: Record<string, unknown>;
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

type MemoryGraphNode = {
  id: string;
  label: string;
  kind: string;
  path?: string | null;
  namespace?: string | null;
  tags?: string[];
  importance?: number | null;
  updated_at?: string | null;
  degree?: number | null;
};

type MemoryVault = {
  root: string;
  notes: {
    id?: string | null;
    title: string;
    path: string;
    namespace?: string | null;
    tags: string[];
    links: string[];
    importance?: number | null;
    updated_at?: string | null;
    content: string;
  }[];
  nodes: MemoryGraphNode[];
  edges: { source: string; target: string; kind: string }[];
  backlinks: Record<string, string[]>;
  top_nodes: MemoryGraphNode[];
  stats: Record<string, number>;
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
  fit?: ModelFit;
};

type ModelCatalog = {
  root: string;
  active_profile: string;
  active_model: ModelArtifact;
  models: ModelArtifact[];
  vram?: VramBudget;
  downloads?: ModelDownloadJob[];
  dispatcher: {
    base_url: string;
    served_model_name: string;
    model_path: string;
    docker_model_path: string;
    env: Record<string, string>;
  };
};

type ModelFit = {
  status: "fits" | "tight" | "no" | "unknown";
  label: string;
  confidence: "high" | "medium" | "low";
  required_bytes: number;
  weights_bytes: number;
  kv_cache_bytes: number;
  overhead_bytes: number;
  gpu_total_bytes: number;
  gpu_free_bytes: number;
  context_tokens: number;
  quant_bits: number;
  parameters?: number | null;
  warnings: string[];
};

type VramBudget = {
  available: boolean;
  name?: string;
  total_bytes?: number;
  used_bytes?: number;
  free_bytes?: number;
  error?: string;
};

type RemoteModel = {
  id: string;
  author?: string | null;
  downloads: number;
  likes: number;
  tags: string[];
  pipeline_tag?: string | null;
  gated?: boolean | string | null;
  size_bytes: number;
  downloadable_files: number;
  fit: ModelFit;
};

type ModelSearchResult = {
  query: string;
  items: RemoteModel[];
  vram: VramBudget;
  token_available: boolean;
};

type ModelDownloadJob = {
  id: string;
  repo_id: string;
  revision: string;
  status: "queued" | "running" | "done" | "error";
  summary: string;
  target: string;
  total_files: number;
  completed_files: number;
  total_bytes: number;
  downloaded_bytes: number;
  current_file?: string;
  error?: string;
  workers?: number;
  resumable?: boolean;
};

type DispatcherRuntime = {
  source?: string;
  model_path?: string;
  model_id?: string;
  served_model_name?: string;
  enforce_eager?: boolean;
  max_model_len?: number | null;
  gpu_memory_utilization?: number | null;
  kv_cache_dtype?: string;
  max_num_seqs?: number | null;
  cpu_offload_gb?: number | null;
  swap_space_gb?: number | null;
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
  desired_model?: ModelArtifact | null;
  runtime?: DispatcherRuntime | null;
  desired_runtime?: DispatcherRuntime;
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
  native_capabilities?: string[];
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

const CHAT_SIDE_TABS = ["status", "missions", "approvals", "briefing", "history"] as const;

type ChatSideTab = (typeof CHAT_SIDE_TABS)[number];

type ActiveOperation = {
  title: string;
  detail?: string;
};

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
  duration_ms?: number;
  error?: string;
  message_id?: string;
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
  const response = await fetch(`${apiUrl()}${path}`, {
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
  const response = await fetch(`${apiUrl()}${path}`, {
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

function fileItemToAttachment(file: FileItem): ChatAttachment {
  return {
    id: file.id,
    name: file.name,
    mime_type: file.mime_type,
    size: file.size,
    url: `/api/files/${encodeURIComponent(file.id)}/download`
  };
}

function normalizeChatAttachment(value: unknown): ChatAttachment | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const id = typeof item.id === "string" ? item.id.trim() : "";
  const name = typeof item.name === "string" ? item.name.trim() : "";
  if (!id || !name) return null;
  return {
    id,
    name,
    mime_type: typeof item.mime_type === "string" ? item.mime_type : null,
    size: typeof item.size === "number" && Number.isFinite(item.size) ? item.size : null,
    url: typeof item.url === "string" ? item.url : null
  };
}

function normalizeChatAttachments(value: unknown): ChatAttachment[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => normalizeChatAttachment(item))
    .filter((item): item is ChatAttachment => item !== null)
    .slice(0, 8);
}

function attachmentsFromMetadata(metadata?: Record<string, unknown>): ChatAttachment[] {
  return normalizeChatAttachments(metadata?.attachments);
}

function attachmentHref(attachment: ChatAttachment) {
  if (attachment.url) {
    if (attachment.url.startsWith("/")) {
      return `${apiUrl()}${attachment.url}`;
    }
    return attachment.url;
  }
  return `${apiUrl()}/api/files/${encodeURIComponent(attachment.id)}/download`;
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
  if (!Number.isFinite(value)) return DEFAULT_MAX_TOKENS;
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

function bootLines(): ChatLine[] {
  return [
    {
      id: "system-boot",
      role: "system",
      content: BOOT_MESSAGE
    }
  ];
}

function randomId(prefix: string) {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function createChatWindow(title = "Новый чат"): ChatWindow {
  return {
    id: randomId("chat"),
    title,
    conversationId: null,
    input: "",
    lines: bootLines(),
    createdAt: Date.now()
  };
}

function createInitialChatWindow(): ChatWindow {
  return {
    id: DEFAULT_CHAT_WINDOW_ID,
    title: "Новый чат",
    conversationId: null,
    input: "",
    lines: bootLines(),
    createdAt: 0
  };
}

function normalizeStoredLine(line: ChatLine): ChatLine {
  const durationMs = coerceDurationMs(line.durationMs);
  const attachments = normalizeChatAttachments(line.attachments);
  if (line.id === "system-boot" && line.content.includes("Command Center")) {
    return { ...line, content: BOOT_MESSAGE, attachments, durationMs, pending: false, startedAt: null };
  }
  return {
    ...line,
    attachments,
    durationMs,
    pending: false,
    startedAt: null
  };
}

function coerceDurationMs(value: unknown): number | null {
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return null;
  return Math.round(numeric);
}

function formatDuration(ms: number) {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 10000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 1000)} s`;
}

function assistantDuration(line: ChatLine, now: number) {
  if (line.role !== "assistant") return null;
  const stored = coerceDurationMs(line.durationMs);
  if (stored !== null) return formatDuration(stored);
  if (line.pending && line.startedAt) {
    return formatDuration(Math.max(0, now - line.startedAt));
  }
  return null;
}

function readStoredChatWindows(): { windows: ChatWindow[]; activeId: string } {
  const fallback = createInitialChatWindow();
  if (typeof window === "undefined") {
    return { windows: [fallback], activeId: fallback.id };
  }
  try {
    const parsed = JSON.parse(localStorage.getItem(CHAT_WINDOWS_KEY) || "{}") as StoredChatWindows;
    const windows = (parsed.windows ?? [])
      .filter((item) => item && typeof item.id === "string")
      .slice(0, 8)
      .map((item) => ({
        ...item,
        title: normalizeChatTitle(item.title),
        input: item.input || "",
        conversationId: item.conversationId ?? null,
        lines:
          Array.isArray(item.lines) && item.lines.length
            ? item.lines.map(normalizeStoredLine)
            : bootLines(),
        createdAt: Number(item.createdAt) || Date.now()
      }));
    if (windows.length) {
      return {
        windows,
        activeId: windows.some((item) => item.id === parsed.activeId)
          ? String(parsed.activeId)
          : windows[0].id
      };
    }
  } catch {
    return { windows: [fallback], activeId: fallback.id };
  }
  return { windows: [fallback], activeId: fallback.id };
}

function readStoredChatSettings(): StoredChatSettings {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(localStorage.getItem(CHAT_SETTINGS_KEY) || "{}") as StoredChatSettings;
  } catch {
    return {};
  }
}

function storedMaxTokens(value: unknown) {
  const tokens = clampMaxTokens(Number(value ?? DEFAULT_MAX_TOKENS));
  return tokens === 512 ? DEFAULT_MAX_TOKENS : tokens;
}

function normalizeChatSideTab(value: unknown): ChatSideTab {
  return CHAT_SIDE_TABS.includes(value as ChatSideTab) ? (value as ChatSideTab) : "status";
}

function clampChatHeight(value: number) {
  if (!Number.isFinite(value)) return DEFAULT_CHAT_HEIGHT;
  return Math.max(460, Math.min(760, Math.round(value)));
}

function compactText(value: string | null | undefined, maxLength = 72) {
  const cleaned = (value ?? "").replace(/\s+/g, " ").trim();
  if (cleaned.length <= maxLength) return cleaned;
  return `${cleaned.slice(0, maxLength - 1)}…`;
}

function runtimeStateLabel(value: string | null | undefined) {
  const normalized = (value ?? "").toLowerCase();
  const labels: Record<string, string> = {
    active: "активен",
    allowed: "разрешён",
    blocked: "заблокирован",
    created: "создан",
    disabled: "выключена",
    exited: "остановлен",
    loading: "загрузка",
    none: "нет",
    offline: "офлайн",
    online: "онлайн",
    ready: "готово",
    running: "работает",
    "container-missing": "контейнер не найден",
    "port open": "порт открыт"
  };
  return labels[normalized] ?? value ?? "нет данных";
}

function communicationStyleLabel(value: string | null | undefined) {
  const labels: Record<string, string> = {
    concise: "кратко",
    balanced: "сбалансированно",
    detailed: "подробно"
  };
  return labels[value ?? ""] ?? value ?? "кратко";
}

function isThrowawayChatTitle(value: string) {
  const normalized = value
    .toLowerCase()
    .replace(/[?!.,;:]+$/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return [
    /^как мы до (?:всего )?этого дошли$/,
    /^(?:а )?что это(?: тут)? такое$/,
    /^что это$/,
    /^проверка(?: связи)?$/,
    /^на связи$/,
    /^есть связь$/,
    /^а теперь$/,
    /^а сейчас$/,
    /^сейчас$/
  ].some((pattern) => pattern.test(normalized));
}

function normalizeChatTitle(value: string | null | undefined) {
  const cleaned = (value ?? "").replace(/\s+/g, " ").trim();
  if (!cleaned || isThrowawayChatTitle(cleaned)) return "Новый чат";
  return cleaned.slice(0, 42) + (cleaned.length > 42 ? "..." : "");
}

function titleFromMessage(message: string) {
  const cleaned = message.replace(/\s+/g, " ").trim();
  if (!cleaned || isThrowawayChatTitle(cleaned)) return "Новый чат";
  return normalizeChatTitle(cleaned);
}

function cleanAssistantText(content: string) {
  return content
    .replace(
      /^\s*(?:\$\s*\\(?:rightarrow|to)\s*\$|\\(?:rightarrow|to)|→|->|⇒)?\s*(?:\*\*)?(?:важное\s+уточнение|уточнение|important\s+note)\s*:?(?:\*\*)?\s*/i,
      ""
    )
    .trimStart();
}

type RichBlock =
  | { type: "paragraph"; text: string }
  | { type: "heading"; level: number; text: string }
  | { type: "list"; items: string[] }
  | { type: "code"; language: string; code: string };

const CONSOLE_LANGUAGES = new Set([
  "bash",
  "bat",
  "cmd",
  "console",
  "log",
  "powershell",
  "ps1",
  "pwsh",
  "sh",
  "shell",
  "terminal",
  "zsh"
]);

function parseRichBlocks(content: string): RichBlock[] {
  const blocks: RichBlock[] = [];
  const fencePattern = /```([^\n`]*)\n?([\s\S]*?)```/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = fencePattern.exec(content)) !== null) {
    blocks.push(...parseTextBlocks(content.slice(cursor, match.index)));
    blocks.push({
      type: "code",
      language: match[1].trim(),
      code: match[2].replace(/\n$/, "")
    });
    cursor = match.index + match[0].length;
  }
  blocks.push(...parseTextBlocks(content.slice(cursor)));
  return blocks;
}

function parseTextBlocks(content: string): RichBlock[] {
  const blocks: RichBlock[] = [];
  const paragraph: string[] = [];
  let listItems: string[] = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push({ type: "paragraph", text: paragraph.join("\n").trimEnd() });
    paragraph.length = 0;
  };
  const flushList = () => {
    if (!listItems.length) return;
    blocks.push({ type: "list", items: listItems });
    listItems = [];
  };

  for (const line of content.replace(/\r\n/g, "\n").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }
    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
      continue;
    }
    const listItem = line.match(/^\s*(?:[-*•]\s+|\d+\.\s+)(.+)$/);
    if (listItem) {
      flushParagraph();
      listItems.push(listItem[1]);
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flushParagraph();
  flushList();
  return blocks;
}

function renderRichMessage(content: string, role: ChatLine["role"]) {
  const cleaned = role === "assistant" ? cleanAssistantText(content) : content;
  const blocks = parseRichBlocks(cleaned);
  if (!blocks.length) {
    return <div className="richMessage empty" />;
  }
  return (
    <div className="richMessage">
      {blocks.map((block, index) => renderRichBlock(block, `block-${index}`))}
    </div>
  );
}

function renderRichBlock(block: RichBlock, key: string): ReactNode {
  if (block.type === "heading") {
    return (
      <div className={`richHeading level${block.level}`} key={key}>
        {renderRichInline(block.text, key)}
      </div>
    );
  }
  if (block.type === "list") {
    return (
      <ul className="richList" key={key}>
        {block.items.map((item, index) => (
          <li key={`${key}-item-${index}`}>{renderRichInlineWithBreaks(item, `${key}-item-${index}`)}</li>
        ))}
      </ul>
    );
  }
  if (block.type === "code") {
    const language = block.language || "code";
    const consoleBlock = isConsoleCodeBlock(language, block.code);
    return (
      <div className={`richCodeBlock ${consoleBlock ? "console" : "code"}`} key={key}>
        <div className="richCodeHeader">
          <Terminal size={13} />
          <span>{consoleBlock ? "console" : language}</span>
        </div>
        <pre>
          <code>{block.code}</code>
        </pre>
      </div>
    );
  }
  return (
    <p className="richParagraph" key={key}>
      {renderRichInlineWithBreaks(block.text, key)}
    </p>
  );
}

function renderRichInlineWithBreaks(text: string, key: string): ReactNode[] {
  return text.split("\n").flatMap((line, index) =>
    index === 0
      ? renderRichInline(line, `${key}-line-${index}`)
      : [<br key={`${key}-br-${index}`} />, ...renderRichInline(line, `${key}-line-${index}`)]
  );
}

function renderRichInline(text: string, key: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const tokenPattern = /(\[[^\]]+\]\(https?:\/\/[^\s)]+\)|https?:\/\/[^\s<)]+|`[^`\n]+`|\*\*[\s\S]+?\*\*)/g;
  let cursor = 0;
  let index = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenPattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }
    const token = match[0];
    if (token.startsWith("**") && token.endsWith("**")) {
      nodes.push(
        <strong key={`${key}-strong-${index}`}>
          {renderRichInline(token.slice(2, -2), `${key}-strong-${index}`)}
        </strong>
      );
    } else if (token.startsWith("`") && token.endsWith("`")) {
      nodes.push(<code key={`${key}-code-${index}`}>{token.slice(1, -1)}</code>);
    } else {
      const markdownLink = token.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      const href = markdownLink ? markdownLink[2] : token;
      const label = markdownLink ? markdownLink[1] : token;
      nodes.push(
        <a href={href} key={`${key}-link-${index}`} rel="noreferrer" target="_blank">
          {label}
        </a>
      );
    }
    cursor = match.index + token.length;
    index += 1;
  }
  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function isConsoleCodeBlock(language: string, code: string) {
  const normalized = language.trim().toLowerCase();
  if (CONSOLE_LANGUAGES.has(normalized)) return true;
  return /(^|\n)\s*(PS\s+[A-Z]:\\[^>]*>|[A-Z]:\\[^>]*>|[$#>]\s+)/i.test(code);
}

export default function CommandCenter() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [diagnostics, setDiagnostics] = useState<DiagnosticCheck[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [memoryVault, setMemoryVault] = useState<MemoryVault | null>(null);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [fileHits, setFileHits] = useState<FileChunkHit[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null);
  const [modelSearchQuery, setModelSearchQuery] = useState("gemma qwen llama nvfp4");
  const [modelSearchResult, setModelSearchResult] = useState<ModelSearchResult | null>(null);
  const [modelSearchBusy, setModelSearchBusy] = useState(false);
  const [modelDownloads, setModelDownloads] = useState<ModelDownloadJob[]>([]);
  const [modelWorkers, setModelWorkers] = useState(3);
  const [modelContextTokens, setModelContextTokens] = useState(8192);
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
  const [chatSideTab, setChatSideTab] = useState<ChatSideTab>("status");
  const [chatHeight, setChatHeight] = useState(DEFAULT_CHAT_HEIGHT);
  const [chatWindows, setChatWindows] = useState<ChatWindow[]>(() => [
    createInitialChatWindow()
  ]);
  const [activeChatWindowId, setActiveChatWindowId] = useState(DEFAULT_CHAT_WINDOW_ID);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [voiceAvailable, setVoiceAvailable] = useState(false);
  const [voiceState, setVoiceState] = useState<VoiceState>("idle");
  const [voiceInterim, setVoiceInterim] = useState("");
  const [memoryDraft, setMemoryDraft] = useState("");
  const [fileQuery, setFileQuery] = useState("");
  const [hostCommandDraft, setHostCommandDraft] = useState("");
  const [webUrlDraft, setWebUrlDraft] = useState("");
  const [webFetchResult, setWebFetchResult] = useState<ToolRunResult | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [maxTokens, setMaxTokens] = useState(DEFAULT_MAX_TOKENS);
  const [busy, setBusy] = useState(false);
  const [chatBusy, setChatBusy] = useState(false);
  const [chatTicker, setChatTicker] = useState(Date.now());
  const [chatFiles, setChatFiles] = useState<File[]>([]);
  const [thinkingEnabled, setThinkingEnabled] = useState(true);
  const [dispatcherBusy, setDispatcherBusy] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [activeOperation, setActiveOperation] = useState<ActiveOperation | null>(null);
  const [storageReady, setStorageReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const voiceBaseInputRef = useRef("");
  const chatFileInputRef = useRef<HTMLInputElement | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const vitalsRequestInFlightRef = useRef(false);
  const telemetryRequestInFlightRef = useRef(false);

  const activeChatWindow = useMemo(
    () => chatWindows.find((window) => window.id === activeChatWindowId) ?? chatWindows[0],
    [activeChatWindowId, chatWindows]
  );
  const input = activeChatWindow?.input ?? "";
  const conversationId = activeChatWindow?.conversationId ?? null;
  const lines = activeChatWindow?.lines ?? bootLines();
  const latestLine = lines[lines.length - 1];

  const updateChatWindow = useCallback((id: string, updater: (window: ChatWindow) => ChatWindow) => {
    setChatWindows((current) => current.map((window) => (window.id === id ? updater(window) : window)));
  }, []);

  const updateActiveChatWindow = useCallback(
    (updater: (window: ChatWindow) => ChatWindow) => {
      setChatWindows((current) => {
        const activeId = current.some((window) => window.id === activeChatWindowId)
          ? activeChatWindowId
          : current[0]?.id;
        return current.map((window) => (window.id === activeId ? updater(window) : window));
      });
    },
    [activeChatWindowId]
  );

  const setInput = useCallback(
    (value: string | ((current: string) => string)) => {
      updateActiveChatWindow((window) => ({
        ...window,
        input: typeof value === "function" ? value(window.input) : value
      }));
    },
    [updateActiveChatWindow]
  );

  const setConversationId = useCallback(
    (value: string | null) => {
      updateActiveChatWindow((window) => ({ ...window, conversationId: value }));
    },
    [updateActiveChatWindow]
  );

  const setLines = useCallback(
    (value: ChatLine[] | ((current: ChatLine[]) => ChatLine[])) => {
      updateActiveChatWindow((window) => ({
        ...window,
        lines: typeof value === "function" ? value(window.lines) : value
      }));
    },
    [updateActiveChatWindow]
  );

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [
        statusData,
        conversationData,
        missionData,
        toolData,
        memoryData,
        memoryVaultData,
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
          api<MemoryVault>("/api/memory/vault"),
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
      setMemoryVault(memoryVaultData);
      setFiles(fileData);
      setAudit(auditData);
      setModelCatalog(modelData);
      setModelDownloads(modelData.downloads ?? []);
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

  const refreshVitals = useCallback(async () => {
    if (vitalsRequestInFlightRef.current) return;
    vitalsRequestInFlightRef.current = true;
    try {
      const [statusData, dispatcherData, downloadData] = await Promise.all([
        api<RuntimeStatus>("/api/status"),
        api<DispatcherStatus>("/api/dispatcher"),
        api<ModelDownloadJob[]>("/api/model-hub/downloads")
      ]);
      setStatus(statusData);
      setDispatcher(dispatcherData);
      setModelDownloads(downloadData);
      if (statusData.health.length) {
        setDiagnostics(statusData.health);
      }
    } catch {
      // The full refresh path owns the visible error state; vitals polling stays quiet.
    } finally {
      vitalsRequestInFlightRef.current = false;
    }
  }, []);

  const refreshLiveTelemetry = useCallback(async () => {
    if (telemetryRequestInFlightRef.current) return;
    telemetryRequestInFlightRef.current = true;
    try {
      setTelemetry(await api<TelemetrySnapshot>("/api/telemetry/live"));
    } catch {
      // Keep the last known snapshot on transient GPU/driver hiccups.
    } finally {
      telemetryRequestInFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const settings = readStoredChatSettings();
    setActiveTab(settings.activeTab ?? "chat");
    setChatSideTab(normalizeChatSideTab(settings.chatSideTab));
    setChatHeight(clampChatHeight(settings.chatHeight ?? DEFAULT_CHAT_HEIGHT));
    setMaxTokens(storedMaxTokens(settings.maxTokens));
    setThinkingEnabled(settings.thinkingEnabled ?? true);
    const storedWindows = readStoredChatWindows();
    setChatWindows(storedWindows.windows);
    setActiveChatWindowId(storedWindows.activeId);
    setStorageReady(true);
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => {
      void refreshVitals();
    }, VITAL_STATUS_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [refreshVitals]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = () => {
      if (stopped) return;
      void refreshLiveTelemetry();
      const delay =
        document.visibilityState === "visible"
          ? LIVE_TELEMETRY_INTERVAL_MS
          : BACKGROUND_TELEMETRY_INTERVAL_MS;
      timer = window.setTimeout(poll, delay);
    };
    poll();
    return () => {
      stopped = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [refreshLiveTelemetry]);

  useEffect(() => {
    if (!storageReady) return;
    localStorage.setItem(
      CHAT_SETTINGS_KEY,
      JSON.stringify({ activeTab, chatSideTab, chatHeight, maxTokens, thinkingEnabled })
    );
  }, [activeTab, chatSideTab, chatHeight, maxTokens, thinkingEnabled, storageReady]);

  useEffect(() => {
    const node = transcriptRef.current;
    if (!node) return;
    const frame = window.requestAnimationFrame(() => {
      node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeChatWindowId, lines.length, latestLine?.content]);

  useEffect(() => {
    if (!chatBusy) return;
    setChatTicker(Date.now());
    const timer = window.setInterval(() => setChatTicker(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [chatBusy]);

  useEffect(() => {
    if (!storageReady) return;
    const compactWindows = chatWindows.slice(0, 8).map((window) => ({
      ...window,
      lines: window.lines.slice(-120)
    }));
    localStorage.setItem(
      CHAT_WINDOWS_KEY,
      JSON.stringify({ activeId: activeChatWindowId, windows: compactWindows })
    );
  }, [activeChatWindowId, chatWindows, storageReady]);

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
  const dispatcherRuntime = dispatcher?.runtime ?? dispatcher?.desired_runtime;
  const dispatcherModelId = dispatcherRuntime?.model_id || modelCatalog?.active_model.id || status?.settings.llm.model || "disabled";
  const dispatcherMode = dispatcherRuntime
    ? dispatcherRuntime.enforce_eager
      ? "eager"
      : "cuda graph"
    : "unknown";
  const primaryGpu = telemetry?.gpu.gpus?.[0];
  const gpuUtilization = Math.round(primaryGpu?.utilization_gpu ?? 0);
  const vramUsage = ratioPercent(primaryGpu?.memory_used_ratio);
  const llmState = llmReady ? "ready" : dispatcher?.port_open ? "loading" : "offline";
  const dispatcherPhaseLabel = runtimeStateLabel(dispatcherPhase);
  const dispatcherModeLabel = dispatcherRuntime
    ? dispatcherRuntime.enforce_eager
      ? "eager"
      : "CUDA graph"
    : "нет данных";
  const llmStatusText = llmReady
    ? "LLM готова"
    : dispatcher?.port_open
      ? dispatcherPhaseLabel
      : "LLM выключена";
  const latestUserPrompt = useMemo(
    () => [...lines].reverse().find((line) => line.role === "user")?.content ?? "",
    [lines]
  );
  const modelActivity = useMemo(() => {
    if (chatBusy) {
      return {
        label: "Отвечает",
        detail: compactText(latestUserPrompt || "формирует ответ в текущем диалоге"),
        tone: "warn" as const
      };
    }
    if (dispatcherBusy) {
      return {
        label: "Система",
        detail: activeOperation?.detail ?? "управление LLM dispatcher",
        tone: "warn" as const
      };
    }
    if (activeOperation) {
      return {
        label: activeOperation.title,
        detail: compactText(activeOperation.detail ?? "выполняет системную операцию"),
        tone: "warn" as const
      };
    }
    if (!status?.settings.llm.enabled) {
      return {
        label: "Отключена",
        detail: compactText(dispatcherModelId),
        tone: "warn" as const
      };
    }
    if (!dispatcher?.port_open) {
      return {
        label: "Не запущена",
        detail: dispatcherPhaseLabel,
        tone: "warn" as const
      };
    }
    if (!llmReady) {
      return {
        label: "Прогревается",
        detail: dispatcherPhaseLabel,
        tone: "warn" as const
      };
    }
    return {
      label: "Готова",
      detail: "ждёт новую задачу",
      tone: "ok" as const
    };
  }, [
    activeOperation,
    chatBusy,
    dispatcher?.port_open,
    dispatcherBusy,
    dispatcherModelId,
    dispatcherPhaseLabel,
    latestUserPrompt,
    llmReady,
    status?.settings.llm.enabled
  ]);
  const activeTabTitle: Record<CommandTab, string> = {
    chat: "Диалог",
    runtime: "Система",
    models: "Модели",
    memory: "Память",
    files: "Файлы",
    diagnostics: "Диагностика",
    resources: "Ресурсы",
    audit: "Аудит"
  };
  const chatSideTabTitle: Record<ChatSideTab, string> = {
    status: "Состояние",
    missions: "Миссии",
    approvals: "Допуски",
    briefing: "Сводка",
    history: "История"
  };
  const chatSideTabs: Array<{ id: ChatSideTab; label: string; badge: string }> = [
    { id: "status", label: "Состояние", badge: llmReady ? "OK" : "..." },
    { id: "missions", label: "Миссии", badge: `${missions.length}` },
    { id: "approvals", label: "Допуски", badge: `${activeApprovals.length}` },
    { id: "briefing", label: "Сводка", badge: autonomyPolicy?.mode ?? "..." },
    { id: "history", label: "История", badge: `${conversations.length}` }
  ];

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
      setError("Голосовой ввод остановился с ошибкой");
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
      setError(err instanceof Error ? err.message : "Не удалось запустить голосовой ввод");
    }
  }

  function stopVoiceInput() {
    recognitionRef.current?.stop();
    recognitionRef.current = null;
    setVoiceState("idle");
    setVoiceInterim("");
  }

  function addChatFiles(fileList: FileList | null) {
    const nextFiles = Array.from(fileList ?? []);
    if (!nextFiles.length) return;
    setChatFiles((current) => {
      const merged = [...current];
      for (const file of nextFiles) {
        const key = `${file.name}:${file.size}:${file.lastModified}`;
        const exists = merged.some((item) => `${item.name}:${item.size}:${item.lastModified}` === key);
        if (!exists) merged.push(file);
      }
      return merged.slice(0, 8);
    });
  }

  function removeChatFile(index: number) {
    setChatFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));
  }

  async function uploadChatFiles(filesToUpload: File[]): Promise<ChatAttachment[]> {
    const uploaded: ChatAttachment[] = [];
    for (const file of filesToUpload) {
      const formData = new FormData();
      formData.append("file", file);
      const response = await fetch(`${apiUrl()}/api/files/upload`, {
        method: "POST",
        body: formData
      });
      if (!response.ok) {
        throw new Error(`${file.name}: ${response.status} ${response.statusText}`);
      }
      const result = (await response.json()) as { file: FileItem; chunks_indexed: number };
      uploaded.push(fileItemToAttachment(result.file));
      setFiles((current) => {
        const withoutDuplicate = current.filter((item) => item.id !== result.file.id);
        return [result.file, ...withoutDuplicate].slice(0, 8);
      });
    }
    return uploaded;
  }

  function downloadChatMessage(line: ChatLine, index: number) {
    if (typeof window === "undefined") return;
    const content = line.role === "assistant" ? cleanAssistantText(line.content) : line.content;
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = window.URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `jarvis-response-${index + 1}.md`;
    anchor.click();
    window.URL.revokeObjectURL(url);
  }

  async function submitChat() {
    const typedMessage = input.trim();
    const filesToSend = chatFiles;
    const chatWindowId = activeChatWindow?.id;
    if ((!typedMessage && filesToSend.length === 0) || chatBusy || !chatWindowId) return;
    const previousConversationId = activeChatWindow.conversationId;
    let assistantId: string | null = null;
    let assistantStartedAt = Date.now();
    let receivedDelta = false;
    setChatBusy(true);
    let attachments: ChatAttachment[] = [];
    const message = typedMessage || "Проанализируй вложенные файлы.";
    const userId = randomId("msg");
    assistantId = randomId("msg");
    updateChatWindow(chatWindowId, (window) => ({
      ...window,
      title: window.title === "Новый чат" || window.title === "Чат"
        ? titleFromMessage(message)
        : window.title,
      input: "",
      lines: [
        ...window.lines,
        { id: userId, role: "user", content: message, attachments },
        {
          id: assistantId,
          role: "assistant",
          content: "",
          pending: true,
          startedAt: assistantStartedAt
        }
      ]
    }));
    try {
      if (filesToSend.length) {
        setActiveOperation({
          title: "Загрузка вложений",
          detail: filesToSend.map((file) => file.name).join(", ")
        });
        attachments = await uploadChatFiles(filesToSend);
        setChatFiles([]);
        updateChatWindow(chatWindowId, (window) => ({
          ...window,
          lines: window.lines.map((line) => (line.id === userId ? { ...line, attachments } : line))
        }));
        setActiveOperation(null);
      }
      await streamApi(
        "/api/chat/stream",
        {
          message,
          conversation_id: previousConversationId,
          max_tokens: maxTokens,
          mode: "auto",
          attachments,
          thinking_enabled: thinkingEnabled
        },
        (item) => {
          if (item.type === "meta" && item.conversation_id) {
            updateChatWindow(chatWindowId, (window) => ({
              ...window,
              conversationId: item.conversation_id ?? window.conversationId
            }));
          }
          if (item.type === "delta" && item.content) {
            receivedDelta = true;
            updateChatWindow(chatWindowId, (window) => ({
              ...window,
              lines: window.lines.map((line) =>
                line.id === assistantId ? { ...line, content: `${line.content}${item.content}` } : line
              )
            }));
          }
          if (item.type === "done") {
            const durationMs = coerceDurationMs(item.duration_ms);
            if (item.conversation_id) {
              updateChatWindow(chatWindowId, (window) => ({
                ...window,
                conversationId: item.conversation_id ?? window.conversationId
              }));
            }
            updateChatWindow(chatWindowId, (window) => ({
              ...window,
              lines: window.lines.map((line) =>
                line.id === assistantId
                  ? {
                      ...line,
                      id: item.message_id ?? line.id,
                      content: !receivedDelta && item.answer ? item.answer : line.content,
                      durationMs,
                      pending: false,
                      startedAt: null
                    }
                  : line
              )
            }));
          }
          if (item.type === "error") {
            const durationMs = Math.max(0, Date.now() - assistantStartedAt);
            updateChatWindow(chatWindowId, (window) => ({
              ...window,
              lines: window.lines.map((line) =>
                line.id === assistantId
                  ? {
                      ...line,
                      content: item.error ?? "Ошибка потока ответа",
                      durationMs,
                      pending: false,
                      startedAt: null
                    }
                  : line
              )
            }));
          }
        }
      );
      await refresh();
    } catch (err) {
      updateChatWindow(chatWindowId, (window) => ({
        ...window,
        lines: window.lines.map((line) =>
          line.id === assistantId
            ? {
                ...line,
                content: err instanceof Error ? `Ошибка backend: ${err.message}` : "Ошибка backend",
                durationMs: Math.max(0, Date.now() - assistantStartedAt),
                pending: false,
                startedAt: null
              }
            : line
        )
      }));
    } finally {
      setChatBusy(false);
      setActiveOperation(null);
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

  function newChatWindow() {
    const window = createChatWindow();
    setChatWindows((current) => [window, ...current].slice(0, 8));
    setActiveChatWindowId(window.id);
  }

  function closeChatWindow(id: string) {
    setChatWindows((current) => {
      if (current.length <= 1) {
        const replacement = createChatWindow();
        setActiveChatWindowId(replacement.id);
        return [replacement];
      }
      const next = current.filter((window) => window.id !== id);
      if (id === activeChatWindowId) {
        setActiveChatWindowId(next[0]?.id ?? current[0].id);
      }
      return next;
    });
  }

  async function clearCurrentChat() {
    if (!activeChatWindow) return;
    const clearedWindow = {
      ...activeChatWindow,
      title: "Новый чат",
      conversationId: null,
      input: "",
      lines: bootLines()
    };
    const idToDelete = activeChatWindow.conversationId;
    updateChatWindow(activeChatWindow.id, () => clearedWindow);
    if (!idToDelete) return;
    try {
      await api<{ ok: boolean }>(`/api/conversations/${idToDelete}`, { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось очистить диалог");
    }
  }

  function beginChatResize(event: ReactPointerEvent<HTMLDivElement>) {
    const startY = event.clientY;
    const startHeight = chatHeight;
    const onMove = (moveEvent: PointerEvent) => {
      setChatHeight(clampChatHeight(startHeight + moveEvent.clientY - startY));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
  }

  async function loadConversation(id: string) {
    const conversation = conversations.find((item) => item.id === id);
    setActiveOperation({
      title: "Загрузка диалога",
      detail: conversation?.title ?? id
    });
    setBusy(true);
    try {
      const messages = await api<MessageItem[]>(`/api/conversations/${id}/messages?limit=120`);
      setConversationId(id);
      updateActiveChatWindow((window) => ({
        ...window,
        title: conversation?.title ? conversation.title.slice(0, 42) : window.title
      }));
      setLines(
        messages
          .filter((message) => ["user", "assistant", "system"].includes(message.role))
          .map((message) => ({
            id: message.id,
            role: message.role,
            content: message.content,
            attachments: attachmentsFromMetadata(message.metadata),
            durationMs: coerceDurationMs(message.metadata?.duration_ms),
            pending: false,
            startedAt: null
          }))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить диалог");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function runDiagnostics() {
    setActiveOperation({
      title: "Диагностика",
      detail: "полная проверка runtime, LLM и системных мостов"
    });
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
      setActiveOperation(null);
    }
  }

  async function runSelfHeal() {
    setActiveOperation({
      title: "Самовосстановление",
      detail: "поиск и исправление проблем окружения"
    });
    setBusy(true);
    try {
      const report = await api<SelfHealReport>("/api/self-heal", {
        method: "POST",
        body: "{}"
      });
      setSelfHealReport(report);
      setLines((current) => [
        ...current,
        { role: "system", content: `Самовосстановление: ${report.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Самовосстановление не завершилось");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function runBenchmark() {
    setActiveOperation({
      title: "Бенчмарк",
      detail: "замер скорости backend, dispatcher и LLM"
    });
    setBusy(true);
    try {
      const report = await api<BenchmarkReport>("/api/benchmark", {
        method: "POST",
        body: "{}"
      });
      setBenchmarkReport(report);
      setLines((current) => [
        ...current,
        { role: "system", content: `Бенчмарк: ${report.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Бенчмарк не завершился");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function updatePolicyMode(mode: AutonomyPolicy["mode"]) {
    setActiveOperation({
      title: "Политика автономии",
      detail: `переключение режима: ${mode}`
    });
    setBusy(true);
    try {
      const updated = await api<AutonomyPolicy>("/api/autonomy/policy", {
        method: "PATCH",
        body: JSON.stringify({ mode })
      });
      setAutonomyPolicy(updated);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось обновить политику автономии");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function savePreferences(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setActiveOperation({
      title: "Настройки",
      detail: "сохранение предпочтений оператора"
    });
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
      setError(err instanceof Error ? err.message : "Не удалось сохранить настройки");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function updateBrowserPolicyMode(mode: BrowserPolicy["mode"]) {
    setActiveOperation({
      title: "Политика браузера",
      detail: `переключение режима: ${mode}`
    });
    setBusy(true);
    try {
      const updated = await api<BrowserPolicy>("/api/browser/policy", {
        method: "PATCH",
        body: JSON.stringify({ mode })
      });
      setBrowserPolicy(updated);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось обновить политику браузера");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function updateDockerTail(maxLogTail: number) {
    setActiveOperation({
      title: "Docker",
      detail: `обновление хвоста логов: ${maxLogTail}`
    });
    setBusy(true);
    try {
      const updated = await api<DockerPolicy>("/api/docker/policy", {
        method: "PATCH",
        body: JSON.stringify({ max_log_tail: maxLogTail })
      });
      setDockerPolicy(updated);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось обновить политику Docker");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function createAutonomyJob(kind: AutonomyJob["kind"]) {
    setActiveOperation({
      title: "Автономная задача",
      detail: `создание задачи: ${kind}`
    });
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
      setError(err instanceof Error ? err.message : "Не удалось создать автономную задачу");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function runAutonomyJob(jobId: string) {
    const jobTitle = autonomyJobs.find((job) => job.id === jobId)?.title ?? jobId;
    setActiveOperation({
      title: "Автономная задача",
      detail: jobTitle
    });
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
        { role: "system", content: `Автономная задача: ${result.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить автономную задачу");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function runRoutine(routineId: string) {
    const routineTitle = routines.find((routine) => routine.id === routineId)?.title ?? routineId;
    setActiveOperation({
      title: "Сценарий",
      detail: routineTitle
    });
    setBusy(true);
    try {
      const result = await api<RoutineRun>(`/api/routines/${routineId}/run`, {
        method: "POST",
        body: "{}"
      });
      setRoutineRun(result);
      setLines((current) => [
        ...current,
        { role: "system", content: `Сценарий: ${result.summary}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось выполнить сценарий");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function ingestDirectory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const path = directoryDraft.trim();
    if (!path || busy) return;
    setActiveOperation({
      title: "Индексация папки",
      detail: path
    });
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
          content: `Индексация папки: ${result.files_indexed}/${result.files_seen} файлов`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось проиндексировать папку");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function runDispatcherAction(action: "start" | "stop" | "logs") {
    if (dispatcherBusy) return;
    const actionLabel = action === "start" ? "запуск" : action === "stop" ? "остановка" : "чтение логов";
    setActiveOperation({
      title: "LLM runtime",
      detail: `dispatcher: ${actionLabel}`
    });
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
      setError(err instanceof Error ? err.message : "Действие dispatcher не выполнено");
    } finally {
      setDispatcherBusy(false);
      setActiveOperation(null);
    }
  }

  async function searchModelHub(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const query = modelSearchQuery.trim();
    if (!query || modelSearchBusy) return;
    setActiveOperation({
      title: "Браузер моделей",
      detail: `поиск: ${query}`
    });
    setModelSearchBusy(true);
    try {
      const params = new URLSearchParams({
        query,
        limit: "12",
        context_tokens: String(modelContextTokens)
      });
      setModelSearchResult(await api<ModelSearchResult>(`/api/model-hub/search?${params}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Поиск моделей не выполнен");
    } finally {
      setModelSearchBusy(false);
      setActiveOperation(null);
    }
  }

  async function downloadRemoteModel(repoId: string) {
    setActiveOperation({
      title: "Скачивание модели",
      detail: repoId
    });
    setBusy(true);
    try {
      const job = await api<ModelDownloadJob>("/api/model-hub/download", {
        method: "POST",
        body: JSON.stringify({ repo_id: repoId, revision: "main", workers: modelWorkers })
      });
      setModelDownloads((current) => [job, ...current].slice(0, 20));
      setLines((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: `Скачивание модели: ${repoId}, потоков: ${job.workers ?? modelWorkers}`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Скачивание модели не запущено");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function activateAndLoadModel(modelId: string) {
    if (dispatcherBusy) return;
    setActiveOperation({
      title: "Загрузка модели",
      detail: modelId
    });
    setDispatcherBusy(true);
    try {
      const activated = await api<{ ok: boolean; summary: string }>("/api/models/activate", {
        method: "POST",
        body: JSON.stringify({ model_id: modelId })
      });
      let latestStatus: DispatcherStatus | null = null;
      if (dispatcher?.container_status?.exists || dispatcher?.port_open) {
        const stopped = await api<DispatcherAction>("/api/dispatcher/stop", {
          method: "POST",
          body: "{}"
        });
        latestStatus = stopped.status;
      }
      const started = await api<DispatcherAction>("/api/dispatcher/start", {
        method: "POST",
        body: "{}"
      });
      latestStatus = started.status;
      setDispatcher(latestStatus);
      setLines((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: `${activated.summary}\n${started.summary}`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Модель не загружена в dispatcher");
    } finally {
      setDispatcherBusy(false);
      setActiveOperation(null);
    }
  }

  async function deleteLocalModel(modelId: string) {
    setActiveOperation({
      title: "Удаление модели",
      detail: modelId
    });
    setBusy(true);
    try {
      const result = await api<{ summary: string }>(
        `/api/models/local/${encodeURIComponent(modelId)}`,
        { method: "DELETE" }
      );
      setLines((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", content: result.summary }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Модель не удалена");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function cleanupRuntime(aggressive = false) {
    setActiveOperation({
      title: "Очистка runtime",
      detail: aggressive ? "агрессивная Docker cleanup" : "безопасная Docker cleanup"
    });
    setCleanupBusy(true);
    try {
      const result = await api<{ ok: boolean; summary: string; steps: { summary: string }[] }>(
        "/api/cleanup",
        { method: "POST", body: JSON.stringify({ aggressive }) }
      );
      setLines((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: `${result.summary}\n${result.steps.map((step) => step.summary).join("\n")}`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Очистка не выполнена");
    } finally {
      setCleanupBusy(false);
      setActiveOperation(null);
    }
  }

  async function executeNextMissionStep(missionId: string) {
    const mission = missions.find((item) => item.id === missionId);
    const nextTask = mission?.tasks.find((task) => task.status !== "done");
    setActiveOperation({
      title: "Шаг миссии",
      detail: nextTask?.title ?? mission?.title ?? missionId
    });
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
      setError(err instanceof Error ? err.message : "Шаг миссии не выполнен");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function updateTaskStatus(missionId: string, taskId: string, statusValue: string) {
    const task = missions
      .find((mission) => mission.id === missionId)
      ?.tasks.find((item) => item.id === taskId);
    setActiveOperation({
      title: "Статус задачи",
      detail: `${task?.title ?? taskId}: ${statusValue}`
    });
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
      setError(err instanceof Error ? err.message : "Не удалось обновить задачу");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function saveMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = memoryDraft.trim();
    if (!content || busy) return;
    setActiveOperation({
      title: "Память",
      detail: content
    });
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
      setError(err instanceof Error ? err.message : "Не удалось сохранить память");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function uploadSelectedFile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFile || busy) return;
    const form = event.currentTarget;
    const formData = new FormData();
    formData.append("file", selectedFile);
    setActiveOperation({
      title: "Индексация файла",
      detail: selectedFile.name
    });
    setBusy(true);
    try {
      const response = await fetch(`${apiUrl()}/api/files/upload`, {
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
          content: `Файл загружен: ${result.file.name} (${result.chunks_indexed} фрагментов)`
        }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить файл");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function searchFileChunks(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = fileQuery.trim();
    if (!query) {
      setFileHits([]);
      return;
    }
    setActiveOperation({
      title: "Поиск по файлам",
      detail: query
    });
    setBusy(true);
    try {
      const hits = await api<FileChunkHit[]>(
        `/api/files/search?q=${encodeURIComponent(query)}&limit=6`
      );
      setFileHits(hits);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Поиск по файлам не выполнен");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function updateApprovalStatus(approvalId: string, statusValue: string) {
    const approval = approvals.find((item) => item.id === approvalId);
    setActiveOperation({
      title: "Согласование",
      detail: `${approval?.title ?? approvalId}: ${statusValue}`
    });
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
      setError(err instanceof Error ? err.message : "Не удалось обновить допуск");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function executeApproval(approvalId: string) {
    const approval = approvals.find((item) => item.id === approvalId);
    setActiveOperation({
      title: "Выполнение допуска",
      detail: approval?.title ?? approvalId
    });
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
      setError(err instanceof Error ? err.message : "Не удалось выполнить допуск");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function runLearningTick() {
    setActiveOperation({
      title: "Самообучение",
      detail: "сохранение новых уроков и наблюдений"
    });
    setBusy(true);
    try {
      const result = await api<{ lesson_count: number }>("/api/learning/tick", {
        method: "POST",
        body: "{}"
      });
      setLines((current) => [
        ...current,
        { role: "system", content: `Шаг обучения: сохранено уроков ${result.lesson_count}` }
      ]);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Шаг обучения не выполнен");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function requestHostCommandApproval(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const command = hostCommandDraft.trim();
    if (!command || busy) return;
    setActiveOperation({
      title: "Запрос допуска",
      detail: command
    });
    setBusy(true);
    try {
      const approval = await api<ApprovalItem>("/api/approvals", {
        method: "POST",
        body: JSON.stringify({
          title: "Команда хоста",
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
      setError(err instanceof Error ? err.message : "Не удалось запросить допуск для команды");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  async function runWebFetch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = webUrlDraft.trim();
    if (!url || busy) return;
    setActiveOperation({
      title: "Веб-запрос",
      detail: url
    });
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
      setError(err instanceof Error ? err.message : "Веб-запрос не выполнен");
    } finally {
      setBusy(false);
      setActiveOperation(null);
    }
  }

  const webFetchText = typeof webFetchResult?.data.text === "string" ? webFetchResult.data.text : "";
  const webFetchStatus =
    typeof webFetchResult?.data.status_code === "number" ? webFetchResult.data.status_code : null;
  const vitalsPanel = (
    <div className="vitalsPanel">
      <article className={`vitalHero ${llmReady ? "ok" : "warn"}`}>
        <Sparkles size={18} />
        <div>
          <strong>{llmReady ? "LLM готова" : "LLM прогревается"}</strong>
          <p>{llmCheck?.message ?? dispatcherPhaseLabel}</p>
        </div>
        <span>{dispatcher?.port_open ? "8001" : "выкл"}</span>
      </article>
      <article className={`activitySummary ${modelActivity.tone}`}>
        <Activity size={16} />
        <div>
          <strong>{modelActivity.label}</strong>
          <p>{modelActivity.detail}</p>
        </div>
      </article>
      <div className="vitalGrid">
        <div>
          <span>Backend</span>
          <strong>{status ? "онлайн" : "офлайн"}</strong>
        </div>
        <div>
          <span>Мост Windows</span>
          <strong>{hostBridge?.port_open ? "онлайн" : "офлайн"}</strong>
        </div>
        <div>
          <span>Dispatcher</span>
          <strong>{dispatcherPhaseLabel}</strong>
        </div>
        <div>
          <span>GPU</span>
          <strong>{telemetry?.gpu.available ? "онлайн" : "офлайн"}</strong>
        </div>
      </div>
    </div>
  );
  const missionsPanel = (
    <div className="missionList">
      {missions.length === 0 ? (
        <div className="emptyState">Нет активных планов миссий</div>
      ) : (
        missions.slice(0, 5).map((mission) => (
          <article className="missionItem" key={mission.id}>
            <div className="missionTitle">
              <CheckCircle2 size={17} />
              <div>
                <h3>{mission.title}</h3>
                <div className="progressTrack" aria-label="Прогресс миссии">
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
                      title="Готово"
                      aria-label="Готово"
                      disabled={busy || task.status === "done"}
                      onClick={() => updateTaskStatus(mission.id, task.id, "done")}
                    >
                      <ClipboardCheck size={14} />
                    </button>
                    <button
                      type="button"
                      title="Заблокировано"
                      aria-label="Заблокировано"
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
  );
  const approvalsPanel = (
    <div className="approvalList">
      {activeApprovals.length === 0 ? (
        <div className="emptyState compact">Нет ожидающих допусков</div>
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
                title="Одобрить"
                aria-label="Одобрить"
                disabled={busy || approval.status !== "pending"}
                onClick={() => updateApprovalStatus(approval.id, "approved")}
              >
                <CheckCircle2 size={14} />
              </button>
              <button
                type="button"
                title="Отклонить"
                aria-label="Отклонить"
                disabled={busy || approval.status !== "pending"}
                onClick={() => updateApprovalStatus(approval.id, "rejected")}
              >
                <ShieldAlert size={14} />
              </button>
              <button
                type="button"
                title="Выполнить"
                aria-label="Выполнить"
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
  );
  const briefingPanel = (
    <div className="briefingPanel">
      <div className="briefingHero">
        <Brain size={16} />
        <strong>{briefing?.headline ?? "Снимок runtime готовится"}</strong>
        <span>{briefing?.operator_name ?? preferences?.operator_name ?? "оператор"}</span>
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
  );
  const historyPanel = (
    <div className="conversationList">
      {conversations.length === 0 ? (
        <div className="emptyState compact">Нет сохранённых диалогов</div>
      ) : (
        conversations.slice(0, 8).map((conversation) => (
          <button
            className={`conversationRow ${conversation.id === conversationId ? "active" : ""}`}
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
  );
  const healthPanel = (
    <>
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
          <span>Самовосстановление</span>
        </button>
        {selfHealReport && (
          <article className={`selfHealReport ${selfHealReport.ok ? "ok" : "warn"}`}>
            <div>
              <strong>{selfHealReport.summary}</strong>
              <span>{selfHealReport.actions.length} действий</span>
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
  );

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
          label="Система"
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
        <IconButton active={activeTab === "audit"} label="Аудит" tab="audit" onSelect={setActiveTab}>
          <History size={20} />
        </IconButton>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">JARVIS GPT</p>
            <h1>Центр управления</h1>
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

        <section className="realtimeStrip" aria-label="Живые индикаторы runtime">
          <article className={`livePill ${llmState}`}>
            <Sparkles size={17} />
            <div>
              <span>LLM</span>
              <strong>{llmStatusText}</strong>
            </div>
            <small>{dispatcher?.port_open ? dispatcherModeLabel : "выкл"}</small>
          </article>
          <article className={`livePill ${telemetry?.gpu.available ? "ready" : "offline"}`}>
            <Zap size={17} />
            <div>
              <span>GPU</span>
              <strong>{telemetry?.gpu.available ? `${gpuUtilization}%` : "офлайн"}</strong>
            </div>
            <div className="liveMeter" aria-label="Загрузка GPU">
              <span style={{ width: `${telemetry?.gpu.available ? gpuUtilization : 0}%` }} />
            </div>
          </article>
          <article className={`livePill ${telemetry?.gpu.available ? "ready" : "offline"}`}>
            <Gauge size={17} />
            <div>
              <span>VRAM</span>
              <strong>{telemetry?.gpu.available ? `${vramUsage}%` : "офлайн"}</strong>
            </div>
            <div className="liveMeter" aria-label="Использование VRAM">
              <span style={{ width: `${telemetry?.gpu.available ? vramUsage : 0}%` }} />
            </div>
          </article>
          <article className={`livePill ${dispatcher?.port_open ? "ready" : "offline"}`}>
            <Server size={17} />
            <div>
              <span>Dispatcher</span>
              <strong>{dispatcherPhaseLabel}</strong>
            </div>
            <small>{dispatcher?.container_status?.exists ? "docker" : "нет"}</small>
          </article>
        </section>

        {error && (
          <div className="notice" role="status">
            <ShieldAlert size={18} />
            <span>{error}</span>
          </div>
        )}

        <section className="statusGrid" id="runtime" aria-label="Сводка системы">
          <StatusTile
            icon={<Server size={19} />}
            label="Профиль"
            value={status?.settings.profile.name ?? "офлайн"}
            tone={status ? "ok" : "warn"}
          />
          <StatusTile
            icon={<Sparkles size={19} />}
            label="LLM"
            value={dispatcherModelId}
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
            icon={<Activity size={19} />}
            label="Задача"
            value={modelActivity.label}
            detail={modelActivity.detail}
            tone={modelActivity.tone}
          />
        </section>

        <section className="mainGrid">
          <section
            className="chatPanel"
            id="dialog"
            aria-label="Диалог"
            style={{ "--chat-target-height": `${chatHeight}px` } as CSSProperties}
          >
            <div className="panelHeader chatHeader">
              <h2>Диалог</h2>
              <div className="chatHeaderActions">
                <span>{conversationId ? "активен" : "новый"}</span>
                <button type="button" title="Новое окно" aria-label="Новое окно" onClick={newChatWindow}>
                  <Plus size={15} />
                </button>
                <button
                  type="button"
                  title="Очистить текущий чат"
                  aria-label="Очистить текущий чат"
                  onClick={clearCurrentChat}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
            <div className="chatWindowBar" role="tablist" aria-label="Окна чата">
              {chatWindows.map((window, index) => (
                <div
                  className={`chatWindowTab ${window.id === activeChatWindowId ? "active" : ""}`}
                  key={window.id}
                  onClick={() => setActiveChatWindowId(window.id)}
                  role="tab"
                  tabIndex={0}
                  aria-selected={window.id === activeChatWindowId}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setActiveChatWindowId(window.id);
                    }
                  }}
                >
                  <MessageSquare size={13} />
                  <span>{window.title || `Чат ${index + 1}`}</span>
                  {chatWindows.length > 1 && (
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        closeChatWindow(window.id);
                      }}
                      aria-label="Закрыть окно"
                    >
                      ×
                    </button>
                  )}
                </div>
              ))}
            </div>
            <div className="transcript" ref={transcriptRef}>
              {lines.map((line, index) => {
                const durationLabel = assistantDuration(line, chatTicker);
                return (
                  <article className={`bubble ${line.role}`} key={line.id ?? `${line.role}-${index}`}>
                    <div className="bubbleMeta">
                      <span>{line.role}</span>
                      <div className="bubbleMetaRight">
                        {durationLabel && (
                          <time className="bubbleTimer" dateTime={`PT${Math.max(0, coerceDurationMs(line.durationMs) ?? 0) / 1000}S`}>
                            {durationLabel}
                          </time>
                        )}
                        {line.role === "assistant" && line.content.trim() && !line.pending && (
                          <button
                            className="bubbleAction"
                            type="button"
                            title="Скачать ответ как Markdown"
                            aria-label="Скачать ответ как Markdown"
                            onClick={() => downloadChatMessage(line, index)}
                          >
                            <Download size={13} />
                          </button>
                        )}
                      </div>
                    </div>
                    {renderRichMessage(line.content, line.role)}
                    {line.attachments && line.attachments.length > 0 && (
                      <div className="bubbleAttachments">
                        {line.attachments.map((attachment) => (
                          <a href={attachmentHref(attachment)} key={attachment.id} rel="noreferrer" target="_blank">
                            <FileText size={13} />
                            <span>{attachment.name}</span>
                            {typeof attachment.size === "number" && <small>{formatBytes(attachment.size)}</small>}
                          </a>
                        ))}
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
            <form className="composer" onSubmit={sendChat}>
              <div className="composerMain">
                {chatFiles.length > 0 && (
                  <div className="composerAttachments">
                    {chatFiles.map((file, index) => (
                      <span className="composerAttachment" key={`${file.name}-${file.size}-${file.lastModified}`}>
                        <FileText size={13} />
                        <span>{file.name}</span>
                        <small>{formatBytes(file.size)}</small>
                        <button
                          type="button"
                          title="Убрать файл"
                          aria-label="Убрать файл"
                          onClick={() => removeChatFile(index)}
                        >
                          <X size={12} />
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              <textarea
                aria-label="Сообщение"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={handleComposerKeyDown}
                placeholder="JARVIS, оформи это как mission plan..."
                rows={3}
              />
              </div>
              <div className="composerSide">
                <input
                  ref={chatFileInputRef}
                  className="srOnly"
                  type="file"
                  multiple
                  onChange={(event) => {
                    addChatFiles(event.target.files);
                    event.currentTarget.value = "";
                  }}
                />
                <button
                  className="attachButton"
                  type="button"
                  title="Прикрепить файл"
                  aria-label="Прикрепить файл"
                  onClick={() => chatFileInputRef.current?.click()}
                >
                  <Paperclip size={16} />
                </button>
                <button
                  className={`thinkingButton ${thinkingEnabled ? "active" : "off"}`}
                  type="button"
                  title={thinkingEnabled ? "Мышление модели включено" : "Мышление модели выключено"}
                  aria-label={thinkingEnabled ? "Выключить мышление модели" : "Включить мышление модели"}
                  aria-pressed={thinkingEnabled}
                  onClick={() => setThinkingEnabled((value) => !value)}
                >
                  <Brain size={16} />
                </button>
                <input
                  aria-label="Максимум токенов"
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
                  title={voiceState === "listening" ? "Остановить голосовой ввод" : "Голосовой ввод"}
                  type="button"
                  aria-label={voiceState === "listening" ? "Остановить голосовой ввод" : "Голосовой ввод"}
                >
                  {voiceState === "listening" ? <MicOff size={16} /> : <Mic size={16} />}
                </button>
                <span className="srOnly" aria-live="polite">
                  {voiceState === "listening" ? voiceInterim || "Слушаю" : ""}
                </span>
                <button className="sendButton" type="submit" disabled={chatBusy || (!input.trim() && chatFiles.length === 0)}>
                  {chatBusy ? <Loader2 className="spin" size={19} /> : <Send size={19} />}
                </button>
              </div>
            </form>
            <div
              className="chatResizeHandle"
              onPointerDown={beginChatResize}
              role="separator"
              aria-orientation="horizontal"
              aria-label="Изменить высоту чата"
            >
              <GripHorizontal size={16} />
            </div>
          </section>

          <section className="opsPanel" aria-label={activeTabTitle[activeTab]}>
            {activeTab === "chat" && (
              <>
                <div className="panelHeader">
                  <h2>Панель диалога</h2>
                  <span>{chatSideTabTitle[chatSideTab]}</span>
                </div>
                <div className="sideTabs" role="tablist" aria-label="Разделы панели диалога">
                  {chatSideTabs.map((tab) => (
                    <button
                      className={chatSideTab === tab.id ? "active" : ""}
                      key={tab.id}
                      onClick={() => setChatSideTab(tab.id)}
                      role="tab"
                      type="button"
                      aria-selected={chatSideTab === tab.id}
                    >
                      <span>{tab.label}</span>
                      <strong>{tab.badge}</strong>
                    </button>
                  ))}
                </div>
                <div className="sideTabBody">
                  {chatSideTab === "status" && (
                    <>
                      {vitalsPanel}
                      <div className="panelHeader compact" id="health">
                        <h2>Диагностика</h2>
                        <span>{diagnostics.length}</span>
                      </div>
                      {healthPanel}
                    </>
                  )}
                  {chatSideTab === "missions" && missionsPanel}
                  {chatSideTab === "approvals" && approvalsPanel}
                  {chatSideTab === "briefing" && briefingPanel}
                  {chatSideTab === "history" && historyPanel}
                </div>
              </>
            )}

            {activeTab === "runtime" && (
              <>
                <div className="panelHeader">
                  <h2>Миссии</h2>
                  <span>{missions.length}</span>
                </div>
                {missionsPanel}

                <div className="panelHeader lower">
                  <h2>Допуски</h2>
                  <span>{activeApprovals.length}</span>
                </div>
                {approvalsPanel}

                <div className="panelHeader lower">
                  <h2>Сводка</h2>
                  <span>{briefing?.policy_mode ?? autonomyPolicy?.mode ?? "..."}</span>
                </div>
                {briefingPanel}
              </>
            )}

            {(activeTab === "diagnostics" || activeTab === "runtime") && (
              <>
                <div className={`panelHeader ${activeTab === "runtime" ? "lower" : ""}`} id="health">
                  <h2>Диагностика</h2>
                  <span>{diagnostics.length}</span>
                </div>
                {healthPanel}
              </>
            )}

            {(activeTab === "models" || activeTab === "runtime") && (
              <>
                <div className={`panelHeader ${activeTab === "runtime" ? "lower" : ""}`} id="models">
                  <h2>Браузер моделей</h2>
                  <span>{modelCatalog?.models.length ?? 0}</span>
                </div>
                <div className="modelBrowser">
                  {dispatcher && (
                    <div className={`dispatcherRow ${dispatcher.port_open ? "online" : ""}`}>
                      <Server size={14} />
                      <strong>{dispatcherRuntime?.model_id ?? dispatcher.model}</strong>
                      <span>{dispatcher.port_open ? dispatcherModeLabel : "офлайн"}</span>
                      <small>
                        {runtimeStateLabel(dispatcher.container_status?.status ?? "port 8001")}
                      </small>
                      <div className="dispatcherControls">
                        <button
                          type="button"
                          title="Запустить dispatcher"
                          aria-label="Запустить dispatcher"
                          disabled={dispatcherBusy || dispatcher.port_open}
                          onClick={() => runDispatcherAction("start")}
                        >
                          {dispatcherBusy ? <Loader2 className="spin" size={14} /> : <Play size={14} />}
                        </button>
                        <button
                          type="button"
                          title="Остановить dispatcher"
                          aria-label="Остановить dispatcher"
                          disabled={dispatcherBusy || !dispatcher.container_status?.exists}
                          onClick={() => runDispatcherAction("stop")}
                        >
                          <Square size={14} />
                        </button>
                        <button
                          type="button"
                          title="Логи dispatcher"
                          aria-label="Логи dispatcher"
                          disabled={dispatcherBusy || !dispatcher.container_status?.exists}
                          onClick={() => runDispatcherAction("logs")}
                        >
                          <FileText size={14} />
                        </button>
                      </div>
                    </div>
                  )}

                  <div className="modelBudget">
                    <Gauge size={15} />
                    <strong>{modelCatalog?.vram?.name ?? "GPU"}</strong>
                    <span>
                      {modelCatalog?.vram?.available
                        ? `${formatBytes(modelCatalog.vram.free_bytes ?? 0)} свободно`
                        : "VRAM недоступна"}
                    </span>
                    <small>
                      {modelCatalog?.vram?.available
                        ? `${formatBytes(modelCatalog.vram.total_bytes ?? 0)} всего`
                        : modelCatalog?.vram?.error ?? "nvidia-smi не ответил"}
                    </small>
                  </div>

                  <form className="modelSearchForm" onSubmit={searchModelHub}>
                    <Search size={15} />
                    <input
                      aria-label="Поиск модели"
                      value={modelSearchQuery}
                      onChange={(event) => setModelSearchQuery(event.target.value)}
                      placeholder="Qwen 2.5 coder 7B GGUF, Gemma NVFP4..."
                    />
                    <input
                      aria-label="Контекст"
                      min={512}
                      max={131072}
                      step={512}
                      type="number"
                      value={modelContextTokens}
                      onChange={(event) => setModelContextTokens(Number(event.target.value))}
                      title="Контекст для расчёта KV-cache"
                    />
                    <input
                      aria-label="Потоки скачивания"
                      min={1}
                      max={6}
                      type="number"
                      value={modelWorkers}
                      onChange={(event) => setModelWorkers(Number(event.target.value))}
                      title="Параллельные файловые потоки"
                    />
                    <button
                      type="submit"
                      disabled={modelSearchBusy || !modelSearchQuery.trim()}
                      title="Искать модели"
                      aria-label="Искать модели"
                    >
                      {modelSearchBusy ? <Loader2 className="spin" size={15} /> : <Search size={15} />}
                    </button>
                  </form>

                  {modelSearchResult && (
                    <div className="remoteModelList">
                      {modelSearchResult.items.map((model) => (
                        <article className={`remoteModelRow ${fitTone(model.fit)}`} key={model.id}>
                          <Database size={15} />
                          <div className="remoteModelInfo">
                            <strong>{model.id}</strong>
                            <p>{model.pipeline_tag ?? model.tags.slice(0, 3).join(", ")}</p>
                            <div className="remoteModelMeta">
                              <span className={`fitPill ${fitTone(model.fit)}`}>{fitSummary(model.fit)}</span>
                              <small>{formatBytes(model.size_bytes)} · {model.downloadable_files} файлов</small>
                            </div>
                            {model.fit.warnings.slice(0, 1).map((warning) => (
                              <small key={warning}>{warning}</small>
                            ))}
                          </div>
                          <button
                            type="button"
                            disabled={busy || model.downloadable_files === 0}
                            onClick={() => downloadRemoteModel(model.id)}
                            title="Скачать с докачкой"
                            aria-label="Скачать модель"
                          >
                            <Download size={14} />
                          </button>
                        </article>
                      ))}
                    </div>
                  )}

                  {modelDownloads.length > 0 && (
                    <div className="downloadQueue">
                      <div className="miniHeader">
                        <strong>Скачивание</strong>
                        <span>{modelDownloads.length}</span>
                      </div>
                      {modelDownloads.slice(0, 5).map((job) => (
                        <article className={`downloadRow ${job.status}`} key={job.id}>
                          <Download size={14} />
                          <div>
                            <strong>{job.repo_id}</strong>
                            <p>{job.summary}</p>
                            <div className="progressTrack">
                              <span
                                style={{
                                  width: `${progressPercent(job.downloaded_bytes, job.total_bytes)}%`
                                }}
                              />
                            </div>
                          </div>
                          <span>{job.completed_files}/{job.total_files}</span>
                          <small>{job.workers ?? 1} потока · {formatBytes(job.downloaded_bytes)}</small>
                        </article>
                      ))}
                    </div>
                  )}

                  <div className="miniHeader">
                    <strong>Локальные модели</strong>
                    <span>{modelCatalog?.root ?? "..."}</span>
                  </div>
                  <div className="localModelList">
                    {modelCatalog ? (
                      modelCatalog.models.map((model) => (
                        <article className={`localModelRow ${model.active ? "active" : ""}`} key={model.id}>
                          <Cpu size={15} />
                          <div>
                            <strong>{model.id}</strong>
                            <p>
                              {model.quantization ?? model.dtype ?? model.model_type ?? "model"} ·{" "}
                              {formatBytes(model.size_bytes)}
                            </p>
                          </div>
                          <span className={`fitPill ${fitTone(model.fit)}`}>{fitSummary(model.fit)}</span>
                          <div className="modelActions">
                            <button
                              type="button"
                              title="Загрузить в dispatcher"
                              aria-label="Загрузить модель"
                              disabled={dispatcherBusy || !model.exists}
                              onClick={() => activateAndLoadModel(model.id)}
                            >
                              {dispatcherBusy ? <Loader2 className="spin" size={14} /> : <Play size={14} />}
                            </button>
                            <button
                              type="button"
                              title="Удалить локальную модель"
                              aria-label="Удалить модель"
                              disabled={busy || model.active}
                              onClick={() => deleteLocalModel(model.id)}
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </article>
                      ))
                    ) : (
                      <div className="emptyState compact">Каталог моделей недоступен</div>
                    )}
                  </div>

                  <div className="cleanupStrip">
                    <Wrench size={15} />
                    <strong>Очистка runtime</strong>
                    <span>{dockerContainers?.containers.length ?? 0} контейнеров</span>
                    <button
                      type="button"
                      className="iconText compact"
                      disabled={cleanupBusy}
                      onClick={() => cleanupRuntime(false)}
                    >
                      {cleanupBusy ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />}
                      <span>Безопасно</span>
                    </button>
                    <button
                      type="button"
                      className="iconText compact"
                      disabled={cleanupBusy}
                      onClick={() => cleanupRuntime(true)}
                    >
                      <ShieldAlert size={14} />
                      <span>Prune</span>
                    </button>
                  </div>
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
                <strong>ОЗУ</strong>
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
                    ? `${Math.round(telemetry.gpu.gpus?.[0]?.utilization_gpu ?? 0)}% загрузка`
                    : "офлайн"}
                </small>
              </div>
              <div className={`bridgeRow ${hostBridge?.port_open ? "online" : ""}`}>
                <Server size={14} />
                <strong>Мост Windows</strong>
                <span>{hostBridge?.port_open ? "онлайн" : "офлайн"}</span>
                <small>
                  {hostBridge?.token_available ? "токен есть" : "нет токена"} ·{" "}
                  {hostBridge?.native_capabilities?.length ?? 0} native
                </small>
              </div>
              <form className="hostCommandForm" onSubmit={requestHostCommandApproval}>
                <input
                  aria-label="Команда хоста"
                  value={hostCommandDraft}
                  onChange={(event) => setHostCommandDraft(event.target.value)}
                  placeholder="Get-Date"
                />
                <button
                  type="submit"
                  disabled={busy || !hostCommandDraft.trim()}
                  title="Запросить допуск"
                  aria-label="Запросить допуск"
                >
                  <Terminal size={15} />
                </button>
              </form>
              <div className={`bridgeRow ${autonomy?.enabled ? "online" : ""}`}>
                <Brain size={14} />
                <strong>Автономия</strong>
                <span>{autonomy?.enabled ? "вкл" : "выкл"}</span>
                <small>{autonomy?.running_tasks.length ?? 0} задач</small>
              </div>
              <button
                className="iconText compact full"
                type="button"
                onClick={runLearningTick}
                disabled={busy}
              >
                <Brain size={15} />
                <span>Шаг обучения</span>
              </button>
            </div>
              </>
            )}

            {(activeTab === "diagnostics" || activeTab === "runtime") && (
              <>
            <div className={`panelHeader ${activeTab === "runtime" ? "lower" : ""}`}>
              <h2>Политика автономии</h2>
              <span>{autonomyPolicy?.max_autonomous_steps ?? 0} шагов</span>
            </div>
            <div className="policyPanel">
              <div className="segmentedControl" role="group" aria-label="Режим автономии">
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
              <h2>Автономные задачи</h2>
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
                      <span>{kind === "diagnostics" ? "диагностика" : kind === "self_heal" ? "самолечение" : "бенчмарк"}</span>
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
                    title="Запустить задачу"
                    aria-label="Запустить задачу"
                    onClick={() => runAutonomyJob(job.id)}
                  >
                    <Play size={14} />
                  </button>
                </div>
              ))}
            </div>

            <div className="panelHeader lower">
              <h2>Сценарии</h2>
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
              <h2>Производительность</h2>
              <span>
                {typeof benchmarkReport?.metrics.total_ms === "number"
                  ? `${Math.round(benchmarkReport.metrics.total_ms)} ms`
                  : "готово"}
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
                <span>Бенчмарк</span>
              </button>
              {benchmarkReport && (
                <article className="benchmarkReport">
                  <strong>{benchmarkReport.summary}</strong>
                  <div className="metricRows">
                    <span>storage {Math.round(benchmarkReport.metrics.storage_ping_ms ?? 0)} ms</span>
                    <span>LLM {Math.round(benchmarkReport.metrics.llm_health_ms ?? 0)} ms</span>
                    <span>
                      {benchmarkReport.dispatcher.port_open ? "dispatcher вкл" : "dispatcher выкл"}
                    </span>
                  </div>
                  {benchmarkReport.recommendations.slice(0, 3).map((item) => (
                    <p key={item}>{item}</p>
                  ))}
                </article>
              )}
            </div>

            <div className="panelHeader lower">
              <h2>Политика браузера</h2>
              <span>{browserPolicy?.mode ?? "..."}</span>
            </div>
            <div className="policyPanel">
              <div className="segmentedControl" role="group" aria-label="Режим браузера">
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
                <span>{browserPolicy?.max_urls_per_action ?? 0} вкладок</span>
                <span>{browserPolicy?.allowed_hosts.slice(0, 2).join(", ")}</span>
              </div>
            </div>

            <div className="panelHeader lower">
              <h2>Docker</h2>
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
                  <span>{dockerPolicy?.max_log_tail ?? 200} строк логов</span>
                </button>
                <button
                  className="iconText compact"
                  type="button"
                  disabled={cleanupBusy}
                  onClick={() => cleanupRuntime(false)}
                >
                  {cleanupBusy ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />}
                  <span>Очистить</span>
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
                  <span>{container.allowed ? "разрешён" : "заблокирован"}</span>
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
                title="Загрузить"
                aria-label="Загрузить"
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
                aria-label="Папка для индексации"
              />
              <button
                type="submit"
                disabled={busy || !directoryDraft.trim()}
                title="Индексировать папку"
                aria-label="Индексировать папку"
              >
                <Database size={15} />
              </button>
            </form>
            {directoryIngest && (
              <article className="directoryResult">
                <strong>{directoryIngest.root}</strong>
                <span>
                  {directoryIngest.files_indexed}/{directoryIngest.files_seen} проиндексировано
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
              <h2>Настройки</h2>
              <span>{communicationStyleLabel(preferences?.communication_style)}</span>
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
                placeholder="Оператор"
                aria-label="Имя оператора"
              />
              <select
                value={preferenceDraft.communication_style}
                onChange={(event) =>
                  setPreferenceDraft((current) => ({
                    ...current,
                    communication_style: event.target.value as RuntimePreferences["communication_style"]
                  }))
                }
                aria-label="Стиль общения"
              >
                <option value="concise">кратко</option>
                <option value="balanced">сбалансированно</option>
                <option value="detailed">подробно</option>
              </select>
              <input
                value={preferenceDraft.quiet_hours}
                onChange={(event) =>
                  setPreferenceDraft((current) => ({
                    ...current,
                    quiet_hours: event.target.value
                  }))
                }
                placeholder="тихие часы"
                aria-label="Тихие часы"
              />
              <button
                type="submit"
                disabled={busy}
                title="Сохранить настройки"
                aria-label="Сохранить настройки"
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
            {memoryVault && (
              <div className="memoryVault">
                <div className="vaultHeader">
                  <Database size={16} />
                  <div>
                    <strong>Vault</strong>
                    <p>{memoryVault.root}</p>
                  </div>
                </div>
                <div className="vaultStats">
                  <span>{memoryVault.stats.notes ?? 0} notes</span>
                  <span>{memoryVault.stats.nodes ?? 0} nodes</span>
                  <span>{memoryVault.stats.edges ?? 0} edges</span>
                </div>
                <div className="vaultNodeList">
                  {memoryVault.top_nodes.slice(0, 6).map((node) => (
                    <span key={node.id} title={node.id}>
                      {node.label}
                    </span>
                  ))}
                </div>
              </div>
            )}
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
              <h2>Аудит</h2>
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
          <span>{status?.settings.llm.base_url ?? CONFIGURED_API_URL}</span>
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
  detail,
  label,
  value,
  tone
}: {
  icon: ReactNode;
  detail?: string;
  label: string;
  value: string;
  tone: "ok" | "warn" | "neutral";
}) {
  return (
    <article className={`statusTile ${tone}`}>
      <div>{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </article>
  );
}

function formatBytes(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function fitTone(fit?: ModelFit) {
  if (!fit) return "unknown";
  return fit.status;
}

function fitSummary(fit?: ModelFit) {
  if (!fit) return "нет оценки";
  return `${fit.label} · нужно ${formatBytes(fit.required_bytes)}`;
}

function progressPercent(done = 0, total = 0) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

function ratioPercent(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value * 100)));
}

function ratioLabel(value?: number | null) {
  return `${ratioPercent(value)}%`;
}
