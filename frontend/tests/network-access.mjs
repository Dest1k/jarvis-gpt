import assert from "node:assert/strict";

import {
  isIPv4Allowed,
  normalizeIPv4,
  parseIPv4Cidr,
} from "../lib/network-access.mjs";

const allowedCidrs = [
  parseIPv4Cidr("127.0.0.0/8"),
  parseIPv4Cidr("192.168.31.0/24"),
];

assert.equal(normalizeIPv4("::ffff:192.168.31.50"), "192.168.31.50");
assert.equal(normalizeIPv4("::1"), null);
assert.equal(isIPv4Allowed("127.0.0.1", allowedCidrs), true);
assert.equal(isIPv4Allowed("::ffff:127.0.0.1", allowedCidrs), true);
assert.equal(isIPv4Allowed("192.168.31.1", allowedCidrs), true);
assert.equal(isIPv4Allowed("192.168.31.145", allowedCidrs), true);
assert.equal(isIPv4Allowed("192.168.31.254", allowedCidrs), true);
assert.equal(isIPv4Allowed("192.168.30.255", allowedCidrs), false);
assert.equal(isIPv4Allowed("192.168.32.1", allowedCidrs), false);
assert.equal(isIPv4Allowed("10.0.0.1", allowedCidrs), false);
assert.equal(isIPv4Allowed("not-an-address", allowedCidrs), false);
assert.equal(isIPv4Allowed(undefined, allowedCidrs), false);
assert.equal(isIPv4Allowed("203.0.113.20", [parseIPv4Cidr("0.0.0.0/0")]), true);

for (const invalidCidr of [
  "",
  "192.168.31.0",
  "192.168.31.0/",
  "192.168.31.0/33",
  "::1/128",
]) {
  assert.throws(() => parseIPv4Cidr(invalidCidr));
}

console.log("network-access tests passed");
