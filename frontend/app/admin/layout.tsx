import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { OWNER_SESSION_COOKIE, verifyOwnerSession } from "../../lib/owner-session.mjs";
import OwnerLogoutButton from "./OwnerLogoutButton";

export const dynamic = "force-dynamic";

export default async function AdminLayout({ children }: Readonly<{ children: ReactNode }>) {
  const apiToken = (process.env.JARVIS_API_TOKEN ?? "").trim();
  const cookieStore = await cookies();
  if (
    !apiToken ||
    !verifyOwnerSession(
      cookieStore.get(OWNER_SESSION_COOKIE)?.value,
      apiToken,
      (process.env.JARVIS_UI_SESSION_SECRET ?? "").trim()
    )
  ) {
    redirect("/login?next=/admin");
  }
  return (
    <>
      <OwnerLogoutButton />
      {children}
    </>
  );
}
