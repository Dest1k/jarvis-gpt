# Runtime

## 2026-07-22 handoff - owner Telegram operator console

- `/admin/telegram` is an owner-only Telegram-style console with searchable chats,
  three-second polling, stable cursor pagination through the full retained timeline,
  responsive mobile navigation, and literal 1-4096 character Bot API sends.
- The transport journal records inbound messages and edits plus delivered text chunks,
  voice/audio, files, proactive alerts, reminders, and manual operator sends. Telegram
  `message_id` defines equal-second ordering; request hashes suppress synthetic backend
  duplicates. A durable per-turn delivery receipt fences crash/restart replays.
- Manual sends pin `telegram:<bot-id>` with `getMe`, use audited idempotency keys, never
  expose the bot token to the browser, and fail closed on ambiguous delivery. Only owner
  capabilities `admin.telegram.messages.read/send` can access the endpoints.
- Bare quick capture requires a marker followed by whitespace (`+ idea`, `! urgent`), or
  `/note`. Inputs such as `++`, `!!`, `+word`, and `!word` are ordinary chat messages.

## 2026-07-22 handoff - durable multilingual memory and account-aware retrieval

- Chat ingress is written to canonical `messages` before transcription, retrieval,
  planning, tools, or LLM I/O. Failed turns therefore remain searchable with
  `ingress_status=accepted`; successful turns become `processed`. Service-mode and model
  overload replies use the same durable, request-idempotent turn contract.
- `messages_fts`, `memories_fts`, and `file_chunks_fts` use FTS5 trigram tokenization when
  available, with Unicode literal fallback for short terms. Message lookup covers the
  complete tenant history instead of a recent window and excludes the current persisted
  turn from its own recall. RU/EN/ZH/KO/JA migration, edit/delete triggers, restart, and
  tenant-isolation cases are covered. Schema markers prevent a full FTS rebuild on every
  startup; rebuilds run only for a real tokenizer/schema migration.
- File ingestion claims a content hash before extraction and commits chunks, status,
  count, error, and provenance atomically. Startup fails interrupted indexes closed while
  retaining uploaded bytes for retry. Every PDF receives a bounded completeness OCR pass;
  native text is preserved and OCR text is appended atomically with page/truncation
  provenance. Failed jobs can start at most three hash-verified retry generations, and a
  corrupt managed blob is healed only by reuploading bytes with the canonical hash.
  Generated documents use the same claim/reindex protocol.
- `accounts.overview` and `materials.search/recent/read/summarize` are owner/admin-only even under
  direct grants or custom presets. They support exact immutable account selection,
  exact `@username` or exact unique Unicode-normalized formal/display-name selection
  (missing and ambiguous mutable selectors fail closed and never grant authority),
  fair per-account hybrid lexical/semantic retrieval, multilingual query variants,
  full-corpus keyset-paged semantic scanning, provenance, hashed access audit, full-source
  synthesis, and claim-level validated citations. Partial document/OCR indexes carry
  sanitized completeness warnings into both API output and model evidence. Tool history is
  metadata-only. If model prose still fails citation validation after correction, Jarvis
  returns a deterministic, bounded evidence digest with exact citations instead of an
  intermittent empty failure or the invalid draft. If the requester is demoted while retrieval or
  synthesis is running, the result is withheld; previously generated privileged assistant
  turns and compacted memories also disappear from normal-user recall.
  Query-free requests for the latest messages of an exact Jarvis `@username` use
  `materials.recent`, not Telegram channel search; results are snapshot-consistent,
  deterministically ordered, and carry stable `message:<id>` citations. Ordinary accounts
  receive a reduced system context and deterministic denial for clear
  cross-user material or Jarvis-internal requests.
  Date-scoped cross-user document digests use `materials.summarize`, never tenant-own
  `documents.recall`; only supported document types are selected, voice/audio rows are
  excluded, short-digest wording still triggers content reads, and every source carries a
  stable `document:<file_id>` citation. Compact date digests are bounded, cover every
  selected document with one cited bullet, and add a short cited conclusion; overlong or
  incomplete drafts are rewritten or rebuilt from evidence instead of being raw-truncated.
- Public web search defaults to the global `wt-wt` region and accepts `languages` /
  `translated_queries` for RU, EN, ZH, KO, and JA round-robin research, adding UK and FA
  searches when the requested geography calls for them. Explicit language sets use
  independent regional requests and do not reuse another language corpus cache. Search
  locale is diagnostic provenance, never proof that a source belongs to that language or
  internet segment. Global and multi-country news reports measure every requested geography
  and require verified non-RU evidence from at least two independent domains whose articles
  match a requested geography; unrelated foreign news cannot satisfy the quota. A RU-only or
  scope-incomplete result is reported as an evidence gap and is not cached as complete.
  «За сутки» means an exact rolling 24-hour publication window. Every response exposes
  requested/covered/missing language, geography, and source-segment status.
- Telegram channel/supergroup feeds have durable owner/admin registration, live
  `channel_post`/edit ingestion before offset acknowledgement, RU/EN/ZH/KO/JA query variants,
  Unicode search, and bounded provenance-preserving analysis. The Bot API tier covers future
  posts delivered to the bot. Authorized-reader history uses durable `before_message_id`
  checkpoints and resumes after failure beyond 500 posts. An optional JSON-over-stdio adapter
  can use an already authenticated external CLI without passing Jarvis secrets or accepting
  Telegram credentials in tools. Personal-account monitoring remains forbidden; an absent
  external session reports `unconfigured` instead of claiming success. Private-chat transient
  backend failures remain durably ordered and retry with bounded backoff without a 24-hour
  tombstone; permanently rejected attachments still create a searchable delivery record.
- Telegram response modality defaults to `auto`: direct text/captions receive text, direct
  voice/audio without a caption receives speech, and forwarded media remains source material
  answered in text. `/voice text|voice|auto` stores the account-scoped choice durably, while
  a one-shot request does not change that preference. Explicit inline commands such as
  «озвучь этот текст: …», «зачитай: …» and «прочитай вслух: …» return the literal supplied
  text without an LLM rewrite and deliver it as voice. The admitted chat text (up to the API's
  20,000-character bound) is split into as many TTS/Telegram parts as required; no part is
  silently dropped. Successful multipart delivery is logged without answer text, and
  header-only/unplayable WAV renders are rejected.
  Telegram's per-recipient `VOICE_MESSAGES_FORBIDDEN` response is respected and falls back to
  the complete text with an explicit privacy notice; it is never bypassed through `sendAudio`.
  Silero retries presentation-free spoken text when its normalizer rejects Markdown, while
  `JARVIS_TTS_TEMPO=1.08` slightly speeds the same Aidar voice with pitch-preserving FFmpeg
  `atempo`. Failed Opus conversion, synthesis, tempo processing, or delivery keeps a verified
  WAV when possible and otherwise falls back to complete text. Raw WAV is never sent to Telegram.

Current safety bounds remain deliberate: uploads are capped at 50 MiB, automatic plain-text
indexing at 5 MiB, structured extraction at 200k characters, OCR at 30 PDF pages processed
one at a time with 8M pixels / 8192 px / 16 MiB PNG per page, and legacy `.doc`/`.xls`
requires conversion. Stored-but-unindexed files remain
discoverable by metadata and report an actionable status; Jarvis must not claim content
searchability until conversion/OCR/reindex succeeds.

To connect an existing authenticated Telegram history CLI, set
`JARVIS_TELEGRAM_READER_COMMAND_JSON` to a JSON argv array whose first item is an absolute
executable path. The command implements protocol `jarvis.telegram-reader.v1` on stdin/stdout;
Jarvis invokes it without a shell and passes only a minimal OS environment. The executable
must own and protect its session itself. `JARVIS_TELEGRAM_READER_TIMEOUT_SEC` is bounded to
2-300 seconds. Without this command and its pre-existing authenticated session, private/history
capability remains unavailable while Bot API live-channel subscriptions continue to work.

## 2026-07-10 handoff - web answer bugfix

For the operator and the second model:

- `web.answer` fallback output is now user-facing, not a research dump. Chat
  bubbles should contain concise markdown links; confidence, source scoring,
  gaps, and claim citations belong in cards/trace.
- Explicit site intent is respected. Known aliases such as "на ДНС" map to
  `dns-shop.ru`; unrelated sources are filtered out. If the named site is
  blocked or thin, Jarvis can still answer with a direct site search link.
- Bing `/ck/a` result URLs are unwrapped before evidence storage/ranking.
- Answer-cache version was bumped so old verbose cached answers are ignored.
- Long URLs now wrap in the chat UI instead of widening the transcript.

## 2026-07-10 handoff - Google replacement quality pass

For the operator and the second model:

- Use `internet.search_api.status` to check Search API setup without exposing
  secrets. It reports masked key presence, supported verticals, recent
  provider ok/fail stats, and optional live probes with `check=true`.
- `web.answer` now includes claim-level citation data in `claim_citations` and
  `cards.claim_citations`; Command Center renders a compact source/citation
  panel under answers produced by the answer engine.
- `cards.vertical_cards` is the structured extractor layer for vertical search
  results: product prices/availability, contact hints, article dates, and
  schema hints when sources expose them.
- `web.eval` has a broader default catalog (20+ web/news/shopping/places/
  scholar/images cases) but remains bounded by `limit` (default 8, max 30).
- Use `documents.review` before serious Office/PDF edits. It reports OCR need,
  OCR binary availability, Word redline/edit readiness, Excel formula/style
  audit, optional reference comparison, and recommended next steps.
- `web.transcript` can transcribe local/quarantined media paths and explicit
  `allow_download=true` media URLs when the local `whisper` CLI exists. If not,
  it returns `local_transcription.available=false` rather than fabricating text.
- Use `browser.session.diagnose` before hard web tasks in operator Chrome. It
  combines CDP status, active handoff, optional page read, consent/login/forms,
  and recommended route.

## 2026-07-10 handoff - Google replacement answer engine

For the operator and the second model:

- Use `web.answer` as the first-choice "replace Google" tool for ordinary
  internet questions. It expands the question, infers freshness when needed,
  calls `web.research`, ranks fetched/cited sources, diversifies domains,
  verifies coverage, and returns `answer`, `sources`, `citations`,
  `confidence`, `cards`, `synthesis`, `cache`, and `steps`.
- `AgentRuntime._run_web_research` now tries `web.answer` first when the real
  ToolRegistry is active. If `web.answer` is unavailable or fails, the old
  `web.search` -> `web.fetch`/`web.render` path still handles the request.
- `web.answer` deliberately builds on the existing guarded stack instead of
  bypassing it: public-only URL validation, fetch cache, consent detection,
  render/archive fallback, evidence storage, and verification still apply.
- `web.answer` now has a short answer-level TTL cache for repeated same-question
  calls. Use `use_cache=false` for testing or when the answer must be recomputed.
- LLM synthesis is optional and strict. When enabled and the local LLM is live,
  it receives only compact ranked-source payloads, must keep supplied source
  URLs/domains in the visible answer, and is rejected back to the deterministic
  cited report if it is ungrounded, JSON/tool-like, or too short.
- `cards` is the UI/agent-friendly structured layer: source mix, top sources,
  compact fact excerpts, verification gaps, and follow-up queries.
- `web.search` supports optional Search API providers before HTML fallback:
  Brave (`JARVIS_BRAVE_SEARCH_API_KEY`/`BRAVE_SEARCH_API_KEY`), Tavily
  (`JARVIS_TAVILY_API_KEY`/`TAVILY_API_KEY`), and Serper
  (`JARVIS_SERPER_API_KEY`/`SERPER_API_KEY`). With no keys configured it keeps
  the existing DuckDuckGo/Bing/Yandex path.
- Search/research/answer now accept verticals: `web`, `news`, `images`,
  `shopping`, `places`, `scholar`. Serper covers all listed verticals, Brave
  covers web/news/images, and Tavily covers web/news.
- `web.crawl` now has deeper bounded traversal controls: `depth`,
  `follow_text`, `include`, `exclude`, `render_fallback`, and
  `archive_fallback`.
- `web.transcript` extracts public caption/transcript text when available
  (YouTube caption tracks first, HTML transcript fallback). `web.eval` runs a
  bounded answer-quality harness over `web.answer` cases.
- `internet.observability` exposes Search API readiness, supported verticals,
  and answer-cache count; Command Center shows those signals in the internet
  panel.

## 2026-07-10 handoff - internet coverage: archive, feeds, weather, page watches

Для оператора и второй модели. Продолжение интернет-темы Codex: активный сёрфинг
(CDP, render, extract, verify, research, download-карантин) уже есть; этот проход
закрывает четыре оставшихся бытовых кейса, где Jarvis раньше упирался в тупик или
делал ненадёжный обход.

- `web.archive` (safe): чтение Wayback-снапшота публичного URL через
  availability API + существующий public-only fetch-путь. Когда живая страница
  заблокирована/исчезла — это теперь не тупик. Ответ несёт `snapshot_timestamp`
  и `archive_note` («данные исторические»); blocked-ответ `web.fetch` теперь сам
  подсказывает «Try web.archive… or web.render».
- `web.feed` (safe): RSS 2.0/RDF/Atom без скрейпинга HTML — bounded XML parse
  (лимит ~200k символов, отказ на переполнении/не-XML), entries с
  title/link/published/summary, evidence-запись, соблюдение domain cooldown.
- `web.weather` (safe): бесключевой Open-Meteo (геокодинг → forecast),
  русские WMO-описания, «сейчас + N дней» в `data.report`, evidence.
  Погодный fast-path агента теперь пробует `web.weather` ПЕРВЫМ (и для явной,
  и для выведенной из persona локации) через `_try_weather_tool`; форма ответа
  валидируется строго (нужны report и source=open-meteo.com), любой сбой —
  честный фолбэк на старый поисковый маршрут, офлайн-поведение не тронуто.
- `web.watch` — мониторинг страниц («следи за ценой/наличием/статусом»):
  - Новый autonomy job kind `web.watch` (operations whitelist, default
    max_runs=500). Исполнитель `AutonomyExecutor._run_web_watch`: web.fetch →
    нормализованный текст или первый regex-`pattern` матч → sha256 против
    состояния в KV `web.watch.state.{hash(url+pattern)}`. Baseline при первом
    прогоне; изменение → warn-событие `web.watch`, durable memory (namespace
    `web`), bus publish. Сбой fetch НЕ убивает вотч (job остаётся enabled).
  - Safe-инструменты `web.watch.add` (валидация URL/regex/cadence, дедуп по
    url+pattern, лимит 12 активных), `web.watch.list` (с last state),
    `web.watch.remove`. Мутация durable state сознательно разрешена автономно:
    bounded, аудируемая через create_job, видима и отменяема в Command Center —
    тот же принцип, что persona.insight.
- SYSTEM_PROMPT: новый пункт про специализированные интернет-маршруты
  (weather/feed/archive/watch), чтобы модель тянулась к ним по смыслу.
- Тесты: `backend/tests/test_web_coverage.py` (10): парсер RSS+Atom и отказ на
  мусоре, web.feed с evidence, archive снапшот и отсутствие снапшота, формат
  погодного отчёта, погодный маршрут предпочитает Open-Meteo (web.search не
  вызывается), add/list/remove/лимит вотчей, baseline→no-change→change с
  памятью и событием, персист job kind. Прогон — 244 pass, ruff clean,
  frontend typecheck + build clean.
- Кандидаты дальше: показать активные вотчи в Command Center отдельной строкой
  (сейчас видны в общей панели autonomy jobs), цепочка web.feed→web.watch для
  «следи за новостями по теме», и web.archive как автоматический фолбэк внутри
  web.research при blocked-источниках.
## 2026-07-10 handoff - web search, archive, crawl, lazy pages

For the operator and the second model:

- `web.search` now accepts `region`, `freshness`, `pages`, and `provider`.
  Defaults are tuned for Russian-local use (`region=ru-ru`, auto provider).
  Auto provider order is DuckDuckGo HTML, Bing HTML, then Yandex HTML fallback.
- `web.research` now searches up to two pages by default and tries live
  `web.fetch`, `web.render` with scroll passes, then `web.archive` before
  giving up on a blocked or consent-walled source.
- `web.fetch` keeps a 15-minute TTL cache for successful public reads. Use
  `use_cache=false` when testing rate budgets or when a truly fresh live fetch
  is required.
- Cookie/consent walls are reported through `safety.consent_wall_detected` and
  `consent_wall=true`; treat those results as not enough evidence until the
  page is accepted in browser or another source/archive is found.
- `web.crawl` does bounded same-site traversal from a start URL. It prioritizes
  `rel=next`/next-like links and returns page evidence ids plus short excerpts.
- `browser.scroll` is review-gated and uses the operator Chrome CDP session.
  Use it for logged-in/lazy pages after `browser.chrome.launch`.
- `web.render` supports `scroll_passes` for isolated headless lazy-load reads.

## 2026-07-10 handoff - document intelligence tools

For the operator and the second model:

- Uploaded chat files and local paths can now go through the same safe document
  layer. Use `file_id` for chat uploads or `path` for local files under the
  workspace, `JARVIS_HOME`, or the user home directory.
- Low-level engine: `document_runtime` (extract/compare/replace).
- High-level black box: `document_surfer.JarvisDocumentSurfer` (document analogue
  of `web_surfer`) with inspect/read/analyze/review/compare/search/
  summarize_corpus/edit_plan/apply_replacements/generate/convert/package/
  capabilities.
- Tools: `documents.inspect`, `documents.read`, `documents.review`,
  `documents.compare`, `documents.edit.plan`, `documents.apply_replacements`,
  `documents.analyze`, `documents.search`, `documents.corpus.summarize`,
  `documents.generate`, `documents.convert`, `documents.capabilities`, and
  `documents.recall` for durable file-memory retrieval plus analysis evidence.
- DOCX extraction reads paragraphs, tables, comments, and style names. XLSX
  extraction reads sheet previews, shared strings, and formulas. PDF extraction
  uses `pypdf` if available and otherwise a basic text fallback. Text/html/json/
  csv are read directly. Extended best-effort extract: PPTX/ODT/RTF.
- Generation formats: md, txt, csv, json, html, docx, xlsx (stdlib OOXML writers).
- Ingestion indexes DOCX/XLSX/PDF/PPTX/ODT/RTF/text-like uploads into durable
  file chunks, so later conversations can find document content without a new
  attachment.
- `documents.apply_replacements` / surfer mutations write edited copies to
  `data/document-outputs`, register files, and never overwrite originals.
- Use `documents.review` / `documents.analyze` before serious Office/PDF edits.

## 2026-07-12 handoff - document_surfer release

- `document_surfer.JarvisDocumentSurfer` is the production document black box
  (document analogue of `web_surfer`), backed by `file_types` and
  `archive_runtime`. The `documents.*` tools are first-class in the
  ToolRegistry and the agent safe allowlist.

## 2026-07-12 handoff - archives + file type recognition

- `file_types.identify_path/bytes`: magic-byte + compound-extension recognition
  for archives, documents, images, media, executables, text/code, etc.
- `archive_runtime`: safe list/extract/read/create for zip, tar, tar.gz, tar.bz2,
  tar.xz, gz, bz2, xz; optional 7z (`py7zr`) and rar (`rarfile`). Path traversal
  and uncompressed size bombs are rejected.
- New tools: `documents.file.identify`, `documents.file.probe`,
  `documents.archive.list|extract|read_member|create|search`.
- `documents.inspect` on archives returns member listing instead of forcing
  document text extraction.

## 2026-07-12 handoff - durable document recall

- `documents.recall {query, file_ids?, focus?, max_files?, max_chars?}` joins
  persistent `files`/`file_chunks` memory with `document_surfer`: filename and
  content matches resolve to stable `file_id` values, the stored sources are
  read and analyzed, and bounded passages/corpus evidence feed the final LLM
  summary. Specific misses fail closed instead of selecting an unrelated recent
  file.
- Historical document requests get a dedicated task-kernel route. Indexed-file
  prompt context includes `file_id`; same-conversation follow-ups bind to the
  latest attachment turn or validated recalled source ids stored in message
  metadata. Temporal requests choose the newest matching document but never
  broaden a failed named match to an unrelated recent file.
- Persisted ZIP/RAR/7z/TAR requests use a separate archive-memory route and the
  `documents.archive.*` tools instead of being rejected by document recall.
- Document observations use larger, content-first bounds and are explicitly
  marked as untrusted data, so long text reaches synthesis without becoming
  instructions. Tool argument hints now render for the repository's shorthand
  schemas.

## 2026-07-10 handoff - internet production surface

For the operator and the second model:

- Prefer `web.research` for current internet answers that need sources. It runs
  search -> fetch/render fallback -> extract -> verify, returns a report plus
  citations, and keeps recent records in `web.research.records`.
- Use `web.document.read` for downloaded web documents. It only reads Jarvis
  quarantine downloads, extracts bounded text, stores a new evidence id, and
  does not open or execute the file. Oversized files and oversized Office ZIP
  members are refused/skipped.
- Use `internet.observability` to inspect web/browser health: recent ok/failed
  runs, blocked-page summaries, evidence/research counts, rate cooldowns,
  search providers, top domains, and active `browser.handoff.status`.
- Use `internet.smoke` for a live non-mutating check of the internet stack. It
  checks Chrome CDP status, browser handoff, `web.fetch`, `web.extract`,
  `web.verify`, and returns an observability snapshot.
- Command Center -> status now includes the internet panel with handoff,
  observability metrics, recent blocked summaries, top domain/provider, and a
  smoke button.

## 2026-07-10 handoff - internet surfing quality

For the operator and the second model:

- `browser.click`, `browser.type`, and `browser.select` can now use semantic
  `target` hints instead of brittle CSS selectors. The resolved selector and
  target info are returned in tool data. These tools remain review-gated.
- `browser.handoff.status` exposes the current CAPTCHA/login/sensitive-form
  checkpoint. If a page needs human work, finish it in the Chrome CDP window and
  retry `browser.read` or the same browser action.
- `web.search` falls back from DuckDuckGo HTML to Bing HTML when needed and
  stores an evidence id for the result page.
- `web.fetch` and URL-based `web.extract` now keep parsed HTML metadata in
  evidence: JSON-LD/schema.org, OpenGraph/meta, canonical URL, and simple
  readability paragraphs/headings.
- `web.verify` checks a claim against saved evidence, URLs, or search snippets
  and reports coverage, independent domains, missing terms, and confidence.
- Agent tool-loop guidance now points web tasks through
  `web.search` -> `web.fetch`/`web.render` -> `web.extract` -> `web.verify`
  when the claim needs current source-backed evidence.

## 2026-07-10 handoff - internet workflow tools

For the operator and the second model:

- New review-gated Chrome CDP tools: `browser.click`, `browser.type`,
  `browser.select`, and `browser.screenshot`. They open/read through the local
  Chrome DevTools endpoint, return snapshots, and do not read form values.
  `browser.type` blocks password/card/token-like targets unless
  `allow_sensitive` is explicitly approved.
- Web/browser observations now save compact runtime evidence records and return
  `evidence_id`. Use `web.evidence.list` to inspect recent records before doing
  follow-up synthesis.
- `web.extract` can pull structured article/product/contact/table hints from a
  URL, an `evidence_id`, or supplied text.
- Web requests now have per-domain budgets and cooldowns after blocked or
  rate-limited responses. Treat this as intentional backoff, not a transient
  network failure.
- `web.download.inspect` inspects only files under the Jarvis quarantine
  download cache, reports signature/SHA256/executable risk, and lists ZIP
  entries without opening or executing them.

## 2026-07-22 handoff - authenticated LAN deployment

For the operator and the second model:

- `jarvis-launcher.ps1 start` and `app` remain loopback-only by default. With
  `-Lan -LanSubnet 192.168.31.0/24`, only the authenticated Command Center is
  bound to the selected LAN address; it validates each socket peer against the
  configured subnet. The launcher still generates an ACL-restricted server API
  token; it is exchanged for a signed HttpOnly UI session and is never exposed
  to browser JavaScript.
- The launcher refuses to reuse or terminate listeners on ports 3000, 8000, or
  8765 unless their command line belongs to the corresponding Jarvis service.
- Local and Compose starts bind the UI to fixed `127.0.0.1:3000`. LAN mode uses
  the dedicated authenticated UI server, while the backend, dispatcher, and host
  bridge remain on loopback/private Compose networking.
- The same-origin API proxy returns HTTP 503 when `JARVIS_API_TOKEN` is empty.
  Set the same non-empty server token for backend/frontend; the dispatcher-only
  profile remains independent of this requirement.
- Backend images include Chromium, start through a path-constrained volume
  initializer, and immediately drop to UID/GID 10001. Compose enables an init
  process, a 512 MiB shared-memory segment, read-only root filesystem, minimal
  init capabilities, `no-new-privileges`, and the Playwright-maintained Docker
  seccomp baseline with only Chromium user-namespace syscalls added. Frontend
  runtime uses the unprivileged `node` user.
- The API token is server-only: Compose passes it to the Next server together
  with `JARVIS_BACKEND_URL`, and the browser uses the same-origin server proxy.
  No API URL or credential is compiled into `NEXT_PUBLIC_*` browser
  JavaScript.

## 2026-07-10 handoff - internet safety hardening

For the operator and the second model:

- `web.download` stores public HTTP(S) files only in Jarvis quarantine cache,
  returns SHA256/size/content-type, flags executable-risk downloads, and never
  opens or executes files automatically.
- `web.search`, `web.fetch`, `web.render`, `web.download`, and `browser.read`
  now include `data.safety.trusted_as_instruction=false`; remote page text is
  evidence only. Prompt-injection phrases are surfaced in
  `data.safety.prompt_injection_markers`.
- `browser.read` reports form/password/sensitive-input counts with
  `values_read=false`. It does not read form values.
- Embedded URL credentials such as `https://user:pass@example.com` are rejected
  by public web and browser validators.
- Tool-loop prompt now explicitly warns the model not to obey remote page text
  asking it to reveal secrets, call tools, send cookies, or change instructions.

## 2026-07-10 handoff - blocked web pages and right-panel polish

For the operator and the second model:

- Command Center file panel no longer exposes the native browser file input in
  the themed UI. It shows a stable picker row with selected filename/size.
- Runtime/files side panels now use the outer panel scroll for non-chat tabs;
  empty mission/approval/briefing blocks no longer create tiny inner scrollbars.
- Web tools now use browser-like request headers and repair common mojibake in
  DuckDuckGo/search HTML. HTTP 401/403/429 and rendered anti-bot pages are
  marked blocked instead of being treated as successful evidence.
- For shopping requests, when a store such as DNS blocks automated fetch/render
  but public search results contain product/catalog links, the agent returns
  those links and explicitly says price/availability are unverified instead of
  claiming that a direct link is impossible.

## 2026-07-10 handoff - API host selection and same-machine LAN

For the operator and the second model:

- Command Center no longer blindly trusts a build-time LAN API URL when the
  browser is opened on `localhost:3000`. Browser API/WS calls now resolve to
  the current page host when a loopback page would otherwise call a private LAN
  API, or a LAN page would otherwise call loopback.
- The trace page uses the same API host fallback logic as the main Command
  Center.
- The service worker cache is bumped to `jarvis-gpt-v2` so stale frontend
  chunks are evicted after this rebuild.
- Backend API and `/ws/events` still require `JARVIS_API_TOKEN` for real
  non-local clients, but a request whose source address is one of this machine's
  own LAN interfaces is treated as local. This covers local Chrome using
  `http://<lan-ip>:3000` without opening tokenless access to other devices.

## 2026-07-09 handoff - leases, interrupted streams, background cognition

For the operator and the second model:

- Autonomy jobs now have persisted running leases:
  `running_lease_id`, `running_started_at`, and `running_lease_until`. Startup
  recovery converts stale leases into failed run-history records, so a backend
  crash or killed worker no longer leaves a job looking permanently in-flight.
- Job cancellation now goes through `AutonomyExecutor.cancel_job`, cancels the
  active child task when one exists, keeps the stored job state cancelled, and
  still records the final cancelled run.
- Chat streaming now persists a partial assistant message when the HTTP stream
  is cancelled before `done`. Partial messages carry
  `metadata.interrupted=true`; the last interrupted stream marker is available
  at `/api/chat/stream/interrupted/{conversation_id}`.
- New background cognition loop: when autonomy and LLM are enabled, supervisor
  starts `jarvis-cognition-loop` every `JARVIS_COGNITION_INTERVAL_SEC` (default
  300). It asks the local model for strict JSON observations over recent runtime
  events, learning observations, counters, and autonomy jobs, then saves
  `cognition.last_pulse` and a `cognition.pulse` learning observation. It is
  intentionally observational: no browsing, no host mutation, no automatic job
  creation.
- Config/env additions: `JARVIS_COGNITION_ENABLED`,
  `JARVIS_COGNITION_INTERVAL_SEC`, `JARVIS_COGNITION_MAX_TOKENS`, and
  `JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK`.
- Tool-run persistence now redacts obvious secrets (`token`, `secret`,
  `password`, `authorization`, `cookie`, bearer values) before storage/audit/
  learning. `system.inspect` screen capture can request OCR if `tesseract` is
  installed.
- Command Center chat changes: live streaming no longer forces the transcript
  back to the bottom after the operator manually scrolls up, and desktop chat/
  side panels stretch to the viewport instead of leaving dead lower space.

## 2026-07-09 handoff - headless browsing, distilled learning, autonomy controls

For the operator and the second model:

- Model profiles are intentionally left as future scaffolding in this pass.
- `web.render` is now available for JS-heavy public pages. It launches an
  isolated headless Chrome/Edge process with a temporary profile, returns visible
  DOM text, and never opens the operator's working browser.
- `web.search` and `web.fetch` now use a public-only async transport that pins
  TCP connections to DNS answers Jarvis already validated as public. This closes
  the earlier DNS-rebinding gap while keeping SNI/Host on the original hostname.
- `system.inspect` now includes read-only `screen.capture`, writing screenshots
  to Jarvis cache by default. Mutating desktop/native actions stay behind the
  approval-gated `windows.native` path.
- `learning.tick` has an async LLM-assisted path. It still derives deterministic
  lessons first, then asks the configured local LLM for strict JSON with up to
  two short, non-secret, grounded lessons from recent feedback/runtime signals.
- New quality surface: `GET /api/operator/quality` and a Command Center Quality
  panel summarize recent negative feedback, verifier revise signals, and top
  repeated gaps.
- Autonomy jobs now support `priority`, optional `deadline_at`, cancellation,
  runtime budget timeouts, priority-aware due-job ordering, and queue items for
  failed/cancelled jobs.
- The trace page now includes a compact event timeline below the graph, making
  observable routing/tool/synthesis events easier to review without exposing
  hidden chain-of-thought.

## 2026-07-09 handoff - runtime guardrails and autonomy observability

For the operator and the second model:

- Backend API is local-first by default. Loopback clients still work without
  setup; non-loopback clients now need `JARVIS_API_TOKEN` via bearer auth or
  `X-Jarvis-Api-Token`. The current frontend keeps this token server-side and
  forwards authenticated traffic through its same-origin proxy. Browser origins
  must match loopback or `JARVIS_CORS_ORIGINS`.
- New operator endpoints:
  `GET /api/runtime/security`, `POST /api/runtime/backup`, and
  `GET /api/autonomy/job-runs`. The backup endpoint uses SQLite's backup API and
  writes durable audit/event records.
- Autonomy jobs now keep `consecutive_failures`, `last_started_at`,
  `last_finished_at`, `last_duration_ms`, and `next_run_after`. Failed enabled
  jobs get bounded exponential retry backoff instead of tight retry loops.
- `AutonomyExecutor.run_job` records both successful runs and caught exceptions
  into the job run history, so background failures are visible after the fact.
- Command Center surfaces API guard status, manual DB backup, last backup path,
  job retry state, and the last few job runs in the runtime/resources panels.
- Config sync: `.env.example` includes the server-only `JARVIS_API_TOKEN`.

## 2026-07-09 handoff - reasoning-first arbiter now owns local_action

Для оператора и второй модели. Продолжение system.inspect: тот инструмент дал
модели руки для инспекции, но МАРШРУТ к нему всё ещё решали keyword-эвристики.
Теперь понимание местных задач тоже перешло к арбитру, как раньше для
web_research.

- Две дыры закрыты в `agent.py`:
  1. Арбитр возвращал `local_action`, но код это решение ИГНОРИРОВАЛ — оно
     проваливалось в web-ветки, и «покажи автозагрузку» уходило в интернет-поиск
     вместо локальной инспекции. Теперь `_try_direct_action` обрабатывает
     `arbiter.route == "local_action"` (confidence >= 0.6): переписывает план
     через новый `_local_action_plan_from_intent` и возвращает None → агентный
     loop с нативными инструментами (system.inspect safe + windows.native под
     approval).
  2. Арбитр запускался ТОЛЬКО для `web_research`. Обычные запросы о машине
     (`_looks_like_local_query`) шли в `route=reasoning/local_admin_advice` мимо
     арбитра — модель могла лишь ПОСОВЕТОВАТЬ команду, а не выполнить инспекцию.
     Гейт `_understand_intent` расширен на этот локальный bucket, поэтому арбитр
     подтверждает `local_action` и уводит запрос к инструментам. Расширение
     узкое (только `local_admin_advice`), офлайн-путь не тронут (арбитр гейтится
     на `llm_enabled`), доп. LLM-вызов — только в этом уже-эвристически-локальном
     bucket, не на каждом сообщении.
- Промпт арбитра усилен: `local_action` теперь явно включает и ЧТЕНИЕ состояния
  машины (железо/ОС/диски/RAM/батарея/службы/автозагрузка/принтеры/сеть/процессы),
  и ДЕЙСТВИЯ (открыть приложение, ввести текст, переключить окно, локальная
  команда), с примерами и явным «это НЕ web_research: состояние читается локально».
- Детерминированные нативные fast-path (`_native_action_from_message`,
  host-команды, URL) не тронуты — они срабатывают ДО арбитра и возвращаются
  раньше, поэтому все офлайн-тесты нативного слоя без изменений.
- Тесты: `test_arbiter_routes_local_query_to_native_inspection` (арбитр→
  local_action→ модель зовёт system.inspect, web.search не вызывается),
  `test_arbiter_gate_opens_for_local_bucket_and_stays_closed_for_chat` (гейт
  открыт для local_admin_advice, закрыт для обычного чата). Прогон — 190 pass,
  ruff clean, frontend clean.
- Остаётся кандидатом: провести через арбитр также mission/native мутирующие
  действия глубже (сейчас мутации уходят в approval-гейт, что корректно), и
  symmetричный broad WinAPI read.

## 2026-07-09 handoff - system.inspect: unlock the model's WMI/WinAPI understanding

Для оператора и второй модели. Ответ на вопрос «что мешает 26-31B модели
понимать бытовые Windows-запросы с полуслова». Диагноз: не знание модели (Gemma
хорошо знает Win32_* и PowerShell), а то, что нативный маршрут решают
детерминированные keyword-эвристики ДО модели, и единственная read-only
инспекция, покрывающая большинство бытовых вопросов о машине — WMI — была
недоступна модели:
- `_wmi_action_from_message` срабатывал только на литеральном слове «wmi»/«cim»
  и мапил в жёсткую таблицу из 5 классов (process/service/gpu/bios/disk).
  «Сколько оперативки», «заряд батареи», «что в автозагрузке», «список
  принтеров» туда не попадали.
- `wmi.query` жил внутри `windows.native` (danger, т.к. тот же инструмент делает
  process.start/keyboard.send), поэтому агентный tool-loop — где модель применяет
  своё понимание — был от него отрезан.

Фикс (хирургический, на-тезис «понимание вместо затычек», «у модели есть руки»):
- Новый safe read-only инструмент `system.inspect` (danger_level=safe,
  category=host): действия только из allowlist `SAFE_INSPECT_ACTIONS`
  ({capabilities, window.list, wmi.query}). Мутирующие действия (process.start,
  keyboard.send, app.open_and_type, window.focus) отклоняются с подсказкой уйти
  на approval-gated `windows.native`. Переиспользует уже валидированный путь
  `wmi.query` (SELECT-only, алфавит-валидация класса/свойств, без вызова методов)
  через общий `_run_native_bridge_command`.
- Так как инструмент safe и не в `AGENTIC_TOOL_DENYLIST`, он автоматически в
  `_autonomous_tools()` и в tool-protocol-промпте каждого хода. Модель сама
  выбирает Win32_* класс и свойства по своему знанию на любой бытовой запрос —
  без слова «wmi» и без keyword-таблицы. Эвристика остаётся офлайн-фолбэком.
- SYSTEM_PROMPT: добавлен явный указатель использовать system.inspect для
  вопросов о состоянии машины и не ждать слова «wmi».
- Деградация честная: нет host bridge → tool возвращает ok=False с понятным
  сообщением, модель говорит про деградацию, а не выдумывает.
- Тесты: `test_system_inspect_runs_read_only_wmi_query`,
  `test_system_inspect_refuses_desktop_mutating_action`,
  `test_system_inspect_is_a_safe_autonomous_tool`,
  `test_agentic_loop_inspects_system_without_the_word_wmi`. Прогон — 188 pass,
  ruff clean, frontend clean.
- Кандидат на будущее (не сделано, выше риск против покрытых тестами эвристик):
  провести весь local_action-маршрут через reasoning-first арбитр, как уже
  сделано для web_research; и symmetричный safe read-only инструмент для WinAPI
  (окна/фокус read) шире, чем window.list.

## 2026-07-09 handoff - hardening pass (API smoke, verify timeout, config sync)

Для оператора и второй модели. Не фича, а закрытие насущных пробелов после трёх
feature-слоёв (understanding / result integrity / experience loop). Аудит
критичных путей (безопасность, устойчивость, конкурентность, покрытие) выявил
три реальные проблемы; SQLite-конкурентность (единое соединение под RLock,
`check_same_thread=False`) и лимит тела `web.fetch` уже были корректны.

- End-to-end смоук API (`backend/tests/test_api_smoke.py`): раньше НИ один тест
  не гонял ~40 роутов через реальный ASGI, только компоненты в изоляции —
  регрессия роутинга (неверный response_model, забытый await) уехала бы молча.
  Тест поднимает приложение (offline LLM, autonomy off) и проходит критичный
  путь оператора: health/status/models, chat offline + feedback roundtrip
  (+404/422), mission create/run/report (+404 до готовности), operator queue,
  memory, tools (safe-run + отказ danger без approval), approvals, persona
  get/patch. Уже окупился: поймал два неверных предположения о контракте
  (`/health` даёт `{ok}`, `/api/persona` — плоский объект).
- Таймаут-бюджет самопроверки: критик и ремонт запускаются ПОСЛЕ готового
  черновика, поэтому зависший критик не должен держать ответ. Обе LLM-операции
  теперь в `asyncio.wait_for(..., self._verify_timeout())`
  (`VERIFY_TIMEOUT_SEC=45`, но не больше `llm_timeout_sec`); таймаут/ошибка
  деградируют до «отдать черновик», а не блок на 240с. Тест
  `test_slow_self_check_does_not_block_the_ready_draft`.
- `.env.example` синхронизирован с config: добавлены `JARVIS_VERIFY_ANSWERS`,
  `JARVIS_EMBEDDINGS_ENABLED/BASE_URL/MODEL`, `JARVIS_AUTONOMY_MISSION_INTERVAL_SEC`.
- Остаточный риск (осознанно НЕ трогал): `web.fetch` проверяет публичность хоста
  префлайтом `getaddrinfo`, но httpx резолвит заново при запросе — узкое окно
  DNS-rebinding TOCTOU. Реалистичные атаки (литеральные внутренние IP, внутренние
  хостнеймы, смешанные A-записи public+private) уже блокируются
  `_hostname_is_private`. Полный пиннинг к IP ломает TLS SNI/валидацию сертификата
  для легитимного HTTPS — для локального однопользовательского инструмента это
  худшая регрессия, чем редкий rebinding. Кандидат: кастомный httpx-транспорт с
  корректным SNI, если появится многопользовательский сценарий.
- Полный прогон — 184 pass, ruff clean, frontend typecheck + build clean.

## 2026-07-09 handoff - experience loop (feedback -> lessons -> behavior)

Для оператора и второй модели. Раньше петля самообучения была разомкнута:
сигналы качества рождались, но оператор не мог оценить ответ, LearningEngine
строил шаблонные уроки и не видел новые сигналы, а уроки влияли на ход только
если retrieval случайно их находил. Теперь петля замкнута.

- Фидбек оператора: `POST /api/messages/{id}/feedback` (`rating: up|down`,
  `comment`), `storage.set_message_feedback` пишет оценку в metadata сообщения
  (UI восстанавливает её после перезагрузки), в append-only learning journal
  (`operator.feedback`, переживает удаление чата), в аудит и в событие
  `feedback` (WS). В Command Center у каждого ответа есть 👍/👎; на 👎 можно
  добавить комментарий «что не так».
- Вердикты самопроверки — теперь сигнал обучения: `revise` пишется в журнал как
  `verification.revise` с missing-пунктами.
- LearningEngine v2: приоритетные уроки из негативного фидбека (цитирует ответ
  и комментарий оператора, importance 0.9), похвалы с комментарием (0.68),
  повторяющихся пробелов самопроверки (0.74) и отклонённых approval-гейтов
  («не предлагай повторно», 0.8). Шаблонные уроки активности остались ниже по
  приоритету; кап поднят до 6 уроков за tick.
- Уроки теперь реально меняют поведение: `_lessons_prompt()` вставляет топ
  learning-уроков (сорт по importance/свежести, бюджет ~900 символов, максимум
  5 строк) системным блоком в КАЖДЫЙ ход `chat`/`stream_chat` и в исполнение
  шага миссии. Раньше уроки жили только в памяти и всплывали от случая к случаю.
- Качество на виду: `answer_quality_report` агрегирует свежий негативный фидбек
  и revise-вердикты; operator queue получает элементы kind=`quality`
  (`quality:feedback` — high, `quality:self-check` при >=3 revise — medium).
- Command Center: 👍/👎 и бейдж самопроверки (щит: pass/gaps) на ответах,
  кнопка «Отчёт» на завершённых миссиях, авто-показ итогового отчёта после
  «Запустить всё».
- Тесты: `backend/tests/test_experience_loop.py` (5): metadata+journal фидбека
  и выживание после удаления чата, уроки из сигналов, инъекция уроков в промпт,
  quality-элементы очереди, запись `verification.revise` из реального чата.
  Полный прогон — 178 pass, ruff clean, frontend typecheck + build clean.

## 2026-07-09 handoff - result integrity layer (self-check, mission deliverable, clarify)

Для оператора и второй модели. Этот проход закрывает вторую половину тезиса
«безупречно понять задачу → безупречно выдать результат»: раньше никто не
проверял ответ против задачи, завершённая миссия не оставляла оператору
итогового результата, а неоднозначная задача исполнялась по догадке.

- Новый модуль `backend/src/jarvis_gpt/verification.py` — слой целостности
  результата: строгий JSON-критик (`answer-verification-v1`), парсер вердикта,
  промпты ремонта (полный rewrite для request/response, короткая «Поправка после
  самопроверки» для уже отстримленного текста), детерминированный и
  LLM-синтезированный итоговый отчёт миссии.
- Самопроверка ответов: substantive-ответ (использовал инструменты или длиннее
  `VERIFY_MIN_ANSWER_CHARS=400`) получает один критик-проход против задачи и
  `completion_criteria` из task kernel, затем максимум один ремонт-раунд.
  `chat()` может переписать ответ целиком; `stream_chat()` достримливает только
  поправку (отстримленное не отзывается); шаг миссии переписывает отчёт до
  записи в notes. Событие `verification` идёт в ленту/trace, вердикт — в payload
  `assistant_done` и в `data.verification` шага миссии.
- Деградация железная: критик не позвался/вернул мусор → вердикт None → ответ
  стоит как есть; ремонт вернул JSON/пустоту → черновик выживает. Выключатели:
  env `JARVIS_VERIFY_ANSWERS=0` или `experience.autonomy_policy.verify_answers=false`.
  Короткий tool-less чат не проверяется вовсе (без лишней латентности).
- Итоговый mission-отчёт: когда миссия достигает `done` (execute-next, run,
  resume-after-approval — все три пути), `_maybe_finalize_mission` один раз
  синтезирует операторский отчёт (LLM с fallback на детерминированную сводку
  шагов), сохраняет его в память (`missions`, тег `report`), в runtime KV
  `mission.report.{id}`, эмитит событие `mission_report` и отдаёт через
  `MissionRunResponse.final_report` и `GET /api/missions/{id}/report`.
- Clarify-маршрут арбитра: intent-роутер теперь может ответить
  `route=clarify + clarification`; при confidence >= 0.65 Jarvis задаёт этот
  один точный вопрос вместо уверенной догадки (событие `thought`/«Нужно
  уточнение»). Порог и «не выбирай clarify, если допущение очевидно» прописаны
  в промпте, офлайн-поведение не тронуто.
- Тесты: `backend/tests/test_verification.py` (9): парсер вердикта, ремонт в
  chat, pass-без-ремонта, policy opt-out, стрим-поправка, ремонт отчёта шага
  миссии, офлайн-отчёт завершённой миссии (+идемпотентность), детерминированная
  сводка, clarify-вопрос. Прогон — 173 pass, ruff clean, frontend clean.
- Замечание для legacy-тестов механики цикла: они выключают самопроверку через
  `experience.autonomy_policy.verify_answers=false`, чтобы счётчики LLM-вызовов
  остались про механику, а не про критика.

## 2026-07-09 handoff - persona auto-learning, file fallback retrieval, mission by understanding

Для оператора и второй модели. Этот проход закрывает три пункта, которые ранее
были явно помечены «на будущее», и все три — про соответствие тезисам
«понимание вместо затычек», «persona — слой понимания» и «retrieval — отдельная
подсистема».

- Persona auto-learning через агентный tool-loop: новые safe-инструменты
  `persona.get` (прочитать durable-профиль) и `persona.insight` (доклеить ОДИН
  устойчивый факт в list-поле persona: языки, экспертиза, стек, интересы,
  текущий фокус, standing instructions). `persona.insight` сознательно НЕ в
  `AGENTIC_TOOL_DENYLIST`: это reasoning-first замена regex-извлечения persona,
  а мутация ограничена одним фактом, дедупом, пер-полевыми капами, аудитом
  (`persona.insight`) и событием для Command Center. SYSTEM_PROMPT просит модель
  сохранять только стабильные факты и делать это скупо.
- Файловый гибридный retrieval больше не слепнет при нулевом лексическом
  пересечении: если keyword-поиск не дал НИ одного кандидата, берётся
  ограниченный пул `storage.recent_file_chunks(24)` (аналог recent/important
  пула памяти), фильтруется порогом связности
  `FILE_FALLBACK_MIN_RELATEDNESS = 0.1` (fuzzy-вектор косинус к запросу), и
  только связанные чанки попадают в контекст (максимум 3, помечены
  `retrieval="semantic-recent"`). Нерелевантные недавние файлы в промпт не
  утекают: на тестовых строках связанный чанк даёт ~0.23, чужой — 0.0.
- Mission-детекция переведена на понимание: решение intent-арбитра `mission`
  (confidence >= 0.7, порог выше, чем у reasoning/chat, потому что создаётся
  durable state) переписывает task kernel через `_mission_plan_from_intent`, и
  `chat()`/`stream_chat()` перечитывают `context.task_plan` после
  `_try_direct_action`. Миссия без ключевых слов («найди варианты недорогого
  NAS для дома» при арбитре mission) становится персистентным mission plan.
  Счётчик `_looks_like_mission` остаётся детерминированным офлайн-фолбэком.
- Тесты: `test_agentic_loop_learns_persona_insight_from_dialogue`,
  `test_persona_insight_tool_learns_deduplicates_and_validates`,
  `test_hybrid_files_falls_back_to_recent_chunks_without_lexical_overlap`,
  `test_reasoning_arbiter_can_promote_research_to_mission`. Полный прогон —
  164 pass, ruff clean, frontend typecheck + build clean.
- На будущее: арбитр пока не управляет `local_action` (детерминированные
  биндинги покрыты тестами); persona insights можно зеркалить в learning
  journal; для больших корпусов остаётся кандидатом персист векторов чанков.

## 2026-07-09 handoff - long-lived LLM executor

For the operator and the second model:

- `AutonomyExecutor` is the single backend path for persisted autonomy jobs,
  routine steps, and background mission runs. API, routines, and supervisor now
  call this executor instead of duplicating job logic.
- New autonomy job kind: `mission`. Payload can contain an existing `mission_id`
  or a new `goal`. The executor calls `AgentRuntime.run_mission`, persists a
  newly created `mission_id` back into the job payload, keeps the job enabled
  while budget remains, pauses it on blocked/approval-needed missions, and marks
  it done when the mission completes.
- Supervisor starts `jarvis-background-jobs` when autonomy is enabled and runs
  due jobs every `JARVIS_AUTONOMY_MISSION_INTERVAL_SEC` seconds (default 120).
  Cadences now support `once`, `startup`, `background`, `hourly`, `daily`,
  `interval:15m`, `every 15m`, and short forms like `30s`, `15m`, `2h`.
- The LLM itself now gets a compact capability/current-work manifest in chat and
  mission prompts: profile/model, current conversation/mission/task, autonomy
  policy, safe autonomous tools, gated tools, recent missions, and background
  jobs. This prevents "wrapper knows, model does not" drift.
- Command Center mission cards now have `В фон`, which creates a persisted
  mission autonomy job so the page does not need to stay open for progress.

## 2026-07-09 handoff - per-answer thought trace

For the operator and the second model:

- Assistant chat bubbles now show a Brain action once the saved backend message id
  is known. The action opens `/trace/{messageId}`.
- `GET /api/agent/trace/message/{message_id}` returns the previous user input,
  assistant output, recorded event metadata, and a nodes/edges graph for the turn.
- The trace page visualizes the observable runtime path with an animated signal:
  input -> task kernel / memory / tools / synthesis / assistant_done -> output.
- This deliberately exposes operational trace, not hidden chain-of-thought. It is
  built from persisted metadata and does not trigger a new LLM call.

## 2026-07-09 handoff - web evidence synthesis

For the operator and the second model:

- Direct web research is now `search/fetch/render -> evidence synthesis -> answer`.
  `_run_web_research` gathers public pages through backend `web.search`,
  `web.fetch`, and, when normal fetch is thin or blocked, `web.render`, then asks
  the local LLM to produce a conclusion from the fetched evidence only, with
  uncertainty and source URLs.
- The deterministic formatter remains the fallback. If the synthesis response is
  empty, router-shaped JSON, or otherwise invalid, Jarvis returns the old source
  list instead of exposing broken routing output.
- Per-conversation evidence is persisted in runtime KV as `research.last_web.*`
  and mirrored into the append-only learning journal as `web.research`.
  Follow-ups such as "какой вывод?" use that stored evidence and do not re-open
  the operator's browser.
- Source payloads include a small quality label (`primary-or-vendor`,
  `vendor-docs`, `fetched-page`, `snippet-only`, etc.) so the synthesis prompt can
  treat snippets as weak and fetched official/vendor pages as stronger evidence.
- Launcher stop/restart now guards stale `launcher-state.json` PIDs: a saved
  backend/frontend/bridge PID is stopped only if its command line still matches
  the expected Jarvis service. Port/signature cleanup still handles real leftovers.

## 2026-07-09 handoff - open browsing and durable learning journal

For the operator and the second model:

- Browser policy default is `open`: validated HTTP(S) URLs can be opened without
  approval. The validator still rejects non-http schemes and policy-locked URLs.
- `browser.open` and `browser.open_many` are intentionally denied from the
  autonomous tool loop. Background/current-data research should use backend
  `web.search`, `web.fetch`, and `web.render`, which do not touch the operator's
  desktop browser.
- Added `learning_observations`: an append-only journal for dialogue messages,
  tool runs, web/browser observations, and conversation deletion markers. Deleting
  a chat removes visible history but leaves the learning journal intact.
- Learning tick now derives lessons from the journal as well as audit/tool/approval
  history. Supervisor runs learning immediately on startup and then every 120s by
  default.
- `GET /api/learning/journal` exposes recent learning observations for inspection.
- Command Center chat links are clickable for Markdown links, bare `http(s)` URLs,
  and `www.` URLs. Chat height now adapts to the viewport and the resize handle is
  no longer capped at 760px.

## 2026-07-09 handoff - operator queue and generation resilience

For the operator and the second model. This pass adds a thin runtime kernel
surface instead of another one-off UI rule:

- `GET /api/operator/queue` merges pending/executable approvals, blocked/running
  mission tasks, health warnings, lingering generation truncation, memory hygiene,
  and the future model-profile roadmap into one operator queue.
- `operator_context()` now exposes local runtime facts for prompts and UI:
  local time, active profile/model, operator name, home location, working roots,
  active missions, and pending approvals.
- Answers stopped by `finish_reason=length` are auto-continued internally for
  chat and stream paths. The old token-limit warning only appears if continuation
  still cannot finish the answer.
- Memory hygiene has explicit API surfaces: `GET /api/memory/hygiene` and
  `POST /api/memory/consolidate`. The report highlights duplicates, missing
  source tags, and low-confidence/stale notes.
- Model profiles are deliberately only scaffolded: `GET /api/model-profiles`
  reports current Gemma profiles plus future planner/reviewer and fast-executor
  roles, but no multi-model routing is active yet.
- Command Center opens on the new queue tab, shows mission/task links on approvals,
  and has one-click approve+execute for approved-gate recovery.
- Tests added: `test_agentic_answer_auto_continues_after_length_finish` and
  `backend/tests/test_operator_queue.py`.

## 2026-07-09 handoff - mission approval resume

For the operator and the second model. This closes the deeper approval follow-up
left by track 5.3: approving a gated mission tool no longer leaves the mission
to retry from scratch.

- Agent loop: `_run_agentic_tool` now writes `mission_id`, `task_id`, and a
  compact `resume` snapshot into approval payloads created from mission steps.
  Safe autonomous tool runs also receive mission/task ids, so the audit trail is
  attached to the mission.
- Approval executor: `tool.run` approvals execute the approved tool with
  `allow_danger=True` and mission/task ids, then call
  `AgentRuntime.resume_mission_after_approval` when the approval came from a
  mission step.
- Resume flow: the approved tool observation is fed back into the saved agentic
  messages through `_continue_agentic_answer`. The task is marked `done` only if
  the approved tool and resumed answer both succeed; otherwise it remains
  `blocked`. A second gated action creates a new approval instead of bypassing
  policy.
- Persistence/events: successful resumed steps write a mission memory and emit a
  `mission_step` event, so Command Center refreshes through the existing WS/REST
  flow after `/api/approvals/{id}/execute`.
- Tests: `test_approval_execution_resumes_blocked_mission_step` covers
  block -> approve -> execute approved tool -> resume model -> task done.

## 2026-07-09 handoff — mission approval linkage + retry (track 5.3)

Для оператора и второй модели. Замыкаем петлю approval↔миссия из трека 3/4: когда шаг миссии просит опасный инструмент, создаётся approval и шаг блокируется; не хватало явной связи и способа продолжить.

- Backend: approval, созданный во время шага миссии (`_run_agentic_tool`), теперь несёт `mission_id` в payload и в событии `approval` (парсим из `context.conversation_id`, который для миссий = `mission:{id}`). Аудит/UI могут ассоциировать допуск с миссией. Тест: `test_mission_step_approval_carries_mission_id`.
- Frontend: у заблокированной задачи миссии кнопка меняется на «Повторить» (RefreshCw) — сбрасывает задачу в `pending` (статус разрешён в `MissionTaskUpdateRequest`). Операционная петля: шаг заблокирован → оператор одобряет связанный гейт в панели допусков (виден и по WS) → «Повторить» на задаче → «Запустить всё»/«Шаг» продолжает миссию.
- Полный прогон — 148 pass, ruff clean, frontend typecheck + build clean.
- Осознанно НЕ сделано (глубокая версия, кандидат на будущее): автоматически «скармливать» результат одобренного инструмента в возобновлённый шаг миссии (сейчас повтор шага — свежая агентная попытка; approve+execute гейта выполняет инструмент независимо). Для полноценного resume нужен проброс результата approved-действия в контекст шага.

## 2026-07-09 handoff — live WS events in Command Center (track 5.2)

Для оператора и второй модели. Раньше фронт знал о событиях только через REST-поллинг; серверный event bus (`/ws/events`) во фронте не использовался.

- Frontend подписывается на `ws(s)://<host>:8000/ws/events` (`wsUrl()` = `apiUrl()` с http→ws), автопереподключение через 3s. Агентские события идут как `{channel:"agent", type, title, content, payload}`.
- На событие: пишем в компактную ленту живых событий (`liveEvents`, последние 8) под activity-карточкой + индикатор `liveDot` (пульс когда подключено). На `type` c префиксом `mission` — дебаунс-обновление `/api/missions` (прогресс миссии виден живьём, даже при серверном `/run` или действии из другой вкладки); на `approval` — обновление `/api/approvals`.
- Клиентская цепочка миссий (трек 4) остаётся; теперь её события также приходят по WS, а серверный `/run` тоже даёт живой прогресс в UI.
- Проверка: WS end-to-end через Starlette TestClient (publish в шину → приём на клиенте). Frontend typecheck + build clean.
- Осталось по треку 5: (3) интеграция approval-гейта миссии в поток approvals Command Center (сейчас блокирующий шаг создаёт approval, он виден в панели допусков и по WS; не хватает кнопки «продолжить миссию после approve»).

## 2026-07-09 handoff — semantic hybrid for file chunks (track 5.1)

Для оператора и второй модели. Продолжение трека 2: гибридный retrieval был только для памяти; файловые чанки (`search_file_chunks`) оставались чисто лексическими.

- Рефактор: общий `agent._hybrid_rerank(query, lexical_hits, extra_pool, id_key, limit)` — DRY-ядро фьюза лексики и семантики (RRF) с деградацией. На его основе тонкие `_augment_semantic_memory` (id_key="id", extra = недавние/важные) и новый `_augment_semantic_files` (id_key="chunk_id", extra = oversampled `search_file_chunks(query, 30)`). Оба вызываются в `chat()`/`stream_chat()` после `_prepare_context`.
- Улучшение фьюза: кандидаты переупорядочиваются по семантике перед стабильной сортировкой по fused-скору, поэтому при равном RRF (например, лексика и семантика дали ровно обратные порядки) тай-брейк идёт в сторону семантики — более сильного сигнала для перефразирования. Заодно усиливает и память.
- Ограничение файлового гибрида v1: если лексический поиск не вернул ничего (полное отсутствие пересечения по токенам), пул пуст и семантика не помогает — в отличие от памяти, где есть recent/important пул. Кандидат на будущее: тянуть соседние чанки того же файла или персист векторов чанков.
- Тесты: `test_hybrid_files_reranks_chunks_by_semantic_closeness`. Полный прогон — 147 pass, ruff clean.
- Осталось по треку 5: (2) WS-подписка фронта на `/ws/events` для живых событий миссий/инструментов; (3) интеграция approval-гейта миссии в поток approvals Command Center.

## 2026-07-08 handoff — mission auto-chaining + live progress (track 4)

Для оператора и второй модели. Продолжение трека 3: раньше миссия двигалась по одному шагу за вызов, и «исполнение» было ручным кликом. Теперь миссия может пройти до конца.

- Backend: `agent.run_mission(mission_id, max_steps=None)` последовательно гоняет `execute_next_mission_step` (тот самый агентный executor) до завершения миссии, заблокированного шага (например, нужен approval) или бюджета шагов. Возвращает `MissionRunResponse(mission, steps[], completed, stopped_reason ∈ completed|blocked|budget|empty, executed_steps)` и эмитит событие `mission_run`. Бюджет — из `experience.autonomy_policy.max_autonomous_steps` или явного `max_steps` (cap 24). Approval-гейты НЕ обходятся: заблокированный шаг останавливает цепочку.
- API: `POST /api/missions/{id}/run?max_steps=`. CLI: `mission-run <id> [--max-steps N]`.
- Офлайн детерминирован (каждый шаг — `mission.brief`), поэтому цепочка тестируется без LLM: `test_run_mission_chains_all_steps_offline`, `test_run_mission_respects_step_budget`.
- Frontend (Command Center): кнопка «Запустить всё» в панели миссий делает клиентскую цепочку `execute-next` (через `missionsRef` для свежего состояния между await), чтобы прогресс шёл в UI ЖИВО — прогресс-бар растёт, задачи перекрашиваются в done/blocked, каждый шаг логируется в чат; выполняющаяся миссия подсвечивается, кнопка крутит спиннер. Серверный `/run` остаётся для headless. Причина клиентской цепочки: во фронте нет WS, только REST-поллинг, а per-step `execute-next` даёт живые апдейты без стрима.
- Полный прогон — 146 pass, ruff clean, frontend typecheck + build clean.
- На будущее: WS-подписка на `/ws/events` для полностью серверной цепочки с live-событиями; live-стрим tool-событий шага миссии в UI; интеграция approval-гейта миссии в поток approvals Command Center (сейчас блок останавливает цепочку, approval виден в панели approvals).

## 2026-07-08 handoff — real mission executor (track 3/3)

Для оператора и второй модели. Трек 3 из 3: `execute_next_mission_step` был заглушкой — гонял `mission.brief` (текстовую рекомендацию), а не работу. «Миссии» были планами, которые ничего не делали. Размер модели это не лечит: исполнитель был пустым.

- `agent._execute_mission_step_agentic(mission, task)`: шаг миссии теперь исполняется через агентный tool-loop (`_agentic_answer`) — модель реально вызывает безопасные инструменты (собрать данные, проверить систему, прочитать файлы), опасные становятся approval-гейтами, внутренние tool-runs пишутся в аудит → у миссии появляется настоящий след исполнения. Результат синтезируется в `ToolRunResponse(tool="mission.execute_next", ok, summary=отчёт, data={tool_steps, autonomous})`. Промпт — `MISSION_EXECUTOR_PROMPT` (исполняй шаг, не пиши план).
- Ветвление в `execute_next_mission_step`: при `llm_enabled` — агентное исполнение; при выключенном LLM — прежний `mission.brief` (офлайн-контракт и тест `test_agent_executes_next_mission_step` сохранены, `runs[0]=="mission.brief"`).
- Тест: `test_mission_step_executes_with_tools_when_llm_enabled` (в `test_agentic_loop.py`). Полный прогон — 144 pass, ruff clean.
- Итог трёх треков: (1) у модели появились руки (tool-loop), (2) память стала находить релевантное при перефразировании (гибридный retrieval), (3) миссии реально исполняются. Все три — про архитектуру, а не про веса модели.
- На будущее: авто-цепочка шагов миссии (сейчас один шаг за вызов execute-next), UI-прогресс исполнения в реальном времени, и связка mission-executor с operator-approval потоком в Command Center.

## 2026-07-08 handoff — hybrid semantic memory (track 2/3)

Для оператора и второй модели. Трек 2 из 3: retrieval был чисто лексическим (FTS5 BM25 + LIKE) — перефразированные/иначе склонённые записи не находились, и модель не получала контекст, который «должна была вспомнить». Размер модели это не лечит: retrieval — отдельная подсистема.

- Новый модуль `backend/src/jarvis_gpt/embeddings.py`: `lexical_vector` (чистый Python: слова + символьные триграммы, L2-норма — ловит морфологию/опечатки/порядок слов, которые keyword-поиск упускает), `sparse_cosine`/`dense_cosine`, `reciprocal_rank_fusion`, `EmbeddingBackend` (опциональный OpenAI-совместимый `/embeddings`, при недоступности → None), `semantic_similarity_order` (dense при наличии, иначе lexical).
- Интеграция в `agent.py`: `_augment_semantic_memory(context, message)` вызывается в `chat()`/`stream_chat()` сразу после `_prepare_context`. Берёт пул кандидатов (лексические хиты + недавние/важные из `search_memory(None, 60)`), считает семантический порядок и фьюзит с лексическим через RRF, переписывает `context.memory_hits` (top-8) и проставляет `relevance`/`retrieval="hybrid"`. Пул ограничен → опциональный remote-embed это ОДИН батч-запрос на ход, без персиста векторов и без изменения схемы/пути записи.
- Деградация: пул < 2 → no-op (поэтому все прежние тесты с 1 записью памяти не меняются); любой сбой эмбеддинга → лексический порядок; всё в try/except, ход не ломается.
- Конфиг (новые env, дефолт выключено): `JARVIS_EMBEDDINGS_ENABLED` (false), `JARVIS_EMBEDDINGS_BASE_URL` (по умолчанию = LLM base url), `JARVIS_EMBEDDINGS_MODEL` (пусто). Пока не задан model — работает чистый Python гибрид (уже лучше keyword). Для настоящей семантики укажи локальный embeddings-эндпоинт (llama.cpp/TEI/vLLM-embed).
- Не сделано в этом треке (кандидаты на будущее): гибрид для file_chunks (сейчас только память), персист векторов для больших корпусов вместо ре-эмбеддинга пула на каждый запрос.
- Тесты: `backend/tests/test_embeddings.py` (5). Полный прогон — 143 pass, ruff clean.

## 2026-07-08 handoff — agentic tool loop (track 1/3)

Для оператора и второй модели. Часть плана «убрать узкие места, которые не лечит размер модели». Трек 1 из 3: дать модели реальные руки.

- Было: путь ответа LLM в `chat()`/`stream_chat()` — один forward-pass без доступа к инструментам; всё tool-использование решалось эвристиками ДО модели. Теперь модель сама вызывает инструменты в цикле, видит результат и продолжает.
- Протокол — **JSON-act поверх обычных completions** (деградирует на любой модели, не требует нативного OpenAI tool-calling): модель возвращает `{"tool": "<имя>", "arguments": {...}}` одной строкой → выполняем → возвращаем observation → повтор, пока не хватит, затем финальный текст.
- Безопасность: `allow_safe_tools` / `allow_review_tools` / `allow_danger_tools` определяют, какие классы инструментов модель может предложить, но сами не дают права исполнения. Review/danger создают HITL-approval gate без точного допустимого current-turn capability; в model-driven agentic loop инструменты из `approval_required_for` остаются gated даже при таком capability. Широкий lexical scope никогда не разрешает выбранные моделью operands: matcher сверяет точные пути, URL, payload и аргументы. `AGENTIC_TOOL_DENYLIST` удерживает durable safe-writes (`memory.save`, `learning.tick`, `mission.brief`) вне фонового proposal-loop. Бюджет шагов — из `experience.autonomy_policy.max_autonomous_steps` (bounded 1..24, дефолт политики 3); при исчерпании форсируется финальный ответ (`FINAL_ANSWER_PROMPT`).
- Ключевые части в `agent.py`: `_autonomous_tools()`, `_max_tool_steps()`, `_run_agentic_tool()`, `_agentic_answer()` (non-stream), стрим-версия внутри `stream_chat` через `_ToolActionSniffer` (классифицирует поток как tool-JSON или обычный ответ, чтобы обычные ответы стримились токен-за-токеном без лишнего вызова, а tool-JSON не утекал оператору). Хелперы: `_tool_protocol_prompt`, `_schema_hint`, `_parse_tool_action` (требует, чтобы сообщение НАЧИНАЛОСЬ с JSON — иначе это обычный ответ), `_tool_observation_excerpt`.
- Офлайн/деградация: `_autonomous_tools()` возвращает `[]` при `llm_enabled == False` → путь идентичен прежнему одиночному completion → все офлайн-тесты неизменны. Арбитр интентов (reasoning-first) вызывается только для web_research-планов и кэшируется, так что двойных вызовов роутера нет.
- Регрессии model-driven loop находятся в `backend/tests/test_agentic_loop.py` и покрывают safe-tool→observation→ответ, approval без выполнения, step-budget, interrupted stream, protocol/synthesis recovery, unknown-outcome reconciliation и deduplication эквивалентных эффектов. Актуальные команды проверки перечислены в корневом `CLAUDE.md` и CI; фиксированные исторические счётчики здесь намеренно не приводятся.
- На будущее по треку: при thinking_enabled модель, обернувшая tool-JSON в `<think>`, классифицируется как ответ (JSON может утечь) — сознательный компромисс v1. Ещё не сделано: трек 2 (семантическая память) и трек 3 (реальный mission-executor поверх этого loop).

## 2026-07-08 handoff — operator persona layer

Для оператора и для второй модели (кто продолжит работу).

- Добавлен слой **operator persona** — durable структурированный профиль оператора, который агент читает на каждом ходу. Цель: закрыть «понимание оператора» широко, а не патчить каждый юзкейс отдельной эвристикой.
- Новый модуль `backend/src/jarvis_gpt/persona.py`: схема + нормализация (`normalize_persona`, `load_persona`), рендер системного блока (`render_system_block`), аксессоры (`home_location`, `primary_language`, `is_configured`) и `PersonaManager` (update/insight с audit + event).
- Поля persona: `display_name, headline, role, location, timezone, languages, expertise, tech_stack, interests, current_focus, standing_instructions, glossary, notes`. Хранится в runtime_kv под ключом `experience.persona`.
- Интеграция в `agent.py`: `_build_llm_messages` подмешивает блок persona; `_infer_weather_location` теперь СНАЧАЛА берёт `persona.location` (обобщение прежнего weather-only кэша — домашний город стал общим фактом для погоды/локальных/гео запросов); добавлены `_persona_prompt`, `_operator_home_location`.
- API: `GET/PATCH /api/persona`, `POST /api/persona/insight` (доклеивание одного факта в list-поле, с дедупом). CLI: `persona`, `persona-set --set key=value`.
- Command Center: в панели «Настройки» добавлена секция «Профиль оператора» (`personaForm`).
- `experience.daily_briefing` выносит `current_focus` оператора в начало focus-списка.
- Тесты: `backend/tests/test_persona.py` (9). Полный прогон — 131 pass, ruff clean, frontend typecheck + build clean.
- Незакрытое/на будущее: авто-обучение persona из диалога (сейчас `add_insight` есть, но агент его из чата не вызывает — сознательно, чтобы не плодить regex-эвристики); можно добавить UI для `glossary` и `languages`, и связать persona.primary_language с языком ответа.

## 2026-07-08 handoff — reasoning-first intent understanding

Для оператора и второй модели. Цель правки: JARVIS должен ПОНИМАТЬ входящую задачу и рассуждать по контексту, а не проходить каскад `_looks_like_*`-затычек.

- Раньше семантический роутер вызывался только в узкой калитке `_should_use_semantic_router` (когда эвристика уже выбрала `web_research` И совпали маркеры) и работал лишь как вето в research-ветке. Это и была корневая «затычечность».
- Теперь в `agent.py` есть **reasoning-first арбитр** `_understand_intent(message, context)`: он вызывается для всей fuzzy web-семьи (гейт — `task_plan.route == "web_research"`, куда эвристика и так сводит weather/shopping/travel/place/osint/generic-research), обогащён operator-контекстом (`_intent_operator_context`: role, home_location, tech_stack, interests) и решает по смыслу: `reasoning|chat|web_research|local_action|mission`.
- Место вызова: `_try_direct_action`, ПОСЛЕ детерминированных fast-path (typed native OS action, URL) и ПЕРЕД fuzzy-ветками. Произвольные host-command строки больше не являются fast-path. Если арбитр уверенно (`confidence >= 0.6`) говорит `reasoning`/`chat` — возвращаем None, и основной LLM отвечает рассуждением; при этом `context.task_plan` переписывается `_reroute_plan(...)`, чтобы промпт был когерентным (не «execution contract web_research»). Решение кэшируется на context (`intent_consulted`/`intent_decision`) — ровно один вызов роутера за ход.
- Детерминированные fast-path и офлайн-режим не тронуты: арбитр гейтится на `settings.llm_enabled`, поэтому при выключенном LLM эвристики остаются авторитетом (все офлайн-тесты неизменны).
- Удалён мёртвый `_should_use_semantic_router` (узкая калитка). `_intent_router_messages` переписан в reasoning-first формулировку (сохранена подстрока `intent-router`, которую пинят тесты).
- Промпты: SYSTEM_PROMPT теперь начинается с «сначала пойми задачу и рассуждай по контексту; правила — умолчания, а не скрипт»; task-kernel prompt смягчён с «execution contract» на «стартовая гипотеза, следуй задаче, а не ярлыку».
- Тесты: `test_reasoning_arbiter_can_override_shopping_keyword_plug` (арбитр переопределяет shopping-затычку в reasoning — старая калитка это исключала) и `test_intent_router_receives_operator_persona_context`. Оба пина роутера сохранены. Полный прогон — 133 pass, ruff clean.
- На будущее: арбитр пока не управляет mission-детекцией (`_looks_like_mission` по счётчику ключевых слов) и native/local_action — они детерминированы и покрыты тестами; при желании их тоже можно перевести на понимание.

## 2026-07-08 handoff

- Default runtime is now `gemma4-turbo` / `gemma4-26b-a4b-nvfp4`.
- `gemma4-31b-it-nvfp4` remains in the catalog, but it currently exhausts available KV cache memory at the 32k context target after loading the weights.
- Dispatcher stability flags are pinned for Docker Desktop on Windows: `VLLM_USE_V2_MODEL_RUNNER=0`, `VLLM_WEIGHT_OFFLOADING_DISABLE_UVA=1`, `JARVIS_QWEN_TOKENIZER_MODE=slow`, `JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY=prefetch`.
- Verified tonight: backend `pytest`, `ruff`, frontend `typecheck`, frontend `build`.
- Follow-up closed: `/api/chat/stream` now streams NDJSON deltas and the default generation budget is 512 tokens.
- HITL follow-up closed: approved gates can now be executed through the whitelisted approval executor.
- Conversation history is now durable through `/api/conversations` and can be restored in Command Center.
- Host bridge follow-up closed: bundled `scripts/windows_rpc_bridge.py` exposes local token-auth command execution for approved host actions.
- Autonomous supervisor now persists health snapshots on its own interval, so `/api/status` stays fresh without manual diagnostics.

## 2026-07-10 handoff

- Browser work in progress for Claude: added `backend/src/jarvis_gpt/browser_cdp.py` with a local-only Chrome DevTools Protocol reader.
- New tools registered in `backend/src/jarvis_gpt/tools.py`: `browser.chrome.status`, `browser.chrome.launch`, and approval-gated `browser.read`.
- Safety boundary: do not copy/decrypt/export Chrome cookies or cache. The supported path is a dedicated Chrome profile launched with `--remote-debugging-port=9222`; the operator logs in or completes checks in that browser, then Jarvis reads visible DOM text through CDP.
- `browser.read` returns `needs_human_verification=true` instead of trying to bypass CAPTCHA/anti-bot pages.
- Tests were added in `backend/tests/test_tools.py`; current verification passed with `pytest backend/tests`, targeted `ruff check`, and `python -m compileall backend/src/jarvis_gpt`.

## Переменные окружения

| Variable | Default | Purpose |
| --- | --- | --- |
| `JARVIS_HOME` | `D:\jarvis` | Внешний runtime root для моделей, кэша, БД и логов |
| `JARVIS_PROFILE` | `gemma4-turbo` | Активный профиль |
| `JARVIS_MODEL_ROOT` | `D:\jarvis\data\models` если существует, иначе `D:\jarvis\models` | Root локальных моделей |
| `JARVIS_LLM_BASE_URL` | `http://localhost:8001/v1` | OpenAI-compatible endpoint |
| `JARVIS_LLM_MODEL` | `dispatcher` | Имя модели для chat completions |
| `JARVIS_LLM_ENABLED` | `1` | Включить/выключить LLM route |
| `JARVIS_VERIFY_ANSWERS` | `1` | Самопроверка substantive-ответов и отчётов шагов миссии |
| `JARVIS_EMBEDDINGS_ENABLED` | `0` | Включить remote-эмбеддинги для гибридного retrieval |
| `JARVIS_EMBEDDINGS_BASE_URL` | `= JARVIS_LLM_BASE_URL` | OpenAI-совместимый `/embeddings` endpoint |
| `JARVIS_EMBEDDINGS_MODEL` | `` | Имя embeddings-модели (пусто = только чистый Python гибрид) |
| `JARVIS_AUTONOMY_ENABLED` | `1` | Включить безопасный фоновой supervisor |
| `JARVIS_TELEMETRY_INTERVAL_SEC` | `120` | Интервал telemetry snapshots |
| `JARVIS_HEALTH_INTERVAL_SEC` | `300` | Интервал автономных health snapshots |
| `JARVIS_LEARNING_INTERVAL_SEC` | `600` | Интервал autonomous learning tick |
| `JARVIS_AUTONOMY_MISSION_INTERVAL_SEC` | `120` | Background autonomy job sweep interval |
| `JARVIS_CORS_ORIGINS` | `` | Optional comma-separated trusted non-loopback browser origins |
| `JARVIS_API_TOKEN` | `` | Optional token required for non-loopback backend/API/WS clients |
| `JARVIS_API_HOST` | `0.0.0.0` | Host FastAPI backend |
| `JARVIS_API_PORT` | `8000` | Port FastAPI backend |
| `JARVIS_BACKEND_URL` | `http://127.0.0.1:8000` | Server-only Next proxy target; Compose uses `http://backend:8000` |

## CLI

```powershell
py -3.11 .\jarvis.py init
py -3.11 .\jarvis.py profiles
py -3.11 .\jarvis.py status
py -3.11 .\jarvis.py backup
py -3.11 .\jarvis.py models
py -3.11 .\jarvis.py models --env
py -3.11 .\jarvis.py llm-health
py -3.11 .\jarvis.py dispatcher-status
py -3.11 .\jarvis.py dispatcher-compose --env
py -3.11 .\jarvis.py dispatcher-up
py -3.11 .\jarvis.py dispatcher-down
py -3.11 .\jarvis.py telemetry --persist
py -3.11 .\jarvis.py host-bridge
py -3.11 .\scripts\windows_rpc_bridge.py
py -3.11 .\jarvis.py host-bridge-action window.list --payload-json '{"limit":10}'
py -3.11 .\jarvis.py autonomy
py -3.11 .\jarvis.py persona
py -3.11 .\jarvis.py persona-set --set location=Kazan --set tech_stack=Proxmox,Debian
py -3.11 .\jarvis.py learning-tick
py -3.11 .\jarvis.py diag
py -3.11 .\jarvis.py chat "JARVIS, оформи это как mission plan: ..."
py -3.11 .\jarvis.py tools
py -3.11 .\jarvis.py tool-run memory.search --set query=runtime --set limit=5
py -3.11 .\jarvis.py tool-run web.download --set url=https://example.com/file.pdf
py -3.11 .\jarvis.py tool-run browser.chrome.status
py -3.11 .\jarvis.py tool-run browser.chrome.launch --allow-danger
py -3.11 .\jarvis.py tool-run browser.read --set url=https://example.com --allow-danger
py -3.11 .\jarvis.py ingest README.md
py -3.11 .\jarvis.py files
py -3.11 .\jarvis.py file-search Jarvis --limit 5
py -3.11 .\jarvis.py audit
py -3.11 .\jarvis.py approvals
py -3.11 .\jarvis.py approval-request "Host action" "Needs review" --risk danger
py -3.11 .\jarvis.py approval-update <approval_id> --status approved
py -3.11 .\jarvis.py approval-execute <approval_id>
py -3.11 .\jarvis.py mission-next <mission_id>
py -3.11 .\jarvis.py mission-run <mission_id> --max-steps 8
py -3.11 .\jarvis.py serve --reload
.\scripts\doctor.ps1
```

## API

```text
GET  /health
GET  /api/status
GET  /api/runtime/security
POST /api/runtime/backup
GET  /api/models
GET  /api/dispatcher
POST /api/dispatcher/start
POST /api/dispatcher/stop
GET  /api/telemetry
GET  /api/host-bridge
GET  /api/operator/quality
GET  /api/autonomy
GET  /api/autonomy/jobs
GET  /api/autonomy/job-runs
POST /api/autonomy/jobs
PATCH /api/autonomy/jobs/{job_id}
POST /api/autonomy/jobs/{job_id}/cancel
POST /api/autonomy/jobs/{job_id}/run
GET  /api/routines
POST /api/routines/{routine_id}/run
GET  /api/persona
PATCH /api/persona
POST /api/persona/insight
POST /api/learning/tick
POST /api/chat
POST /api/chat/stream
GET  /api/agent/trace/{conversation_id}
GET  /api/agent/trace/message/{message_id}
GET  /api/conversations
GET  /api/conversations/{conversation_id}/messages
POST /api/messages/{message_id}/feedback
GET  /api/missions
POST /api/missions
POST /api/missions/{mission_id}/execute-next
POST /api/missions/{mission_id}/run
GET  /api/missions/{mission_id}/report
PATCH /api/missions/{mission_id}/tasks/{task_id}
GET  /api/memory
POST /api/memory
GET  /api/files
POST /api/files/upload
GET  /api/files/search
GET  /api/files/{file_id}
GET  /api/audit
GET  /api/approvals
POST /api/approvals
PATCH /api/approvals/{approval_id}
POST /api/approvals/{approval_id}/execute
GET  /api/tools
POST /api/tools/{tool_name}/run
GET  /api/tool-runs
POST /api/diagnostics
WS   /ws/events
```

## Browser Reading

`web.fetch` remains the safe public HTTP reader. For sites that need a real browser session, use Chrome CDP:

```powershell
py -3.11 .\jarvis.py tool-run browser.chrome.launch --allow-danger
py -3.11 .\jarvis.py tool-run browser.read --set url=https://example.com --allow-danger
```

Chrome is launched with a versioned dedicated profile under `D:\jarvis\cache\jarvis-gpt\chrome-profile` and local DevTools on `127.0.0.1:9222`. If a site asks for login or human verification, complete it in that Chrome window and retry `browser.read`.

The CDP endpoint is usable only while its bridge launch attestation still matches the
nonce, listening Chrome PID/start time, profile, debug URL, proxy and full command
line. All browser traffic goes through the bridge's fail-closed loopback proxy, which
validates every redirect/CONNECT and connects to the checked numeric IP. Public-only
sessions cannot reach private addresses; an explicitly allowed localhost/private
session is private-only and cannot pivot to public content. Restarting the bridge
restores the exact HMAC-signed proxy/session record before CDP actions resume.

## Host Bridge

`scripts/windows_rpc_bridge.py` is a local-only bridge for Windows host actions. It binds to `127.0.0.1:8765`, creates or reads `D:\jarvis\.jarvis\bridge.token`, exposes unauthenticated `/health`, and requires `Authorization: Bearer <token>` for typed `POST /action` requests. The legacy arbitrary-command `/execute` endpoint returns `410 Gone`.

The bridge contract is `action.v1`: `{action, payload, timeout_sec}`. Action and payload fields are allowlisted; process launch uses direct argv with `shell=False`, accepts only a fixed native desktop-app/argument grammar, and rejects shell/script hosts and general-purpose native executables. WMI/window/keyboard/screen actions can invoke only the bridge's fixed bundled implementation. Startup and reuse require an authenticated `capabilities` probe with the current protected token and exact `policy_revision`, not only the public health response. `capabilities.process_policy` reports allowed apps, actually available canonical paths, and grammar IDs.

Apps installed outside canonical Windows/System32/Program Files locations are not inferred from user-writable PATH or HKCU App Paths. An operator may pin an exact reviewed path with `JARVIS_BRIDGE_APP_PATHS_JSON`, for example `{"code.exe":"D:\\VS Code\\Microsoft VS Code\\Code.exe"}`. The variable belongs in the protected launch environment; unsupported app names and basename/path mismatches fail closed.

Example read-only bridge diagnostic:

```powershell
py -3.11 .\scripts\windows_rpc_bridge.py
py -3.11 .\jarvis.py host-bridge-action window.list --payload-json '{"limit":10}'
```

Model-facing OS work does not use the bridge as a shell. It goes through the `jarvis.execution.v1` substrate tools described below.

## Deterministic Execution Substrate

The public execution envelope is strictly schema validated:

```json
{
  "protocol": "jarvis.execution.v1",
  "action": {
    "kind": "fs.stat",
    "action_id": "inspect_state",
    "path": "D:\\jarvis\\data"
  }
}
```

Tool boundaries:

- `execution.capabilities`: schema and effective capability policy.
- `execution.inspect`: safe read-only actions.
- `execution.apply`: approval-gated mutations, argv-only process runs, and owned-process control.
- `execution.transaction`: approval-gated reversible FS/registry batch with durable checkpoint/WAL and rollback.
- `execution.session`: create/list/inspect/transition bounded in-memory session state.
- `execution.cancel`: approval-gated cancellation of exact processes owned by a session.

Actions cover typed FS stat/list/read/mkdir/write/copy/move/delete, process run/terminate, DNS/TCP inspection, and registry get/set/delete. Paths must be absolute and stay under configured roots; runtime state, logs, `.jarvis` secrets, and the repository `.env` remain denied even when a parent root is allowed. Shell interpreters are not process actions. Processes are disabled until an administrator supplies per-executable argv regex rules; explicit environment variables require per-executable value regexes and environment inheritance is disabled by default. Network hosts and registry prefixes are also deny-by-default.

Optional configuration:

```text
JARVIS_EXECUTION_ROOTS=D:\jarvis;D:\jarvis-gpt
JARVIS_EXECUTION_CAPABILITIES_FILE=D:\jarvis\execution-capabilities.json
```

The capabilities file is strict JSON. It may define `executables` (`path`, positional `argument_patterns`, optional `additional_argument_pattern`, and an `environment_patterns` object), `network_hosts`, `allow_private_network`, registry read/write prefixes, and `allow_inherited_process_environment`. Unknown fields fail startup. Start from `docs/execution-capabilities.example.json`, replace paths and regexes for the deployment, then point `JARVIS_EXECUTION_CAPABILITIES_FILE` at the reviewed copy. Crash recovery scans durable active checkpoints when the kernel starts.

Single actions use `action_id`; transaction batches use `idempotency_key`. Resource locks cover action IDs, filesystem ancestor scopes, registry keys, and process working directories, preventing rollback from racing a committed sibling operation. Committed batch fingerprints and bounded outcomes are written through the checkpoint WAL into an atomically replaced, fsync-backed replay journal before checkpoint cleanup. Cold start imports a committed WAL record if the ledger replacement was interrupted, rejects key/fingerprint collisions and corrupt journals, and returns a verified replay without reapplying mutations. Incomplete or failed startup rollback latches the kernel into a read-only degraded state until every checkpoint reaches a durable terminal state.

Session-bound process starts reserve ownership before spawn, reject concurrent roots in one session, track stable PID birth identity, and cancel only owned process groups/Job Objects. Process output is drained asynchronously with total/stall deadlines, bounded tails, permissions, PID tree, and observed filesystem diff. When history limits are exceeded, old steps are compressed into bounded dry facts and failure counts.

## Executive Function, Verification, and Memory

Every mission is mirrored into a persisted `jarvis.executive.v1` record containing a strict `jarvis.planner.v1` snapshot and a mapping from DAG steps to durable mission tasks. The planner validates acyclicity, environment preconditions, bounded attempts, and atomic revision numbers. A task can be claimed only when all dependencies passed their assertions. Unexpected results replace only the unfinished branch with diagnosis and recovery steps; successful nodes remain immutable. Goal completion is a separate verification state after all step assertions pass.

Mission approvals are bound to one exact plan revision, step attempt, environment digest, typed action, arguments hash, semantic subject/effect, and postcondition through `jarvis.executive-approval.v1`. Only one gate is created at a time. Context is validated both before and after the atomic approval claim. Rejected, cancelled, failed, interrupted, or rollback-failed approvals use a durable `jarvis.approval-reconciliation.v1` outbox and revise their branch without replay before the mission can continue. Cold-start reconciliation repairs both planner/task write orders, retries only known-safe interrupted steps, preserves exact resumable approval gates, and converts ambiguous action outcomes into an exact `execution.verify` branch. That safe tool re-inspects the original typed postcondition without executing it; generic logs, read-only prose, and a second mutation cannot close the branch.

One `jarvis.primary-runtime-lease.v1` OS file lock owns executive mutation for the lifetime of the API process. Mutating CLI chat, mission, and approval commands acquire the same lease, so they serialize when the API is stopped and fail closed while it is live; read-only CLI inspection remains available. The operating system releases the lease on process or power loss before cold-start recovery begins.

The cold-start host profile uses schema `jarvis.host-profile.v1` and is atomically written to `<JARVIS_HOME>\host_profile.json`. It records OS/architecture, CPU and memory, GPU/CUDA/NPU observations, active interfaces, and available compilers/linters. The stable capability SHA-256 excludes collection time and the volatile active-interface snapshot; `snapshot_sha256` still covers the complete collected profile. Hardware/toolchain capability changes insert an environment-revalidation branch into active plans, while ordinary interface churn is observable without invalidating a DAG. Startup fails closed if neither a fresh profile nor a previously verified profile is available.

Execution experience is stored separately in `<state_dir>\execution-playbooks.sqlite3` using schema `jarvis.execution-playbook.v1`. Records contain symptom, solution, verification, outcome counters, confidence, and a deduplication fingerprint. Only a typed `execution.apply`/`execution.transaction` result whose action identity matches independent verifier evidence can create a playbook; LLM mission reports, stderr, action content, indexed files, and remote text never become trusted lessons. Retrieved playbooks and memory are quoted to the model as untrusted user data rather than system instructions.

`StateVerifier` performs a fresh inspection instead of trusting an action's exit code. Supported expectations cover file identity/hash/content and strict syntax validation, TCP reachability across resolved addresses, exact process birth identity, and registry value/type. Mutating execution actions are verified before checkpoint commit, so a failed postcondition rolls back. `process.run` additionally captures a pre-action baseline for every declared path/TCP/process subject and requires each asserted effect to transition; a pre-existing file or unrelated listener cannot satisfy a no-op command. The postcondition digest participates in replay identity. Dispatcher start requires both an `Up` container state and a live port 8001 socket; stop requires both container and socket absence. Native process launch is followed by an independent `Win32_Process` lookup.

`SafeGate` classifies typed actions as low/medium/high/critical. Every mutating request produces dry-run evidence first. Protected system roots fail closed; high/critical execution requires a short-lived HMAC permit bound to the exact canonical action fingerprint, and a permit is consumed once during preflight. These gates complement, rather than replace, the existing operator approval boundary.

New inspection surfaces:

- tools: `execution.preflight`, `execution.verify`, `environment.profile`, `memory.playbooks.lookup`, `executive.plan.status`, and `web.surfer.capabilities`;
- conditional tool: `web.surfer` is registered only when its black-box service contract is present;
- API: `GET /api/environment/profile`, `GET /api/memory/playbooks?query=...`, `GET /api/executive/plans/{mission_id}`, and `GET /api/internet/web-surfer`.

`execution.verify` accepts only `{"source_tool":"execution.apply|execution.transaction","arguments":{...}}`. It parses the original typed mutation payload and its exact verification expectation, applies the same filesystem/network/registry capability policy, and performs readback without executing the action. It is reserved for reconciliation of ambiguous outcomes. `fs.copy` and `fs.move` may bind `expected_sha256`; executive state mutations require the source digest for `fs.move`, preventing a changed source from satisfying an approved plan.

The web integration boundary is `jarvis.web-surfer-adapter.v1`; its framed child protocol is `jarvis.web-surfer-worker.v1`. The backend discovers `JARVIS_WEB_SURFER_MODULE` (default `jarvis_gpt.web_surfer`) without importing it into the API process, constructs its public `JarvisWebSurfer` class only inside the worker, awaits its lifecycle hooks, then invokes only public async `fast_fact`, `deep_research`, and `aggressive_shopping` methods. Constructor kwargs such as a reviewed resident proxy pool may be supplied through the protected `JARVIS_WEB_SURFER_FACTORY_KWARGS_JSON` environment value. A Windows Job Object or a Linux subreaper with an inherited parent-pipe EOF guard contains the complete browser tree. Linux IPC uses an abstract AF_UNIX endpoint authenticated with `SO_PEERCRED`; worker/PID identity is pinned and bounded. Requests/results are bounded JSON frames, credentials are recursively redacted, shopping targets must resolve only to public HTTP(S) addresses, nested service failures remain failures, calls are serialized to preserve the service session, and timeout, worker exit, parent death, or caller cancellation kills and reaps the complete generation before a later call starts clean. API shutdown performs bounded lifecycle close followed by forced tree termination. Synchronous methods, malformed contracts, missing dependencies, and absent browser provisioning fail closed while the existing generic web tools remain available. `web_surfer.py` remains Claude-owned and was integrated without changing its internals.

## Storage

SQLite хранится в:

```text
D:\jarvis\data\jarvis-gpt\state\jarvis.sqlite3
```

Файлы, загруженные через Command Center или CLI, копируются в:

```text
D:\jarvis\data\jarvis-gpt\files
```

Активные модели по умолчанию ищутся в:

```text
D:\jarvis\data\models
```

`gemma4-mono` / `gemma4-mono-perf` указывают на `gemma4-31b-it-nvfp4`, `gemma4-turbo` — на `gemma4-26b-a4b-nvfp4`.
Команда `models --env` печатает переменные для OpenAI-compatible vLLM dispatcher.

### 31B on RTX 5090 (32GB) + 128GB RAM

- Use `gemma4-turbo` as the recommended interactive vLLM profile. It keeps the 26B
  checkpoint GPU-resident and avoids the Docker/WSL CPU weight path.
- `gemma4-mono-perf` is the vLLM 31B text-only quality profile: 2.5GB CPU weight
  offload, eager mode, FP8 KV, util 0.93, 4k context and 1 concurrent seq. It
  disables multimodal profiling/cache and caps batched tokens at 512. A 3x32
  streaming certification measured p50 TTFT 899.3ms and 2.446 tok/s decode
  (~4.1x the old profile), but it is still not the recommended interactive path.
- `gemma4-mono` is experimental and only for stability/long-context checks: partial weight
  offload (24GB CPU) + native KV offload (16GB), eager mode, util 0.85, 16k
  context, 1 concurrent seq. Measured decode is below 1 tok/s.
- Avoid `gpu_memory_utilization` ≥ 0.94 with 16k context on 31B — that profile
  historically OOMs and can cascade into driver faults.
- Launcher: `.\jarvis.cmd` → Start/Restart → arrow-select profile
  (recommended Turbo 26B / experimental Docker/WSL Mono profiles).

Dispatcher запускается отдельно, чтобы не грузить GPU при обычном старте Command Center:

```powershell
.\scripts\dispatcher.ps1 up
.\scripts\dispatcher.ps1 status
.\scripts\dispatcher.ps1 logs
```

Сейчас схема покрывает:

- `conversations`
- `messages`
- `memories`
- `missions`
- `mission_tasks`
- `files`
- `file_chunks`
- `runtime_events`
- `health_snapshots`
- `tool_runs`
- `approvals`
- `telemetry_snapshots`
- `audit_log`

Если SQLite собран с FTS5, память индексируется в `memories_fts`, а файловые чанки — в `file_chunks_fts`. Если FTS5 нет, поиск автоматически деградирует до `LIKE`.

## 2026-07-12 — current-turn operator authorization

- Явная команда текущего persisted user message открывает только релевантные mutating tools.
- Перед выполнением runtime сверяет tool и operands с исходным сообщением и выдаёт одноразовый
  capability, связанный с conversation id, message id и canonical argument hash.
- Capability нельзя повторить, подменить аргументы или перенести в mission/task/resume/history.
- Прямые `browser.open` и `windows.native` используют тот же путь; совпавшая команда выполняется
  сразу, а не превращается в approval. Незапрошенные действия сохраняют прежний HITL gate.
- Нормализуются полные URL и домены без схемы; локальный файл можно открыть указанным либо
  системным приложением. `filesystem.write_text mode=create` атомарно создаёт в разрешённом корне
  новый, в том числе пустой, файл и не перезаписывает существующий.
- Команда открыть найденный товар разрешает только один детерминированно выбранный URL из
  сохранённой текущим диалогом shopping-выдачи; произвольная подмена URL остаётся заблокированной.
