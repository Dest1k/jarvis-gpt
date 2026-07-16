import assert from "node:assert/strict";

import {
  isEmptyAssistantPlaceholder,
  withoutEmptyAssistantPlaceholders
} from "../lib/runtime-helpers.mjs";

const before = [
  { role: "user", content: "hi" },
  { role: "assistant", content: "", pending: true, durationMs: 0 },
  { role: "assistant", content: "ok", pending: false, durationMs: 12 }
];
const after = withoutEmptyAssistantPlaceholders(before);

assert.equal(after.length, 2);
assert.equal(after.some(isEmptyAssistantPlaceholder), false);
assert.equal(
  withoutEmptyAssistantPlaceholders([{ role: "assistant", content: "x", pending: true }]).length,
  1
);
assert.equal(isEmptyAssistantPlaceholder({ role: "user", content: "", pending: true }), false);
assert.equal(
  withoutEmptyAssistantPlaceholders([
    { role: "assistant", content: "", pending: false, durationMs: 137 }
  ]).length,
  0
);
console.log("stream-placeholder-ok");
