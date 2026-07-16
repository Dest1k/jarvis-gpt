const CHAT_WINDOWS_KEY_BASE = "jarvis.chatWindows.v1";
const CHAT_SETTINGS_KEY_BASE = "jarvis.chatSettings.v1";

/** Stable client identity for browser storage (FUNC-FIND-015 / SPARK-0015). */
export function runtimeClientIdentity(home, profileName) {
  return `${String(home || "").trim().toLowerCase()}::${String(profileName || "").trim().toLowerCase()}`;
}

export function scopedChatWindowsKey(identity) {
  return `${CHAT_WINDOWS_KEY_BASE}::${encodeURIComponent(identity)}`;
}

export function scopedChatSettingsKey(identity) {
  return `${CHAT_SETTINGS_KEY_BASE}::${encodeURIComponent(identity)}`;
}

/** Empty pending assistant bubbles must not survive stream teardown (FUNC-FIND-011). */
export function isEmptyAssistantPlaceholder(line) {
  if (!line || line.role !== "assistant") return false;
  const content = String(line.content ?? "").trim();
  // A stream that ends without done/error may already have finalized the
  // placeholder and assigned a positive elapsed duration. It is still an
  // empty assistant bubble and must never survive teardown.
  return !content;
}

export function withoutEmptyAssistantPlaceholders(lines) {
  return lines.filter((line) => !isEmptyAssistantPlaceholder(line));
}
