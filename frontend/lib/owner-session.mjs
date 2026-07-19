import {
  createHash,
  createHmac,
  randomBytes,
  timingSafeEqual
} from "node:crypto";

export const OWNER_SESSION_COOKIE = "jarvis_owner_session";
export const OWNER_SESSION_TTL_SECONDS = 8 * 60 * 60;

const SESSION_VERSION = 1;
const CLOCK_SKEW_SECONDS = 60;
const MAX_COOKIE_LENGTH = 2048;
const SIGNING_CONTEXT = "jarvis-owner-ui-session-signing-v1";

function digest(value) {
  return createHash("sha256").update(value, "utf8").digest();
}

/** Compare credentials without an early exit based on their contents or length. */
export function ownerCredentialMatches(candidate, expected) {
  if (typeof candidate !== "string" || typeof expected !== "string" || !expected) {
    return false;
  }
  return timingSafeEqual(digest(candidate), digest(expected));
}

function sessionSigningKey(apiToken, explicitSecret = "") {
  const secret = explicitSecret.trim();
  // The API token always remains key material, so a weak optional secret can
  // never weaken an otherwise strong API credential.
  return createHmac("sha256", apiToken)
    .update(SIGNING_CONTEXT, "utf8")
    .update("\0", "utf8")
    .update(secret, "utf8")
    .digest();
}

function signatureFor(encodedPayload, apiToken, explicitSecret = "") {
  return createHmac("sha256", sessionSigningKey(apiToken, explicitSecret))
    .update(encodedPayload, "ascii")
    .digest();
}

function canonicalBase64Url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) return false;
  try {
    return Buffer.from(value, "base64url").toString("base64url") === value;
  } catch {
    return false;
  }
}

export function createOwnerSession(
  apiToken,
  explicitSecret = "",
  nowSeconds = Math.floor(Date.now() / 1000)
) {
  if (!apiToken) throw new Error("JARVIS_API_TOKEN is required");
  const issuedAt = Math.floor(nowSeconds);
  const payload = {
    v: SESSION_VERSION,
    sub: "owner",
    iat: issuedAt,
    exp: issuedAt + OWNER_SESSION_TTL_SECONDS,
    nonce: randomBytes(18).toString("base64url")
  };
  const encodedPayload = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  const signature = signatureFor(encodedPayload, apiToken, explicitSecret).toString("base64url");
  return `${encodedPayload}.${signature}`;
}

export function verifyOwnerSession(
  cookieValue,
  apiToken,
  explicitSecret = "",
  nowSeconds = Math.floor(Date.now() / 1000)
) {
  if (
    typeof cookieValue !== "string" ||
    !apiToken ||
    cookieValue.length === 0 ||
    cookieValue.length > MAX_COOKIE_LENGTH
  ) {
    return false;
  }

  const parts = cookieValue.split(".");
  if (parts.length !== 2 || !parts.every(canonicalBase64Url)) return false;
  const [encodedPayload, encodedSignature] = parts;

  let suppliedSignature;
  try {
    suppliedSignature = Buffer.from(encodedSignature, "base64url");
  } catch {
    return false;
  }
  const expectedSignature = signatureFor(encodedPayload, apiToken, explicitSecret);
  if (
    suppliedSignature.length !== expectedSignature.length ||
    !timingSafeEqual(suppliedSignature, expectedSignature)
  ) {
    return false;
  }

  let payload;
  try {
    payload = JSON.parse(Buffer.from(encodedPayload, "base64url").toString("utf8"));
  } catch {
    return false;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;

  const now = Math.floor(nowSeconds);
  return (
    payload.v === SESSION_VERSION &&
    payload.sub === "owner" &&
    Number.isInteger(payload.iat) &&
    Number.isInteger(payload.exp) &&
    typeof payload.nonce === "string" &&
    /^[A-Za-z0-9_-]{24}$/.test(payload.nonce) &&
    payload.iat <= now + CLOCK_SKEW_SECONDS &&
    payload.exp > now &&
    payload.exp === payload.iat + OWNER_SESSION_TTL_SECONDS
  );
}

/** A small process-local limiter; bounded storage prevents attacker-driven growth. */
export class BoundedLoginRateLimiter {
  constructor({ limit = 8, windowMs = 10 * 60 * 1000, maxKeys = 2048 } = {}) {
    this.limit = limit;
    this.windowMs = windowMs;
    this.maxKeys = maxKeys;
    this.entries = new Map();
  }

  consume(key, nowMs = Date.now()) {
    const normalizedKey = digest(String(key || "unknown")).toString("hex");
    this.prune(nowMs);
    let entry = this.entries.get(normalizedKey);
    if (!entry || entry.resetAt <= nowMs) {
      if (!entry && this.entries.size >= this.maxKeys) {
        const oldestKey = this.entries.keys().next().value;
        if (oldestKey !== undefined) this.entries.delete(oldestKey);
      }
      entry = { count: 0, resetAt: nowMs + this.windowMs };
    } else {
      // Reinsert to make Map order an inexpensive least-recently-used order.
      this.entries.delete(normalizedKey);
    }
    entry.count += 1;
    this.entries.set(normalizedKey, entry);
    return {
      allowed: entry.count <= this.limit,
      retryAfterSeconds: Math.max(1, Math.ceil((entry.resetAt - nowMs) / 1000))
    };
  }

  reset(key) {
    this.entries.delete(digest(String(key || "unknown")).toString("hex"));
  }

  prune(nowMs = Date.now()) {
    for (const [key, entry] of this.entries) {
      if (entry.resetAt <= nowMs) this.entries.delete(key);
    }
  }
}
