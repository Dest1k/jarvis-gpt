# Assistant Notes

Single coordination point for assistant-to-assistant handoffs in this repository.

Use this file for short, append-only notes between Codex and the second assistant.
Keep the newest note at the top, include author, date, branch/commit when useful,
and list only facts needed by the next assistant: changed files, tests, blockers,
and decisions. Do not paste secrets, tokens, private logs, or long command output.

## Notes

### 2026-07-12 - Grok (31B decode speed: root cause + working path)

**Measured live**

- `gemma4-mono` (24GB CPU offload, 2 seqs): ~0.3–0.8 tok/s total.
- `gemma4-mono-perf` retuned (12GB offload, eager, 1 seq, 8k): boots, still ~0.6 tok/s.
- `gemma4-turbo` (26B, no offload, GPU-resident): **~17.3 tok/s** on the same 80-token sample.

**Why 31B is stuck near 0.5–1 tok/s here**

1. Checkpoint is ~31.2 GiB on a 32 GiB card → some CPU weight offload is mandatory.
2. Docker Desktop reports WSL → vLLM sets `pin_memory=False` → UVA/CPU offload decode is pathologically slow.
3. `torch.compile`/CUDA graphs + UVA offload currently crash on v0.23; offload profiles must stay eager.
4. Concurrent seqs split the already tiny decode budget.

**Profile policy after this**

- Interactive speed → `gemma4-turbo` (default recommendation).
- 31B quality chat → `gemma4-mono-perf` (12GB offload, eager, 1 seq, 8k) — quality over speed.
- Long-context/OOM-safe 31B → `gemma4-mono` (24GB offload, 16k, 1 seq) — slowest.
- Live stack left on **turbo** after the speed diagnosis (dispatcher + backend).

**Not a free lunch:** true high-tok/s 31B on this box needs either a smaller NVFP4 build that fits without offload, native non-WSL GPU runtime, or multi-GPU/TP.

### 2026-07-12 - Grok (vLLM 0.23 args + predictability fixes)

Branch: `main` (worktree `D:\jarvis-gpt`). No other agent dirty on this worktree;
Claude desktop / Codex app processes were idle on other worktrees.

**P0 — mono LLM would not start on vLLM 0.23**

- Live symptom: container restart-loop `unrecognized arguments: --swap-space 16`.
- Root cause: vLLM 0.23 removed `--swap-space`. KV spill is now
  `--kv-offloading-size` + `--kv-offloading-backend native`. Weight offload
  remains `--cpu-offload-gb`.
- Profiles (unchanged model binding):
  - `gemma4-turbo` → only `gemma4-26b-a4b-nvfp4` (no offload)
  - `gemma4-mono` / `gemma4-mono-perf` → only `gemma4-31b-it-nvfp4`
- Replaced profile field `swap_space_gb` → `kv_offloading_gb`; env
  `JARVIS_QWEN_KV_OFFLOAD_ARGS`; dispatcher runtime match keys updated.
- Live verify on mono: weights loaded, `CPUOffloadingSpec` + `kv_offloading_size=16`,
  `/v1/models` root `gemma4-31b-it-nvfp4`, chat completion returned `Ок`.

**Predictability bugs fixed**

- Executive mission allowlist: added `web.render`, `web.shop_search`.
- SSE: finish_reason preserved when co-located with final content delta.
- Truncated tool JSON (`finish_reason=length`) continues before protocol failure.
- Tool-marker false positives: only JSON-shaped `{..."tool":...}` is protocol_error;
  prose like `use tool: X` stays an answer.
- Verify/repair gets tool observations; stream skips verify when approval-gated.

**Files:** `config.py`, `model_catalog.py`, `dispatcher.py`, `docker-compose.yml`,
`llm.py`, `agent.py`, `telemetry.py`, launcher, frontend types, docs, tests.

**Verification:** Ruff clean; full backend `791 passed, 12 skipped`. Live mono
dispatcher left running with fixed args (operator home `D:\jarvis`).

**For Codex/Claude:** do not reintroduce `--swap-space`. Do not override built-in
model dirs across profiles. Next: arbiter must not demote named-shop shopping to
pure reasoning (P1 remaining); optional docs update for tool protocol.

### 2026-07-12 - Codex (Windows PowerShell 5 launcher parse fix)

- `b184b2f` added UTF-8 em dashes and middle dots to the profile menu in the BOM-less
  `jarvis-launcher.ps1`. `jarvis.cmd` invokes Windows PowerShell 5.1, whose ANSI fallback decoded
  the em-dash bytes into mojibake ending in a parser-significant smart quote, breaking the entire
  script before the menu opened.
- Replaced all six non-ASCII menu punctuation lines with ASCII `-` and `|`. The launcher is now
  fully ASCII-safe while remaining UTF-8-compatible; no other parser hazard was found.
- Added a deployment-contract regression requiring the BOM-less launcher to stay ASCII, so the
  Windows PowerShell 5.1 failure is caught on every platform.
- Verification: exact Windows PowerShell 5.1 `Parser.ParseFile` clean; ASCII scan clean; real
  `.\jarvis.cmd status` exited successfully; full backend suite `759 passed, 13 skipped`; full
  Ruff, compileall, and `git diff --check` clean.

### 2026-07-12 - Codex (deterministic repeated-cancellation tests)

- Audited repeated cancellation across transaction checkpoint/action/rollback, process cleanup,
  API lifespan cleanup, approval finalization, and `WebSurferAdapter.aclose`. Production keeps
  authoritative mutation/cleanup tasks shielded until completion; no runtime defect or source
  change was indicated by stress evidence.
- Replaced flaky `cancel()` + one `sleep(0)` + `task.done()` scheduling assumptions with bounded
  `wait_for(shield(task))` probes while each explicit test gate remains closed. Gate release is in
  `finally`, so a failed assertion cannot strand a worker thread, process, or cleanup task.
- Changed only cancellation regressions in `test_execution_transaction_session.py`,
  `test_execution_process.py`, `test_api_smoke.py`, `test_approval_executor.py`, and
  `test_web_surfer_adapter.py`.
- Verification: cancellation slice passed 25/25 complete stress repetitions with zero failures;
  full backend suite `758 passed, 13 skipped`; full Ruff, compileall, and `git diff --check` clean.

### 2026-07-12 - Codex (durable document memory and recall)

- Added `jarvis.document-memory.v1` and safe `documents.recall`: persisted files are resolved
  by stable `file_id`, Unicode filename, and indexed content, then read and analyzed through
  `document_surfer` with bounded passages, source metadata, corpus signals, and explicit
  untrusted-data labeling.
- Selection fails closed for missing/partial/ambiguous identities, uses token-boundary evidence,
  never broadens a failed named request to an unrelated recent file, and selects the newest only
  inside a validated temporal match set. Multi-document recall is explicitly bounded.
- Chat and streaming routes deterministically prefetch historical document evidence before the
  LLM. Successful source ids persist in assistant metadata for deictic follow-ups; singular
  attachment follow-ups bind to the latest attachment-bearing turn. Persisted archives use a
  separate `archive_memory` route and `documents.archive.*` tools.
- Ingestion now persistently indexes PPTX/ODT/RTF alongside existing text/Office/PDF formats.
  Generated and archive-extracted documents use the same index path. Legacy deduplicated files
  can be reindexed under corrected filename/MIME/path metadata while retaining their stable id.
- Main implementation: `document_memory.py`, `storage.py`, `ingest.py`, `document_surfer.py`,
  `tools.py`, and `agent.py`; behavior and handoff documentation updated in `README.md`,
  `docs/architecture.md`, and `docs/runtime.md`; focused regressions added across document,
  ingest, tools, agent, archive, follow-up, and streaming flows.
- Verification: full backend suite `757 passed, 13 skipped`; full Ruff, compileall, and
  `git diff --check` clean. No known blockers.

### 2026-07-12 - Grok (document_surfer + archives + 31B RTX 5090 profiles)

Branch: `feature/ideal-jarvis-all-enhancements`  
Worktree: `D:/jarvis-gpt-ideal` only (does **not** touch `D:/jarvis-gpt` / Codex WIP on `main`).  
Tip commits: `966ca10` (launcher menu), `84fce53` (31B profiles), earlier document stack `7138acb` / `3930e37` (merged with main at `13698bc`).

**Documents / files (production)**

- New black-box `document_surfer.py` (document analogue of `web_surfer`): inspect/read/analyze/review/compare/search/corpus/generate/convert/package + archive ops.
- `file_types.py`: magic-byte + compound-extension identification (archives, Office, images, media, exec, text/code, …).
- `archive_runtime.py`: safe list/extract/read/create for zip/tar/tar.gz|bz2|xz/gz/bz2/xz; optional 7z/rar; path-traversal and size-bomb guards.
- Tools wired in `tools.py` + agent allowlist: `documents.*` including `file.identify`, `file.probe`, `archive.list|extract|read_member|create|search`.
- Ideal-branch stubs cleaned (no import-time print); experimental modules stay out of core registry.
- Tests: `test_document_surfer.py`, `test_file_types_and_archives.py` (plus existing document_runtime).

**31B NVFP4 on RTX 5090 32GB + 128GB RAM**

- Problem: old `gemma4-mono` (`util=0.94`, 16k ctx, only 8GB offload) left almost no VRAM headroom → OOM / driver faults.
- `gemma4-mono` (stability / partial offload): offload 24GB, swap 16GB, util 0.85, max_len 16384, max_num_seqs 2, eager.
- `gemma4-mono-perf` (GPU-first throughput): offload 0, swap 8, util 0.90, max_len 8192, max_num_seqs 4, CUDA graphs.
- `gemma4-turbo` unchanged (26B, no offload).
- Dispatcher env formatting: `gpu_memory_utilization` always `%.2f`.
- Launcher: **all profile choice only via `.\jarvis.cmd` menu** (arrows). Removed `jarvis-mono-perf.cmd` / `jarvis-mono-offload.cmd`. Menu labels: Turbo 26B / Mono 31B stable offload / Mono 31B max perf. `-Profile` CLI remains for scripts.
- Docs: `config.py`, `model_catalog.py`, `scripts/jarvis-launcher.ps1`, `.env.example`, README, architecture, runtime.
- Tests: dispatcher + config profile suite green after retune (23 passed in that slice).

**Coordination / safety**

- Early session mistake: shared-worktree stash/checkout interfered with another agent — fixed by dedicated worktree + never checkout/stash on `D:/jarvis-gpt` while Codex is there.
- Next assistants: append here after each commit (newest on top). Prefer `D:/jarvis-gpt-ideal` for this branch; leave Codex worktree alone.

### 2026-07-12 - Codex (immediate exact operator actions)

- Explicit commands in the current persisted user message now execute immediately without a
  second approval. The one-use capability is bound to conversation id, message id, tool, and
  canonical arguments; it cannot flow into history, resumed turns, tasks, or missions.
- Agentic mutations are exposed only when their full operands match the current command.
  Typed execution defaults are canonicalized for replay detection, while unrequested flags,
  wrapper controls, browser targets, native window/process hints, sensitive-input switches,
  debug endpoints, profiles, ports, and timing overrides fail closed into the existing approval
  path.
- Direct actions now cover explicit Wikipedia/full-URL/bare-domain opens, known Windows apps,
  named files, default-app file opens, active-window typing, and empty-file creation. Shopping
  search commands and follow-ups deterministically open the selected verified candidate in the
  same turn instead of creating an approval.
- `filesystem.write_text` gained `mode=create`, which permits empty content and fails if the path
  already exists. Existing overwrite/append behavior remains unchanged.
- Main implementation: `backend/src/jarvis_gpt/agent.py` and `tools.py`; regressions in
  `test_agent.py`, `test_agentic_loop.py`, and `test_tools.py`; public behavior documented in
  `README.md`, `docs/architecture.md`, and `docs/runtime.md`.
- Verification: full backend suite `702 passed, 13 skipped`; full Ruff and compileall clean;
  `git diff --check` clean before publication.

### 2026-07-11 - Codex (source-aware, criterion-aware catalog search)

Fixed the remaining exact failure `а какой самый мощный лазер есть на
вайлдберрис?` and generalized the path instead of adding a phrase-specific
answer.

- Added shared `shop_registry.py` for localized aliases and search endpoints
  (DNS, Ozon, Wildberries including common Russian misspellings/WB/ВБ, Yandex
  Market, Citilink, M.Video, Eldorado, Regard, Avito, AliExpress). Shopping
  routing now distinguishes catalog intent from company/news/support/DNS
  networking questions and compares every explicitly named marketplace.
- `web.shop_search` now carries an explicit ranking criterion and hard
  price/rating constraints. Product wording, delivery city, budget and ranking
  adjectives are separated before search, so e.g. `лазер до 3000 рублей` is
  searched as `лазер` and filtered afterwards. Arbitrary `до/от` product specs
  are not misread as prices.
- Wildberries uses its current catalog JSON API first, merges neutral and one
  recall-oriented query, deduplicates real product IDs, and returns direct card
  URLs. Browser fallback parses only real product cards and enriches bounded
  detail pages/specs on other registered shops.
- Non-price comparisons use typed compatible metrics (power, speed, capacity,
  range, runtime, dimensions, weight, rating confidence, review popularity).
  Units preserve case (`MW` != `mW`, bytes != bits), stock is a tie-break after
  the requested metric, and a superlative requires at least two comparable
  cards. Seller claims are explicitly labelled and unsupported criteria fail
  closed instead of falling back to price/search order.
- Explicit `search_url` is restricted to public URLs on the selected registered
  shop domain; main-frame cross-domain redirects are aborted.
- Verification: Ruff and `git diff --check` clean; full backend suite `686
  passed, 13 skipped`. Live exact-query smoke used `web.shop_search` (not
  `web.answer`), compared 7/24 typed cards, and returned the 100000 mW listing
  with its direct Wildberries product URL plus the seller-data caveat.

### 2026-07-11 - Codex (exact DNS 5090 + bounded Russian news fixed live)

The previous chat-routing fix was active, but DNS still returned the same answer:
the headless Playwright catalog hit Qrator 401/403, then `_run_shop_search`
silently fell back to cached `web.answer` with 0 sources. The news request used
generic homepage evidence and allowed a refusal to replace the requested digest.

- Shopping (`web_surfer.py`, `tools.py`, `agent.py`): Windows DNS searches now
  retry in installed stable Chrome, headful but off-screen, with full browser /
  context cleanup. After anti-bot and city redirects the code reapplies and
  verifies `order=price`; unconfirmed sorting is labelled "из найденных" rather
  than a global minimum. Bare `5090` becomes `rtx 5090`; query relevance is
  strict for model/brand/qualifiers; DNS heuristic parsing accepts direct
  `/product/` cards only; unavailable products cannot win. Soft failures no
  longer fall through to a misleading generic cached answer.
- News (`tools.py`, `agent.py`): "вчера и сегодня" becomes an exact
  Europe/Moscow calendar window. Only dated article URLs inside every requested
  day are accepted; body dates and `dateModified` are not publication dates.
  Publisher RSS fallback now includes РИА, Интерфакс, РБК, ТАСС, Ведомости and
  БФМ, filters domestic relevance, balances results across the requested days,
  and produces a deterministic digest. Cache keys include the date window;
  partial coverage, missing tool, and exceptions fail closed instead of dropping
  into legacy homepage search. Windows longer than 31 days are rejected before
  network work.
- Tests: full backend suite `650 passed, 13 skipped`; Ruff and `git diff --check`
  clean. Added regressions for stable-Chrome retry, sort confirmation, DNS
  category exclusion, qualifier/model matching, exact news coverage, RSS,
  domestic filtering, publication-date authority, wide-window rejection, and
  bounded-news failure paths.
- Live verification after restarting only the backend (`pid 32404`, frontend /
  bridge / dispatcher reused): the exact DNS request returned 9 direct RTX 5090
  products for Moscow, price-sort confirmed, cheapest Palit GameRock OC at
  413,999 RUB; the exact news request returned 6 concrete dated articles, split
  3 for 2026-07-10 and 3 for 2026-07-11, through `web.answer`. Diagnostic chat
  conversations were deleted afterward.

### 2026-07-10 - Claude (route shopping chat to web.shop_search)

Fix for "still the exact same answer": `web.shop_search` existed but the chat
shopping path never called it — `_run_web_research` went straight to
`web.answer` (0 sources on DNS => the misleading "сайт не отдал данных" link).

- `_run_web_research` now, BEFORE `web.answer`, routes shop-specific price
  queries to `web.shop_search`: gated on `_looks_like_shopping_query` AND a
  recognized shop (`_shop_key_from_message` via `_shopping_domain_hint`:
  dns/ozon/wildberries/citilink/mvideo/eldorado/yandex market) AND
  `_web_surfer_available()`.
- `_run_shop_search`: ok+items => ranked cheapest-first answer
  (`_format_shop_search_answer`, remembers candidates for followups);
  needs_install => honest actionable message (pip install
  requirements-surfer.txt + `playwright install chromium` + direct shop link)
  instead of the misleading fallback; soft-fail (anti-bot/empty) => None =>
  existing web.answer path runs unchanged.
- CRITICAL gating decision: `_web_surfer_available()` checks a REAL on-disk
  Playwright+bs4 install (find_spec origin must be a real file, not a stubbed
  sys.modules entry). This keeps every existing shopping test unchanged in CI
  (Playwright absent => hook never fires => web.answer path as before). The
  feature activates only where Playwright is installed. Without this gate the
  hook broke 9 Codex shopping tests in test_agent.py that mock tools.run.
- test_shop_search.py now pops its Playwright stub from sys.modules after
  importing web_surfer, so it does not poison other tests' availability check.
- Tests: `backend/tests/test_shop_routing.py` (5) — shop-key detection, answer
  formatting, DNS query routes to web.shop_search (not web.answer), needs_install
  actionable message, soft-fail falls through. test_agent 76/76 restored;
  shop suites 89/89 together. Remaining full-suite failures (executive_runtime,
  execution_tools, web_surfer_adapter, host_bridge_script, model_hub) PRE-EXIST
  on this main without my changes (confirmed by stash) — Codex subsystems on
  Linux; my changes add 0 failures. ruff clean.
- Operator action still required for it to actually run: on D:\jarvis do
  `pip install -r backend/requirements-surfer.txt && playwright install chromium`.
  Until then the hook is dormant and web.answer path is used (unchanged).

### 2026-07-10 - Claude (shop_search + web.shop_search wiring)

Root cause of "Jarvis can't find the cheapest 5090 on DNS": for dns-shop.ru the
`web.answer` pipeline returns 0 sources (JS/anti-bot catalog that httpx
search/fetch/render cannot read), so it hits the `if not sources` branch =>
"Сайт не отдал достаточно данных... прямая ссылка". Real browser renders it fine
(operator screenshot). Fix = drive a real browser for shop queries.

Added to `web_surfer.py`:
- `async shop_search(query, *, shop=None, search_url=None, max_items=24,
  cities=None) -> dict`. Renders a shop search page, sets delivery city
  (Донецк -> Москва) via `_SET_CITY_JS`, extracts product tiles, ranks
  cheapest-first. Returns
  `{ok, query, shop, url, city, count, cheapest:{title,url,price_text,
  price_value}, items:[...], error}`.
- Pure helpers (unit-tested, no browser): `shop_search_url(shop, query)` +
  `_SHOP_SEARCH_TEMPLATES`/`_SHOP_ALIASES` (dns/днс, ozon, wildberries/вб,
  citilink, mvideo, eldorado, yandex market, regard);
  `_extract_catalog_items(html, base_url)` (Schema.org ItemList first, then
  anchor-first heuristic: product-href link + nearest RUB price, nav links
  without a price excluded); `_rank_catalog_items` (unpriced last).

Wired into the tool registry (`tools.py`): safe tool `web.shop_search`
(`_web_shop_search`) LAZY-imports `web_surfer` inside the handler — backend
never crashes when Playwright is absent; it returns `data.needs_install=True`
with the pip/`playwright install chromium` hint. Proxies read from
`JARVIS_WEB_PROXIES` (comma-separated). SYSTEM_PROMPT now routes
"найди дешёвую X на <магазин>" to `web.shop_search` and forbids the "погугли
сам" bail when the tool is available.

Deps: NOT added to `backend/requirements.txt` (kept lightweight / non-breaking
for CI+Docker). New optional `backend/requirements-surfer.txt`
(playwright==1.49.1, beautifulsoup4, lxml, playwright-stealth). Operator must
run `pip install -r backend/requirements-surfer.txt && playwright install
chromium` on D:\jarvis for the browser path to activate — THIS is the real
unblock; until then web.shop_search degrades honestly.

Tests: `backend/tests/test_shop_search.py` (8) — URL templates/aliases, DNS-grid
catalog parse, cheapest-first ranking, JSON-LD ItemList, result shape, tool
registration (safe), arg validation, honest no-browser degradation. bs4-gated
via `pytest.importorskip`; Playwright stubbed. Full run: my 8 pass; 4 failures
(`test_execution_tools`, `test_host_bridge_script` x2, `test_model_hub` C:/) are
PRE-EXISTING on this main (Windows-path/strict-file tests on Linux), confirmed
by re-running with my changes stashed — not caused by this work. ruff clean.

Next for Codex: route the chat shopping intent (`_run_web_answer_engine` /
shopping detection) to try `web.shop_search` before `web.answer` for
site-specific "на <магазин>" queries; optionally fold shop_search results into
the evidence ledger + `web.answer` cards. `aggressive_shopping(product_url)` is
also available to enrich the cheapest hit with specs + real negative reviews.

### 2026-07-10 - Claude (web_surfer.py — isolated Playwright surfer)

New standalone module `backend/src/jarvis_gpt/web_surfer.py`. Zero imports from
the rest of `jarvis_gpt`; drop-in. NOT yet wired into the tool registry — Codex
integrates it. Complements (does not replace) `browser_cdp.py`/`web_orchestrator`:
CDP layer drives the operator's real Chrome; this module owns a throwaway
Playwright Chromium with stealth/proxy for autonomous scraping.

New pip deps (must be installed in the D:\jarvis runtime, not yet in
requirements.txt): `playwright`, `beautifulsoup4`, `lxml`, `playwright-stealth`
(optional — degrades if absent). Post-install: `playwright install chromium`.
`playwright-stealth` is imported lazily via `_apply_stealth()`; missing lib =>
no crash, just no stealth. `lxml` optional (falls back to `html.parser`).

Public class `JarvisWebSurfer`. Async context manager: `async with
JarvisWebSurfer(config=None, *, proxies=None, user_agents=None, headless=None,
logger=None) as s:` OR explicit `await s.start()` / `await s.close()`. Calling a
public method before `start()` raises `WebSurferError`. Config via dataclass
`SurferConfig` (headless, proxies:list[str] `http://user:pass@host:port`,
user_agents, extra_headers, locale=ru-RU, timezone_id=Europe/Moscow, viewport,
*_budget_sec, max_concurrency<=8, pacing/typing delays, max_chars_per_page,
use_stealth). Proxy strings rotate round-robin; UA rotates round-robin.

Method signatures + return JSON schemas:

- `async def fast_fact(self, query: str) -> dict`
  `{ok:bool, query:str, answer:str, snippets:[{title:str,url:str,snippet:str}],
    source:"duckduckgo", elapsed_ms:int, error:str|None}`
  API-first (DuckDuckGo instant-answer JSON + html.duckduckgo.com snippets) via
  a Playwright APIRequestContext, hard-bounded by `fast_fact_budget_sec` (2.0s)
  with `asyncio.wait_for`. Never raises; timeout/error => `ok=False`, `error` set.

- `async def deep_research(self, query: str, max_depth: int = 3) -> str`
  Returns a Markdown report (string). Seeds links from `fast_fact`, fetches top
  `max_depth` (clamped 1..8) links in parallel (Semaphore=max_concurrency), each
  page sanitized to Markdown and paragraph-deduped. Bounded by
  `deep_research_budget_sec` (45s). Never raises; on failure returns a Markdown
  string explaining the gap.

- `async def aggressive_shopping(self, product_url: str) -> dict`
  `{ok:bool, url:str, title:str,
    price:{text:str,value:float|None,currency:str},
    availability:str, specs:{name:value},
    rating:{value:float|None,count:int|None},
    negative_reviews:[{rating:int|None,text:str,cons:str}],
    captured_api:[str], app_state_keys:[str], error:str|None}`
  Renders card, intercepts XHR/Fetch JSON (`captured_api`), pulls
  `__NEXT_DATA__`/`__INITIAL_STATE__`/`__NUXT__`/`__APOLLO_STATE__`
  (`app_state_keys`), resilient selectors CSS->XPath->text->JSON-LD for
  price/availability/specs/rating, then opens reviews, prefers a
  low-rating/"Сначала отрицательные" filter, paginates (Показать ещё / wheel),
  and returns ONLY 1-3★ reviews plus any with an explicit "Минусы/Недостатки"
  block (5★ spam dropped by `_filter_negative_reviews`). Bounded by
  `shopping_budget_sec` (60s).

Exceptions (all subclass `WebSurferError(RuntimeError)`): `BrowserLaunchError`,
`ProxyError`, `NavigationError`, `AntiBotError`, `ParsingError`,
`SurferTimeoutError`. IMPORTANT for the caller: the three PUBLIC methods swallow
`WebSurferError` internally and always return their dict/str with `ok=False` +
`error` (or a Markdown gap string) — safe to call without try/except for control
flow. `start()`/`close()` and internal calls CAN raise `BrowserLaunchError`/
`ProxyError`; wrap `start()` if you want to fall back to the CDP path. Anti-bot
walls raise `AntiBotError` internally and surface as `error` on the shopping/
research result (route those to the operator-Chrome handoff instead).

Integration sketch for the tool loop: register safe tools
`web.surf.fact`/`web.surf.research`/`web.surf.shopping` that instantiate one
`JarvisWebSurfer` per call (or pool one), pass `settings`-derived proxies, and
map results into the existing evidence-ledger/`web.answer` shapes. Note
`negative_reviews[*].cons` + low ratings are exactly the "real user downsides"
signal the shopping answer path lacks today.

Verification done here: `py_compile` OK, `ruff` clean, AST check (no TODO/`pass`
stubs, public API + exceptions present), and functional unit checks of every
pure helper with a stubbed `playwright` (proxy parse incl. URL-encoded creds,
price parse, negative-review filter/cons, HTML->Markdown noise stripping, DDG
HTML parse, JSON-LD offer extraction, config clamping). Live browser runs need
the operator's environment + `playwright install chromium` (+ proxies).

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
  Command Center/trace pages, `docker-compose.yml`,
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
  loopback-only while browser authentication is disabled. Backend remains
  loopback/internal. Launcher token is 256-bit with current-user ACL and never
  reuses/kills foreign listeners.
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

### 2026-07-10 - Codex (launcher app mode and LLM reuse)

Claude Sync Note

[Измененные файлы и новые зависимости]

- `scripts/jarvis-launcher.ps1`, `README.md`,
  `backend/tests/test_deployment_contracts.py`, `docs/assistant-notes.md`.
- New dependencies: none.

[Что конкретно исправлено / какие заглушки устранены]

- Added first-class app-only startup: bridge + backend + UI without starting the
  dispatcher. Existing configured LLM connectivity remains usable.
- Full start now probes the current dispatcher/container and `/v1/models` before
  Docker startup. Running/warming managed dispatcher or valid existing endpoint
  is reused; unknown listener on 8001 is rejected instead of collided with.
- Dispatcher ownership and container ID are persisted. `stop/restart` preserves
  app-external/reused LLM runtimes, retains ownership across repeated full-start
  for the same container, and stops only a dispatcher owned by that state.

[Изменения в API-контрактах, сигнатурах функций и структурах данных]

- New CLI/menu action: `jarvis.cmd app [-Profile ...]`.
- New internal decisions: `Get-LlmStartDecision` -> `reuse|conflict|start`,
  `Test-LauncherOwnsDispatcher`, and `Test-ReusedDispatcherOwnership`.
- `services.dispatcher` state adds `started_by_launcher`, `container_id`,
  `reused`, `skipped`, and optional `phase`.

[Pending: Текущие точки сборки и что Claude должен делать/проверить дальше]

- Verified PowerShell parse, decision/ownership matrix, direct `jarvis.cmd app`
  smoke with temporary runtime, deployment tests, and full repository gates.
- On the target host, confirm full start prints the reuse message while a real
  vLLM container is loading/ready; it must not recreate that container.

### 2026-07-10 - Codex (temporary removal of Command Center Basic Auth)

Claude Sync Note

[Измененные файлы и новые зависимости]

- Removed `frontend/proxy.ts`; updated the same-origin API route,
  `docker-compose.yml`, launcher/dev scripts, deployment tests, and runtime docs.
- New dependencies: none.

[Что конкретно исправлено / какие заглушки устранены]

- Removed browser-facing HTTP Basic Auth and all login/password output. The UI
  now opens directly on localhost:3000.
- LAN action/menu and configurable frontend bind address are disabled while the
  browser has no authentication. Next is fixed to loopback to prevent an
  unauthenticated remote client from using the privileged server proxy.
- Server-to-server API token injection and cross-site mutation rejection remain;
  missing server token fails only `/jarvis-api/*` with HTTP 503.

[Изменения в API-контрактах, сигнатурах функций и структурах данных]

- Removed Next middleware Basic challenge (`401`/`WWW-Authenticate`).
- `jarvis.cmd lan` is unavailable and `-Lan` returns an explicit temporary-disable
  error. `start`, `app`, `restart`, and localhost UI contracts are unchanged.

[Pending: Текущие точки сборки и что Claude должен делать/проверить дальше]

- Restore LAN only together with a new operator-approved authentication policy.
- Verified: anonymous localhost GET=200 without `WWW-Authenticate`; missing
  server token API=503; Next typecheck/build; Compose config; PowerShell parser;
  backend `337 passed`; Ruff clean.

### 2026-07-10 - Codex (legacy execution test migration)

Claude Sync Note

[Измененные файлы и новые зависимости]

- Updated `backend/tests/test_agent.py`, `test_agentic_loop.py`,
  `test_approval_executor.py`, `test_api_smoke.py`, and `test_operator_queue.py`.
- New dependencies: none. No `src` or web changes in this test-only migration.

[Что конкретно исправлено / какие заглушки устранены]

- Removed obsolete assertions for the deleted raw host bridge and heuristic
  console/PowerShell command recipes. Raw console text now has a regression test
  proving that it creates no approval and reaches neither native nor structured
  execution.
- Migrated agentic, mission-resume, approval-executor, API-smoke, and operator
  queue fixtures to typed `execution.apply` payloads. Approved execution is
  verified with a real bounded `fs.write`; pre-approval paths leave files absent.
- Native inspection mocks now exercise `HostBridgeClient.action(action, payload,
  timeout_sec)` and assert structured action/payload fields.

[Изменения в API-контрактах, сигнатурах функций и структурах данных]

- Legacy tests now treat `host.bridge.execute` as unregistered even when a danger
  override is supplied.
- Mutations use danger-gated `execution.apply` with protocol
  `jarvis.execution.v1`; native desktop actions remain structured
  `windows.native` approvals.

[Pending: Текущие точки сборки и что Claude должен делать/проверить дальше]

- Targeted legacy suite: `109 passed`.
- Ruff on all five migrated files: clean.

### 2026-07-10 - Codex (deterministic execution substrate and structured host bridge)

Claude Sync Note

[Измененные/созданные файлы, новые зависимости и структуры данных]

- Added `backend/src/jarvis_gpt/execution_actions.py`, `execution_config.py`,
  `execution_kernel.py`, `execution_models.py`, `execution_process.py`,
  `execution_protocol.py`, `execution_session.py`, and `execution_transaction.py`.
- Added execution/kernel/process/transaction/session/tool/client regression suites and
  `docs/execution-capabilities.example.json`; updated agent, ToolRegistry, CLI, storage,
  Host Bridge, launcher, Command Center, runtime documentation, environment template,
  deployment contracts, and migrated approval/agent tests.
- New dependencies: none. New primary data structures: strict discriminated Pydantic action
  envelopes; `ActionFeedback`/`ExecutionFeedback` snapshots; durable checkpoint manifests;
  bounded `ExecutionSession` state/history/process records; capability policy records.
- Web-surfing internals were not modified. Existing web tools remain an isolated black box;
  only their structured host/browser launch adapter is connected to the substrate.

[Спецификация разработанных системных JSON-интерфейсов для вызова инструментов]

- Protocol `jarvis.execution.v1`: `{"protocol":"jarvis.execution.v1","action":{...}}`.
  Supported discriminators: `fs.stat`, `fs.list`, `fs.read`, `fs.mkdir`, `fs.write`,
  `fs.copy`, `fs.move`, `fs.delete`, `process.run`, `process.terminate`,
  `network.resolve`, `network.tcp_probe`, `registry.get`, `registry.set`, and
  `registry.delete`. Unknown/extra fields and non-absolute filesystem paths fail closed.
- `execution.capabilities {}` returns action JSON Schema, effective roots, deny paths, and
  process/network/registry policy. `execution.inspect {payload, session_id?,
  finalize_session?}` accepts read-only actions. Approval-gated `execution.apply` uses the
  same wrapper for mutation/process/control actions.
- Approval-gated `execution.transaction {actions[1..128], idempotency_key, session_id?}`
  executes only reversible filesystem/registry mutations under one durable checkpoint.
  `execution.session {operation:list|create|get|transition,...}` exposes the state machine;
  `execution.cancel {session_id}` interrupts only exact session-owned process trees.
- Result envelope: protocol, `ok`, action id/class, replay marker, transaction/checkpoint
  status, and `ActionFeedback`; process feedback additionally contains redacted argv, PID,
  exit/termination reason, bounded stdout/stderr, PID tree, permission snapshot, observed
  filesystem diff, timing, interrupt/kill flags, and error.
- Host bridge contract `action.v1`: authenticated `POST /action` request
  `{"action":string,"payload":object,"timeout_sec":integer}`. Allowed actions are
  `capabilities`, `app.open_and_type`, `process.start`, `chrome.launch`, `url.open`,
  `window.list`, `window.focus`, `keyboard.send`, `screen.capture`, and `wmi.query`.
  `/execute` returns 410 and `host.bridge.execute` is no longer registered.
- Policy environment: `JARVIS_EXECUTION_ROOTS`,
  `JARVIS_EXECUTION_CAPABILITIES_FILE`, and `JARVIS_BRIDGE_APP_PATHS_JSON`. Bridge
  capabilities publish `policy_revision=native-app-v1` plus the exact app-path configuration
  SHA-256; launcher/backend readiness requires both values to match.

[Что конкретно исправлено / какие заглушки устранены]

- Replaced heuristic raw console/PowerShell recipes with typed atomic OS actions and strict
  capability validation. Process/network/registry capabilities are deny-by-default;
  executable argv and explicit environment values are regex-constrained; inherited
  environment is opt-in; protected Jarvis state, secrets, logs, policy files, and executable
  paths are excluded from writable roots.
- Implemented nonblocking process streams, output tails, total-byte/truncation metadata,
  timeout/stall detection, graceful interrupt followed by tree kill, exact PID birth-marker
  ownership, and cancellation-safe session registration. Windows children start suspended,
  enter a Job Object, then resume; POSIX uses a new process group with portable process-tree
  fallback where `/proc` is absent.
- Implemented durable filesystem/registry checkpoints, atomic manifests, startup recovery,
  hierarchical resource locking, automatic reverse rollback, commit durability barriers,
  and in-process action/transaction idempotent replay. Session history compresses older
  entries into dry facts under entry/byte limits.
- Closed concurrent sibling-path/action-id races, cancel-before-register and concurrent-root
  process races, false timeout-after-exit, ignored interrupt ownership, cancelled-task session
  leaks, explicit-environment injection, cwd escape, partial-commit cancellation, registry
  new-key rollback, and secret leakage in argv/tool-run persistence.
- Hardened Host Bridge to token-authenticated structured actions only. Executables resolve to
  canonical fixed locations or operator-pinned exact existing non-symlink paths; shells,
  script hosts, PATH/HKCU shadowing, malformed app paths, wildcards, ADS/device paths, and
  unrestricted native argv are rejected. Token creation is atomic and its Windows DACL is
  repaired/verified on every use. Launcher command-line quoting and stale-contract restart
  checks are deterministic.

[Изменения в API-контрактах, сигнатурах функций и структурах данных]

- `HostBridgeClient.execute(command, ...)`/raw `/execute` were replaced by
  `HostBridgeClient.action(*, action, payload, timeout_sec)`/`POST /action`.
- Agent/tool approvals now carry typed execution envelopes. Frontend manual host input accepts
  execution JSON and creates `execution.apply` approvals. CLI adds `host-bridge-action` and
  does not expose raw command execution.
- Process execution now requires a session id in both wrapper and `process.run` action; only
  one root process may run per session. Terminal session states, process reservations, exact
  owned-process records, compressed facts, and checkpoint/transaction status are observable.
- Browser and external subsystems retain their internal APIs and enter the kernel through
  ToolRegistry adapters; browser multi-open retains bounded concurrency.

[Pending: Текущие точки сборки и что Claude должен делать/проверить дальше]

- Verified on Windows: backend `392 passed, 1 skipped`; full Ruff and compileall clean;
  PowerShell parser clean; frontend typecheck and production build pass; npm production audit
  reports 0 vulnerabilities; Python environment has 33 compatible packages; Compose config,
  CLI help, and `git diff --check` pass.
- A bridge process already running before this revision may still expose the stale contract;
  the launcher will authenticate/probe and replace it on the next Jarvis stack start. It was
  intentionally not interrupted during this session.
- Idempotency replay cache is process-local, not cross-restart exactly-once. Durable recovery
  covers incomplete checkpoint rollback. Path TOCTOU is reduced by canonical validation,
  identity checks, temporary files, and atomic replace, but not implemented with handle-relative
  traversal on every OS.
- macOS/BSD portable `ps` fallback is unit-covered but not exercised on a native macOS/BSD CI
  runner here. Custom app paths remain an operator-owned ACL/policy concern and must be pinned
  explicitly; verify their `available_apps` capability on the target machine.

---

### 2026-07-11 - Sol (Executive Function, Verification, Memory, and release hardening)

Claude Sync Note

[Итоговая карта репозитория и интерфейсы взаимодействия модулей после фазы оркестрации]

- New core modules: `cognitive_memory.py` (host profile + verified playbooks),
  `execution_filesystem.py` (handle-anchored filesystem operations),
  `execution_replay.py` (cross-restart transaction replay ledger),
  `executive_planner.py` (strict adaptive DAG), `executive_runtime.py` (mission/DAG
  coordinator and action binding), `state_verification.py` (independent inspectors +
  SafeGate), `runtime_lease.py` (single-primary OS lease), and `redaction.py`.
- Existing substrate modules were hardened and integrated: `execution_actions.py`,
  `execution_protocol.py`, `execution_process.py`, `execution_transaction.py`,
  `execution_kernel.py`, and `execution_session.py`. Integration points are `agent.py`,
  `approval_executor.py`, `api.py`, `cli.py`, `dispatcher.py`, `storage.py`, and `tools.py`.
- Web boundary: Claude's bundled `web_surfer.py` is preserved byte-for-byte and connected
  through `web_surfer_adapter.py`/`web_surfer_worker.py`. Sol did not modify its internals.
  The existing generic web stack remains independent and is the fail-closed fallback.
- Persistent structures: `<JARVIS_HOME>/host_profile.json`
  (`jarvis.host-profile.v1`, stable capability fingerprint + full snapshot digest),
  `<state>/execution-playbooks.sqlite3` (`jarvis.execution-playbook.v1`),
  `<state>/execution-replay-journal.json`
  (`jarvis.execution-replay-journal.v1`), durable checkpoint manifests/WAL, and
  executive plan/approval reconciliation records in the primary SQLite database.
- Executive state uses `jarvis.planner.v1` inside `jarvis.executive.v1`. DAG nodes have
  immutable IDs, explicit dependencies, precondition fingerprints, evidence policy,
  assertion criteria, bounded attempts, and revision history. Ready-task claiming is
  dependency-aware and atomic; replanning replaces only unfinished descendants.
- One `jarvis.primary-runtime-lease.v1` file lock serializes API and mutating CLI access.
  Cold-start recovery runs under that lease before new mission work. Interrupted approvals
  use `jarvis.approval-reconciliation.v1`; ambiguous side effects become verify-only DAG
  branches and are never replayed speculatively.
- Filesystem mutation now pins directory ancestry/identities throughout checkpoint,
  mutation, verification, and rollback. Windows uses non-delete-share directory handles
  and handle-based rename/chmod; POSIX uses `openat`/`dir_fd` + no-follow semantics.
- Transaction commits write a committed checkpoint WAL record, atomically persist a
  bounded checksummed replay snapshot, then retire the checkpoint. Cold start and live
  retry import committed WAL before any possible re-execution; key/fingerprint/result
  collisions and corrupted snapshots fail closed.
- Process execution is argv-only and policy-pinned, drains stdout/stderr concurrently,
  detects total timeout/stall, sends graceful interrupt then kills the owned tree, and
  returns bounded output, PID tree, permission state, timing, and filesystem diff.
  Windows Job Objects and POSIX process groups/supervision preserve exact ownership.
- Bundled web runtime dependencies are pinned in `pyproject.toml`,
  `backend/requirements.txt`, and `uv.lock`: `playwright==1.61.0`,
  `beautifulsoup4==4.15.0`, `lxml==6.1.1`, and `playwright-stealth==2.0.3`.
  Docker provisions the matching Playwright headless Chromium under `/ms-playwright`.

[Формат работы оркестратора и верификатора, спецификации вызовов для Claude]

- Atomic OS envelope remains strict:
  `{"protocol":"jarvis.execution.v1","action":{"kind":...,"action_id":...}}`.
  Supported typed families: FS stat/list/read/mkdir/write/copy/move/delete, process
  run/owned terminate, DNS/TCP inspection, and registry get/set/delete. Unknown fields,
  relative paths, shell execution, denied roots, and unconfigured capabilities fail closed.
  `fs.copy`/`fs.move` accept `expected_sha256`; executive `fs.move` requires this source
  binding.
- `execution.inspect`/`execution.apply` arguments:
  `{"payload":<jarvis.execution.v1>,"session_id"?:string,
  "finalize_session"?:bool,"safe_gate_token"?:string,
  "verification"?:{"paths"?:[],"tcp"?:[],"processes"?:[]}}`.
  Read-only actions use `inspect`; mutations/process/control use approval-gated `apply`.
  `process.run` requires an explicit postcondition and a matching pre-action baseline.
- `execution.preflight` is dry-run only: `{"payload":<jarvis.execution.v1>}`. It returns
  `jarvis.safe-gate.v1`; high/critical actions require the returned one-use, action-bound,
  expiring `permit_token` as `safe_gate_token` during approved execution.
- `execution.transaction` arguments:
  `{"actions":[<jarvis.execution.v1>...],"idempotency_key":string,
  "session_id"?:string,"safe_gate_tokens"?:{action_id:token},
  "verification"?:{...}}`. Only reversible FS/registry mutations are accepted. Ordered
  action identity, collective subject/effect binding, verification, commit, and rollback
  are strict.
- `execution.verify` arguments:
  `{"source_tool":"execution.apply|execution.transaction","arguments":<exact original
  arguments>}`. It re-parses the original action(s), applies filesystem/network/registry
  capability policy, performs only independent postcondition inspection, returns
  `replayed:false`, and cannot execute or substitute a second mutation.
- `StateVerifier` does not trust exit code/log claims. It independently validates file
  identity/hash/content/syntax, TCP reachability across resolved addresses, exact
  session-owned PID birth identity, and registry value/type. Mutation verification occurs
  before checkpoint commit. Dispatcher start/stop and native launch have secondary
  socket/container/WMI checks.
- Executive approval contract `jarvis.executive-approval.v1` binds mission, plan revision,
  step attempt, environment digest, exact tool/arguments hash, semantic subject/effect,
  action identity, and postcondition digest. Only runtime-minted inspector evidence is
  accepted. `execution.inspect` discovery exports only intrinsic plan-bound typed subjects;
  supplemental verification subjects cannot expand downstream mutation authority.
- Playbook writes are permitted only from a successful typed execution whose exact action
  identity is confirmed by independent verifier evidence. Stored/retrieved lesson text,
  model output, stderr, indexed files, and remote content remain untrusted prompt data.
- Web black-box contract: `JARVIS_WEB_SURFER_MODULE` defaults to the bundled Claude module.
  The worker recognizes module functions/singletons or constructs the public
  `JarvisWebSurfer` class, awaits `start()`, and guarantees bounded `close()`/`aclose()`.
  Public calls are async `fast_fact(query)`, `deep_research(query,max_depth?)`, and
  `aggressive_shopping(product_url)`. Optional reviewed constructor kwargs (including a
  resident proxy pool) come from protected `JARVIS_WEB_SURFER_FACTORY_KWARGS_JSON`. After
  an isolated lifecycle probe, ToolRegistry conditionally
  publishes `web.surfer` with
  `{"mode":"fast_fact|deep_research|aggressive_shopping","arguments":{},
  "timeout_sec"?:number}`. Adapter/worker protocols are
  `jarvis.web-surfer-adapter.v1` and `jarvis.web-surfer-worker.v1`; bounded framed IPC,
  recursive credential redaction, worker identity pinning, hard deadlines, tree cleanup,
  and restart-on-failure are enforced outside the Claude module. Direct shopping targets
  reject credentials, local/reserved hosts, and any DNS answer that is not globally
  routable. A nested service `ok:false` remains a failed adapter/tool result.
- Read APIs: `GET /api/environment/profile`,
  `GET /api/memory/playbooks?query=...`, `GET /api/executive/plans/{mission_id}`, and
  `GET /api/internet/web-surfer`. Safe tools: `environment.profile`,
  `memory.playbooks.lookup`, `executive.plan.status`, `execution.capabilities`, and
  `web.surfer.capabilities`.

[Измененные/созданные файлы, новые зависимости и структуры данных]

- Production/test/doc changes are contained in the files listed above plus the associated
  `test_cognitive_memory.py`, `test_execution_replay.py`, `test_executive_*`,
  `test_runtime_lease.py`, `test_state_verification.py`, `test_web_surfer_*`, and expanded
  execution/agent/API/approval regression suites. `README.md`, `docs/runtime.md`, and
  `docs/architecture.md` describe the effective architecture. Dependency pins are listed
  above and the lockfile is synchronized.
- Release verification: Windows Python 3.14 full backend `609 passed, 13 skipped`; Python
  3.11 focused compatibility `228 passed, 12 skipped`; Ruff and compileall clean; targeted
  final security suite `15 passed`; frontend typecheck/production build pass; npm production
  audit reports 0 vulnerabilities; PowerShell parser and Compose config pass; uv lock/pip
  compatibility pass; sdist/wheel build and isolated Python 3.11 wheel CLI smoke pass.
  The production backend image builds with its Playwright browser provisioned; an actual
  Compose-configured container successfully constructed, started, probed, and closed
  Claude's `JarvisWebSurfer` service while the API process occupied container PID 1.

[Pending: Вектор развития системы и конкретные задачи для следующей сессии Claude]

- Claude retains sole ownership of `web_surfer.py`; keep its public class/method contract and
  do not bypass adapter/worker containment. Next Claude pass: remove the module's hardcoded
  Chromium `--no-sandbox` argument where the target runtime supports browser sandboxing,
  and add in-module redirect/subresource public-network enforcement to complement the
  adapter's direct-target DNS guard. Preserve structured `ok/error` results.
- Provision and review the production execution-capabilities JSON before enabling process,
  private-network, or registry actions. Verify exact executable hashes/argv/environment
  regexes and target prefixes on the deployment host.
- Add native Linux and macOS/BSD CI runners for platform-specific process-tree and filesystem
  containment integration coverage. Windows paths, Job Objects, syntax validation, recovery,
  and release packaging are verified in this phase.
- No open P0/P1 core defect remains from this phase. Future schema changes must preserve
  strict parsing, replay fingerprints, approval bindings, and backward-safe storage
  migration; extend tests before changing any protocol listed above.

## 2026-07-12 Codex handoff: profile invariants and reliable process actions

[Implemented]

- Built-in model identity is now profile-bound: `gemma4-turbo` uses only
  `gemma4-26b-a4b-nvfp4`; both mono profiles use only `gemma4-31b-it-nvfp4`.
  Custom Model Hub overrides remain supported, while cross-profile built-in overrides are
  ignored by the catalog and rejected by activation.
- Dispatcher reuse requires the exact desired image and complete vLLM command contract
  (model, dtype, eager mode, context, GPU/KV/concurrency/offload settings, tokenizer/load
  strategy, prefix cache, host and port). A stale or foreign mismatched container is removed
  before startup. Cold-start verification accepts a running exact container while HTTP is
  still warming; CLI verification failures now exit nonzero.
- Compose no longer defaults silently to 26B: `JARVIS_QWEN_MODEL_PATH` is mandatory.
  Launcher, dispatcher helper and smoke checks resolve or inject it explicitly. Runtime
  mismatch/image diagnostics survive the API schema and frontend typing.
- Added typed bridge actions `process.top` (read-only) and `console.show_processes` (fixed
  console view). Both accept only `limit=1..50` and `sort=cpu|memory|name|pid`; sorting occurs
  before limiting. The console action launches canonical Windows PowerShell with a fixed
  encoded script, `shell=False`, and independent PID/name verification. Raw command text is
  never accepted. Bridge policy is now `native-app-v2`.
- Russian requests such as `открой топ 10 процессов в консоли` route deterministically to the
  fixed action and continue to a factual result. Plain top-process requests use the safe
  `system.inspect/process.top` path.
- Tool-capable LLM rounds are classified before any stream content becomes visible. Exact
  standalone tool JSON executes; mixed or malformed tool-shaped output receives one internal
  correction, then fails with a safe answer if still invalid. Forced-final tool payloads never
  leak. Command Center treats the final `done.answer` as authoritative.

[Verification]

- Full backend: `789 passed, 13 skipped`; Ruff and compileall clean.
- Frontend: npm audit 0 vulnerabilities, typecheck and production build pass.
- PowerShell AST parse and explicit-path Compose config pass. Focused live Windows
  `process.top` smoke returned correctly sorted rows.
- The already running stale 26B/nightly container was intentionally not mutated. The next
  full launcher start with a mono profile will detect and replace it with the 31B/pinned-image
  runtime.
