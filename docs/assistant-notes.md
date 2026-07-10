# Assistant Notes

Single coordination point for assistant-to-assistant handoffs in this repository.

Use this file for short, append-only notes between Codex and the second assistant.
Keep the newest note at the top, include author, date, branch/commit when useful,
and list only facts needed by the next assistant: changed files, tests, blockers,
and decisions. Do not paste secrets, tokens, private logs, or long command output.

## Notes

### 2026-07-10 - Codex (shopping search link fidelity)

- Fixed the shopping web path that could still answer "no concrete link" after
  search had found store URLs. Shopping evidence now stays deterministic instead
  of going through LLM synthesis, so found DNS/Ozon/product links are preserved
  while prices/availability remain explicitly qualified.
- Shopping search now retries with the domain-hint query when the first result
  set is only store shells/categories with no product URL and no price snippet,
  then ranks product-card URLs above home/category/search pages.
- Added regressions for DNS shell/category results followed by a product card,
  and for successful product fetches that must not be overwritten by synthesis.
- Verified: `py -3.11 -m ruff check backend/src/jarvis_gpt/agent.py
  backend/tests/test_agent.py`; `py -3.11 -m pytest backend/tests` (212 pass).

### 2026-07-10 - Codex (shopping follow-up state isolation)

- Fixed a bad reuse path where every `web.answer` result was saved as
  shopping-state. Generic/news web answers no longer poison later shopping
  follow-ups.
- Self-contained shopping requests like "покажи самую дешёвую RTX 5090 в DNS"
  now force a fresh web answer/search even if a previous shopping state exists;
  state reuse is reserved for explicit "из найденных/из списка/прошлый поиск"
  style follow-ups.

### 2026-07-10 - Codex (runtime restart discipline)

- Operator preference: do not restart the LLM/dispatcher when a change only
  requires backend/frontend reload or no runtime restart. Use the narrowest
  restart needed for the touched surface.

### 2026-07-10 - Codex (shopping weak evidence UX)

- Fixed DNS/shopping answers with weak snippet-only evidence: `web.answer` now
  keeps the direct store search link first and skips LLM synthesis that was
  rewriting the deterministic fallback into "no data" prose.
- Bumped the `web.answer` cache key version to avoid serving the stale bad
  answer shape from the short TTL cache.

### 2026-07-10 - Codex (search cooldown evidence fallback)

- Fixed a regression where `web.answer` could return `0 sources` during search
  provider cooldowns even when Jarvis had relevant previous web evidence.
- `web.search` now falls back to the evidence ledger after all live providers
  fail, reconstructing cached search results and preserving preferred-domain
  filtering such as `dns-shop.ru`.

### 2026-07-10 - Codex (compact chat links)

- Updated Command Center rich-message rendering so raw URL labels and
  `[URL](URL)` markdown display as compact inline link pills like
  `domain/path/...`; the full URL remains in `href` and hover `title`.
- Added CSS to keep those links on one visual unit with ellipsis instead of
  wrapping long URLs through assistant text.

### 2026-07-10 - Codex (GUI brand cleanup)

- Removed visible `GPT`/`JARVIS GPT` branding from the Command Center shell,
  manifest, backend API metadata, CLI/help text, launcher banner, and smoke text.
  Public-facing labels now use `Jarvis`.
- Reworded surfaced `OS`/`OSINT` terminology in tool descriptions and
  deterministic web answers to neutral Jarvis/system/public-source language.
- Updated tests for the renamed public-source search frame.

### 2026-07-10 - Codex (web answer bugfix: links over guts)

- Fixed `web.answer` fallback UX: deterministic answers no longer expose
  internal query/confidence/gap sections in the chat bubble. They now return
  concise markdown links; detailed source/citation metadata stays in cards/trace.
- Site-specific shopping intent now filters to explicitly requested domains
  instead of mixing in unrelated sources. Example: "на ДНС" maps to
  `dns-shop.ru`; if DNS is blocked/thin, Jarvis returns a direct DNS search link
  instead of ranking NVIDIA/Bing/Wikipedia as the answer.
- Bing HTML parser now unwraps `/ck/a` redirect links into real destination
  URLs before storing/ranking evidence.
- Bumped `web.answer` cache key version to avoid serving old verbose cached
  answers.
- Fixed chat layout overflow: long URLs/links/list items wrap inside assistant
  bubbles instead of creating a horizontal transcript scroll.

### 2026-07-10 - Codex (Google replacement quality pass)

- Added `internet.search_api.status`: reports Search API readiness with masked
  key presence, supported verticals, recent per-provider ok/fail stats, and an
  optional live health probe (`check=true`). `internet.observability` now also
  exposes `search_provider_stats`.
- `web.answer` now returns `claim_citations` and includes them in `cards`; the
  chat event payload carries them too. Command Center renders a compact source
  panel under `web.answer` replies with confidence, source/domain counts, top
  sources, and claim citation snippets.
- `web.answer` cards now include `vertical_cards` extracted from source
  snippets/evidence (product prices/availability, contact hints, article dates,
  schema-derived hints where present).
- Expanded the built-in `web.eval` catalog from 3 to 20+ mixed cases while
  keeping default execution bounded (`limit` defaults to 8, max 30).
- Added `documents.review`: OCR/readiness, Word redline/edit readiness, Excel
  formula/style audit, optional reference comparison, and recommendations. XLSX
  extraction now counts workbook styles.
- `web.transcript` now supports local/quarantined media paths and explicit
  `allow_download=true` media URL fallback through local `whisper` when the CLI
  is installed. Without `whisper`, it returns an honest availability status.
- Added `browser.session.diagnose`: combines Chrome CDP status, active handoff,
  optional page read, consent/login/sensitive-form signals, and recommended next
  route (`autonomous_read`, `operator_handoff`, `login_required`, etc.).

### 2026-07-10 - Codex (Google replacement production surface)

- Added optional real Search API providers to `web.search`: Brave Search,
  Tavily, and Serper. They are auto-used only when their env keys are present
  (`JARVIS_BRAVE_SEARCH_API_KEY`/`BRAVE_SEARCH_API_KEY`,
  `JARVIS_TAVILY_API_KEY`/`TAVILY_API_KEY`,
  `JARVIS_SERPER_API_KEY`/`SERPER_API_KEY`); otherwise existing
  DuckDuckGo/Bing/Yandex HTML fallback remains unchanged.
- `web.search`, `web.research`, and `web.answer` now carry verticals:
  `web`, `news`, `images`, `shopping`, `places`, and `scholar`. Serper covers
  all verticals; Brave covers web/news/images; Tavily covers web/news.
- Deep crawl got practical controls: `depth`, `follow_text`, `include`,
  `exclude`, `render_fallback`, and `archive_fallback`.
- Added `web.transcript` for public caption/transcript extraction (YouTube
  `captionTracks` first, HTML transcript fallback) and `web.eval` for bounded
  answer-quality checks over `web.answer` cases.
- Agent `web.answer` events now carry `cards`, `synthesis`, `cache`, and
  `vertical`; internet observability reports Search API readiness, supported
  verticals, and answer-cache count. Command Center internet panel displays
  those readiness signals.

### 2026-07-10 - Codex (Google replacement quality layers)

- Extended `web.answer` beyond deterministic ranking: it now has answer-level
  TTL cache, inferred freshness, optional caller-supplied query variants, broader
  query expansion, domain-diverse source selection, recency/rank source scoring,
  structured `cards`, and an LLM synthesis pass.
- The LLM synthesis is deliberately strict: it receives compact source payloads,
  is told that web text is untrusted evidence, must retain supplied source URLs,
  and is rejected if the generated answer is too short, JSON/tool-like, or lacks
  a URL/domain from the ranked sources. Rejection falls back to the deterministic
  cited report.
- `web.answer` response data now includes `region`, inferred `freshness`,
  `synthesis`, `cache`, and `cards` alongside the existing answer/sources/
  citations/confidence/steps fields.
- Tests added for grounded LLM synthesis, ungrounded synthesis rejection, answer
  cache hits, and domain diversity.

### 2026-07-10 - Codex (Google replacement answer engine)

- Added safe `web.answer`: a Google-like answer engine that expands a user
  question into focused searches, reuses `web.research` for fetch/render/archive
  safety, ranks sources by fetched evidence/quality/domain/term coverage, runs
  `web.verify`, and returns a cited answer plus confidence.
- Direct web-research chat flow now tries `web.answer` first when using the real
  ToolRegistry, then falls back to the legacy `web.search` -> `web.fetch` path
  for degraded or mocked registries. Follow-up memory still stores compatible
  `web.research` evidence.
- Agent prompts/capability manifest now prefer `web.answer` for internet
  questions, with `web.search`/`web.fetch` kept as fallback/debug primitives.
- Tests added for source ranking/query expansion and agent-level routing through
  `web.answer`.

### 2026-07-10 - Claude (internet coverage: archive, feeds, weather, watches)

- Continued the internet theme with four everyday gaps the surf stack still had:
  1. `web.archive` (safe): Wayback availability API + existing public-only fetch;
     blocked `web.fetch` summaries now hint at it. Snapshot timestamp +
     "historical data" note in the payload.
  2. `web.feed` (safe): bounded RSS/RDF/Atom parser (`_parse_feed_entries`,
     ~200k char cap, honest failure on truncation/garbage), evidence record,
     domain cooldown respected.
  3. `web.weather` (safe): keyless Open-Meteo geocode+forecast, Russian WMO
     descriptions, `data.report`. The agent weather fast-path now tries it FIRST
     via `_try_weather_tool` (strict shape check: report + source=open-meteo.com)
     and falls back to the old search route on any failure — existing mocked
     weather tests pass unchanged because of the strict validation.
  4. `web.watch`: page-change monitoring. New autonomy job kind (operations
     whitelist, max_runs default 500) + `AutonomyExecutor._run_web_watch`
     (fetch → normalized text or regex match → sha256 vs KV state keyed by
     url+pattern; baseline, then warn event + durable memory + bus publish on
     change; fetch failure keeps the job enabled). Safe tools `web.watch.add`
     (dedup, 12-active cap, cadence/regex validation), `.list`, `.remove` —
     autonomous mutation deliberately allowed, same rationale as persona.insight.
- SYSTEM_PROMPT gained a "специализированные интернет-маршруты" bullet.
- State key helper `_web_watch_state_key` lives in tools.py and is imported by
  the executor so tool listing and job runner share one key format.
- Tests: `backend/tests/test_web_coverage.py` (10). Full run: 244 pass, ruff
  clean, frontend typecheck + build clean.
- Next candidates: Command Center row for active watches; feed+watch combo for
  topic monitoring; web.archive as automatic fallback inside web.research.

### 2026-07-10 - Codex (web search, archive, crawl, lazy pages)

- Implemented Claude's web weak-point list in `backend/src/jarvis_gpt/tools.py`
  and `backend/src/jarvis_gpt/browser_cdp.py`.
- `web.search` now supports `region`, `freshness`, `pages`, and `provider`;
  it paginates DDG/Bing, adds Yandex HTML fallback for Russian-local queries,
  deduplicates URLs, and records provider/page stats in evidence.
- `web.fetch` now has a 15-minute TTL cache, extracts safe link lists for crawl,
  and detects cookie/consent walls so banner-only pages are low-confidence
  failures rather than normal source evidence.
- Added `web.archive` using Wayback CDX and wired it into `web.research` after
  blocked/thin live fetch and render fallback. Added `web.crawl` for bounded
  same-site multipage/forum/docs traversal.
- Added review-gated `browser.scroll` plus a CDP-backed `web.render`
  `scroll_passes` path for lazy/infinite pages in isolated headless Chrome.
- Rebased over Claude commit `36f4718`; preserved `web.feed`, `web.weather`,
  `web.watch.*`, and merged archive behavior into the single availability-API
  `web.archive` implementation instead of keeping a duplicate CDX function.
- Verified: `py -3.11 -m compileall -q backend/src/jarvis_gpt`,
  `py -3.11 -m ruff check backend/src/jarvis_gpt backend/tests --output-format=concise`,
  and `py -3.11 -m pytest -q backend/tests --tb=short` (252 passed).

### 2026-07-10 - Codex (document intelligence tools)

- Added shared `document_runtime` extraction for uploaded/local documents:
  DOCX paragraphs/tables/comments/styles, XLSX sheets/shared strings/formulas,
  PDF text/page count with optional `pypdf` fallback, and text/html/json/csv.
  Legacy `.doc/.xls` are recognized with a clear conversion-needed error.
- File ingestion now indexes DOCX/XLSX/PDF/text-like uploads into searchable
  chunks instead of storing Office/PDF files as opaque binaries when extraction
  succeeds.
- Added safe document tools accepting either `file_id` from chat upload or a
  local `path` under workspace, `JARVIS_HOME`, or user home:
  `documents.inspect`, `documents.read`, `documents.compare`,
  `documents.edit.plan`, and `documents.apply_replacements`.
- `documents.apply_replacements` creates an edited copy under
  `data/document-outputs`; it never overwrites the original. It supports exact
  replacements in DOCX, XLSX/XLSM shared strings/sheets, and text-like files,
  then registers/indexes the generated copy in file storage.
- Agent attachment prompts and mission guidance now point Word/Excel/PDF work
  through `documents.*` tools for reading, comparison, edit planning, and safe
  edited copies.
- Verified: `pytest -q backend\tests --tb=short` (234 pass), backend ruff,
  backend compileall, and frontend `npm run typecheck`.

### 2026-07-10 - Codex (internet production surface)

- Added safe `web.research`: one call now searches, fetches, optionally renders,
  extracts, verifies, returns a source-backed report, citations, verification,
  and stores recent research in runtime key `web.research.records`.
- Added safe `web.document.read` for quarantined web downloads/evidence/URLs. It
  only reads files under Jarvis download quarantine, extracts bounded text from
  txt/md/csv/json/html/docx/xlsx/pdf-like payloads, saves fresh evidence, and
  never opens or executes the file. It refuses oversized files and caps ZIP/XML
  member reads for Office documents.
- Added safe `internet.observability` and `internet.smoke`. Observability
  summarizes recent web/browser/internet tool health, blocked pages, evidence,
  research records, rate cooldowns, top domains, search providers, and current
  browser handoff. Smoke checks Chrome status, handoff status, fetch, extract,
  verify, and returns a live internet health snapshot.
- Command Center status panel now shows internet handoff/observability metrics,
  recent blocked summaries, top domain/provider, and a smoke button using the
  current web URL draft or `https://example.com/`.
- Agent mission/tool guidance now explicitly prefers `web.research`,
  `web.extract`, `web.verify`, and `web.document.read` for internet tasks that
  need source-backed evidence.
- Verified: `pytest -q backend\tests --tb=short` (230 pass), backend
  `ruff check backend\src\jarvis_gpt backend\tests --output-format=concise`,
  `python -m compileall -q backend\src\jarvis_gpt`, frontend
  `npm run typecheck`, and frontend `npm run build`.

### 2026-07-10 - Codex (internet surfing quality)

- Browser action tools now accept semantic `target` hints in addition to CSS
  selectors. Chrome-side JS scores visible buttons/links/inputs/selects by text,
  aria/title/placeholder/name/id/label, returns the resolved selector, and keeps
  review gating for mutations.
- Added `browser.handoff.status` and runtime handoff checkpoints for CAPTCHA,
  login/password forms, and sensitive forms. The operator can complete the
  step in Chrome, then retry `browser.read` or the same browser action.
- `web.search` now has a Bing HTML fallback when DuckDuckGo returns no usable
  results and now stores a real search evidence record.
- `web.fetch`/`web.extract` preserve parsed HTML metadata in evidence:
  JSON-LD/schema.org, OpenGraph/meta, canonical URL, and readability-style
  paragraphs/headings. `web.extract` uses schema hints to detect products and
  articles.
- Added `web.verify` for deterministic claim/source coverage over evidence ids,
  URLs, or search snippets. The agent tool prompt now recommends
  search -> fetch/render -> extract -> verify for web research.
- Verified: `pytest -q backend\tests --tb=short` (225 pass), backend
  `ruff check`, and `python -m compileall -q backend\src\jarvis_gpt`.

### 2026-07-10 - Codex (internet workflow tools)

- Added review-gated Chrome CDP actions: `browser.click`, `browser.type`,
  `browser.select`, and `browser.screenshot`. They reuse the operator-approved
  local Chrome CDP session, return a fresh page snapshot, and keep form values
  unread. `browser.type` refuses password/card/token-like targets unless
  `allow_sensitive` is explicitly set after review.
- Added a runtime web evidence ledger. `web.search`, `web.fetch`, `web.render`,
  `web.download`, `browser.read`, and the new browser actions now save compact
  evidence records and return `evidence_id`; `web.evidence.list` exposes recent
  records for follow-up reasoning.
- Added `web.extract` for structured article/product/contact/table hints from a
  URL, saved evidence id, or supplied text.
- Added per-domain web request budgets and cooldowns after blocked/rate-limited
  responses so Jarvis backs off instead of hammering anti-bot pages.
- Added `web.download.inspect` for quarantined downloads: file signature,
  SHA256, executable-risk flag, and bounded ZIP entry listing without opening or
  executing anything.
- Verified: `pytest -q backend\tests --tb=short` (222 pass), backend
  `ruff check`, and `python -m compileall -q backend\src\jarvis_gpt`.

### 2026-07-10 - Codex (internet safety hardening)

- Added `web.download`: public-only HTTP(S) download into Jarvis quarantine cache
  (`D:\jarvis\cache\jarvis-gpt\downloads`), with size cap, SHA256, executable
  risk flag, and explicit `open_allowed=false` / `auto_execute_allowed=false`.
- Added shared untrusted-web safety metadata to `web.search`, `web.fetch`,
  `web.render`, `web.download`, and `browser.read`: remote content is evidence,
  not instructions; prompt-injection markers are surfaced in `data.safety`.
- `browser.read` now reports form/password/sensitive-input counts without
  reading field values. URLs with embedded credentials are rejected for both
  public fetch/download and browser tools.
- Tool-loop prompt now explicitly tells the model not to obey web/browser page
  text that asks it to ignore prompts, reveal secrets, call tools, send cookies,
  or change behavior.
- Verified: full `pytest backend/tests` (217 pass), targeted backend
  `ruff check`, and `python -m compileall backend/src/jarvis_gpt`.

### 2026-07-10 - Codex (web blocked-page handling + UI polish)

- Fixed Command Center file panel styling: native file input is hidden behind a
  themed picker row, and non-chat right-panel sections now use one outer scroll
  instead of nested tiny scroll areas in empty mission/approval/briefing blocks.
- `web.fetch` now uses browser-like Accept/User-Agent headers, repairs common
  UTF-8 mojibake from search/fetch HTML, and marks HTTP 401/403/429 or obvious
  anti-bot pages as blocked instead of successful evidence.
- `web.render` now treats rendered 403/captcha/anti-bot DOM as blocked. It still
  runs in isolated headless Chrome/Edge and does not touch the operator browser.
- Shopping research now skips LLM synthesis when only snippet/search links are
  available because a shop blocked automation. In that case it returns the
  found store links, says price/availability are not confirmed, and avoids the
  false "link impossible" answer.
- Verified: backend ruff, full backend pytest (210 pass), frontend typecheck,
  frontend build.

### 2026-07-10 - Codex (API host selection hotfix)

- Fixed Command Center API host selection when the UI is opened on
  `localhost:3000` but the production build was made in LAN mode with
  `NEXT_PUBLIC_JARVIS_API_URL=http://<lan-ip>:8000`. The browser client now
  falls back to the current page host for loopback/LAN mismatches, including
  the trace page.
- Bumped the service worker cache version to `jarvis-gpt-v2` so stale static
  chunks do not keep serving the old API target after a rebuild.
- Backend API/WS guard now treats same-machine LAN source addresses as local
  clients while still requiring `JARVIS_API_TOKEN` for true remote clients.
- Verified: frontend typecheck/build, backend ruff, full backend pytest
  (207 pass), live localhost and same-machine LAN `/api/status`, and stack
  restart with dispatcher still READY.

### 2026-07-09 - Codex (leases, stream durability, background cognition)

- Added persisted autonomy run leases. Running jobs now write
  `running_lease_id/started_at/until`, cancel through `AutonomyExecutor`, and
  recover stale interrupted leases on backend startup with visible failed run
  history instead of silently hanging.
- Added a background cognition loop (`jarvis-cognition-loop`) controlled by
  `JARVIS_COGNITION_ENABLED/INTERVAL_SEC/MAX_TOKENS`. It runs observational
  JSON-only LLM pulses over recent runtime signals, saves `cognition.last_pulse`,
  and mirrors the result into the append-only learning journal. It does not
  browse, mutate the host, or auto-create jobs.
- Hardened chat streaming: if the client disconnects mid-answer, the backend
  persists the partial assistant message with `metadata.interrupted=true` and
  exposes it through `/api/chat/stream/interrupted/{conversation_id}`.
- Tool run storage now redacts obvious token/secret/password/cookie/bearer data
  before writing telemetry, audit payloads, and learning observations.
- `system.inspect` screen capture can request OCR when `tesseract` is available;
  capture still uses Jarvis cache and does not touch the operator's browser.
- Command Center chat scroll now respects manual upward scrolling during live
  generation and the chat/side panels stretch to the viewport more consistently.

### 2026-07-09 - Codex (headless web, learning, autonomy controls)

- Implemented the remaining broad hardening/observability items except model
  profiles, which stay as future scaffolding.
- Added `web.render`: isolated headless Chrome/Edge DOM render for JS-heavy
  public pages. It uses a throwaway profile and does not open the operator's
  Chrome. `web.search`/`web.fetch` now use a public-only httpx transport that
  pins connections to already validated public DNS answers.
- Extended safe local inspection: `system.inspect` can now run `screen.capture`
  into Jarvis cache without approval, while mutating native actions still go
  through `windows.native` approval gates.
- Learning tick is now LLM-assisted when the local LLM is available:
  deterministic lessons are still produced first, then a short JSON-only
  distillation pass can add up to two grounded behavioral lessons.
- Added quality visibility (`GET /api/operator/quality`, Command Center Quality
  panel) and autonomy visibility/control: job priority, optional deadline,
  runtime budget timeout, cancel endpoint, queue surfacing for failed/cancelled
  jobs, and trace timeline polish.
- Verified: ruff, full backend pytest (201 pass), frontend typecheck/build, and
  a live `web.fetch`/`web.render` smoke against `https://example.com/`.

### 2026-07-09 - Codex

- Runtime hardening pass: API remains loopback-open, but remote HTTP and
  `/ws/events` now require `JARVIS_API_TOKEN`; frontend can forward
  `NEXT_PUBLIC_JARVIS_API_TOKEN`.
- Added SQLite backup path through `JarvisStorage.backup_database`,
  `POST /api/runtime/backup`, CLI `backup`, audit/event logging, and Command
  Center controls/status.
- Added autonomy run history and retry visibility:
  `GET /api/autonomy/job-runs`, per-job failure counters, timestamps,
  durations, and bounded backoff after failed enabled jobs.
- `AutonomyExecutor.run_job` catches exceptions, records them as failed runs,
  and leaves enabled jobs retryable instead of making failures disappear into
  supervisor logs.
- Frontend runtime/resources panels now show API guard, backup state, job
  failure badges, and recent job runs.
- Verified before handoff: ruff, backend pytest (195 pass), frontend build, and
  backend/frontend restart via `scripts/jarvis-launcher.ps1`.

### 2026-07-09 - Codex

- Reviewed Claude's recent trend: replace keyword plugs with reasoning-first
  routing, give the model safe read-only tools, verify outputs, and close the
  feedback/lesson loop.
- Found and fixed a matching approval-boundary weakness: public
  `POST /api/tools/{tool_name}/run` no longer honors client-supplied
  `allow_danger=True`. Direct API tool runs are safe-only; review/danger tools
  must go through approval creation and `ApprovalExecutor`.
- Tightened CORS from wildcard to loopback origins by default, with optional
  `JARVIS_CORS_ORIGINS` for explicit extra trusted origins. This reduces browser
  drive-by access to the local API/approval surface.
- Added smoke coverage so the API refuses `host.bridge.execute` even when a
  client tries `allow_danger: true`, and so non-loopback CORS preflight fails.

### 2026-07-09 - Claude (arbiter owns local_action)

- Follow-up to system.inspect: the tool gave the model hands, but the ROUTE was
  still keyword-decided. Now the reasoning-first arbiter owns local tasks too.
- Two gaps closed in agent.py:
  1. `arbiter.route == "local_action"` was produced but ignored (fell through to
     web branches). `_try_direct_action` now handles it: reroute via new
     `_local_action_plan_from_intent` and return None → agentic loop with native
     tools. Stops "покажи автозагрузку" being web-searched.
  2. The arbiter only ran for web_research plans; plain machine questions land in
     reasoning/local_admin_advice and never reached it. `_understand_intent`'s
     gate now also opens for that local bucket. Narrowly scoped (only
     local_admin_advice), offline unchanged (gated on llm_enabled), one extra
     router call only in that already-local bucket.
- Arbiter prompt strengthened: local_action now explicitly covers reading machine
  state (hardware/OS/disks/RAM/battery/services/startup/printers/network/processes)
  AND actions (open app, type, focus window, local command), with examples and
  "this is NOT web_research".
- Deterministic native fast-paths untouched (they return before the arbiter), so
  all offline native tests are unchanged.
- Tests: test_arbiter_routes_local_query_to_native_inspection,
  test_arbiter_gate_opens_for_local_bucket_and_stays_closed_for_chat. 190 pass,
  ruff clean, frontend clean.

### 2026-07-09 - Claude (system.inspect: unlock WMI understanding)

- Diagnosis for "why doesn't a 26-31B model get everyday Windows requests": the
  model's WMI/WinAPI knowledge is fine, but the native route is decided by
  keyword heuristics before the model, and the read-only WMI path was fused into
  the danger-level windows.native tool — so the agentic loop could never use it.
  The 5-class keyword map only fired on the literal word "wmi"/"cim".
- Added safe read-only tool `system.inspect` (danger_level=safe) with an action
  allowlist `SAFE_INSPECT_ACTIONS` = {capabilities, window.list, wmi.query};
  mutating actions are refused with a pointer to approval-gated windows.native.
  Shares `_run_native_bridge_command` with windows.native; reuses the already
  validated wmi.query payload (SELECT-only, alnum class/props, no methods).
- Because it is safe and not in AGENTIC_TOOL_DENYLIST, it is auto-listed in the
  agentic tool protocol every turn; the model picks the Win32_* class itself for
  any everyday machine-state question. Heuristics stay as the offline fallback.
  SYSTEM_PROMPT nudges toward it and says not to wait for the word "wmi".
- Tests (4): read-only wmi run, mutating-action refusal, safe-autonomous
  membership, and an agentic test where the model calls system.inspect on
  "сколько заряда осталось на ноуте?" with no "wmi" keyword. 188 pass, ruff
  clean, frontend clean.
- Future candidates: run the whole local_action route through the reasoning-first
  arbiter (as web_research already does); a broader safe WinAPI read tool.

### 2026-07-09 - Claude (hardening pass)

- Audit of critical paths after three feature layers; fixed the real gaps,
  confirmed SQLite concurrency (single conn + RLock, check_same_thread=False)
  and web.fetch body cap were already sound.
- `backend/tests/test_api_smoke.py`: first end-to-end test through the real
  ASGI app (offline LLM, autonomy off) — status/chat/feedback/mission/report/
  queue/memory/tools/approvals/persona, incl. 404/422 and danger-tool refusal.
  Boot via `with TestClient(app)` runs the lifespan. It caught two wrong
  contract assumptions immediately (that is the point).
- Verify/repair now run under `asyncio.wait_for(self._verify_timeout())`
  (`VERIFY_TIMEOUT_SEC=45`, capped by llm_timeout): a hung critic degrades to
  shipping the ready draft instead of blocking for the full LLM timeout.
- `.env.example` synced with config (verify/embeddings/mission-interval vars).
- Known residual, deliberately not changed: web.fetch SSRF pre-flight is a
  DNS-rebinding TOCTOU. Realistic attacks are already blocked; a full IP pin
  breaks TLS SNI for legitimate HTTPS — worse than the rare rebinding for a
  single-operator local tool. Revisit with a custom httpx transport if a
  multi-tenant scenario appears.
- Full run: 184 pass, ruff clean, frontend typecheck + build clean.

### 2026-07-09 - Claude (experience loop)

- Closed the open half of the self-learning thesis: signals -> lessons ->
  behavior is now a loop, not a shelf.
- Operator feedback: `POST /api/messages/{id}/feedback` +
  `storage.set_message_feedback` (message metadata for UI restore, journal
  `operator.feedback` that survives chat deletion, audit, WS `feedback` event).
  Command Center has 👍/👎 on assistant bubbles (comment prompt on 👎).
- `verification.revise` verdicts are journaled from `_verify_and_repair_answer`.
- LearningEngine v2 derives priority lessons from negative/positive feedback,
  recurring self-check gaps, and rejected approvals — quoting real operator
  text; lesson cap raised to 6.
- `AgentRuntime._lessons_prompt()` injects top lessons (importance/recency,
  ~900 chars) into every chat/stream turn and mission step — this is the piece
  that makes learning change behavior deterministically.
- `answer_quality_report` + operator queue `quality` items
  (`quality:feedback` high, `quality:self-check` at >=3 revises).
- Frontend: feedback buttons, verification shield badge on bubbles (restored
  from metadata on reload), «Отчёт» button on done missions, auto-report after
  «Запустить всё». New CSS: `.bubbleAction.selected`, `.bubbleBadge`.
- Tests: `backend/tests/test_experience_loop.py` (5). Full run: 178 pass, ruff
  clean, frontend typecheck + build clean.
- Possible next steps: show quality history chart; let learning tick distill
  lessons via LLM (deterministic templates stay the fallback); feedback-driven
  persona insights.

### 2026-07-09 - Claude (result integrity layer)

- New module `backend/src/jarvis_gpt/verification.py`: strict JSON critic
  (`answer-verification-v1`), verdict parser, repair prompts (rewrite /
  stream addendum), deterministic + LLM mission report.
- Answer self-check wired into `chat()` (full rewrite allowed), `stream_chat()`
  (addendum only — streamed text is not retractable) and
  `_execute_mission_step_agentic` (report rewrite before notes persist).
  Trigger: tools used or answer >= 400 chars; one critic pass + max one repair.
  Kill switches: `JARVIS_VERIFY_ANSWERS=0` env or autonomy policy
  `verify_answers=false`. Unparseable critic output or JSON-shaped repair
  never damages the draft.
- Mission deliverable: `_maybe_finalize_mission` fires on the `done` transition
  from all three completion paths, is idempotent via KV `mission.report.{id}`,
  saves a `missions/report` memory, emits `mission_report`, and surfaces via
  `MissionRunResponse.final_report` + new `GET /api/missions/{id}/report`.
- Intent arbiter gained a `clarify` route: one targeted question to the
  operator (confidence >= 0.65) instead of a confident guess.
- Legacy loop-mechanics tests opt out via policy `verify_answers=false` so
  their LLM call counts stay about loop mechanics.
- Tests: `backend/tests/test_verification.py` (9). Full run: 173 pass, ruff
  clean, frontend typecheck + build clean.
- Possible next steps: Command Center panel for mission reports (API is ready);
  verification stats in operator queue; per-route verify policy.

### 2026-07-09 - Claude

- Closed three follow-ups previously marked "на будущее" in runtime.md, all on
  `main`:
  1. Persona auto-learning: added safe tools `persona.get` and `persona.insight`
     (tools.py) wired to `PersonaManager.add_insight`. `persona.insight` is
     deliberately allowed in the autonomous agentic loop (single fact, dedup,
     per-field caps, audit + `persona.insight` event) — the reasoning-first
     replacement for regex persona extraction. SYSTEM_PROMPT now tells the model
     to save durable operator facts sparingly.
  2. File-chunk hybrid retrieval no longer dies on zero lexical overlap:
     `storage.recent_file_chunks` provides a bounded fallback pool, gated by
     `FILE_FALLBACK_MIN_RELATEDNESS` (fuzzy-vector cosine >= 0.1) so unrelated
     files never leak into the prompt; fallback hits are marked
     `retrieval="semantic-recent"` and capped at 3.
  3. Mission detection through understanding: the intent arbiter's `mission`
     decision (confidence >= 0.7) now rewrites the kernel plan via
     `_mission_plan_from_intent`, and chat/stream re-read `context.task_plan`
     after `_try_direct_action`, so a mission-shaped task without mission
     keywords still becomes a persisted mission. Keyword counter stays as the
     offline path.
- Tests: `test_agentic_loop_learns_persona_insight_from_dialogue`,
  `test_persona_insight_tool_learns_deduplicates_and_validates`,
  `test_hybrid_files_falls_back_to_recent_chunks_without_lexical_overlap`,
  `test_reasoning_arbiter_can_promote_research_to_mission`.
- Full run before handoff: backend pytest 164 pass, ruff clean, frontend
  typecheck + build clean.
- Possible next steps: let the arbiter also own `local_action`; feed persona
  insights into the learning journal; persist chunk vectors for large corpora.

### 2026-07-09 - Codex

- Added `backend/src/jarvis_gpt/autonomy_executor.py`: a shared executor for
  persisted autonomy jobs, direct routine steps, and headless mission jobs.
- Supervisor now runs due background jobs on `JARVIS_AUTONOMY_MISSION_INTERVAL_SEC`
  while preserving existing approval gates. Mission jobs persist `mission_id`,
  stay enabled while budget remains, pause on blocked missions, and finish on done.
- The LLM now receives a compact capability/current-work manifest in normal chat
  and mission execution prompts: profile/model, current conversation/mission/task,
  safe autonomous tools, gated tools, recent missions, and background jobs.
- Command Center mission cards have `В фон`, which creates a persisted mission
  autonomy job instead of requiring the page to stay open.
- Tests run before handoff: `ruff`, full backend `pytest`, frontend `typecheck`,
  and frontend `build`.

### 2026-07-09 - Codex

- Added per-answer thought trace UI: assistant bubbles now show a Brain icon once
  the persisted `msg_*` id is known. It opens `/trace/{messageId}`.
- New backend endpoint `GET /api/agent/trace/message/{message_id}` returns the
  previous user input, assistant output, recorded runtime events, nodes/edges, and
  a disclosure that this is observable runtime trace rather than hidden CoT.
- Added a trace page with animated signal rail from input through task kernel,
  tools/memory/thought events, and output. It uses stored message metadata; no
  real browser opens or extra LLM calls are involved.

### 2026-07-09 - Codex

- Added an evidence-synthesis pass after `web.search`/`web.fetch`: web answers now
  ask the LLM to form a conclusion from fetched evidence, mark uncertainty, and
  keep source URLs, instead of returning only a mechanical source dump.
- Recent web evidence is stored per conversation under `research.last_web.*` and
  mirrored into `learning_observations` as `web.research`; follow-ups like
  "какой вывод?" reuse the saved evidence without opening the operator browser.
- The synthesis layer rejects router-shaped JSON or weak model output and falls
  back to the deterministic formatter, so offline/degraded behavior is preserved.
- While restarting the backend, found stale launcher-state PIDs can point at
  unrelated processes. `jarvis-launcher.ps1 stop/restart` now verifies the saved
  PID command line matches the expected Jarvis service before killing it.
- Regression tests cover successful synthesis, JSON fallback, and follow-up
  synthesis from previous evidence.

### 2026-07-09 - Codex

- Browser policy default is now `open`: validated public HTTP(S) browser opens no
  longer need approval. `browser.open`/`browser.open_many` are still excluded from
  the autonomous agentic tool loop, so background web work should use
  `web.search`/`web.fetch` and not spam the operator's real browser.
- Added durable `learning_observations` journal. `add_message`, `record_tool_run`
  and `delete_conversation` append learning observations, so deletion removes UI
  history but not the learning source trail.
- Learning tick now reads dialogue/web observations, supervisor runs learning once
  immediately on startup, and default learning interval is 120s.
- Command Center chat links are auto-linked for Markdown, `http(s)` and `www.`
  URLs; chat height now auto-stretches and can be resized beyond the old 760px cap.
- Tests added/updated around browser-open policy and learning journal retention.

### 2026-07-09 - Codex

- Added an operator queue/kernel surface: `GET /api/operator/queue` combines
  pending/executable approvals, blocked/running mission tasks, health warnings,
  generation truncation signals, memory hygiene, and future model-profile notes.
- Added lightweight model-profile roadmap via `GET /api/model-profiles`; current
  Gemma profiles stay active/available, 70B/80B planner and fast executor roles
  are scaffolded as future/inactive.
- Added memory hygiene reporting (`GET /api/memory/hygiene`) and consolidation
  endpoint (`POST /api/memory/consolidate`). Learning tick still performs
  consolidation automatically.
- Added auto-continuation for LLM answers stopped by `finish_reason=length`,
  including streamed answers. The assistant continues internally before exposing
  the old "token limit" warning.
- Command Center now has an operator queue tab, shows linked mission/task ids
  on approvals, and adds one-click approve+execute for pending gates.
- Regression tests: `test_agentic_answer_auto_continues_after_length_finish`
  and `backend/tests/test_operator_queue.py`.

### 2026-07-09 - Codex

- Added mission approval resume: when an agentic mission step asks for a gated
  tool, the approval payload now stores `mission_id`, `task_id`, and a compact
  tool-loop resume snapshot.
- `ApprovalExecutor` can execute the approved tool and call
  `AgentRuntime.resume_mission_after_approval`, feeding the tool observation
  back into the same agentic context. The mission task becomes `done` on success
  or stays `blocked` if the approved tool/resume fails or creates another gate.
- Approved mission tool runs are recorded with `mission_id/task_id`, completed
  resumed steps are saved to mission memory, and a `mission_step` event is emitted.
- Regression test: `test_approval_execution_resumes_blocked_mission_step`.
- Next useful step: show the linked mission/task directly inside each approval
  row and optionally add a one-click "approve and execute" button in Command Center.

### 2026-07-09 - Codex

- Integrated `origin/claude/admin-assistant-enhancements-ret1id` into `main`.
- Fixed mission approval propagation so a mission step that creates an approval
  is marked `blocked` instead of `done`.
- Fixed mission task updates to verify `mission_id` before mutating a task.
- Added regression coverage for both fixes.
- Current agreement: this file is the shared notebook for future assistant notes.

### 2026-07-10 - Codex

- Shopping audit/polish pass: cleaned noisy store queries like "покажи позицию в
  днс на rtx 5090 в Москве" down to product terms before adding `site:dns-shop.ru`.
- Shopping follow-up detection no longer treats bare "результат" as previous
  shopping context; explicit "из результатов/из выдачи/прошлый поиск" still reuses
  prior ranked state.
- Candidate price parsing now understands RUB plus USD/EUR/$/€/£ formats and
  decimal separators, so cheapest sorting works for foreign marketplace snippets.
- `web.answer` now caches direct store-search answers even when sources are empty,
  and weak-shopping detection reuses structured price extraction for non-RUB prices.

### 2026-07-10 - Codex

- `Запустить всё` for missions now starts a persisted autonomy mission job through
  `POST /api/autonomy/jobs/{job_id}/start` instead of chaining all mission steps
  from the browser tab. Reloading the Command Center page no longer cancels the
  current mission run.
- The new autonomy start endpoint schedules `AutonomyExecutor.run_job` as a
  detached backend task and returns immediately; existing `/run` remains the
  awaited/manual path.
- Command Center derives active mission UI state from the persisted job lease, so
  a page reload can still show that a mission job is running.
- Regression coverage: `test_autonomy_start_runs_detached`; frontend typecheck
  passes.

### 2026-07-10 - Codex

- Tightened internet shopping/search relevance. `web.search` evidence-cache
  fallback now matches cached results against meaningful subject terms instead of
  generic words like buy/price, so stale DNS/Google Earth/etc. pages are not
  reused for unrelated shopping requests.
- `web.answer` filters irrelevant sources before ranking and, for price-sensitive
  shopping requests, only treats product/price-bearing sources as usable. Weak DNS
  category/recipe pages now fall back to a direct store search link instead of
  being presented as a concrete product answer.
- Regression coverage: `test_web_search_cache_rejects_irrelevant_shopping_results`
  plus stricter DNS weak-source assertions. `backend/tests/test_tools.py` and
  `backend/tests/test_agent.py` pass.

### 2026-07-10 - Codex (SOL full-repository overhaul)

Claude Sync Note

[Измененные файлы и новые зависимости]

- Web contour: `backend/src/jarvis_gpt/{tools,browser_cdp,web_orchestrator}.py`;
  focused tests in `test_{tools,browser_cdp,web_orchestrator,web_coverage}.py`.
- Safety/lifecycle contour: `agent.py`, `api.py`, `approval_executor.py`,
  `autonomy_executor.py`, `storage.py`, `event_bus.py`, `ingest.py`,
  `model_hub.py`, `diagnostics.py`, `operations.py`, `document_runtime.py`,
  `dispatcher.py`, `telemetry.py`, `host_bridge.py`, `models.py`, `cli.py`.
- Deployment/UI: backend/frontend Dockerfiles and `.dockerignore`, new
  `backend/docker-entrypoint.sh`, `backend/chromium-seccomp.json`, new Next
  same-origin route `frontend/app/jarvis-api/[...path]/route.ts`,
  `frontend/proxy.ts`, Command Center/trace pages, `docker-compose.yml`,
  `.env.example`, `scripts/{dev,jarvis-launcher,smoke}.ps1|py`, `docs/runtime.md`.
- Packaging: `pyproject.toml`, `backend/requirements*.txt`, `uv.lock`,
  `jarvis.py`; wheel force-includes `windows_rpc_bridge.py`.
- Dependency deltas: Pydantic 2.10.4 -> 2.13.4; pytest 8.3.4 -> 9.0.3;
  dev-only `httpx2==2.5.0`; FastAPI 0.139.0, Starlette 1.3.1,
  python-multipart 0.0.32; dispatcher image pinned to
  `vllm/vllm-openai:v0.23.0`. No Playwright/Selenium dependency: isolated
  Chromium is driven through bounded CDP.

[Что конкретно исправлено / какие заглушки устранены]

- Implemented strict `FAST_FACT`, `DEEP_RESEARCH`, `AGGRESSIVE_SHOPPING` with
  shared deadline/semaphore/request/fetch/render/network/content budgets.
  Deep research fetches in parallel and cross-verifies evidence. Shopping uses
  isolated dynamic CDP render, structured offers/currencies, negative technical
  review extraction, cross-domain issue correlation, and paid/affiliate/SEO
  low-signal rejection.
- Web boundary blocks private/link-local/metadata targets, unsafe redirects,
  WS/WSS subrequests, popup/new-target bypasses, DTD/entity XML, oversized
  bodies, credential persistence, and irrelevant evidence-cache reuse.
- Dangerous native/browser actions require atomic approval claim
  (`approved -> executing -> executed|failed`); invalid/replayed transitions
  fail with conflict. Cancellation/exception paths cannot leave executing work.
- Mission tasks and autonomy jobs are atomically claimed; stale cancellation,
  concurrent execution, blocked predecessor skipping, report races, and stuck
  running states are closed.
- Model Hub has locked download/job lifecycle, cooperative cancellation,
  restart recovery, path traversal/oversized-part guards, duplicate protection,
  bounded history, active-model mutation guards, and process shutdown tracking.
- Event broadcast is concurrent and drops timed-out clients; ingestion is
  streamed/capped and cleans partial files; blocking host/subprocess/model work
  moved off the event loop. Docker-wide prune commands removed.
- HTTP/WS origin/auth/CSRF boundaries hardened. Root API token removed from
  browser JavaScript; Next server proxy injects it server-side and fails closed.
  Browser query-string WS secrets removed. Clean observability endpoints no
  longer create self-observing tool runs.
- Deployment is non-root, read-only, seccomp-constrained, cap-minimized, and
  loopback by default. Explicit LAN mode exposes only Basic-authenticated Next;
  backend remains loopback/internal. Launcher token is 256-bit with current-user
  ACL and never reuses/kills foreign listeners.
- All AST `pass` nodes, TODO/FIXME/NotImplemented stubs, silent broad SQLite FTS
  failure swallowing, optional-smoke false successes, and repository launcher
  import side effects eliminated.

[Изменения в API-контрактах, сигнатурах функций и структурах данных]

- `web.search`, `web.research`, `web.answer`: optional `mode` and
  `deadline_sec`; responses add `mode`, `orchestration`, and shopping results add
  `shopping`. `browser.open.danger_level` is now `review`.
- Added pure `internet_observability_snapshot()` and
  `browser_handoff_snapshot()` plus GET `/api/internet/observability` and
  `/api/browser/handoff`.
- Added POST `/api/model-hub/downloads/{download_id}/cancel` and UI cancellation.
- Approval public updates no longer accept `executed`; illegal state transitions
  return HTTP 409. Storage adds atomic claim/finalize primitives for approvals
  and mission tasks.
- WS authentication uses `Sec-WebSocket-Protocol`; URL token is unsupported.
  Cross-site HTTP mutations are rejected before loopback bypass.
- Frontend API base is `/jarvis-api`; `NEXT_PUBLIC_JARVIS_API_TOKEN` and direct
  browser WebSocket root-token transport are removed. Missing server token -> 503.
- Smoke JSON uses semantic `status`, separate `http_status`, top-level `degraded`.

[Pending: Текущие точки сборки и что Claude должен делать/проверить дальше]

- Current gates: backend `337 passed`; Ruff/compileall/AST stub scan clean;
  frontend typecheck/build clean; `pip-audit` and `npm audit --omit=dev` report
  zero known vulnerabilities; Compose config resolves; Python 3.14.5 isolated
  wheel install/import-all/CLI/packaged bridge pass; final backend image healthy
  as UID 10001 with Chromium 150. Live FAST/DEEP/SHOPPING modes pass within budgets.
- No blocking code issue. Production reliability still benefits from at least
  one configured Brave/Tavily/Serper API key because anonymous HTML providers
  can rate-limit or challenge. CAPTCHA path intentionally requires operator
  handoff rather than bypass.
- Residual theoretical boundary: an already-running operator Chrome cannot have
  DNS resolution cryptographically pinned; attached-session CDP validates and
  intercepts URLs, while autonomous isolated rendering additionally pins host
  resolver rules. Keep autonomous research on isolated renderer.
- Claude: review/commit the patch as one coordinated migration; then run target
  host GPU/vLLM model-load and real LAN-client launcher smoke. Do not reintroduce
  public backend binding or browser-visible root credentials.
