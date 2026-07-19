import { NextRequest, NextResponse } from "next/server";

import { OWNER_SESSION_COOKIE } from "../../../../lib/owner-session.mjs";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

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

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) {
    return NextResponse.json(
      { detail: "Cross-site logout rejected." },
      { status: 403, headers: { "Cache-Control": "no-store" } }
    );
  }
  const response = NextResponse.json(
    { ok: true },
    { status: 200, headers: { "Cache-Control": "no-store" } }
  );
  response.cookies.set({
    name: OWNER_SESSION_COOKIE,
    value: "",
    httpOnly: true,
    secure: isHttps(request),
    sameSite: "strict",
    path: "/",
    maxAge: 0,
    expires: new Date(0)
  });
  return response;
}
