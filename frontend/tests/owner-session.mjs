import assert from "node:assert/strict";

import {
  BoundedLoginRateLimiter,
  createOwnerSession,
  OWNER_SESSION_TTL_SECONDS,
  ownerCredentialMatches,
  verifyOwnerSession
} from "../lib/owner-session.mjs";

const token = "owner-token-should-never-be-in-a-cookie";
const secret = "independent-session-secret-with-enough-entropy";
const now = 1_800_000_000;

assert.equal(ownerCredentialMatches(token, token), true);
assert.equal(ownerCredentialMatches(`${token}-wrong`, token), false);
assert.equal(ownerCredentialMatches("", token), false);
assert.equal(ownerCredentialMatches(token, ""), false);

const session = createOwnerSession(token, secret, now);
assert.equal(session.includes(token), false);
assert.equal(verifyOwnerSession(session, token, secret, now), true);
assert.equal(verifyOwnerSession(session, token, secret, now + OWNER_SESSION_TTL_SECONDS - 1), true);
assert.equal(verifyOwnerSession(session, token, secret, now + OWNER_SESSION_TTL_SECONDS), false);
assert.equal(verifyOwnerSession(session, `${token}-rotated`, secret, now), false);
assert.equal(verifyOwnerSession(session, token, `${secret}-rotated`, now), false);

const [payload, signature] = session.split(".");
const tamperedPayload = `${payload.slice(0, -1)}${payload.endsWith("A") ? "B" : "A"}`;
const tamperedSignature = `${signature.startsWith("A") ? "B" : "A"}${signature.slice(1)}`;
const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const finalIndex = alphabet.indexOf(signature.at(-1));
const nonCanonicalSignature = `${signature.slice(0, -1)}${alphabet[finalIndex + 1]}`;
assert.equal(verifyOwnerSession(`${tamperedPayload}.${signature}`, token, secret, now), false);
assert.equal(verifyOwnerSession(`${payload}.${tamperedSignature}`, token, secret, now), false);
assert.equal(
  Buffer.from(nonCanonicalSignature, "base64url").equals(Buffer.from(signature, "base64url")),
  true
);
assert.equal(verifyOwnerSession(`${payload}.${nonCanonicalSignature}`, token, secret, now), false);
assert.equal(verifyOwnerSession("not-a-session", token, secret, now), false);

const derivedKeySession = createOwnerSession(token, "", now);
assert.equal(verifyOwnerSession(derivedKeySession, token, "", now), true);
assert.equal(verifyOwnerSession(derivedKeySession, `${token}-rotated`, "", now), false);

const limiter = new BoundedLoginRateLimiter({ limit: 2, windowMs: 1000, maxKeys: 2 });
assert.equal(limiter.consume("client-a", 0).allowed, true);
assert.equal(limiter.consume("client-a", 1).allowed, true);
assert.equal(limiter.consume("client-a", 2).allowed, false);
limiter.reset("client-a");
assert.equal(limiter.consume("client-a", 3).allowed, true);
limiter.consume("client-b", 4);
limiter.consume("client-c", 5);
assert.ok(limiter.entries.size <= 2);
assert.equal(limiter.consume("client-c", 1006).allowed, true);

console.log("owner-session-ok");
