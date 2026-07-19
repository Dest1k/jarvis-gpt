const CHAT_REQUEST_LEDGER_PROTOCOL = "jarvis.ui-chat-request.v1";
const CHAT_REQUEST_LEDGER_KEY_BASE = "jarvis.chatRequests.v1";
export const CHAT_REQUEST_LEDGER_TTL_MS = 26 * 60 * 60 * 1000;
const CHAT_REQUEST_LEDGER_MAX_ENTRIES = 8;
const CHAT_REQUEST_LEDGER_MAX_BYTES = 256 * 1024;
const CHAT_STREAM_TYPES = new Set(["meta", "event", "delta", "done", "error"]);
export const CHAT_STREAM_RETRY_MIN_DELAY_MS = 5_000;
export const CHAT_STREAM_RETRY_MAX_DELAY_MS = 5 * 60 * 1000;
export const CHAT_STREAM_IN_PROGRESS_MAX_DELAY_MS = 60_000;

export class ChatStreamError extends Error {
  constructor(
    message,
    {
      status = null,
      retryClass = null,
      retryAfterMs = null,
      interrupted = false,
      terminalType = null
    } = {}
  ) {
    super(message);
    this.name = "ChatStreamError";
    this.status = status;
    this.retryClass = retryClass;
    this.retryAfterMs = retryAfterMs;
    this.interrupted = interrupted;
    this.terminalType = terminalType;
  }
}

export function scopedChatRequestLedgerKey(identity) {
  return `${CHAT_REQUEST_LEDGER_KEY_BASE}::${encodeURIComponent(String(identity || ""))}`;
}

export function chatRequestIdsFromLedgerSnapshot(serialized) {
  if (typeof serialized !== "string" || !serialized.trim()) return [];
  try {
    const value = JSON.parse(serialized);
    if (!Array.isArray(value?.entries)) return [];
    return Array.from(
      new Set(
        value.entries
          .map((entry) => String(entry?.requestId || "").trim())
          .filter(Boolean)
      )
    );
  } catch {
    return [];
  }
}

export function chatRequestIdsRemovedByLedgerChange(oldValue, newValue) {
  const next = new Set(chatRequestIdsFromLedgerSnapshot(newValue));
  return chatRequestIdsFromLedgerSnapshot(oldValue).filter((requestId) => !next.has(requestId));
}

function normalizedLedgerEntry(value) {
  if (!value || typeof value !== "object") return null;
  const requestId = String(value.requestId || "").trim();
  const chatWindowId = String(value.chatWindowId || "").trim();
  const userMessageId = String(value.userMessageId || "").trim();
  const assistantMessageId = String(value.assistantMessageId || "").trim();
  const payload = value.payload;
  if (
    !requestId ||
    !chatWindowId ||
    !userMessageId ||
    !assistantMessageId ||
    !payload ||
    typeof payload !== "object" ||
    String(payload.request_id || "") !== requestId ||
    !String(payload.message || "").trim()
  ) {
    return null;
  }
  const status = new Set(["pending", "interrupted", "error"]).has(value.status)
    ? value.status
    : "pending";
  const now = Date.now();
  return {
    protocol: CHAT_REQUEST_LEDGER_PROTOCOL,
    requestId,
    chatWindowId,
    userMessageId,
    assistantMessageId,
    payload: JSON.parse(JSON.stringify(payload)),
    createdAt: Number.isFinite(Number(value.createdAt)) ? Number(value.createdAt) : now,
    updatedAt: Number.isFinite(Number(value.updatedAt)) ? Number(value.updatedAt) : now,
    status,
    attempts: Math.max(0, Math.floor(Number(value.attempts) || 0)),
    retryAt: Math.max(0, Number(value.retryAt) || 0),
    lastError: String(value.lastError || "").slice(0, 1000),
    retryClass: String(value.retryClass || "").slice(0, 100),
    lastConversationId: String(value.lastConversationId || "").slice(0, 200)
  };
}

export function readChatRequestLedger(storage, identity) {
  if (!storage || !identity) return [];
  let raw;
  try {
    raw = JSON.parse(storage.getItem(scopedChatRequestLedgerKey(identity)) || "{}");
  } catch {
    return [];
  }
  const entries = Array.isArray(raw.entries) ? raw.entries : [];
  const cutoff = Date.now() - CHAT_REQUEST_LEDGER_TTL_MS;
  const normalized = entries
    .map(normalizedLedgerEntry)
    .filter(Boolean)
    // A retry must never extend its own durability window indefinitely.  The
    // creation timestamp is the fixed recovery boundary; updatedAt is only
    // observability metadata.
    .filter((item) => item.createdAt > cutoff)
    .sort((left, right) => left.createdAt - right.createdAt);
  if (normalized.length !== entries.length) {
    try {
      storage.setItem(
        scopedChatRequestLedgerKey(identity),
        JSON.stringify({ protocol: CHAT_REQUEST_LEDGER_PROTOCOL, entries: normalized })
      );
    } catch {
      // Cleanup is best-effort. A quota/write failure must not hide valid pending work.
    }
  }
  return normalized;
}

export function upsertChatRequestLedger(storage, identity, entry) {
  if (!storage || !identity) {
    throw new Error("Durable chat request storage is unavailable");
  }
  const normalized = normalizedLedgerEntry(entry);
  if (!normalized) {
    throw new Error("Durable chat request entry is invalid");
  }
  const current = readChatRequestLedger(storage, identity);
  const replacing = current.some((item) => item.requestId === normalized.requestId);
  if (!replacing && current.length >= CHAT_REQUEST_LEDGER_MAX_ENTRIES) {
    throw new Error("Durable chat request ledger is full");
  }
  const entries = [
    ...current.filter((item) => item.requestId !== normalized.requestId),
    normalized
  ].sort((left, right) => left.createdAt - right.createdAt);
  const serialized = JSON.stringify({ protocol: CHAT_REQUEST_LEDGER_PROTOCOL, entries });
  if (new TextEncoder().encode(serialized).byteLength > CHAT_REQUEST_LEDGER_MAX_BYTES) {
    throw new Error("Durable chat request ledger exceeds its safe size limit");
  }
  storage.setItem(scopedChatRequestLedgerKey(identity), serialized);
  return normalized;
}

export function removeChatRequestLedgerEntry(storage, identity, requestId) {
  return removeChatRequestLedgerEntries(storage, identity, [requestId]).length > 0;
}

export function removeChatRequestLedgerEntries(storage, identity, requestIds) {
  if (!storage || !identity) return [];
  const requested = new Set(
    Array.from(requestIds || [], (requestId) => String(requestId || "").trim()).filter(Boolean)
  );
  if (!requested.size) return [];
  const current = readChatRequestLedger(storage, identity);
  const removed = current.filter((item) => requested.has(item.requestId));
  if (!removed.length) return [];
  const entries = current.filter((item) => !requested.has(item.requestId));
  // localStorage.setItem is atomic for this key.  Callers may only mutate the
  // visible chat or cancel timers after this write succeeds.
  storage.setItem(
    scopedChatRequestLedgerKey(identity),
    JSON.stringify({ protocol: CHAT_REQUEST_LEDGER_PROTOCOL, entries })
  );
  return removed;
}

export function chatRequestRecoveryRemainingMs(entry, now = Date.now()) {
  const createdAt = Number(entry?.createdAt);
  if (!Number.isFinite(createdAt)) return 0;
  return Math.max(0, createdAt + CHAT_REQUEST_LEDGER_TTL_MS - now);
}

export function chatRequestAutoRetryAllowed(entry, now = Date.now()) {
  return entry?.status !== "error" && chatRequestRecoveryRemainingMs(entry, now) > 0;
}

export function retryAfterMilliseconds(value, now = Date.now()) {
  const text = String(value || "").trim();
  if (!text) return null;
  const seconds = Number(text);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.round(seconds * 1000);
  const date = Date.parse(text);
  if (!Number.isFinite(date)) return null;
  return Math.max(0, date - now);
}

function responseRetryMetadata(response) {
  return {
    retryClass: response.headers.get("x-jarvis-retry-class"),
    retryAfterMs: retryAfterMilliseconds(response.headers.get("retry-after"))
  };
}

async function responseErrorMessage(response) {
  try {
    const payload = await response.json();
    const detail = payload?.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (detail && typeof detail.message === "string" && detail.message.trim()) {
      return detail.message.trim();
    }
  } catch {
    // The status line remains the safe fallback for non-JSON proxy errors.
  }
  return `${response.status} ${response.statusText}`.trim();
}

function parseStreamLine(line) {
  let item;
  try {
    item = JSON.parse(line);
  } catch (error) {
    throw new ChatStreamError(
      error instanceof Error ? `Invalid chat stream JSON: ${error.message}` : "Invalid chat stream JSON",
      { interrupted: true }
    );
  }
  if (!item || typeof item !== "object" || !CHAT_STREAM_TYPES.has(item.type)) {
    throw new ChatStreamError("Chat stream emitted an invalid item", { interrupted: true });
  }
  return item;
}

export async function consumeChatStreamResponse(response, onItem) {
  const responseRetry = responseRetryMetadata(response);
  if (!response.ok) {
    throw new ChatStreamError(await responseErrorMessage(response), {
      status: response.status,
      ...responseRetry
    });
  }
  if (!response.body) {
    throw new ChatStreamError("Streaming response has no body", {
      status: response.status,
      interrupted: true,
      ...responseRetry
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = null;

  const drain = (flush = false) => {
    const lines = buffer.split("\n");
    buffer = flush ? "" : (lines.pop() ?? "");
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      const item = parseStreamLine(line);
      onItem(item);
      if (item.type === "done" || item.type === "error") {
        terminal = item;
        break;
      }
    }
  };

  try {
    while (!terminal) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      drain();
    }
    if (!terminal) {
      buffer += decoder.decode();
      if (buffer.trim()) buffer += "\n";
      drain(true);
    }
  } catch (error) {
    if (error instanceof ChatStreamError) throw error;
    throw new ChatStreamError(
      error instanceof Error ? error.message : "Chat stream was interrupted",
      { status: response.status, interrupted: true, ...responseRetry }
    );
  } finally {
    try {
      await reader.cancel();
    } catch {
      // Teardown is best-effort. The caller still receives the exact protocol outcome.
    }
  }

  if (!terminal) {
    throw new ChatStreamError("Chat stream ended without done/error", {
      status: response.status,
      interrupted: true,
      ...responseRetry
    });
  }
  if (terminal.type === "error") {
    throw new ChatStreamError(String(terminal.error || "Chat stream failed"), {
      status: response.status,
      retryClass: String(terminal.retry_class || responseRetry.retryClass || "") || null,
      retryAfterMs: responseRetry.retryAfterMs,
      terminalType: "error"
    });
  }
  return terminal;
}

export function isRetryableChatStreamError(error) {
  if (!(error instanceof ChatStreamError)) return false;
  if (error.interrupted) return true;
  if (new Set(["llm-outage", "chat-request-in-progress"]).has(error.retryClass)) return true;
  return new Set([408, 425, 429, 502, 503, 504]).has(error.status);
}

export function chatStreamRetryDelay(error, attempts) {
  const safeFloor = CHAT_STREAM_RETRY_MIN_DELAY_MS;
  if (error instanceof ChatStreamError && error.retryAfterMs !== null) {
    // Retry-After is a lower bound.  A zero/invalidly-small value must not turn
    // a persistent outage into a browser tight-loop.
    return Math.max(safeFloor, error.retryAfterMs);
  }
  const exponent = Math.max(0, Math.min(8, Math.floor(Number(attempts) || 0) - 1));
  const cap =
    error instanceof ChatStreamError && error.retryClass === "chat-request-in-progress"
      ? CHAT_STREAM_IN_PROGRESS_MAX_DELAY_MS
      : CHAT_STREAM_RETRY_MAX_DELAY_MS;
  return Math.min(cap, safeFloor * (2 ** exponent));
}
