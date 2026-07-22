"use client";

import { MessageSquare, Users } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import styles from "./admin-tabs.module.css";

const tabs = [
  { href: "/admin", label: "Пользователи", icon: Users, exact: true },
  { href: "/admin/telegram", label: "Telegram", icon: MessageSquare, exact: false }
] as const;

export default function AdminTabs() {
  const pathname = usePathname();

  return (
    <nav className={styles.bar} aria-label="Разделы администратора">
      <div className={styles.tabs} role="tablist" aria-label="Администрирование">
        {tabs.map((tab) => {
          const active = tab.exact ? pathname === tab.href : pathname.startsWith(tab.href);
          const Icon = tab.icon;
          return (
            <Link
              aria-current={active ? "page" : undefined}
              aria-selected={active}
              className={`${styles.tab} ${active ? styles.active : ""}`}
              href={tab.href}
              key={tab.href}
              role="tab"
            >
              <Icon size={16} />
              <span>{tab.label}</span>
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
