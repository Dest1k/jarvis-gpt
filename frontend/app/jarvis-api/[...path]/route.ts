import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-type",
  "if-match",
  "if-none-match",
  "if-modified-since",
  "range"
];
const FORWARDED_RESPONSE_HEADERS = [
  "accept-ranges",
  "cache-control",
  "content-disposition",
  "content-length",
  "content-range",
  "content-type",
  "etag",
  "last-modified"
];
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

type RouteContext = { params: Promise<{ path: string[] }> };

function crossSiteMutation(request: NextRequest) {
  if (!UNSAFE_METHODS.has(request.method.toUpperCase())) return false;
  if ((request.headers.get("sec-fetch-site") ?? "").toLowerCase() === "cross-site") return true;
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try {
    return new URL(origin).origin !== request.nextUrl.origin;
  } catch {
    return true;
  }
}

async function forward(request: NextRequest, context: RouteContext) {
  if (crossSiteMutation(request)) {
    return NextResponse.json({ detail: "Cross-site API mutation rejected." }, { status: 403 });
  }

  const backend = (process.env.JARVIS_BACKEND_URL ?? "http://127.0.0.1:8000").trim();
  const token = (process.env.JARVIS_API_TOKEN ?? "").trim();
  let base: URL;
  try {
    base = new URL(backend);
  } catch {
    return NextResponse.json({ detail: "Invalid JARVIS_BACKEND_URL." }, { status: 500 });
  }
  if (!new Set(["http:", "https:"]).has(base.protocol)) {
    return NextResponse.json({ detail: "Unsupported backend URL scheme." }, { status: 500 });
  }

  const { path } = await context.params;
  if (path.some((part) => !part || part === "." || part === ".." || /[\\/\0]/.test(part))) {
    return NextResponse.json({ detail: "Invalid backend path." }, { status: 400 });
  }
  const encodedPath = path.map((part) => encodeURIComponent(part)).join("/");
  const upstream = new URL(encodedPath, `${base.toString().replace(/\/$/, "")}/`);
  upstream.search = request.nextUrl.search;

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (token) headers.set("X-Jarvis-Api-Token", token);

  const init: RequestInit & { duplex?: "half" } = {
    method: request.method,
    headers,
    redirect: "manual",
    cache: "no-store",
    signal: request.signal
  };
  if (!new Set(["GET", "HEAD"]).has(request.method.toUpperCase()) && request.body) {
    init.body = request.body;
    init.duplex = "half";
  }

  try {
    const response = await fetch(upstream, init);
    const responseHeaders = new Headers();
    for (const name of FORWARDED_RESPONSE_HEADERS) {
      const value = response.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    return new NextResponse(response.body, {
      status: response.status,
      headers: responseHeaders
    });
  } catch (error) {
    return NextResponse.json(
      {
        detail: "Jarvis backend is unavailable.",
        error: error instanceof Error ? error.message : String(error)
      },
      { status: 502 }
    );
  }
}

export const GET = forward;
export const HEAD = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
