"use client";

import {
  ArrowLeft,
  Ban,
  Check,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Shield,
  ShieldCheck,
  UserRound,
  Users
} from "lucide-react";
import Link from "next/link";
import { useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";

import styles from "./admin.module.css";

const API = "/jarvis-api";
const CHANGE_REASON = "Изменено через web-панель администратора";
const USER_PAGE_SIZE = 50;

type AdminUser = {
  id: string;
  status: "pending" | "active" | "suspended" | "deleted";
  display_name?: string;
  preset_key?: string;
  provider?: string;
  provider_subject_id?: string;
  username?: string;
  first_name?: string;
  last_name?: string;
  last_seen_at?: string;
  created_at?: string;
};

type PermissionPreset = {
  preset_key: string;
  display_name: string;
  kind: "builtin" | "custom";
  version?: number;
  description?: string;
  security_ids?: string[];
};

type SecurityId = {
  security_id: string;
  description: string;
  category: string;
  risk_level: number;
  default_requires_hitl?: number | boolean;
  status: string;
};

type PermissionDecision = {
  security_id: string;
  effect: "allow" | "deny";
  reason_code: string;
  source?: string | null;
};

type SecurityAudit = {
  id: string;
  ts: string;
  actor_user_id?: string;
  action: string;
  target_type: string;
  target_id?: string;
  target_user_id?: string;
  reason?: string;
};

function asList<T>(value: unknown, key: string): T[] {
  if (Array.isArray(value)) return value as T[];
  if (value && typeof value === "object") {
    const nested = (value as Record<string, unknown>)[key];
    if (Array.isArray(nested)) return nested as T[];
  }
  return [];
}

function errorText(payload: unknown, fallback: string) {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = (payload as Record<string, unknown>).detail;
  if (typeof detail === "string") return detail;
  if (detail) return JSON.stringify(detail);
  return fallback;
}

async function requestJson(path: string, init?: RequestInit) {
  const baseHeaders = {
    ...(init?.body ? { "Content-Type": "application/json" } : {}),
    ...(init?.headers || {})
  };
  const requestInit: RequestInit = {
    ...init,
    cache: "no-store",
    headers: baseHeaders
  };
  let response = await fetch(`${API}${path}`, requestInit);
  let payload = await response.json().catch(() => null);
  const detail = payload && typeof payload === "object"
    ? (payload as Record<string, unknown>).detail
    : null;
  const approvalId = detail && typeof detail === "object"
    ? String((detail as Record<string, unknown>).approval_id || "")
    : "";
  if (response.status === 428 && approvalId) {
    if (!window.confirm("Операция повышенного риска. Подтвердить одноразовое разрешение?")) {
      throw new Error("Операция отменена пользователем.");
    }
    const approvalResponse = await fetch(
      `${API}/api/approvals/${encodeURIComponent(approvalId)}`,
      {
        method: "PATCH",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          status: "approved",
          result: { operator: "admin-web", confirmed_at: new Date().toISOString() }
        })
      }
    );
    if (!approvalResponse.ok) {
      const approvalPayload = await approvalResponse.json().catch(() => null);
      throw new Error(errorText(approvalPayload, `HTTP ${approvalResponse.status}`));
    }
    response = await fetch(`${API}${path}`, {
      ...requestInit,
      headers: {
        ...baseHeaders,
        "X-Jarvis-Approval-Id": approvalId
      }
    });
    payload = await response.json().catch(() => null);
  }
  if (!response.ok) {
    if (response.status === 401) {
      window.location.assign("/login?next=/admin");
    }
    throw new Error(errorText(payload, `HTTP ${response.status}`));
  }
  return payload;
}

function userTitle(user: AdminUser) {
  const fullName = [user.first_name, user.last_name].filter(Boolean).join(" ").trim();
  return fullName || user.display_name || (user.username ? `@${user.username}` : user.id);
}

function telegramLabel(user: AdminUser) {
  if (user.provider !== "telegram") return user.provider || "локальный";
  return user.username ? `@${user.username}` : `Telegram ${user.provider_subject_id || ""}`;
}

function formattedDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("ru-RU");
}

export default function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [matchingUsers, setMatchingUsers] = useState(0);
  const [totalUsers, setTotalUsers] = useState(0);
  const [telegramUsers, setTelegramUsers] = useState(0);
  const [inactiveUsers, setInactiveUsers] = useState(0);
  const [userPage, setUserPage] = useState(0);
  const [presets, setPresets] = useState<PermissionPreset[]>([]);
  const [securityIds, setSecurityIds] = useState<SecurityId[]>([]);
  const [permissions, setPermissions] = useState<PermissionDecision[]>([]);
  const [auditEntries, setAuditEntries] = useState<SecurityAudit[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [query, setQuery] = useState("");
  const [permissionQuery, setPermissionQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<string>("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [statusDraft, setStatusDraft] = useState<AdminUser["status"]>("active");
  const [presetDraft, setPresetDraft] = useState("");
  const [presetKey, setPresetKey] = useState("");
  const [presetName, setPresetName] = useState("");
  const [presetDescription, setPresetDescription] = useState("");
  const [presetPermissions, setPresetPermissions] = useState<string[]>([]);
  const [basePresetKey, setBasePresetKey] = useState("");
  const [expandedPreset, setExpandedPreset] = useState<string>("");
  const [showPresetForm, setShowPresetForm] = useState(false);
  const [showUserForm, setShowUserForm] = useState(false);
  const [userKind, setUserKind] = useState<"local" | "telegram">("local");
  const [userDisplayName, setUserDisplayName] = useState("");
  const [userPreset, setUserPreset] = useState("guest");
  const [userTelegramId, setUserTelegramId] = useState("");
  const [userUsername, setUserUsername] = useState("");
  const [serviceEnabled, setServiceEnabled] = useState(false);
  const [serviceMessage, setServiceMessage] = useState("");
  const [serviceUntil, setServiceUntil] = useState("");
  const deferredQuery = useDeferredValue(query);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [usersPayload, presetsPayload, securityPayload, auditPayload] = await Promise.all([
        requestJson(
          `/api/admin/users?limit=${USER_PAGE_SIZE}&offset=${userPage * USER_PAGE_SIZE}` +
          `&search=${encodeURIComponent(deferredQuery.trim())}`
        ),
        requestJson("/api/admin/presets"),
        requestJson("/api/admin/security-ids"),
        requestJson("/api/admin/audit?limit=50")
      ]);
      const nextUsers = asList<AdminUser>(usersPayload, "users");
      setUsers(nextUsers);
      const pageMeta = usersPayload && typeof usersPayload === "object"
        ? usersPayload as Record<string, unknown>
        : {};
      setMatchingUsers(Number(pageMeta.total) || 0);
      setTotalUsers(Number(pageMeta.overall_total) || 0);
      setTelegramUsers(Number(pageMeta.telegram_total) || 0);
      setInactiveUsers(Number(pageMeta.inactive_total) || 0);
      setPresets(asList<PermissionPreset>(presetsPayload, "presets"));
      setSecurityIds(asList<SecurityId>(securityPayload, "security_ids"));
      setAuditEntries(asList<SecurityAudit>(auditPayload, "audit"));
      setSelectedId((current) =>
        current && nextUsers.some((user) => user.id === current)
          ? current
          : nextUsers[0]?.id || ""
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [deferredQuery, userPage]);

  const loadPermissions = useCallback(async (userId: string) => {
    if (!userId) {
      setPermissions([]);
      return;
    }
    try {
      const payload = await requestJson(
        `/api/admin/users/${encodeURIComponent(userId)}/permissions`
      );
      setPermissions(asList<PermissionDecision>(payload, "permissions"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  const selected = useMemo(
    () => users.find((user) => user.id === selectedId) || null,
    [selectedId, users]
  );

  useEffect(() => {
    if (!selected) return;
    setStatusDraft(selected.status);
    setPresetDraft(selected.preset_key || "guest");
    void loadPermissions(selected.id);
  }, [loadPermissions, selected]);

  const decisions = useMemo(
    () => new Map(permissions.map((permission) => [permission.security_id, permission])),
    [permissions]
  );

  const filteredSecurityIds = useMemo(() => {
    const needle = permissionQuery.trim().toLocaleLowerCase("ru");
    return securityIds.filter((item) => {
      if (item.status !== "active") return false;
      return (
        !needle ||
        [item.security_id, item.description, item.category].some((value) =>
          value.toLocaleLowerCase("ru").includes(needle)
        )
      );
    });
  }, [permissionQuery, securityIds]);

  const allowedCount = permissions.filter((item) => item.effect === "allow").length;
  const totalUserPages = Math.max(1, Math.ceil(matchingUsers / USER_PAGE_SIZE));

  async function mutate(label: string, action: () => Promise<unknown>, refreshPermissions = false) {
    setWorking(label);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice("Изменение применено и записано в аудит.");
      await loadCatalog();
      if (refreshPermissions && selectedId) await loadPermissions(selectedId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setWorking("");
    }
  }

  async function saveStatus() {
    if (!selected || statusDraft === selected.status) return;
    if (
      statusDraft === "deleted" &&
      !window.confirm("Пометить пользователя удалённым и отозвать его активные сессии?")
    ) {
      return;
    }
    await mutate("status", () =>
      requestJson(`/api/admin/users/${encodeURIComponent(selected.id)}/status`, {
        method: "PATCH",
        body: JSON.stringify({ status: statusDraft, reason: CHANGE_REASON })
      })
    );
  }

  async function savePreset() {
    if (!selected || !presetDraft || presetDraft === selected.preset_key) return;
    await mutate(
      "preset",
      () =>
        requestJson(`/api/admin/users/${encodeURIComponent(selected.id)}/preset`, {
          method: "PUT",
          body: JSON.stringify({ preset_key: presetDraft, reason: CHANGE_REASON })
        }),
      true
    );
  }

  async function setOverride(securityId: string, effect: "grant" | "deny" | "inherit") {
    if (!selected) return;
    const path = `/api/admin/users/${encodeURIComponent(selected.id)}/permissions/${encodeURIComponent(securityId)}`;
    await mutate(
      `permission:${securityId}`,
      () =>
        effect === "inherit"
          ? requestJson(path, { method: "DELETE" })
          : requestJson(path, {
              method: "PUT",
              body: JSON.stringify({ effect, can_delegate: false, reason: CHANGE_REASON })
            }),
      true
    );
  }

  function togglePresetPermission(securityId: string) {
    setPresetPermissions((current) =>
      current.includes(securityId)
        ? current.filter((item) => item !== securityId)
        : [...current, securityId]
    );
  }

  async function createPreset() {
    if (!presetKey.trim() || !presetName.trim()) return;
    await mutate("create-preset", async () => {
      await requestJson("/api/admin/presets", {
        method: "POST",
        body: JSON.stringify({
          key: presetKey.trim(),
          name: presetName.trim(),
          description: presetDescription.trim(),
          security_ids: presetPermissions,
          base_preset_key: basePresetKey.trim() || null
        })
      });
      setPresetKey("");
      setPresetName("");
      setPresetDescription("");
      setPresetPermissions([]);
      setBasePresetKey("");
      setShowPresetForm(false);
    });
  }

  function applyBasePreset(key: string) {
    setBasePresetKey(key);
    const base = presets.find((item) => item.preset_key === key);
    if (!base?.security_ids?.length) return;
    setPresetPermissions((current) =>
      Array.from(new Set([...(base.security_ids || []), ...current]))
    );
  }

  async function createUserAccount() {
    await mutate("create-user", async () => {
      const body: Record<string, unknown> = {
        kind: userKind,
        display_name: userDisplayName.trim(),
        preset_key: userPreset || "guest",
        reason: CHANGE_REASON
      };
      if (userKind === "telegram") {
        const id = Number(userTelegramId.trim());
        if (!Number.isFinite(id) || id <= 0) {
          throw new Error("Укажите корректный Telegram user/chat id");
        }
        body.telegram_user_id = id;
        body.username = userUsername.trim() || null;
        body.first_name = userDisplayName.trim() || null;
      }
      await requestJson("/api/admin/users", {
        method: "POST",
        body: JSON.stringify(body)
      });
      setShowUserForm(false);
      setUserDisplayName("");
      setUserTelegramId("");
      setUserUsername("");
      setUserKind("local");
      setUserPreset("guest");
    });
  }

  async function deleteSelectedUser() {
    if (!selected) return;
    if (!window.confirm(`Удалить пользователя ${userTitle(selected)}?`)) return;
    await mutate("delete-user", async () => {
      await requestJson(`/api/admin/users/${selected.id}`, {
        method: "DELETE",
        body: JSON.stringify({ reason: CHANGE_REASON })
      });
      setSelectedId("");
    });
  }

  async function saveServiceMode() {
    await mutate("service-mode", async () => {
      await requestJson("/api/admin/runtime/service-mode", {
        method: "PUT",
        body: JSON.stringify({
          enabled: serviceEnabled,
          message: serviceMessage.trim(),
          until: serviceUntil.trim() || null,
          reason: CHANGE_REASON
        })
      });
    });
  }

  useEffect(() => {
    void (async () => {
      try {
        const payload = await requestJson("/api/admin/runtime/notices");
        if (payload && typeof payload === "object") {
          const mode = (payload as Record<string, unknown>).service_mode;
          if (mode && typeof mode === "object") {
            const m = mode as Record<string, unknown>;
            setServiceEnabled(Boolean(m.enabled));
            setServiceMessage(String(m.message || ""));
            setServiceUntil(String(m.until || ""));
          }
        }
      } catch {
        // Owner may not have loaded admin.runtime.service_mode yet on first paint.
      }
    })();
  }, []);

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleBlock}>
          <div className={styles.brandIcon}><ShieldCheck size={24} /></div>
          <div>
            <p>Jarvis Security</p>
            <h1>Пользователи и разрешения</h1>
          </div>
        </div>
        <div className={styles.headerActions}>
          <Link className={styles.secondaryButton} href="/"><ArrowLeft size={15} /> Центр</Link>
          <button className={styles.secondaryButton} onClick={() => void loadCatalog()} disabled={loading}>
            <RefreshCw className={loading ? styles.spin : ""} size={15} /> Обновить
          </button>
        </div>
      </header>

      <section className={styles.stats} aria-label="Сводка">
        <article><Users size={18} /><span>Всего пользователей</span><strong>{totalUsers}</strong></article>
        <article><UserRound size={18} /><span>Telegram</span><strong>{telegramUsers}</strong></article>
        <article><KeyRound size={18} /><span>Security ID</span><strong>{securityIds.length}</strong></article>
        <article className={inactiveUsers ? styles.warningStat : ""}>
          <Ban size={18} /><span>Неактивные</span><strong>{inactiveUsers}</strong>
        </article>
      </section>

      {error ? <div className={styles.errorBanner}>{error}</div> : null}
      {notice ? <div className={styles.noticeBanner}><Check size={15} /> {notice}</div> : null}

      <section className={`${styles.panel} ${styles.presetPanel}`}>
        <div className={styles.panelHeader}>
          <div>
            <h2>Режим техработ</h2>
            <span>Баннер в UI и ответ бота в Telegram / чате</span>
          </div>
        </div>
        <div className={styles.formFields}>
          <label>
            <span>Состояние</span>
            <select
              value={serviceEnabled ? "on" : "off"}
              onChange={(event) => setServiceEnabled(event.target.value === "on")}
            >
              <option value="off">Выключен</option>
              <option value="on">Включён (техработы)</option>
            </select>
          </label>
          <label>
            <span>До (ISO, опционально)</span>
            <input
              value={serviceUntil}
              onChange={(event) => setServiceUntil(event.target.value)}
              placeholder="2026-07-19T18:00:00+03:00"
            />
          </label>
          <label className={styles.fullField}>
            <span>Сообщение пользователям</span>
            <input
              value={serviceMessage}
              onChange={(event) => setServiceMessage(event.target.value)}
              placeholder="Ведутся технические работы…"
            />
          </label>
        </div>
        <div className={styles.formActions}>
          <button
            className={styles.primaryButton}
            disabled={working !== ""}
            onClick={() => void saveServiceMode()}
          >
            {working === "service-mode" ? <Loader2 className={styles.spin} size={15} /> : <Check size={15} />}
            Сохранить режим
          </button>
        </div>
      </section>

      <div className={styles.layout}>
        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div><h2>Учётные записи</h2><span>Локальные и Telegram, в т.ч. заранее добавленные</span></div>
            <div className={styles.headerActions}>
              <button className={styles.primaryButton} onClick={() => setShowUserForm((v) => !v)}>
                <Plus size={15} /> Добавить
              </button>
              <span className={styles.count}>{matchingUsers}</span>
            </div>
          </div>
          {showUserForm ? (
            <div className={styles.presetForm}>
              <div className={styles.formFields}>
                <label>
                  <span>Тип</span>
                  <select value={userKind} onChange={(event) => setUserKind(event.target.value as "local" | "telegram")}>
                    <option value="local">Локальная</option>
                    <option value="telegram">Telegram</option>
                  </select>
                </label>
                <label>
                  <span>Пресет</span>
                  <select value={userPreset} onChange={(event) => setUserPreset(event.target.value)}>
                    {presets.map((preset) => (
                      <option key={preset.preset_key} value={preset.preset_key}>
                        {preset.display_name} ({preset.preset_key})
                      </option>
                    ))}
                  </select>
                </label>
                <label className={styles.fullField}>
                  <span>Имя / display name</span>
                  <input value={userDisplayName} onChange={(event) => setUserDisplayName(event.target.value)} placeholder="Иван" />
                </label>
                {userKind === "telegram" ? (
                  <>
                    <label>
                      <span>Telegram ID</span>
                      <input value={userTelegramId} onChange={(event) => setUserTelegramId(event.target.value)} placeholder="123456789" />
                    </label>
                    <label>
                      <span>@username</span>
                      <input value={userUsername} onChange={(event) => setUserUsername(event.target.value)} placeholder="ivan" />
                    </label>
                  </>
                ) : null}
              </div>
              <div className={styles.formActions}>
                <button className={styles.secondaryButton} onClick={() => setShowUserForm(false)}>Отмена</button>
                <button
                  className={styles.primaryButton}
                  disabled={working !== "" || (userKind === "telegram" && !userTelegramId.trim())}
                  onClick={() => void createUserAccount()}
                >
                  {working === "create-user" ? <Loader2 className={styles.spin} size={15} /> : <Plus size={15} />}
                  Создать (guest по умолчанию)
                </button>
              </div>
            </div>
          ) : null}
          <label className={styles.searchBox}>
            <Search size={15} />
            <input
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setUserPage(0);
              }}
              placeholder="Имя, @username, Telegram ID…"
            />
          </label>
          <div className={styles.userList}>
            {loading ? <div className={styles.empty}><Loader2 className={styles.spin} size={20} /> Загрузка…</div> : null}
            {!loading && !users.length ? <div className={styles.empty}>Пользователи не найдены.</div> : null}
            {users.map((user) => (
              <button
                key={`${user.id}:${user.provider || "local"}:${user.provider_subject_id || ""}`}
                className={`${styles.userRow} ${selectedId === user.id ? styles.selected : ""}`}
                onClick={() => setSelectedId(user.id)}
              >
                <span className={`${styles.statusDot} ${styles[user.status]}`} />
                <span className={styles.userIdentity}>
                  <strong>{userTitle(user)}</strong>
                  <small>{telegramLabel(user)} · {formattedDate(user.last_seen_at)}</small>
                </span>
                <span className={styles.presetBadge}>{user.preset_key || "без роли"}</span>
              </button>
            ))}
          </div>
          <div className={styles.pagination}>
            <button
              disabled={loading || userPage === 0}
              onClick={() => setUserPage((current) => Math.max(0, current - 1))}
            >Назад</button>
            <span>{userPage + 1} / {totalUserPages}</span>
            <button
              disabled={loading || userPage + 1 >= totalUserPages}
              onClick={() => setUserPage((current) => current + 1)}
            >Далее</button>
          </div>
        </section>

        <section className={styles.panel}>
          {!selected ? (
            <div className={styles.emptyDetail}><Shield size={28} /><p>Выберите пользователя слева.</p></div>
          ) : (
            <>
              <div className={styles.panelHeader}>
                <div>
                  <h2>{userTitle(selected)}</h2>
                  <span>{telegramLabel(selected)} · ID {selected.id}</span>
                </div>
                <span className={`${styles.stateBadge} ${styles[selected.status]}`}>{selected.status}</span>
              </div>

              <div className={styles.controls}>
                <label>
                  <span>Состояние</span>
                  <div className={styles.inlineControl}>
                    <select value={statusDraft} onChange={(event) => setStatusDraft(event.target.value as AdminUser["status"])}>
                      <option value="active">active</option>
                      <option value="suspended">suspended</option>
                      <option value="deleted">deleted</option>
                    </select>
                    <button onClick={() => void saveStatus()} disabled={working !== "" || statusDraft === selected.status}>Сохранить</button>
                  </div>
                </label>
                <label>
                  <span>Пресет</span>
                  <div className={styles.inlineControl}>
                    <select value={presetDraft} onChange={(event) => setPresetDraft(event.target.value)}>
                      {presets.map((preset) => <option key={preset.preset_key} value={preset.preset_key}>{preset.display_name} ({preset.preset_key})</option>)}
                    </select>
                    <button onClick={() => void savePreset()} disabled={working !== "" || presetDraft === selected.preset_key}>Назначить</button>
                  </div>
                </label>
                <div className={styles.formActions}>
                  <button
                    className={styles.secondaryButton}
                    disabled={working !== "" || selected.preset_key === "owner"}
                    onClick={() => void deleteSelectedUser()}
                  >
                    <Ban size={15} /> Удалить пользователя
                  </button>
                </div>
              </div>

              <div className={styles.permissionHeader}>
                <div><h3>Эффективные права</h3><span>{allowedCount} из {permissions.length} разрешено</span></div>
                <label className={styles.compactSearch}><Search size={14} /><input value={permissionQuery} onChange={(event) => setPermissionQuery(event.target.value)} placeholder="Фильтр security_id" /></label>
              </div>
              <div className={styles.permissionList}>
                {filteredSecurityIds.map((item) => {
                  const decision = decisions.get(item.security_id);
                  const direct = decision?.reason_code === "direct_grant"
                    ? "grant"
                    : decision?.reason_code === "explicit_deny" && decision.source === "permission"
                      ? "deny"
                      : "inherit";
                  const busy = working === `permission:${item.security_id}`;
                  return (
                    <article key={item.security_id} className={styles.permissionRow}>
                      <div className={styles.permissionText}>
                        <div><code>{item.security_id}</code><span className={styles.category}>{item.category}</span>{item.default_requires_hitl ? <span className={styles.hitl}>HITL</span> : null}</div>
                        <p>{item.description}</p>
                      </div>
                      <span className={`${styles.effect} ${decision?.effect === "allow" ? styles.allow : styles.deny}`}>{decision?.effect || "deny"}</span>
                      <select
                        aria-label={`Прямое правило ${item.security_id}`}
                        value={direct}
                        disabled={busy || working !== ""}
                        onChange={(event) => void setOverride(item.security_id, event.target.value as "grant" | "deny" | "inherit")}
                      >
                        <option value="inherit">Из пресета</option>
                        <option value="grant">Разрешить</option>
                        <option value="deny">Запретить</option>
                      </select>
                    </article>
                  );
                })}
              </div>
            </>
          )}
        </section>
      </div>

      <section className={`${styles.panel} ${styles.presetPanel}`}>
        <div className={styles.panelHeader}>
          <div><h2>Пресеты разрешений</h2><span>Встроенные неизменяемы; пользовательские публикуются версионно</span></div>
          <button className={styles.primaryButton} onClick={() => setShowPresetForm((value) => !value)}><Plus size={15} /> Новый пресет</button>
        </div>
        <div className={styles.presetGrid}>
          {presets.map((preset) => {
            const expanded = expandedPreset === preset.preset_key;
            const ids = preset.security_ids || [];
            return (
              <article key={preset.preset_key} className={styles.presetCard}>
                <div>
                  <strong>{preset.display_name}</strong>
                  <code>{preset.preset_key}</code>
                </div>
                <p>
                  {preset.description ||
                    (preset.kind === "builtin" ? "Стандартный пресет Jarvis" : "Пользовательский пресет")}
                </p>
                <span>
                  {ids.length} прав · v{preset.version || 1} · {preset.kind}
                </span>
                <button
                  className={styles.secondaryButton}
                  type="button"
                  onClick={() =>
                    setExpandedPreset((current) =>
                      current === preset.preset_key ? "" : preset.preset_key
                    )
                  }
                >
                  {expanded ? "Скрыть права" : "Показать набор прав"}
                </button>
                {expanded ? (
                  <ul className={styles.presetIdList}>
                    {ids.length ? (
                      ids.map((securityId) => {
                        const meta = securityIds.find((item) => item.security_id === securityId);
                        return (
                          <li key={securityId}>
                            <code>{securityId}</code>
                            <small>{meta?.description || "—"}</small>
                          </li>
                        );
                      })
                    ) : (
                      <li><small>В пресете нет явных grant (owner имеет полный доступ отдельно).</small></li>
                    )}
                  </ul>
                ) : null}
              </article>
            );
          })}
        </div>

        {showPresetForm ? (
          <div className={styles.presetForm}>
            <div className={styles.formFields}>
              <label><span>Ключ</span><input value={presetKey} onChange={(event) => setPresetKey(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))} placeholder="researcher" /></label>
              <label><span>Название</span><input value={presetName} onChange={(event) => setPresetName(event.target.value)} placeholder="Исследователь" /></label>
              <label>
                <span>Базовый built-in / пресет</span>
                <select
                  value={basePresetKey}
                  onChange={(event) => applyBasePreset(event.target.value)}
                >
                  <option value="">— вручную, без базы —</option>
                  {presets.map((preset) => (
                    <option key={preset.preset_key} value={preset.preset_key}>
                      {preset.display_name} ({preset.preset_key}) · {preset.security_ids?.length || 0} прав
                    </option>
                  ))}
                </select>
              </label>
              <label className={styles.fullField}><span>Описание изменения</span><input value={presetDescription} onChange={(event) => setPresetDescription(event.target.value)} placeholder="Для безопасного поиска и работы с памятью" /></label>
            </div>
            <div className={styles.presetPermissionHeader}>
              <strong>Security ID</strong>
              <span>Выбрано: {presetPermissions.length}{basePresetKey ? ` · база: ${basePresetKey}` : ""}</span>
            </div>
            <div className={styles.checkboxGrid}>
              {securityIds.filter((item) => item.status === "active").map((item) => (
                <label key={item.security_id}>
                  <input type="checkbox" checked={presetPermissions.includes(item.security_id)} onChange={() => togglePresetPermission(item.security_id)} />
                  <span><code>{item.security_id}</code><small>{item.description}</small></span>
                </label>
              ))}
            </div>
            <div className={styles.formActions}>
              <button className={styles.secondaryButton} onClick={() => setShowPresetForm(false)}>Отмена</button>
              <button className={styles.primaryButton} disabled={!presetKey.trim() || !presetName.trim() || working !== ""} onClick={() => void createPreset()}>{working === "create-preset" ? <Loader2 className={styles.spin} size={15} /> : <Plus size={15} />} Создать и опубликовать</button>
            </div>
          </div>
        ) : null}
      </section>

      <section className={`${styles.panel} ${styles.auditPanel}`}>
        <div className={styles.panelHeader}>
          <div>
            <h2>Аудит безопасности</h2>
            <span>Последние изменения пользователей, ролей и разрешений</span>
          </div>
          <span className={styles.count}>{auditEntries.length}</span>
        </div>
        <div className={styles.auditList}>
          {!auditEntries.length ? <div className={styles.empty}>Изменений пока нет.</div> : null}
          {auditEntries.map((entry) => (
            <article key={entry.id} className={styles.auditRow}>
              <div>
                <code>{entry.action}</code>
                <strong>{entry.target_type}{entry.target_id ? ` · ${entry.target_id}` : ""}</strong>
              </div>
              <p>{entry.reason || "Без комментария"}</p>
              <small>{formattedDate(entry.ts)} · actor {entry.actor_user_id || "system"}</small>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}
