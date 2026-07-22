"use client";

import {
  ArrowLeft,
  Check,
  CircleAlert,
  Loader2,
  MessageSquare,
  RefreshCw,
  Search,
  Send,
  UserRound,
  Wifi,
  WifiOff
} from "lucide-react";
import Link from "next/link";
import {
  Fragment,
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState
} from "react";

import styles from "./telegram.module.css";

const API = "/jarvis-api";
const CHAT_LIMIT = 100;
const MESSAGE_LIMIT = 200;
const POLL_INTERVAL_MS = 3000;
const MAX_MESSAGE_LENGTH = 4096;

type TelegramChat = {
  key: string;
  realm_id: string;
  chat_id: number | string;
  conversation_id: string;
  user_id?: string | null;
  status?: string | null;
  preset_key?: string | null;
  display_name?: string | null;
  username?: string | null;
  first_name?: string | null;
  last_name?: string | null;
  title?: string | null;
  updated_at?: string | null;
  last_message?: string | null;
  last_message_at?: string | null;
  message_count: number;
};

type TelegramMessage = {
  id: string;
  role: string;
  direction: string;
  content: string;
  created_at: string;
  edited_at?: string | null;
  reply_to_message_id?: string | null;
  metadata?: Record<string, unknown> | null;
  operator_authored?: boolean;
  delivery_status?: string | null;
  sort_sequence?: number;
  sort_rank?: number;
};

type ChatListResponse = {
  chats: TelegramChat[];
  total: number;
  limit: number;
  offset: number;
};

type ChatMessagesResponse = {
  chat: TelegramChat;
  messages: TelegramMessage[];
  has_more: boolean;
  next_before: string | null;
};

type SendMessageResponse = {
  send: TelegramSend;
  message: TelegramMessage | null;
};

type TelegramSend = {
  id?: string;
  client_request_id?: string;
  status?: string;
};

type OlderScrollAnchor = {
  chatKey: string;
  messageId: string | null;
  offsetTop: number;
  scrollHeight: number;
  scrollTop: number;
};

class ApiRequestError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.payload = payload;
  }
}

function classNames(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ");
}

function errorText(payload: unknown, fallback: string) {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = (payload as Record<string, unknown>).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail) {
    if (typeof detail === "object") {
      const record = detail as Record<string, unknown>;
      if (record.error === "telegram_delivery_failed") {
        const status = typeof record.status === "string" ? deliveryLabel(record.status) : "ошибка";
        const code = record.code == null ? "" : " · код " + String(record.code);
        return "Telegram не подтвердил отправку: " + status + code;
      }
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-Jarvis-Frontend", "admin-telegram");
  const response = await fetch(API + path, {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined") {
      const next = window.location.pathname + window.location.search + window.location.hash;
      window.location.assign("/login?next=" + encodeURIComponent(next));
    }
    throw new ApiRequestError(
      errorText(payload, "Ошибка API (" + response.status + ")"),
      response.status,
      payload
    );
  }
  return payload as T;
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function isTelegramChat(value: unknown): value is TelegramChat {
  if (!value || typeof value !== "object") return false;
  const chat = value as Record<string, unknown>;
  return (
    typeof chat.key === "string" &&
    chat.key.length > 0 &&
    typeof chat.realm_id === "string" &&
    (typeof chat.chat_id === "number" || typeof chat.chat_id === "string")
  );
}

function chatList(value: unknown): TelegramChat[] {
  return Array.isArray(value) ? value.filter(isTelegramChat) : [];
}

function isTelegramMessage(value: unknown): value is TelegramMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as Record<string, unknown>;
  return (
    typeof message.id === "string" &&
    message.id.length > 0 &&
    typeof message.content === "string" &&
    typeof message.created_at === "string"
  );
}

function messageList(value: unknown): TelegramMessage[] {
  return Array.isArray(value) ? value.filter(isTelegramMessage) : [];
}

function messageClientRequestId(message: TelegramMessage) {
  const value = message.metadata?.client_request_id;
  return typeof value === "string" && value ? value : null;
}

function messageOperatorSendId(message: TelegramMessage) {
  const value = message.metadata?.operator_send_id;
  return typeof value === "string" && value ? value : null;
}

function sameMessage(left: TelegramMessage, right: TelegramMessage) {
  return (
    left.id === right.id &&
    left.role === right.role &&
    left.direction === right.direction &&
    left.content === right.content &&
    left.created_at === right.created_at &&
    left.edited_at === right.edited_at &&
    left.reply_to_message_id === right.reply_to_message_id &&
    left.operator_authored === right.operator_authored &&
    left.delivery_status === right.delivery_status &&
    left.sort_sequence === right.sort_sequence &&
    left.sort_rank === right.sort_rank &&
    messageClientRequestId(left) === messageClientRequestId(right) &&
    messageOperatorSendId(left) === messageOperatorSendId(right)
  );
}

function messageTimestamp(message: TelegramMessage) {
  const parsed = Date.parse(message.created_at);
  return Number.isFinite(parsed) ? parsed : 0;
}

function messageSortSequence(message: TelegramMessage) {
  return Number.isSafeInteger(message.sort_sequence) ? Number(message.sort_sequence) : 0;
}

function messageSortRank(message: TelegramMessage) {
  if (message.sort_rank === 0 || message.sort_rank === 1) return message.sort_rank;
  return message.direction === "inbound" || message.role === "user" ? 0 : 1;
}

function compareMessageIds(left: string, right: string) {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

function mergeTelegramMessages(
  current: TelegramMessage[],
  incoming: TelegramMessage[]
): TelegramMessage[] {
  const acknowledgedRequests = new Set(
    incoming.map(messageClientRequestId).filter((value): value is string => Boolean(value))
  );
  const settledOperatorSends = new Set(
    incoming.map(messageOperatorSendId).filter((value): value is string => Boolean(value))
  );
  const byId = new Map<string, TelegramMessage>();
  for (const message of current) {
    const requestId = messageClientRequestId(message);
    if (message.id.startsWith("pending:") && requestId && acknowledgedRequests.has(requestId)) {
      continue;
    }
    if (settledOperatorSends.has(message.id)) continue;
    byId.set(message.id, message);
  }
  for (const message of incoming) {
    const previous = byId.get(message.id);
    byId.set(message.id, previous && sameMessage(previous, message) ? previous : message);
  }
  const merged = Array.from(byId.values()).sort(
    (left, right) =>
      messageTimestamp(left) - messageTimestamp(right) ||
      messageSortSequence(left) - messageSortSequence(right) ||
      messageSortRank(left) - messageSortRank(right) ||
      compareMessageIds(left.id, right.id)
  );
  if (
    merged.length === current.length &&
    merged.every((message, index) => message === current[index])
  ) {
    return current;
  }
  return merged;
}

function telegramSend(value: unknown): TelegramSend | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  return {
    id: typeof record.id === "string" ? record.id : undefined,
    client_request_id:
      typeof record.client_request_id === "string" ? record.client_request_id : undefined,
    status: typeof record.status === "string" ? record.status : undefined
  };
}

function sendFromError(error: unknown) {
  if (!(error instanceof ApiRequestError) || !error.payload || typeof error.payload !== "object") {
    return null;
  }
  return telegramSend((error.payload as Record<string, unknown>).send);
}

function failedDeliveryStatus(error: unknown, send: TelegramSend | null) {
  const explicit = send?.status?.trim().toLowerCase();
  if (explicit) return explicit;
  if (error instanceof ApiRequestError && error.status >= 400 && error.status < 500) {
    return "failed";
  }
  return "uncertain";
}

function telegramMessagesPath(realmId: string, chatId: number | string) {
  return (
    "/api/admin/telegram/chats/" +
    encodeURIComponent(realmId) +
    "/" +
    encodeURIComponent(String(chatId)) +
    "/messages"
  );
}

function chatTitle(chat: TelegramChat) {
  const fullName = [chat.first_name, chat.last_name].filter(Boolean).join(" ").trim();
  return (
    fullName ||
    chat.display_name?.trim() ||
    chat.title?.trim() ||
    (chat.username ? "@" + chat.username : "") ||
    "Telegram " + String(chat.chat_id)
  );
}

function chatSubtitle(chat: TelegramChat) {
  const parts = [
    chat.username ? "@" + chat.username : "",
    "ID " + String(chat.chat_id),
    chat.preset_key || ""
  ].filter(Boolean);
  return parts.join(" · ");
}

function initials(value: string) {
  const result = value
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
  return result || "TG";
}

function formatTime(value?: string | number | null) {
  if (value == null || value === "") return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function formatListTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return formatTime(value);
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
}

function dayIdentity(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return [date.getFullYear(), date.getMonth(), date.getDate()].join("-");
}

function formatDay(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Сегодня";
  if (date.toDateString() === yesterday.toDateString()) return "Вчера";
  return date.toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "long",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric"
  });
}

function deliveryLabel(value?: string | null) {
  if (!value) return "";
  const labels: Record<string, string> = {
    sending: "отправляется",
    pending: "в очереди",
    sent: "отправлено",
    delivered: "доставлено",
    completed: "отправлено",
    failed: "ошибка"
  };
  return labels[value.toLowerCase()] || value.replace(/_/g, " ");
}

function createClientRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return "admin-telegram:" + crypto.randomUUID();
  }
  return "admin-telegram:" + Date.now() + ":" + Math.random().toString(36).slice(2);
}

export default function TelegramConsole() {
  const [chats, setChats] = useState<TelegramChat[]>([]);
  const [totalChats, setTotalChats] = useState(0);
  const [selectedKey, setSelectedKey] = useState("");
  const [messages, setMessages] = useState<TelegramMessage[]>([]);
  const [threadHasMore, setThreadHasMore] = useState(false);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [chatsLoading, setChatsLoading] = useState(true);
  const [threadLoading, setThreadLoading] = useState(false);
  const [olderLoading, setOlderLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState("");
  const [threadError, setThreadError] = useState("");
  const [sendError, setSendError] = useState("");
  const [notice, setNotice] = useState("");
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null);
  const [mobileThreadOpen, setMobileThreadOpen] = useState(false);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const deferredSearch = useDeferredValue(search);

  const selectedKeyRef = useRef(selectedKey);
  const chatsRequestRef = useRef(0);
  const threadRequestRef = useRef(0);
  const olderRequestRef = useRef(0);
  const chatsAbortRef = useRef<AbortController | null>(null);
  const threadAbortRef = useRef<AbortController | null>(null);
  const olderAbortRef = useRef<AbortController | null>(null);
  const chatsInFlightRef = useRef(false);
  const threadInFlightRef = useRef(false);
  const messageViewportRef = useRef<HTMLDivElement | null>(null);
  const olderScrollAnchorRef = useRef<OlderScrollAnchor | null>(null);
  const paginationInitializedRef = useRef(false);
  const stickToBottomRef = useRef(true);
  const noticeTimerRef = useRef<number | null>(null);

  useEffect(() => {
    selectedKeyRef.current = selectedKey;
  }, [selectedKey]);

  const selectedChat = useMemo(
    () => chats.find((chat) => chat.key === selectedKey) ?? null,
    [chats, selectedKey]
  );
  const selectedRealmId = selectedChat?.realm_id ?? "";
  const selectedChatId = selectedChat?.chat_id ?? null;
  const draft = selectedKey ? drafts[selectedKey] ?? "" : "";

  const showNotice = useCallback((value: string) => {
    setNotice(value);
    if (noticeTimerRef.current != null) window.clearTimeout(noticeTimerRef.current);
    noticeTimerRef.current = window.setTimeout(() => {
      setNotice("");
      noticeTimerRef.current = null;
    }, 2600);
  }, []);

  useEffect(
    () => () => {
      if (noticeTimerRef.current != null) window.clearTimeout(noticeTimerRef.current);
    },
    []
  );

  const loadChats = useCallback(
    async (silent = true) => {
      if (chatsInFlightRef.current) {
        if (silent) return;
        chatsAbortRef.current?.abort();
      }
      const requestId = ++chatsRequestRef.current;
      const controller = new AbortController();
      chatsAbortRef.current = controller;
      chatsInFlightRef.current = true;
      if (!silent) setChatsLoading(true);
      const query = new URLSearchParams({
        limit: String(CHAT_LIMIT),
        offset: "0",
        search: deferredSearch.trim()
      });
      try {
        const payload = await requestJson<ChatListResponse>(
          "/api/admin/telegram/chats?" + query.toString(),
          { signal: controller.signal }
        );
        if (requestId !== chatsRequestRef.current) return;
        const nextChats = chatList(payload.chats);
        setChats(nextChats);
        setTotalChats(Number(payload.total) || 0);
        setSelectedKey((current) =>
          current && nextChats.some((chat) => chat.key === current)
            ? current
            : nextChats[0]?.key || ""
        );
        setChatError("");
        setLastSyncedAt(Date.now());
      } catch (error) {
        if (requestId !== chatsRequestRef.current || isAbortError(error)) return;
        setChatError(error instanceof Error ? error.message : "Не удалось загрузить чаты");
      } finally {
        if (requestId === chatsRequestRef.current) {
          chatsInFlightRef.current = false;
          setChatsLoading(false);
        }
      }
    },
    [deferredSearch]
  );

  useEffect(() => {
    void loadChats(false);
    const interval = window.setInterval(() => {
      void loadChats(true);
    }, POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(interval);
      chatsAbortRef.current?.abort();
    };
  }, [loadChats]);

  const loadThread = useCallback(
    async (silent = true) => {
      if (!selectedKey || !selectedRealmId || selectedChatId == null) return;
      if (threadInFlightRef.current) {
        if (silent) return;
        threadAbortRef.current?.abort();
      }
      const requestId = ++threadRequestRef.current;
      const controller = new AbortController();
      threadAbortRef.current = controller;
      threadInFlightRef.current = true;
      if (!silent) setThreadLoading(true);
      const path =
        telegramMessagesPath(selectedRealmId, selectedChatId) +
        "?limit=" +
        String(MESSAGE_LIMIT);
      try {
        const payload = await requestJson<ChatMessagesResponse>(path, {
          signal: controller.signal
        });
        if (
          requestId !== threadRequestRef.current ||
          selectedKeyRef.current !== selectedKey
        ) {
          return;
        }
        const incoming = messageList(payload.messages);
        setMessages((current) =>
          silent ? mergeTelegramMessages(current, incoming) : mergeTelegramMessages([], incoming)
        );
        if (!silent || !paginationInitializedRef.current) {
          const cursor =
            typeof payload.next_before === "string" && payload.next_before
              ? payload.next_before
              : null;
          paginationInitializedRef.current = true;
          setNextBefore(cursor);
          setThreadHasMore(Boolean(payload.has_more && cursor));
        }
        if (isTelegramChat(payload.chat)) {
          setChats((current) =>
            current.map((chat) => (chat.key === selectedKey ? { ...chat, ...payload.chat } : chat))
          );
        }
        setThreadError("");
        setLastSyncedAt(Date.now());
      } catch (error) {
        if (requestId !== threadRequestRef.current || isAbortError(error)) return;
        setThreadError(error instanceof Error ? error.message : "Не удалось загрузить сообщения");
      } finally {
        if (requestId === threadRequestRef.current) {
          threadInFlightRef.current = false;
          setThreadLoading(false);
        }
      }
    },
    [selectedChatId, selectedKey, selectedRealmId]
  );

  useEffect(() => {
    threadAbortRef.current?.abort();
    olderAbortRef.current?.abort();
    olderRequestRef.current += 1;
    olderScrollAnchorRef.current = null;
    paginationInitializedRef.current = false;
    setMessages([]);
    setThreadHasMore(false);
    setNextBefore(null);
    setOlderLoading(false);
    setThreadError("");
    stickToBottomRef.current = true;
    setAwayFromBottom(false);
    if (!selectedKey || !selectedRealmId || selectedChatId == null) return;
    void loadThread(false);
    const interval = window.setInterval(() => {
      void loadThread(true);
    }, POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(interval);
      threadRequestRef.current += 1;
      olderRequestRef.current += 1;
      threadAbortRef.current?.abort();
      olderAbortRef.current?.abort();
    };
  }, [loadThread, selectedChatId, selectedKey, selectedRealmId]);

  async function loadOlderMessages() {
    if (
      olderLoading ||
      !threadHasMore ||
      !nextBefore ||
      !selectedKey ||
      !selectedRealmId ||
      selectedChatId == null
    ) {
      return;
    }
    const chatKey = selectedKey;
    const requestId = ++olderRequestRef.current;
    const controller = new AbortController();
    olderAbortRef.current?.abort();
    olderAbortRef.current = controller;
    setOlderLoading(true);
    setThreadError("");
    const query = new URLSearchParams({
      limit: String(MESSAGE_LIMIT),
      before: nextBefore
    });
    try {
      const payload = await requestJson<ChatMessagesResponse>(
        telegramMessagesPath(selectedRealmId, selectedChatId) + "?" + query.toString(),
        { signal: controller.signal }
      );
      if (
        requestId !== olderRequestRef.current ||
        selectedKeyRef.current !== chatKey
      ) {
        return;
      }

      const viewport = messageViewportRef.current;
      if (viewport) {
        const viewportTop = viewport.getBoundingClientRect().top;
        const visibleMessage = Array.from(
          viewport.querySelectorAll<HTMLElement>("[data-message-id]")
        ).find((element) => element.getBoundingClientRect().bottom >= viewportTop);
        olderScrollAnchorRef.current = {
          chatKey,
          messageId: visibleMessage?.dataset.messageId || null,
          offsetTop: visibleMessage
            ? visibleMessage.getBoundingClientRect().top - viewportTop
            : 0,
          scrollHeight: viewport.scrollHeight,
          scrollTop: viewport.scrollTop
        };
      }
      stickToBottomRef.current = false;

      const incoming = messageList(payload.messages);
      setMessages((current) => {
        const merged = mergeTelegramMessages(current, incoming);
        if (merged === current) olderScrollAnchorRef.current = null;
        return merged;
      });
      const cursor =
        typeof payload.next_before === "string" && payload.next_before
          ? payload.next_before
          : null;
      paginationInitializedRef.current = true;
      setNextBefore(cursor);
      setThreadHasMore(Boolean(payload.has_more && cursor));
      if (isTelegramChat(payload.chat)) {
        setChats((current) =>
          current.map((chat) => (chat.key === chatKey ? { ...chat, ...payload.chat } : chat))
        );
      }
      setLastSyncedAt(Date.now());
    } catch (error) {
      if (requestId !== olderRequestRef.current || isAbortError(error)) return;
      setThreadError(
        error instanceof Error ? error.message : "Не удалось загрузить ранние сообщения"
      );
    } finally {
      if (requestId === olderRequestRef.current) setOlderLoading(false);
    }
  }

  useLayoutEffect(() => {
    const anchor = olderScrollAnchorRef.current;
    const viewport = messageViewportRef.current;
    if (!anchor || !viewport) return;
    olderScrollAnchorRef.current = null;
    if (anchor.chatKey !== selectedKey) return;

    const viewportTop = viewport.getBoundingClientRect().top;
    const anchoredMessage = anchor.messageId
      ? Array.from(viewport.querySelectorAll<HTMLElement>("[data-message-id]")).find(
          (element) => element.dataset.messageId === anchor.messageId
        )
      : null;
    if (anchoredMessage) {
      const nextOffset = anchoredMessage.getBoundingClientRect().top - viewportTop;
      viewport.scrollTop += nextOffset - anchor.offsetTop;
    } else {
      viewport.scrollTop =
        anchor.scrollTop + Math.max(0, viewport.scrollHeight - anchor.scrollHeight);
    }
    const nearBottom =
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
    stickToBottomRef.current = nearBottom;
    setAwayFromBottom(!nearBottom);
  }, [messages, selectedKey]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    const viewport = messageViewportRef.current;
    if (!viewport) return;
    viewport.scrollTo({ top: viewport.scrollHeight, behavior });
    stickToBottomRef.current = true;
    setAwayFromBottom(false);
  }, []);

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const frame = window.requestAnimationFrame(() => scrollToBottom("auto"));
    return () => window.cancelAnimationFrame(frame);
  }, [messages, scrollToBottom, selectedKey]);

  function handleMessageScroll() {
    const viewport = messageViewportRef.current;
    if (!viewport) return;
    const nearBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
    stickToBottomRef.current = nearBottom;
    setAwayFromBottom(!nearBottom);
  }

  function selectChat(chat: TelegramChat) {
    selectedKeyRef.current = chat.key;
    setSelectedKey(chat.key);
    setMobileThreadOpen(true);
    setSendError("");
    stickToBottomRef.current = true;
    setAwayFromBottom(false);
  }

  async function refreshAll() {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await Promise.all([loadChats(false), loadThread(true)]);
    } finally {
      setRefreshing(false);
    }
  }

  function updateDraft(value: string) {
    if (!selectedKey) return;
    setDrafts((current) => ({ ...current, [selectedKey]: value }));
    setSendError("");
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedChat || sending || !draft.trim()) return;
    const content = draft;
    const sentChatKey = selectedChat.key;
    const clientRequestId = createClientRequestId();
    const pendingId = "pending:" + clientRequestId;
    const optimistic: TelegramMessage = {
      id: pendingId,
      role: "assistant",
      direction: "outbound",
      content,
      created_at: new Date().toISOString(),
      edited_at: null,
      reply_to_message_id: null,
      metadata: { client_request_id: clientRequestId },
      operator_authored: true,
      delivery_status: "sending"
    };
    setDrafts((current) => ({ ...current, [sentChatKey]: "" }));
    setMessages((current) => mergeTelegramMessages(current, [optimistic]));
    setSending(true);
    setSendError("");
    stickToBottomRef.current = true;
    try {
      const path = telegramMessagesPath(selectedChat.realm_id, selectedChat.chat_id);
      const payload = await requestJson<SendMessageResponse>(path, {
        method: "POST",
        body: JSON.stringify({
          content,
          client_request_id: clientRequestId
        })
      });
      const deliveredMessage = isTelegramMessage(payload.message) ? payload.message : null;
      const send = telegramSend(payload.send);
      if (!deliveredMessage && !send) throw new Error("Сервер не подтвердил отправку сообщения");
      const replacement: TelegramMessage =
        deliveredMessage || {
          ...optimistic,
          id: send?.id || pendingId,
          metadata: {
            ...optimistic.metadata,
            ...(send?.id ? { operator_send_id: send.id } : {})
          },
          delivery_status: send?.status || "pending"
        };
      if (selectedKeyRef.current === sentChatKey) {
        setMessages((current) =>
          mergeTelegramMessages(
            current.filter(
              (message) => message.id !== pendingId && (!send?.id || message.id !== send.id)
            ),
            [replacement]
          )
        );
      }
      showNotice(
        deliveredMessage
          ? "Сообщение отправлено от имени Jarvis."
          : "Сообщение принято Telegram-шлюзом."
      );
      void loadChats(true);
      void loadThread(true);
    } catch (error) {
      const failedSend = sendFromError(error);
      const failedStatus = failedDeliveryStatus(error, failedSend);
      const failedMessage: TelegramMessage = {
        ...optimistic,
        id: failedSend?.id || pendingId,
        metadata: {
          ...optimistic.metadata,
          ...(failedSend?.id ? { operator_send_id: failedSend.id } : {})
        },
        delivery_status: failedStatus
      };
      if (selectedKeyRef.current === sentChatKey) {
        setMessages((current) =>
          mergeTelegramMessages(
            current.filter(
              (message) =>
                message.id !== pendingId && (!failedSend?.id || message.id !== failedSend.id)
            ),
            [failedMessage]
          )
        );
      }
      if (failedStatus !== "uncertain") {
        setDrafts((current) => ({
          ...current,
          [sentChatKey]: current[sentChatKey] || content
        }));
      }
      const message = error instanceof Error ? error.message : "Не удалось отправить сообщение";
      setSendError(
        failedStatus === "uncertain"
          ? message + ". Итог доставки неизвестен; проверьте переписку перед повтором."
          : message
      );
      void loadThread(true);
    } finally {
      setSending(false);
    }
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key !== "Enter" ||
      event.shiftKey ||
      event.nativeEvent.isComposing ||
      sending
    ) {
      return;
    }
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  const synchronizationError = chatError || threadError;

  return (
    <main className={styles.page}>
      <header className={styles.pageHeader}>
        <div className={styles.heading}>
          <span className={styles.brandIcon}><MessageSquare size={22} /></span>
          <div>
            <p>Jarvis Telegram</p>
            <h1>Сообщения</h1>
          </div>
        </div>
        <div className={styles.pageActions}>
          <span
            className={classNames(
              styles.syncState,
              synchronizationError ? styles.syncError : styles.syncOk
            )}
            title={synchronizationError || undefined}
          >
            {synchronizationError ? <WifiOff size={14} /> : <Wifi size={14} />}
            {synchronizationError
              ? "Ошибка синхронизации"
              : lastSyncedAt
                ? "Обновлено " + formatTime(lastSyncedAt)
                : "Подключение…"}
          </span>
          <Link className={classNames(styles.secondaryButton, styles.centerLink)} href="/">
            <ArrowLeft size={15} /> Центр
          </Link>
          <button
            className={styles.secondaryButton}
            disabled={refreshing}
            onClick={() => void refreshAll()}
            type="button"
          >
            <RefreshCw className={refreshing ? styles.spin : ""} size={15} />
            Обновить
          </button>
        </div>
      </header>

      {notice ? <div className={styles.notice} role="status"><Check size={15} />{notice}</div> : null}
      {synchronizationError ? (
        <div className={styles.errorBanner} role="alert">
          <CircleAlert size={15} />
          <span>{synchronizationError}</span>
        </div>
      ) : null}

      <section
        className={classNames(styles.console, mobileThreadOpen && styles.mobileThreadOpen)}
        aria-label="Telegram-консоль"
      >
        <aside className={styles.chatPane}>
          <div className={styles.chatPaneHeader}>
            <div className={styles.listTitle}>
              <div>
                <h2>Чаты</h2>
                <span>{chats.length} из {totalChats}</span>
              </div>
              {chatsLoading ? <Loader2 className={styles.spin} size={16} /> : null}
            </div>
            <label className={styles.searchBox}>
              <Search size={15} />
              <input
                aria-label="Поиск Telegram-чатов"
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Имя, @username, Telegram ID…"
                type="search"
                value={search}
              />
            </label>
          </div>
          <div className={styles.chatList}>
            {chatsLoading && chats.length === 0 ? (
              <div className={styles.emptyState}><Loader2 className={styles.spin} size={20} />Загрузка чатов…</div>
            ) : null}
            {!chatsLoading && chats.length === 0 ? (
              <div className={styles.emptyState}><UserRound size={24} />Чаты не найдены.</div>
            ) : null}
            {chats.map((chat) => {
              const title = chatTitle(chat);
              return (
                <button
                  aria-pressed={selectedKey === chat.key}
                  className={classNames(
                    styles.chatRow,
                    selectedKey === chat.key && styles.selectedChat
                  )}
                  key={chat.key}
                  onClick={() => selectChat(chat)}
                  type="button"
                >
                  <span className={styles.avatar} aria-hidden="true">{initials(title)}</span>
                  <span className={styles.chatSummary}>
                    <span className={styles.chatTitleLine}>
                      <strong>{title}</strong>
                      <time dateTime={chat.last_message_at || chat.updated_at || undefined}>
                        {formatListTime(chat.last_message_at || chat.updated_at)}
                      </time>
                    </span>
                    <span className={styles.chatPreview}>
                      <span>{chat.last_message || "Сообщений пока нет"}</span>
                      <small>{chat.message_count || 0}</small>
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </aside>

        <section className={styles.threadPane}>
          {!selectedChat ? (
            <div className={styles.emptyThread}>
              <MessageSquare size={34} />
              <h2>Выберите чат</h2>
              <p>Здесь появится переписка пользователя с Jarvis.</p>
            </div>
          ) : (
            <>
              <header className={styles.threadHeader}>
                <button
                  aria-label="Вернуться к списку чатов"
                  className={styles.mobileBack}
                  onClick={() => setMobileThreadOpen(false)}
                  type="button"
                >
                  <ArrowLeft size={19} />
                </button>
                <span className={styles.avatar} aria-hidden="true">
                  {initials(chatTitle(selectedChat))}
                </span>
                <div className={styles.threadIdentity}>
                  <strong>{chatTitle(selectedChat)}</strong>
                  <span>{chatSubtitle(selectedChat)}</span>
                </div>
                <div className={styles.threadBadges}>
                  {selectedChat.status ? <span>{selectedChat.status}</span> : null}
                  <span>{selectedChat.message_count || messages.length} сообщений</span>
                </div>
              </header>

              <div
                aria-busy={threadLoading || olderLoading}
                className={styles.messageViewport}
                id="telegram-message-log"
                onScroll={handleMessageScroll}
                ref={messageViewportRef}
                role="log"
              >
                {threadHasMore && nextBefore ? (
                  <div className={styles.historyLoader}>
                    <button
                      aria-controls="telegram-message-log"
                      aria-label="Загрузить более ранние сообщения"
                      disabled={olderLoading}
                      onClick={() => void loadOlderMessages()}
                      type="button"
                    >
                      {olderLoading ? <Loader2 className={styles.spin} size={14} /> : null}
                      {olderLoading ? "Загрузка…" : "Загрузить более ранние сообщения"}
                    </button>
                  </div>
                ) : messages.length > 0 && !threadLoading ? (
                  <div className={styles.historyStart}>Начало переписки</div>
                ) : null}
                {threadLoading && messages.length === 0 ? (
                  <div className={styles.emptyState}><Loader2 className={styles.spin} size={20} />Загрузка переписки…</div>
                ) : null}
                {!threadLoading && messages.length === 0 ? (
                  <div className={styles.emptyState}><MessageSquare size={24} />В этом чате пока нет сообщений.</div>
                ) : null}
                <div className={styles.messageList}>
                  {messages.map((message, index) => {
                    const previous = messages[index - 1];
                    const showDay =
                      !previous || dayIdentity(previous.created_at) !== dayIdentity(message.created_at);
                    const system = message.role === "system" || message.direction === "system";
                    const outbound =
                      !system &&
                      (message.direction === "outbound" || message.role === "assistant");
                    const failed = ["failed", "uncertain"].includes(
                      message.delivery_status?.toLowerCase() || ""
                    );
                    return (
                      <Fragment key={message.id}>
                        {showDay ? <div className={styles.daySeparator}>{formatDay(message.created_at)}</div> : null}
                        <article
                          className={classNames(
                            styles.messageRow,
                            outbound && styles.outbound,
                            system && styles.systemMessage
                          )}
                          data-message-id={message.id}
                        >
                          <div className={classNames(styles.bubble, failed && styles.failedBubble)}>
                            {message.reply_to_message_id ? (
                              <span className={styles.replyMarker}>Ответ на {message.reply_to_message_id}</span>
                            ) : null}
                            {message.operator_authored ? (
                              <span className={styles.operatorBadge}>Оператор</span>
                            ) : null}
                            <p>{message.content || "Пустое сообщение"}</p>
                            <footer>
                              {message.edited_at ? <span>изменено</span> : null}
                              <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
                              {outbound && message.delivery_status ? (
                                <span className={classNames(styles.delivery, failed && styles.deliveryFailed)}>
                                  {deliveryLabel(message.delivery_status)}
                                </span>
                              ) : null}
                            </footer>
                          </div>
                        </article>
                      </Fragment>
                    );
                  })}
                </div>
                {awayFromBottom ? (
                  <button
                    className={styles.jumpButton}
                    onClick={() => scrollToBottom()}
                    type="button"
                  >
                    К новым сообщениям
                  </button>
                ) : null}
              </div>

              <form className={styles.composer} onSubmit={sendMessage}>
                {sendError ? <div className={styles.sendError} role="alert">{sendError}</div> : null}
                <div className={styles.composerRow}>
                  <textarea
                    aria-label="Сообщение от имени Jarvis"
                    disabled={sending}
                    maxLength={MAX_MESSAGE_LENGTH}
                    onChange={(event) => updateDraft(event.target.value)}
                    onKeyDown={handleComposerKeyDown}
                    placeholder="Написать от имени Jarvis…"
                    rows={1}
                    value={draft}
                  />
                  <span className={styles.counter}>{draft.length}/{MAX_MESSAGE_LENGTH}</span>
                  <button
                    aria-label="Отправить сообщение"
                    className={styles.sendButton}
                    disabled={sending || !draft.trim()}
                    title="Отправить (Enter)"
                    type="submit"
                  >
                    {sending ? <Loader2 className={styles.spin} size={19} /> : <Send size={19} />}
                  </button>
                </div>
                <small>Enter — отправить, Shift+Enter — новая строка</small>
              </form>
            </>
          )}
        </section>
      </section>
    </main>
  );
}
