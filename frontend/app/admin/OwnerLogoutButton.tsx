"use client";

import { useState } from "react";

import styles from "../owner-session.module.css";

export default function OwnerLogoutButton() {
  const [working, setWorking] = useState(false);

  async function logout() {
    if (working) return;
    setWorking(true);
    try {
      await fetch("/api/ui-session/logout", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store"
      });
    } finally {
      window.location.assign("/login");
    }
  }

  return (
    <button className={styles.logoutButton} type="button" onClick={logout} disabled={working}>
      {working ? "Выход…" : "Выйти"}
    </button>
  );
}
