# Jarvis feature wishlist

Captured 2026-07-17, before the Qwen3.5‑VL brain swap. Grounded in a full audit of the
99‑tool registry + code (see `capability-gap-audit`). This is the backlog we pick from —
nothing here is scheduled yet.

Legend: **V** = value, **E** = effort, 👁 = pairs with the incoming vision brain,
🎙 = pairs with voice, ⭐ = owner‑highlighted.

---

## 0. ⭐ Telegram as a full chat frontend

Make a Telegram bot a **first‑class front‑end for Jarvis**, alongside the web UI: you DM the
bot from your phone and hold a real conversation with the *same* agent — same brain, tools,
memory. Jarvis becomes reachable anywhere, not just at the desktop.

- **Two‑way chat**: message in → the Jarvis agent runs → reply out (with typing indicator +
  streaming). Same conversation model as the web chat.
- **Rich payloads**: send a **photo** and (👁 with the new brain) Jarvis *sees* it; send a
  document and it lands in the archive; send a **voice note** and (🎙) STT → agent → a
  spoken reply back. Jarvis replies with the files it makes (docx/xlsx/pdf), images, buttons.
- **Inline buttons** for approvals / choices (clean way to run the gated‑mode approvals).
- **Security (non‑negotiable)**: respond only to the owner's allow‑listed chat id; bot token
  in `backend/.env.local` (gitignored). One operator, like the rest of the runtime.
- **Architecture**: a long‑lived bridge process (aiogram / python‑telegram‑bot) that relays
  Telegram updates ↔ the backend `/chat` API — a new frontend next to Next.js and the native
  bridge. Needs a BotFather token; no new brain logic.

**Bot vs userbot** — two levels:
1. **Bot frontend** (BotFather token) — the clean, safe core: *chat with Jarvis* in Telegram.
   Recommended first.
2. **Userbot** (MTProto / Telethon, acts as *your* account) — powerful extension: Jarvis
   reads/answers *your real chats* ("прочитай последние от X", "ответь маме черновиком").
   Much more capable but riskier (ToS, acts as you) — do deliberately, opt‑in, later.

**V: very high** (this is "Jarvis in your pocket"). **E: medium** for the bot frontend;
high for the userbot. Huge synergy with vision (phone photos) and voice (voice notes).

---

## 1. Vision‑unlocked (highest synergy with the new brain) 👁

- **Vision pipeline** — images/video from the chat composer (and Telegram) → the VLM. The
  headline capability the brain swap enables. *[already planned]*
- **"Look at my screen"** — `system.inspect screen.capture` already grabs a desktop PNG; the
  VLM turns it into "посмотри на экран и скажи, что не так". Cheap, very *Jarvis*.
- **Vision‑OCR** of scanned PDFs / photos — the VLM reads scans better than tesseract;
  strengthens the whole documents subsystem.
- **Chart / diagram / error‑screenshot Q&A** — read a graph, a UI, a stack trace from an image.

## 2. Documents & output

- **PDF generation + editing** ⭐ — today PDF is read‑only. Generate PDF (from Markdown),
  fill forms, merge/split, annotate/sign, edit text. **V: high, E: medium‑high.**
- **PPTX generation** — native slide decks (docx/xlsx already are native OpenXML; pptx is
  read‑only). "Сделай презентацию по…". **V: high, E: medium.**
- **Charts / plots** — real charts in xlsx + chart PNGs into docx / chat. **V: medium‑high.**

## 3. Compute

- **Code / data sandbox** — run Python to compute, analyze data, build charts (today
  `execution.*` is only transactional FS/registry edits — no code execution). Compounds with
  §2: **compute → chart → drop into a docx/PPTX/PDF report.** **V: high, E: medium‑high.**

## 4. Reach & comms

- **Telegram** — see §0.
- **Email** (SMTP/IMAP) — read/triage/send mail. Core assistant gap. Needs creds/OAuth.
- **Push notifications** — Jarvis pings you (Telegram covers most of this once §0 lands).

## 5. Productivity

- **Calendar / reminders** — "напомни завтра", "что у меня на неделе". No user‑facing
  reminder/scheduler today (missions + operator_queue are internal). **V: high.**
- **Clipboard** — read/write the Windows clipboard. **V: medium, E: low** (cheap win).
- **Richer filesystem ops** — copy/move/delete/rename/mkdir + find‑by‑content on disk
  (today only read/write/list). **V: medium.**

## 6. Voice 🎙

- **TTS / STT** — speak replies + accept voice input. The classic "Jarvis" feel; pairs with
  Telegram voice notes (§0). **V: high, E: medium‑high.**

---

## Recommended sequencing (my take)

1. **After the brain lands:** vision pipeline → "look at my screen" → vision‑OCR (cheap, high
   wow, plumbing partly exists).
2. **Telegram bot frontend** (§0) — turns Jarvis into an anywhere assistant; multiplies the
   value of *everything* else. Voice + vision plug straight into it.
3. **Compounding output trio:** code sandbox → charts → PDF/PPTX generation (the "analyst that
   makes reports" chain).
4. **Assistant backbone:** email + calendar/reminders; clipboard as a quick win.
5. **Voice** and the **Telegram userbot** as deliberate, opt‑in power‑ups.

---

## Progress — delivered 2026-07-18 (Qwen3.5‑VL is the live brain)

Shipped this session (all on `main`, tested): **vision pipeline** (chat image → VLM) +
**"посмотри на экран"** (VLM sees the desktop) + **multi‑image**; **host telemetry**
(RAM/CPU/disk + **GPU/VRAM via nvidia‑smi**); **PPTX / PDF / SVG‑chart generation**
(hand‑rolled, zero deps; **PDF now supports Cyrillic** via embedded system TrueType);
**Telegram bot frontend** (§0, level 1); agentic‑loop hardening (no more retry‑storms).
In flight: **reminders/calendar**. Still open from above: code sandbox, email, voice,
vision‑OCR, Telegram userbot.

## Progress — light daily wins 2026-07-19

Shipped on `main`: **Clipboard + richer FS + PDF edit** (routine speed):
- Deterministic agent routes for `переложи/скопируй/переименуй/создай папку/удали файл`
  with exact path operands (weak model no longer invents fake success).
- Filesystem tools allow **operator home** roots (with execution roots), not only
  `cwd` + `JARVIS_HOME`.
- Folder destinations auto-join source basename; copy/move create parents by default.
- Explorer reveal: «покажи в проводнике …».
- PDF `documents.edit` (regenerate-style replace/append/set_text).
- Clipboard transform requests («переведи что в буфере…») no longer short-circuit to bare read.

## More ideas — brainstorm 2026-07-18 (new features)

**Proactive (Jarvis acts, not only on request):**
- ⭐ **Scheduled agent tasks** — not just a reminder but "run a full agent turn on a
  schedule → result to Telegram" ("каждое утро сводка по AI", "каждый вечер проверь систему").
  Builds on reminders' scheduling.
- ⭐ **Machine‑health alerts** — background monitor over the new RAM/CPU/GPU/disk telemetry →
  push to Telegram when VRAM/temp/disk cross thresholds or the dispatcher dies. Direct payoff
  for a box that runs its own model.

**Vision‑unlocked (max synergy with the new brain):**
- **Watch my screen** — "скажи, когда сборка закончится / придёт уведомление": periodic
  screen.capture + VLM diff.
- **Clipboard / screen as context** — "переведи то, что в буфере / на экране", "объясни эту
  ошибку со скрина". Pairs with clipboard + vision.

**Classic Jarvis:**
- **Desktop voice** — "Джарвис" wake word → listen → act → speak (beyond Telegram voice notes).
- **Quick‑capture inbox (GTD)** — dump a thought (voice/text/Telegram) → Jarvis routes it to
  task / reminder / note / file.

**Work tools:**
- **Dev mode** — "объясни проект", "найди баг", run tests, git ops (compounds with code sandbox).
- **Folder auto‑organize** — watch Downloads, sort/rename by type.
- **Model control from chat** — "переключись на gemma / перезапусти модель / сколько ест VRAM".

## Autonomy extensions — 2026-07-18

Deepening EXISTING subsystems toward more autonomy (not new surfaces):
- **Self‑healing runtime** (supervisor + telemetry): auto‑restart the dispatcher on
  repeated‑token degeneration / OOM; rotate a rate‑limited search provider; propose/act on
  disk cleanup when low. Alert the owner via Telegram on anything it can't self‑fix.
- **Proactive cognition** (background cognition loop): surface insights unprompted and PROPOSE
  or spawn missions ("диск заполняется — почистить?") instead of only passive reflection.
- **Self‑replanning missions** (executive DAG): retry with a different tool/approach on a
  failed step, replan, and only escalate to the owner (now reachable via Telegram) when truly
  stuck; long missions report progress.
- **Multi‑round, fact‑checked web research** (web.research): search → read → find gaps → search
  more until confident, with adversarial source triangulation (the deep‑research pattern).
- **Learned owner‑model** (memory/lessons): learn preferences (verbosity, favourite tools,
  timing) and auto‑apply; recall relevant past context unprompted.
- **Tool fallback chains + learning** (agentic loop): on a tool failure, auto‑try a different
  tool; learn which tool fits which intent over time.
- **Deeper self‑verification** (verify/repair loop): multi‑perspective checks + source‑grounded
  fact verification + calibrated "I'm not sure" honesty before answering.
