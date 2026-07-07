import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "JARVIS GPT",
  description: "Local Command Center for JARVIS GPT",
  icons: [{ rel: "icon", url: "/icon.svg" }],
  manifest: "/manifest.webmanifest"
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
