"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

import styles from "../owner-session.module.css";

function destinationAfterLogin() {
  const requested = new URLSearchParams(window.location.search).get("next") || "/";
  try {
    const destination = new URL(requested, window.location.origin);
    if (destination.origin !== window.location.origin || destination.pathname === "/login") {
      return "/";
    }
    return `${destination.pathname}${destination.search}${destination.hash}`;
  } catch {
    return "/";
  }
}

export default function LoginForm() {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || working) return;
    setWorking(true);
    setError("");
    try {
      const response = await fetch("/api/ui-session/login", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token })
      });
      const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
      if (!response.ok) {
        throw new Error(payload?.detail || `Ошибка входа (${response.status})`);
      }
      setToken("");
      window.location.assign(destinationAfterLogin());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось выполнить вход");
      setWorking(false);
    }
  }

  return (
    <main className={styles.loginShell}>
      <section className={styles.loginCard} aria-labelledby="login-title">
        <div className={styles.mark} aria-hidden="true">J</div>
        <p className={styles.eyebrow}>JARVIS COMMAND CENTER</p>
        <h1 id="login-title">Вход владельца</h1>
        <p className={styles.explanation}>
          Введите серверный ключ владельца. Он проверяется на сервере и не сохраняется в браузере.
        </p>
        <form onSubmit={submit} className={styles.loginForm}>
          <label htmlFor="owner-token">Ключ владельца</label>
          <input
            id="owner-token"
            name="owner-token"
            type="password"
            autoComplete="current-password"
            autoFocus
            maxLength={4096}
            required
            value={token}
            onChange={(event) => setToken(event.target.value)}
            disabled={working}
          />
          {error ? <p className={styles.error} role="alert">{error}</p> : null}
          <button type="submit" disabled={working || !token}>
            {working ? "Проверка…" : "Войти"}
          </button>
        </form>
        <Link href="/" className={styles.backLink}>Вернуться на главную</Link>
      </section>
    </main>
  );
}
