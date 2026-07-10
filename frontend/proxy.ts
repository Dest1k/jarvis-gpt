import { timingSafeEqual } from "node:crypto";

import { NextRequest, NextResponse } from "next/server";


function authorized(request: NextRequest, token: string) {
  const supplied = Buffer.from(request.headers.get("authorization") ?? "", "utf8");
  const expected = Buffer.from(`Basic ${Buffer.from(`jarvis:${token}`).toString("base64")}`, "utf8");
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}


export function proxy(request: NextRequest) {
  const token = (process.env.JARVIS_API_TOKEN ?? "").trim();
  if (!token) {
    return new NextResponse("JARVIS_API_TOKEN is required before Command Center can start.", {
      status: 503,
      headers: { "Cache-Control": "no-store" }
    });
  }
  if (authorized(request, token)) {
    return NextResponse.next();
  }
  return new NextResponse("Jarvis authentication required.", {
    status: 401,
    headers: {
      "Cache-Control": "no-store",
      "WWW-Authenticate": 'Basic realm="Jarvis Command Center", charset="UTF-8"'
    }
  });
}


export const config = {
  matcher: "/:path*"
};
