import { NextRequest, NextResponse } from "next/server";

import {
  BoundedLoginRateLimiter,
  createOwnerSession,
  OWNER_SESSION_COOKIE,
  OWNER_SESSION_TTL_SECONDS,
  ownerCredentialMatches
} from "../../../../lib/owner-session.mjs";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 4096;
const limiter = new BoundedLoginRateLimiter();

function trustsProxyHeaders() {
  return (process.env.JARVIS_TRUST_PROXY_HEADERS ?? "").trim() === "1";
}

function requestOrigin(request: NextRequest) {
  const trusted = trustsProxyHeaders();
  const protocol = trusted
    ? request.headers.get("x-forwarded-proto")?.split(",", 1)[0]?.trim()
    : request.nextUrl.protocol.replace(/:$/, "");
  const host = trusted
    ? request.headers.get("x-forwarded-host")?.split(",", 1)[0]?.trim()
    : request.headers.get("host")?.trim();
  if (!host || !new Set(["http", "https"]).has(protocol ?? "")) return request.nextUrl.origin;
  try {
    return new URL(`${protocol}://${host}`).origin;
  } catch {
    return request.nextUrl.origin;
  }
}

function isHttps(request: NextRequest) {
  if (request.nextUrl.protocol === "https:") return true;
  if (!trustsProxyHeaders()) return false;
  return request.headers.get("x-forwarded-proto")?.split(",", 1)[0]?.trim() === "https";
}

function noStoreJson(body: Record<string, unknown>, status: number, headers?: HeadersInit) {
  return NextResponse.json(body, {
    status,
    headers: { "Cache-Control": "no-store", ...headers }
  });
}

function sameOrigin(request: NextRequest) {
  const fetchSite = (request.headers.get("sec-fetch-site") ?? "").toLowerCase();
  if (fetchSite && fetchSite !== "same-origin" && fetchSite !== "none") return false;
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try {
    return new URL(origin).origin === requestOrigin(request);
  } catch {
    return false;
  }
}

function requesterKey(request: NextRequest) {
  if (!trustsProxyHeaders()) return "direct-client";
  const forwarded = request.headers.get("x-forwarded-for")?.split(",", 1)[0]?.trim();
  const address = forwarded || request.headers.get("x-real-ip")?.trim() || "unknown-proxy-client";
  return address.slice(0, 128);
}

async function readCredential(request: NextRequest) {
  const contentType = (request.headers.get("content-type") ?? "").toLowerCase();
  if (!contentType.startsWith("application/json")) return null;
  const declaredLength = Number(request.headers.get("content-length") ?? 0);
  if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) return null;
  if (!request.body) return null;

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  } catch {
    return null;
  }

  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
    return typeof payload?.token === "string" && payload.token.length <= MAX_BODY_BYTES
      ? payload.token
      : null;
  } catch {
    return null;
  }
}

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) {
    return noStoreJson({ detail: "Cross-site login rejected." }, 403);
  }

  const apiToken = (process.env.JARVIS_API_TOKEN ?? "").trim();
  if (!apiToken) {
    return noStoreJson({ detail: "JARVIS_API_TOKEN is not configured." }, 503);
  }

  const key = requesterKey(request);
  const attempt = limiter.consume(key);
  if (!attempt.allowed) {
    return noStoreJson(
      { detail: "Too many login attempts. Try again later." },
      429,
      { "Retry-After": String(attempt.retryAfterSeconds) }
    );
  }

  const candidate = await readCredential(request);
  if (candidate === null || !ownerCredentialMatches(candidate, apiToken)) {
    return noStoreJson({ detail: "Invalid Command Center credential." }, 401);
  }

  limiter.reset(key);
  const session = createOwnerSession(
    apiToken,
    (process.env.JARVIS_UI_SESSION_SECRET ?? "").trim()
  );
  const response = noStoreJson({ ok: true, expires_in: OWNER_SESSION_TTL_SECONDS }, 200);
  response.cookies.set({
    name: OWNER_SESSION_COOKIE,
    value: session,
    httpOnly: true,
    secure: isHttps(request),
    sameSite: "strict",
    path: "/",
    maxAge: OWNER_SESSION_TTL_SECONDS
  });
  return response;
}
