import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  CHAT_REQUEST_LEDGER_TTL_MS,
  CHAT_STREAM_IN_PROGRESS_MAX_DELAY_MS,
  CHAT_STREAM_RETRY_MAX_DELAY_MS,
  CHAT_STREAM_RETRY_MIN_DELAY_MS,
  ChatStreamError,
  chatRequestAutoRetryAllowed,
  chatRequestRecoveryRemainingMs,
  chatStreamRetryDelay,
  consumeChatStreamResponse,
  isRetryableChatStreamError,
  readChatRequestLedger,
  removeChatRequestLedgerEntry,
  removeChatRequestLedgerEntries,
  retryAfterMilliseconds,
  scopedChatRequestLedgerKey,
  upsertChatRequestLedger
} from "../lib/chat-stream-recovery.mjs";

class MemoryStorage {
  #values = new Map();
  failWrites = false;
  writes = 0;

  getItem(key) {
    return this.#values.get(key) ?? null;
  }

  setItem(key, value) {
    if (this.failWrites) throw new Error("quota unavailable");
    this.writes += 1;
    this.#values.set(key, String(value));
  }

  seed(key, value) {
    this.#values.set(key, String(value));
  }
}

function entry(overrides = {}) {
  const requestId = overrides.requestId ?? "ui:req-001";
  return {
    requestId,
    chatWindowId: "chat-001",
    userMessageId: "msg-user-001",
    assistantMessageId: "msg-assistant-001",
    payload: {
      request_id: requestId,
      message: "Проверь состояние и ответь",
      conversation_id: null,
      mode: "auto",
      max_tokens: 512,
      attachments: [],
      thinking_enabled: true
    },
    createdAt: Date.now(),
    updatedAt: Date.now(),
    status: "pending",
    attempts: 0,
    retryAt: 0,
    ...overrides
  };
}

const storage = new MemoryStorage();
const identity = "d:\\jarvis::qwen36-vl";
const first = upsertChatRequestLedger(storage, identity, entry());
assert.equal(first.requestId, "ui:req-001");
assert.equal(readChatRequestLedger(storage, identity).length, 1);
assert.equal(scopedChatRequestLedgerKey(identity).includes(encodeURIComponent(identity)), true);

upsertChatRequestLedger(storage, identity, {
  ...first,
  status: "interrupted",
  attempts: 1,
  lastError: "network reset"
});
const interrupted = readChatRequestLedger(storage, identity);
assert.equal(interrupted.length, 1, "upsert of the same logical request must not duplicate it");
assert.equal(interrupted[0].requestId, first.requestId);
assert.equal(interrupted[0].status, "interrupted");
assert.equal(removeChatRequestLedgerEntry(storage, identity, "another-request"), false);
assert.equal(readChatRequestLedger(storage, identity).length, 1);
assert.equal(removeChatRequestLedgerEntry(storage, identity, first.requestId), true);
assert.equal(readChatRequestLedger(storage, identity).length, 0);

const ttlNow = Date.now();
const realDateNow = Date.now;
Date.now = () => ttlNow;
const ttlStorage = new MemoryStorage();
const ttlKey = scopedChatRequestLedgerKey(identity);
ttlStorage.seed(
  ttlKey,
  JSON.stringify({
    entries: [
      entry({
        requestId: "ui:inside-ttl",
        createdAt: ttlNow - CHAT_REQUEST_LEDGER_TTL_MS + 1,
        updatedAt: ttlNow - CHAT_REQUEST_LEDGER_TTL_MS + 1
      }),
      entry({
        requestId: "ui:expired",
        createdAt: ttlNow - CHAT_REQUEST_LEDGER_TTL_MS - 1,
        updatedAt: ttlNow
      })
    ]
  })
);
assert.deepEqual(
  readChatRequestLedger(ttlStorage, identity).map((item) => item.requestId),
  ["ui:inside-ttl"],
  "updatedAt must not extend the fixed 26-hour recovery boundary"
);

const atomicStorage = new MemoryStorage();
for (const requestId of ["ui:atomic-a", "ui:atomic-b", "ui:atomic-c"]) {
  upsertChatRequestLedger(atomicStorage, identity, entry({ requestId }));
}
const writesBeforeAtomicRemoval = atomicStorage.writes;
assert.deepEqual(
  removeChatRequestLedgerEntries(atomicStorage, identity, ["ui:atomic-a", "ui:atomic-c"])
    .map((item) => item.requestId),
  ["ui:atomic-a", "ui:atomic-c"]
);
assert.equal(
  atomicStorage.writes,
  writesBeforeAtomicRemoval + 1,
  "multi-request cancellation must commit with one localStorage write"
);
assert.deepEqual(
  readChatRequestLedger(atomicStorage, identity).map((item) => item.requestId),
  ["ui:atomic-b"]
);
atomicStorage.failWrites = true;
assert.throws(
  () => removeChatRequestLedgerEntries(atomicStorage, identity, ["ui:atomic-b"]),
  /quota unavailable/
);
atomicStorage.failWrites = false;
assert.deepEqual(
  readChatRequestLedger(atomicStorage, identity).map((item) => item.requestId),
  ["ui:atomic-b"],
  "a failed atomic cancellation must leave the ledger unchanged"
);

const cleanupFailureStorage = new MemoryStorage();
cleanupFailureStorage.seed(
  ttlKey,
  JSON.stringify({
    entries: [
      entry({ requestId: "ui:valid-pending" }),
      entry({
        requestId: "ui:expired-cleanup",
        createdAt: ttlNow - CHAT_REQUEST_LEDGER_TTL_MS - 1,
        updatedAt: ttlNow - CHAT_REQUEST_LEDGER_TTL_MS - 1
      })
    ]
  })
);
cleanupFailureStorage.failWrites = true;
assert.deepEqual(
  readChatRequestLedger(cleanupFailureStorage, identity).map((item) => item.requestId),
  ["ui:valid-pending"],
  "best-effort cleanup failure must not hide valid pending entries"
);

const boundedStorage = new MemoryStorage();
for (let index = 0; index < 8; index += 1) {
  upsertChatRequestLedger(
    boundedStorage,
    identity,
    entry({ requestId: `ui:bounded-${index}`, createdAt: ttlNow + index })
  );
}
assert.throws(
  () => upsertChatRequestLedger(boundedStorage, identity, entry({ requestId: "ui:overflow" })),
  /ledger is full/
);
assert.doesNotThrow(() =>
  upsertChatRequestLedger(boundedStorage, identity, {
    ...readChatRequestLedger(boundedStorage, identity)[0],
    status: "interrupted"
  })
);
const oversizedRequestId = "ui:oversized";
assert.throws(
  () =>
    upsertChatRequestLedger(
      new MemoryStorage(),
      identity,
      entry({
        requestId: oversizedRequestId,
        payload: {
          ...entry({ requestId: oversizedRequestId }).payload,
          message: "x".repeat(300_000)
        }
      })
    ),
  /safe size limit/
);
Date.now = realDateNow;

const proxySource = await readFile(
  new URL("../app/jarvis-api/[...path]/route.ts", import.meta.url),
  "utf8"
);
for (const header of ["retry-after", "x-jarvis-request-id", "x-jarvis-retry-class"]) {
  assert.equal(proxySource.includes(`"${header}"`), true, `${header} must survive the UI proxy`);
}

const pageSource = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
assert.equal(pageSource.includes("async function submitChat()"), false);
assert.equal(pageSource.includes("await submitChatDurable();"), true);
assert.equal(pageSource.includes("void submitChatDurable();"), true);
assert.equal(pageSource.includes("request_id: requestId"), true);
assert.equal(pageSource.match(/"\/api\/chat\/stream"/g)?.length, 1);
assert.equal(pageSource.includes("retryDurableChatLine(line)"), true);
assert.equal(pageSource.includes("dismissDurableChatLine(line)"), true);
const durableWrite = pageSource.indexOf("const entry = persistDurableChatRequest(identity");
const streamHandoff = pageSource.indexOf(
  "await runDurableChatRequestRef.current(identity, entry)",
  durableWrite
);
assert.equal(durableWrite >= 0 && streamHandoff > durableWrite, true);
const submitStart = pageSource.indexOf("async function submitChatDurable()");
const uploadPreflight = pageSource.indexOf(
  "attachments = await uploadChatFiles(filesToSend",
  submitStart
);
assert.equal(submitStart >= 0 && uploadPreflight > submitStart && durableWrite > uploadPreflight, true);
assert.equal(
  pageSource.slice(submitStart, durableWrite).includes('role: "assistant"'),
  false,
  "upload preflight must not create a fake recoverable assistant line before WAL"
);
const closeStart = pageSource.indexOf("function closeChatWindow(");
const closeCancel = pageSource.indexOf("cancelDurableChatWindowRequests(id)", closeStart);
const closeMutation = pageSource.indexOf("setChatWindows", closeStart);
assert.equal(closeStart >= 0 && closeCancel > closeStart && closeMutation > closeCancel, true);
const clearStart = pageSource.indexOf("async function clearCurrentChat()");
const clearCancel = pageSource.indexOf(
  "cancelDurableChatWindowRequests(activeChatWindow.id)",
  clearStart
);
const clearMutation = pageSource.indexOf("updateChatWindow", clearStart);
assert.equal(clearStart >= 0 && clearCancel > clearStart && clearMutation > clearCancel, true);
assert.equal(pageSource.includes("chatRequestAbortControllersRef.current.get(key)?.abort()"), true);
assert.equal(
  pageSource.includes('window.addEventListener("storage", handleLedgerChange)'),
  true,
  "a cancellation in another tab must fence local timers and in-flight fetches"
);
assert.equal(pageSource.includes("CHAT_AUTO_RETRY_MAX_ATTEMPTS"), false);

const seen = [];
const done = await consumeChatStreamResponse(
  new Response(
    [
      JSON.stringify({ type: "meta", conversation_id: "conv-1" }),
      JSON.stringify({ type: "delta", content: "готово" }),
      JSON.stringify({ type: "done", answer: "готово", message_id: "msg-1" })
    ].join("\n") + "\n",
    { status: 200 }
  ),
  (item) => seen.push(item)
);
assert.equal(done.type, "done");
assert.deepEqual(seen.map((item) => item.type), ["meta", "delta", "done"]);

await assert.rejects(
  consumeChatStreamResponse(
    new Response(`${JSON.stringify({ type: "delta", content: "partial" })}\n`, { status: 200 }),
    () => undefined
  ),
  (error) =>
    error instanceof ChatStreamError &&
    error.interrupted === true &&
    error.message.includes("without done/error")
);

await assert.rejects(
  consumeChatStreamResponse(
    new Response(
      `${JSON.stringify({ type: "error", error: "model unavailable", retry_class: "llm-outage" })}\n`,
      { status: 200 }
    ),
    () => undefined
  ),
  (error) =>
    error instanceof ChatStreamError &&
    error.terminalType === "error" &&
    error.retryClass === "llm-outage" &&
    isRetryableChatStreamError(error)
);

let httpFailure;
try {
  await consumeChatStreamResponse(
    new Response(JSON.stringify({ detail: "chat request is already being processed" }), {
      status: 409,
      headers: {
        "content-type": "application/json",
        "retry-after": "3",
        "x-jarvis-retry-class": "chat-request-in-progress"
      }
    }),
    () => undefined
  );
} catch (error) {
  httpFailure = error;
}
assert.equal(httpFailure instanceof ChatStreamError, true);
assert.equal(httpFailure.status, 409);
assert.equal(httpFailure.retryClass, "chat-request-in-progress");
assert.equal(httpFailure.retryAfterMs, 3000);
assert.equal(isRetryableChatStreamError(httpFailure), true);
assert.equal(chatStreamRetryDelay(httpFailure, 9), CHAT_STREAM_RETRY_MIN_DELAY_MS);

const inProgress = new ChatStreamError("still running", {
  status: 409,
  retryClass: "chat-request-in-progress"
});
assert.equal(chatStreamRetryDelay(inProgress, 20), CHAT_STREAM_IN_PROGRESS_MAX_DELAY_MS);
let leaseElapsed = 0;
let leaseAttempts = 1;
while (leaseElapsed <= 15 * 60 * 1000) {
  leaseElapsed += chatStreamRetryDelay(inProgress, leaseAttempts);
  leaseAttempts += 1;
}
assert.equal(leaseAttempts > 8, true, "recovery must keep polling beyond the old 8-attempt wall");
const recoveryCreatedAt = ttlNow;
const recoveryEntry = entry({ createdAt: recoveryCreatedAt, updatedAt: recoveryCreatedAt });
assert.equal(
  chatRequestAutoRetryAllowed(recoveryEntry, recoveryCreatedAt + 15 * 60 * 1000 + 1),
  true,
  "automatic recovery must outlive the backend 15-minute request lease"
);
assert.equal(
  chatRequestRecoveryRemainingMs(recoveryEntry, recoveryCreatedAt + CHAT_REQUEST_LEDGER_TTL_MS),
  0
);
assert.equal(
  chatRequestAutoRetryAllowed(recoveryEntry, recoveryCreatedAt + CHAT_REQUEST_LEDGER_TTL_MS),
  false,
  "automatic recovery must stop at the fixed 26-hour boundary"
);

assert.equal(retryAfterMilliseconds("2"), 2000);
assert.equal(isRetryableChatStreamError(new ChatStreamError("bad request", { status: 409 })), false);
assert.equal(isRetryableChatStreamError(new ChatStreamError("proxy down", { status: 502 })), true);
assert.equal(
  chatStreamRetryDelay(new ChatStreamError("offline", { interrupted: true }), 0),
  CHAT_STREAM_RETRY_MIN_DELAY_MS
);
assert.equal(
  chatStreamRetryDelay(new ChatStreamError("offline", { interrupted: true }), 20),
  CHAT_STREAM_RETRY_MAX_DELAY_MS
);
assert.equal(
  chatStreamRetryDelay(
    new ChatStreamError("wait", { status: 503, retryAfterMs: 10 * 60 * 1000 }),
    20
  ),
  10 * 60 * 1000,
  "Retry-After must remain a lower bound even beyond the ordinary backoff cap"
);

console.log("chat-stream-recovery-ok");
