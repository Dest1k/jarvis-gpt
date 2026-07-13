# JARVIS live functional harness

Loopback HTTP ignores inherited system proxy settings (`trust_env=False`) so
campaign traffic cannot be redirected outside the isolated runtime.

Этот каталог содержит только изолированный acceptance harness для текущего live
stack. Он не читает прежние audit queue/index/findings и не меняет production
source. Скрипт не запускает и не останавливает сервисы.

## Что проверяется

`run_functional_campaign.py` выполняет 54 явных case ID (`F001`–`F054`):

- health, status, host profile, model profiles/catalog и read-only dispatcher status;
- read-only CLI `profiles`, `status`, `models`, `llm-health`;
- обычный chat, отсутствие offline fallback, history и обе trace surfaces;
- целостность `application/x-ndjson`: JSON на строку, порядок событий, terminal
  `done`, равенство накопленных delta итоговому и сохранённому ответу;
- параллельные независимые conversations и раздельную историю;
- synthetic `.txt`, `.md`, `.json` только под `functional/evidence/synthetic`;
- upload, metadata, byte/SHA download, directory ingest и chunk search;
- одну memory запись в уникальном namespace;
- approval create/list/cancel без approve/execute;
- только три заранее разрешённых safe tools: `runtime.status`,
  `environment.profile`, `memory.search`;
- mission plan fail-closed: direct task completion получает 409, report без
  проверенного выполнения получает 404; `run` и `execute-next` не вызываются;
- validation/404/method errors;
- WebSocket event, hostile Origin и invalid token при strict loopback mode;
- параллельную latency/concurrency проверку `/health` и `/api/status`.

## Жёсткие ограничения безопасности

HTTP client блокирует любую mutation вне небольшого allowlist. В коде отсутствуют
пути к следующим операциям:

- approval execute;
- model download, activate или delete;
- dispatcher start/stop/restart;
- cleanup/self-heal/benchmark;
- autonomy job run/start;
- host bridge, GUI, browser и произвольные tools;
- mission run/execute-next.

Base URL принимается только для `localhost`, `127.0.0.1` или `::1`. CLI
запускается с `shell=False` и только по фиксированному read-only allowlist.

Тем не менее harness создаёт synthetic conversations, files, memory, approval и
mission через публичный API. Поэтому его следует направлять на предусмотренный
prompt отдельный runtime namespace/home, а не на пользовательскую production БД.
Approval автоматически переводится в `cancelled`. Созданные conversations по
умолчанию удаляются после фиксации history/trace evidence; `--keep-conversations`
оставляет их для ручной проверки. Для files, memory и missions публичных delete
endpoint нет, поэтому уникальный runtime home остаётся основной границей изоляции.

## Предусловия

1. Backend уже запущен на loopback и полностью готов.
2. Для backend и Command Center настроен один server-side `JARVIS_API_TOKEN`.
3. Harness запускается project Python, где доступны `httpx` и `websockets`.
4. Для полноценного PASS LLM route должен быть жив: offline fallback и
   `llm-health.ok != true` дают FAIL.
5. Backend cwd либо `JARVIS_HOME` должен разрешать synthetic directory под
   текущим репозиторием; иначе directory-ingest case честно даст FAIL.

## Запуск оператором

Harness в рамках подготовки не запускался. Пример будущего запуска:

```powershell
$env:JARVIS_API_TOKEN = '<тот же server-side token, что у backend>'
py -3.11 .\.audit\runs\20260713T002206Z_686424795712\functional\harness\run_functional_campaign.py `
  --base-url http://127.0.0.1:8000 `
  --campaign-prefix jarvis-functional
```

Полезные настройки:

```text
--parallel-chats 4
--read-concurrency 12
--read-latency-budget-ms 3000
--chat-latency-budget-ms 180000
--timeout 180
--ws-timeout 15
--keep-conversations
--skip-cli
```

`--skip-cli` оставляет обязательные CLI cases со статусом `SKIP`, поэтому общий
verdict будет `INCOMPLETE`, а не ложный `PASS`. Invalid-token WebSocket case
неприменим при отключённом strict loopback token mode; это единственный
необязательный `SKIP`.

## Evidence и verdict

Каждый запуск генерирует новый ID и namespace:

```text
<prefix>-<UTC timestamp>-<random nonce>
functional_acceptance.<UTC timestamp>.<random nonce>
```

Файлы создаются с exclusive mode и никогда не перезаписываются:

```text
functional/evidence/<campaign-id>.jsonl
functional/evidence/<campaign-id>.csv
functional/evidence/<campaign-id>.manifest.json
functional/evidence/synthetic/<campaign-id>/...
```

Case получает `PASS` только если вернул хотя бы одну фактическую проверку и все
проверки истинны. Пустая проверка становится `ERROR`; зависимости без evidence —
`SKIP`. Итоговый `PASS` возможен только без `FAIL`, `ERROR` и обязательных
`SKIP`. Exit codes: `0=PASS`, `1=FAIL`, `2=INCOMPLETE`.

Token и credential-like поля редактируются перед записью evidence. JSONL и CSV
дописываются после каждого case, поэтому уже полученные результаты сохраняются
даже при последующем сбое.
