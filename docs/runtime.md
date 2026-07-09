# Runtime

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

## 2026-07-10 handoff - document intelligence tools

For the operator and the second model:

- Uploaded chat files and local paths can now go through the same safe document
  layer. Use `file_id` for chat uploads or `path` for local files under the
  workspace, `JARVIS_HOME`, or the user home directory.
- New tools: `documents.inspect`, `documents.read`, `documents.compare`,
  `documents.edit.plan`, and `documents.apply_replacements`.
- DOCX extraction reads paragraphs, tables, comments, and style names. XLSX
  extraction reads sheet previews, shared strings, and formulas. PDF extraction
  uses `pypdf` if available and otherwise a basic text fallback. Text/html/json/
  csv are read directly.
- Ingestion now indexes DOCX/XLSX/PDF/text-like uploads into file chunks, so
  attachment context and `files.search` can find Office/PDF content.
- `documents.apply_replacements` writes an edited copy to
  `data/document-outputs`, registers it as a file, and never overwrites the
  original. Use it only for exact replacements; larger formatting/layout work
  should be planned first and visually verified before delivery.

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
  `X-Jarvis-Api-Token`. The frontend can forward it with
  `NEXT_PUBLIC_JARVIS_API_TOKEN`. WebSocket `/ws/events` accepts the same token
  through a header or `?token=...`.
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
- Config sync: `.env.example` includes `JARVIS_API_TOKEN` and
  `NEXT_PUBLIC_JARVIS_API_TOKEN`.

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
- Безопасность: автономно предлагаются только `danger_level == "safe"` инструменты МИНУС мутирующие (`AGENTIC_TOOL_DENYLIST = memory.save, learning.tick, mission.brief`). Если модель просит review/danger инструмент — создаётся HITL-approval gate (`storage.create_approval`) и в observation уходит «нужно подтверждение», инструмент НЕ выполняется. Бюджет шагов — из `experience.autonomy_policy.max_autonomous_steps` (bounded 1..8, дефолт 4); при исчерпании форсируется финальный ответ (`FINAL_ANSWER_PROMPT`).
- Ключевые части в `agent.py`: `_autonomous_tools()`, `_max_tool_steps()`, `_run_agentic_tool()`, `_agentic_answer()` (non-stream), стрим-версия внутри `stream_chat` через `_ToolActionSniffer` (классифицирует поток как tool-JSON или обычный ответ, чтобы обычные ответы стримились токен-за-токеном без лишнего вызова, а tool-JSON не утекал оператору). Хелперы: `_tool_protocol_prompt`, `_schema_hint`, `_parse_tool_action` (требует, чтобы сообщение НАЧИНАЛОСЬ с JSON — иначе это обычный ответ), `_tool_observation_excerpt`.
- Офлайн/деградация: `_autonomous_tools()` возвращает `[]` при `llm_enabled == False` → путь идентичен прежнему одиночному completion → все офлайн-тесты неизменны. Арбитр интентов (reasoning-first) вызывается только для web_research-планов и кэшируется, так что двойных вызовов роутера нет.
- Тесты: `backend/tests/test_agentic_loop.py` (5): safe-tool→observation→ответ; danger-tool→approval без выполнения; step-budget→форс-финал; стрим подавляет tool-JSON и стримит ответ; обычный стрим без регресса. Полный прогон — 138 pass, ruff clean.
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
- Место вызова: `_try_direct_action`, ПОСЛЕ детерминированных fast-path (native OS action, host command, URL) и ПЕРЕД fuzzy-ветками. Если арбитр уверенно (`confidence >= 0.6`) говорит `reasoning`/`chat` — возвращаем None, и основной LLM отвечает рассуждением; при этом `context.task_plan` переписывается `_reroute_plan(...)`, чтобы промпт был когерентным (не «execution contract web_research»). Решение кэшируется на context (`intent_consulted`/`intent_decision`) — ровно один вызов роутера за ход.
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
| `NEXT_PUBLIC_JARVIS_API_TOKEN` | `` | Optional frontend token forwarded to backend/API/WS |

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
py -3.11 .\jarvis.py host-bridge-exec "Get-Date"
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

Chrome is launched with a dedicated profile under `D:\jarvis\cache\jarvis-gpt\chrome-profile` and local DevTools on `127.0.0.1:9222`. If a site asks for login or human verification, complete it in that Chrome window and retry `browser.read`.

## Host Bridge

`scripts/windows_rpc_bridge.py` is a local-only bridge for Windows host actions. It binds to `127.0.0.1:8765`, creates or reads `D:\jarvis\.jarvis\bridge.token`, exposes unauthenticated `/health`, and requires `Authorization: Bearer <token>` for `/execute`.

The normal safe path is:

```powershell
py -3.11 .\scripts\windows_rpc_bridge.py
py -3.11 .\jarvis.py approval-request "Host command" "Run approved local command" --action tool.run --risk danger --payload "{\"tool\":\"host.bridge.execute\",\"arguments\":{\"command\":\"Get-Date\"}}"
py -3.11 .\jarvis.py approval-update <approval_id> --status approved
py -3.11 .\jarvis.py approval-execute <approval_id>
```

For manual diagnostics, `host-bridge-exec` calls the same token-auth bridge directly.

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

`gemma4-mono` указывает на `gemma4-31b-it-nvfp4`, `gemma4-turbo` — на `gemma4-26b-a4b-nvfp4`. Команда `models --env` печатает переменные для OpenAI-compatible vLLM dispatcher.

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
