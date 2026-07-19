import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { OWNER_SESSION_COOKIE, verifyOwnerSession } from "../../lib/owner-session.mjs";
import LoginForm from "./LoginForm";

export const dynamic = "force-dynamic";

type LoginPageProps = {
  searchParams: Promise<{ next?: string | string[] }>;
};

function localDestination(value: string | string[] | undefined) {
  const requested = Array.isArray(value) ? value[0] : value;
  if (!requested) return "/";
  try {
    const destination = new URL(requested, "http://jarvis.local");
    if (destination.origin !== "http://jarvis.local" || destination.pathname === "/login") {
      return "/";
    }
    return `${destination.pathname}${destination.search}${destination.hash}`;
  } catch {
    return "/";
  }
}

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const apiToken = (process.env.JARVIS_API_TOKEN ?? "").trim();
  const cookieStore = await cookies();
  if (
    apiToken &&
    verifyOwnerSession(
      cookieStore.get(OWNER_SESSION_COOKIE)?.value,
      apiToken,
      (process.env.JARVIS_UI_SESSION_SECRET ?? "").trim()
    )
  ) {
    const params = await searchParams;
    redirect(localDestination(params.next));
  }
  return <LoginForm />;
}
