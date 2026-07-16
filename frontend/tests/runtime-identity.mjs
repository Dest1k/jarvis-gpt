import assert from "node:assert/strict";

import {
  runtimeClientIdentity,
  scopedChatSettingsKey,
  scopedChatWindowsKey
} from "../lib/runtime-helpers.mjs";

const previous = runtimeClientIdentity("D:\\jarvis\\old", "gemma4-turbo");
const current = runtimeClientIdentity("D:\\jarvis\\new", "gemma4-turbo");

assert.notEqual(previous, current);
assert.notEqual(scopedChatWindowsKey(previous), scopedChatWindowsKey(current));
assert.equal(scopedChatWindowsKey(previous).includes(encodeURIComponent(previous)), true);
assert.equal(scopedChatSettingsKey(previous).includes(encodeURIComponent(previous)), true);
console.log("runtime-identity-ok");
