# Multi-user security architecture

This document describes the implemented multi-user boundary in Jarvis. It is an
authorization and tenant-isolation layer around the existing safety runtime; it does not
replace HITL approval, execution policy, verification, or rollback.

## Request and tenant context

Every protected application request is resolved to an `ActorContext` before application code runs. The
context contains the internal `user_id`, active preset, identity/session IDs, source, and
`policy_epoch`. It is stored in a `ContextVar` and is therefore available to storage,
tools, events, background work, and approval execution without passing a user ID supplied
by the caller.

Ingress resolves actors as follows:

- A valid `JARVIS_API_TOKEN` authenticates the deterministic legacy user. Its active
  preset and `policy_epoch` are reloaded for every request, so the token is not an
  unconditional owner bypass and suspension or demotion takes effect immediately.
- `X-Jarvis-User-Session` is authenticated against a hashed, unexpired, non-revoked user
  session.
- The Telegram registration endpoint is authenticated with its separate bridge secret;
  subsequent Telegram calls use the returned user session.
- Background jobs load the owning user again and bind that actor before processing the
  user's reminder, notification, or approval reconciliation.

The legacy fallback maps code without an explicit actor to the deterministic owner. This
keeps local single-user code working during migration, but it is not an acceptable ingress
path for new multi-user transports. Every new transport and detached task must bind an
explicit actor.

Tenant isolation is enforced in several places:

- Personal SQLite rows carry `user_id`; reads, writes, search, updates, and deletes include
  that user in their predicates. Cross-tenant resource references fail closed through the
  ownership guard.
- Conversations, messages, memories, missions/tasks, reminders, files/chunks, tool runs,
  learning observations, approvals, runtime events, and the user audit log are scoped.
- Memory full-text search and file search filter by tenant before applying result limits.
- Every user's memory vault and ingested files live under per-user directories, except
  that the deterministic legacy user's existing paths are retained for compatibility.
- Browser shopping cookies, local storage, and persistent profiles are stored under a
  hashed per-user cache namespace; legacy shared browser state is not imported.
- Runtime key/value data is namespaced by user; only the deterministic legacy user keeps
  the old unscoped keys. Assigning `owner` never selects another user's namespace.
  Execution playbooks are keyed by both user and fingerprint.
- WebSocket clients are registered with a user ID, and events are delivered only to
  clients for that user.

Operational data such as health and global telemetry remains runtime-wide. Host execution
and arbitrary filesystem tools are not made tenant-safe merely by adding a database user
ID; those capabilities remain default-deny for ordinary presets.

## IAM data model

The IAM tables are stored in the same SQLite database as the current runtime:

| Table | Purpose |
| --- | --- |
| `users` | Internal principal, status, timestamps, optimistic `row_version`, and `policy_epoch`. |
| `external_identities` | Provider identity mapped to a user; unique on `(provider, realm_id, provider_subject_id)`. |
| `security_ids` | Capability catalog with description, category, risk, HITL default, source, and lifecycle status. |
| `permission_presets` | Stable built-in or custom preset identity and its active version. |
| `permission_preset_versions` | Immutable published preset revisions and change reason. |
| `preset_security_ids` | Grant/deny rows for one preset version, including delegation metadata. |
| `user_preset_assignments` | Preset assignment history; a partial unique index permits one active assignment per user. |
| `user_permissions` | Direct grants/denies, optional expiry, delegation flag, reason, and revocation history. |
| `user_sessions` | Random session tokens stored only as SHA-256 digests, with expiry and revocation. |
| `authorization_decisions` | Allow/deny decision trail, policy version, source, request context, and hashed resource reference. |
| `security_audit_log` | Administrative mutations with actor, target, reason, and before/after JSON. |
| `telegram_realms` | Persistent one-to-one binding between a canonical realm and immutable bot ID. |
| `telegram_updates` | Telegram replay ledger keyed by bot realm/update with bounded attempts and CAS lease tokens. |
| `telegram_owner_invitations` | Hashed, expiring, single-use owner invitations and their immutable claimant identity. |
| `telegram_sources` | Tenant-scoped public-channel subscriptions keyed by canonical bot realm and immutable chat ID. |
| `telegram_source_posts` | Normalized, versioned channel posts with timestamps, hashes, scripts, and provenance. |
| `telegram_source_audit` | Hashed-query audit for source registration, search, and analysis. |
| `material_access_audit` | Hashed-scope/query audit for privileged cross-user reads and searches. |
| `ingress_rate_limits` | Hashed per-principal/global fixed-window ingress budgets. |
| `iam_migrations` | Idempotent schema and capability-catalog migration markers. |

`constraints_json` is persisted for future policy constraints. The current evaluator uses
grant/deny, delegation, direct-permission expiry, user/capability status, active preset
version, and policy epoch; callers must not assume arbitrary constraints are enforced yet.

## Presets and permission evaluation

Jarvis creates five immutable built-in presets:

- `owner`: all registered capabilities, including recovery and security administration.
- `admin`: administrative APIs and operational read access, subject to delegation rules.
- `moderator`: its own tenant-scoped user surface and read-only visibility of users,
  security IDs, and presets; no IAM mutation rights.
- `user`: normal tenant-scoped data and allowlisted tools.
- `guest`: the least-privileged conversational baseline.

The registry is the source of truth; the descriptions above are baselines, not a substitute
for inspecting `/api/admin/security-ids` and the active preset version. New Telegram users
start as `guest`.

Custom presets contain any deduplicated set of active security IDs the assigning actor may
delegate. Permission order has no authorization meaning. Creating a preset publishes
version 1; updating a custom preset publishes a new version and retires the previous active
version. Existing assignments follow the new active version. Built-in presets cannot be
edited through the API.

A user has one active preset and may also have direct overrides. Evaluation is fail-closed:

1. Unknown/inactive users and unknown/disabled capabilities are denied.
2. Any applicable explicit deny wins.
3. An applicable direct grant wins over the preset result.
4. An active-version preset grant allows the operation.
5. Absence of a grant is a deny.

Permission and preset changes increment `policy_epoch` and revoke active sessions. Every
IAM mutation preserves at least one active owner with all effective recovery rights;
counting owner presets alone is insufficient because direct denies override them.
Non-owner administrators can grant only capabilities they currently hold with
`can_delegate`.

## Security ID catalogs and enforcement points

Capabilities use stable, validated dotted identifiers and are synchronized at startup:

- Every registered tool is `tool.<tool-name>`. Tool discovery hides unauthorized tools,
  and every invocation performs and records a fresh authorization decision before its
  handler runs.
- Multiplexed host tools add conjunctive action IDs. Screen/clipboard privacy and each
  `windows.native` action are checked again inside the handler, so granting the parent
  tool alone is insufficient.
- Every regular `/api/...` route has a generated
  `http.<method>.<normalized-route>` identifier, for example `http.post.api.chat`. Path
  parameters become `by_<parameter>`. Middleware resolves and authorizes the matched route
  before the handler; an unmapped API route is denied as `http.unmapped`.
- Security administration uses explicit `admin.*` identifiers on route dependencies.
- Telegram identity bootstrap uses `integration.telegram.session.create`.
- Core transport capabilities such as `events.subscribe` are checked at their own
  enforcement points.

Unknown IDs never imply access. Adding a route or tool requires catalog registration,
built-in default review, a policy-enforcement check before side effects, and tests for both
allow and deny paths. A low tool danger label is not evidence of tenant isolation; only the
small `TENANT_SAFE_TOOL_SECURITY_IDS` allowlist is seeded for ordinary users.

## Privileged account and material access

Canonical conversations, messages, memories, files, and file chunks remain tenant-bound.
Cross-user reads exist only through the explicit `accounts.overview` and
`materials.search/recent/read/summarize` tools. Every one has a persisted
`required_presets = [owner, admin]` floor. Direct grants and custom presets cannot bypass
that floor, and each handler rechecks the requester's current active database role before
reading another tenant.

The caller must select an exact internal `user_id`, an immutable
`provider + realm_id + provider_subject_id`, or explicitly request all active users. A
username is display metadata: an ambiguous username fails closed. `materials.recent` handles
query-free requests for the latest canonical messages from one exact Jarvis account in a
single read snapshot; an `@username` in that context is not a Telegram channel subscription.
Explicit channel/supergroup/feed requests remain in the separate `telegram.sources.*` corpus.
Results omit storage
paths and credentials, retain account/source/timestamp provenance, and use stable
`message:`, `memory:`, or `document:` citations. Model synthesis is accepted only when its
citations belong to the supplied evidence. After one failed correction, the invalid draft
remains hidden and the tool returns a bounded deterministic evidence digest with exact
citations; if even that contract cannot be satisfied, the tool fails closed.

`material_access_audit` stores the requester, action, result count, and SHA-256 digests of
the target scope and query. It does not store the raw query. Retrieved user material is
untrusted evidence and cannot grant capabilities or supply executable instructions.
Privileged tool runs persist metadata only. A live role check runs again after embeddings or
LLM synthesis and immediately before result delivery. Assistant turns derived from these
tools are marked privileged; demotion hides both those turns and derived compacted memories
from chat history, replay, and recall.
Non-owner/admin prompts receive a reduced account-aware system context, never host paths,
model endpoints, or the privileged tool catalog. Clear requests for other users' material
or Jarvis internals are denied deterministically before model execution.

## Telegram identity and sessions

The Telegram bridge accepts only messages where the sender is a real user, the chat is
private, and `chat.id == from.id`. The immutable numeric Telegram user ID—not username or
display name—is the identity key. The bridge derives a stable canonical realm
`telegram:<bot-id>` from the positive immutable bot id returned by Telegram `getMe`.
The backend independently validates that derivation, and the database permanently binds one
realm to one bot id (and vice versa). Optional `JARVIS_TELEGRAM_REALM_ID` and
`JARVIS_TELEGRAM_BOT_ID` values are standalone fail-closed assertions, not identity inputs.
Realm-less legacy history has no trustworthy bot provenance. After verifying the destination
bot with Telegram `getMe`, the operator must set `JARVIS_TELEGRAM_LEGACY_REALM_ID` to that
canonical realm explicitly. The Windows launcher never derives this migration authority from
the currently configured bot token and refuses to start the bridge while the mapping is absent.
If the old store already used a non-default realm, the operator must also set
`JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID` to that exact old realm. The bridge migrates all
realm-scoped conversation, inbox, replay-ledger, migration-marker, and Telegram-identity rows
in one transaction; it rejects a missing source, a source equal to the destination, and mixed
source/canonical state.

For every accepted update, the bridge calls:

`POST /api/integrations/telegram/session`

The backend atomically rejects mismatched `(realm_id, update_id)` replays and bounds exact
retry attempts, upserts the external identity and profile metadata, auto-creates an active
`guest` user when needed, and returns a short-lived random session. A one-time legacy
binding claim moves its conversation, messages, reminders, and learning observations to the
new IAM tenant in the same transaction. The legacy `access_mode` is only a conversation-cache
hint and never grants IAM authority; elevated rights must be restored explicitly by an admin.
Only the token digest is persisted. The
bridge sends the token in `X-Jarvis-User-Session` for the user's API operations. Suspended or
deleted users cannot obtain or use sessions.

Processing attempts use compare-and-swap lease tokens, so a stale retry cannot overwrite a
newer result or publish its session. The bridge persists accepted updates before advancing
Telegram's polling offset, isolates conversation bindings by bot realm, and retries the same
transport request ID idempotently after a crash. Ordinary HTTP/payload failures use 2/10-second
backoff and stop after three attempts. A transport failure, or a backend `503` explicitly marked
`X-Jarvis-Retry-Class: llm-outage`, uses bounded 2/10/30/60/300-second backoff without an
age/attempt tombstone. The earliest transient update remains durable and ordered ahead of later
updates from the same chat until it succeeds; stable request IDs make every retry idempotent.
An unmarked `5xx` never receives this extended retry contract. If Telegram or the backend
permanently rejects an attachment, its safe filename/type/size/failure provenance still enters
canonical chat ingress as a searchable delivery record. File contents are explicitly marked
unavailable and are never claimed as analyzed.

`TELEGRAM_ALLOWED_CHAT_IDS` is an optional deployment restriction; an empty value allows
every valid private Telegram user to register. `TELEGRAM_OWNER_CHAT_IDS` is compatibility
metadata and never grants backend authority. Roles must be assigned through IAM.

An active owner may issue a 30-minute one-time Telegram owner invitation from `/admin`.
The recipient sends `/start owner_<secret>` to the bot. A syntactically valid invitation may
cross the optional deployment allowlist exactly for this claim; after a successful claim the
numeric Telegram ID is persisted as admitted. The bridge replaces the raw secret with a
SHA-256 proof before durable inbox storage, the backend stores only a second hash, and the
identity upsert, invitation consumption, and `owner` assignment commit in one transaction.
Exact transport retries by the winning identity are idempotent; another identity cannot reuse
the invitation. Telegram usernames are display metadata and never participate in the grant.

Required transport controls:

- `JARVIS_TELEGRAM_BRIDGE_SECRET` must be at least 32 characters, shared only by backend
  and bridge, and different from both `TELEGRAM_BOT_TOKEN` and `JARVIS_API_TOKEN`.
- Non-loopback `JARVIS_BACKEND_URL` must use HTTPS. The insecure override is only for an
  isolated trusted container network.
- Session TTL is bounded to 300–86,400 seconds; use a short value such as 900 seconds.
- Persistent per-Telegram-user, global Telegram, and user-session API limits reject excess
  work with HTTP 429 before LLM or tool execution. API-token traffic is not covered
  by this per-user limiter and should also be bounded at the trusted reverse proxy. Tune the
  three documented `*_RATE_LIMIT_PER_MINUTE` values for the deployment capacity.
- The bridge uses a bounded global worker pool, one in-flight turn per Telegram user,
  small per-user queues, and its own intake budget. A slow or flooding account therefore
  cannot serialize every other user's turn or grow an unbounded in-memory backlog.
- Never identify or authorize a user by Telegram username, name, chat title, or forwarded
  message metadata.

### Telegram public-source feeds and voice delivery

Owner/admin accounts can use `telegram.sources.add/list/remove/sync/search/analyze` to keep
a separate tenant-scoped corpus of public channel posts. Registration uses the canonical
bot realm and immutable negative channel `chat.id`; a username is only a display/permalink
snapshot. The bot must already be present in the channel. `channel_post` and
`edited_channel_post` updates are normalized and committed before the polling offset is
acknowledged. Replays and edit versions are idempotent, and search uses Unicode
NFKC/casefold plus explicit/default RU/EN/ZH/KO/JA query variants.

The Bot API tier consumes new posts delivered to the bot. It cannot follow arbitrary
personal accounts, read private history, resolve username-only subscriptions reliably, or
backfill history. A separate `TelegramAuthorizedReader` adapter may import history and media
metadata from public/private channels and supergroups after operator-side account authorization;
Jarvis tools never accept that account's credentials. Pages are bounded to 500 posts, committed
separately, and continued through a durable `before_message_id` checkpoint, so a crash or
provider outage does not truncate a large backfill. The adapter is bound to one immutable hashed
reader identity. Production can connect an already authenticated external CLI through
`JARVIS_TELEGRAM_READER_COMMAND_JSON` (JSON argv, absolute executable) using protocol
`jarvis.telegram-reader.v1`; the child receives no Jarvis tokens and owns its session itself.
If no such authenticated reader exists, capability/sync reports `unconfigured` rather than
pretending that history was imported.
Personal-account monitoring remains forbidden on both tiers.

Telegram modality is a non-overridable `auto` contract for every authenticated user. Direct
text/captions receive text; direct voice/audio without a caption receives speech; forwarded
media remains source material answered in text. The legacy `voice_reply` preference is ignored
by Telegram routing, `/voice auto` is idempotent, and `/voice on|off` cannot persist a different
mode. Long replies are split into numbered TTS parts. Header-only/unplayable WAV output is
rejected. If Telegram privacy forbids voice notes with `VOICE_MESSAGES_FORBIDDEN`, the bridge
transcodes the same speech to MP3 and retries through `sendAudio`; it never sends raw WAV. An
Opus/MP3 conversion, TTS, or delivery failure is logged without answer text or secrets and
falls back to the complete text with an explicit notice.

## Administration API and UI

The implemented administration endpoints are:

| Method and path | Capability | Function |
| --- | --- | --- |
| `GET /api/admin/users` | `admin.users.list` | Paginated registered-user/identity list (`limit`, `offset`). |
| `GET /api/admin/users/{user_id}/permissions` | `admin.users.permissions.list` | Effective decision for every registered security ID. |
| `PATCH /api/admin/users/{user_id}/status` | `admin.users.status.update` | Activate, suspend, or soft-delete a user. |
| `POST /api/admin/users` | `admin.users.create` | Create a local account or pre-provision a Telegram identity. |
| `POST /api/admin/telegram-owner-invitations` | `admin.users.owner.invite` | Issue an expiring, single-use Telegram owner invitation (active owner only). |
| `GET /api/admin/telegram/chats` | `admin.telegram.messages.read` | Search and paginate registered private Telegram chats across tenants (active owner only). |
| `GET /api/admin/telegram/chats/{realm_id}/{chat_id}/messages` | `admin.telegram.messages.read` | Read the cursor-paginated delivered transport timeline (active owner only). |
| `POST /api/admin/telegram/chats/{realm_id}/{chat_id}/messages` | `admin.telegram.messages.send` | Send one audited, idempotent literal Bot API message (active owner only). |
| `DELETE /api/admin/users/{user_id}` | `admin.users.delete` | Permanently delete a non-owner account, external identities, sessions, IAM assignments, and tenant-owned data. |
| `PUT /api/admin/users/{user_id}/preset` | `admin.users.preset.assign` | Replace the active preset assignment. |
| `PUT /api/admin/users/{user_id}/permissions/{security_id}` | `admin.users.permission.set` | Set a direct grant or deny. |
| `DELETE /api/admin/users/{user_id}/permissions/{security_id}` | `admin.users.permission.revoke` | Revoke a direct override. |
| `GET /api/admin/security-ids` | `admin.security_ids.list` | List the capability catalog. |
| `GET /api/admin/audit` | `admin.audit.list` | Paginated administrative security audit. |
| `GET /api/admin/presets` | `admin.presets.list` | List active preset versions and their IDs. |
| `POST /api/admin/presets` | `admin.presets.create` | Create and publish a custom preset. |
| `PUT /api/admin/presets/{preset_key}` | `admin.presets.update` | Publish a new version of a custom preset. |

`/admin` provides loaded-user search, status and preset assignment, effective-permission inspection,
direct grant/deny/inherit controls, security-ID filtering, and custom-preset creation.
`/admin/telegram` inherits the same signed owner session. The browser receives neither
`TELEGRAM_BOT_TOKEN` nor the backend API token. Delivery is server-side, bot-realm-pinned,
audited without message bodies, and fenced against duplicate retries after ambiguous network
results or process crashes. The timeline uses an append/update transport journal so commands,
edited inbound messages, multipart text, voice/audio, documents/photos, reminders, alerts, and
operator-authored messages reflect what Telegram actually carried rather than raw model turns.
Setting the status to `deleted` is an access block: the account and immutable external
identity remain, active sessions are revoked, and a later Telegram message cannot silently
register around the block. The separate permanent-delete action is owner-protected and
transactionally removes the account plus its tenant rows, Telegram binding/inbox, search
indexes, and runtime preferences, then cleans the managed per-user files, memory vault, and
learned execution playbooks. The response reports whether every post-commit artifact cleanup
completed. Security audit rows remain with their user foreign key cleared. If the same Telegram
sender is still admitted by the deployment allowlist (or the bot accepts automatic
registration), a later message creates a new least-privilege account without the deleted
history.
`/login` compares the legacy-user credential to `JARVIS_API_TOKEN` with a timing-safe digest,
rate-limits attempts, and creates an eight-hour signed HttpOnly, SameSite=Strict cookie.
The server-side `/jarvis-api` proxy injects the API token only after validating that
cookie; the backend then reloads the user's current IAM role. The proxy rejects cross-site
mutations. `JARVIS_UI_SESSION_SECRET` can provide additional
cookie-signing material; changing either signing input revokes existing UI sessions.

## Interaction with HITL, verification, and rollback

IAM permission is necessary but never sufficient for a risky action:

- HTTP/admin route capabilities marked `default_requires_hitl` create a user-owned approval
  and return HTTP 428 before the handler runs. After operator approval, the exact request is
  retried with `X-Jarvis-Approval-Id`.
- Route approvals expire after ten minutes and are bound to the method, path/query, body
  hash, security ID, and current policy epoch. IAM approvals also bind the target user's
  optimistic row/policy version or the target preset version. They are atomically claimed
  once and finalized as executed or failed.
- Tool danger gates and per-turn operator authorization continue to run after the tool
  permission check. Possessing a security ID does not set `approved=True` or bypass the
  SafeGate/execution capability policy.
- The approval executor maps the approved action back to its tool security ID and rechecks
  the current actor's permission before claiming or executing it. Unknown mappings fail
  closed.
- Interrupted approval executions are reconciled on startup without replaying the original
  side effect.
- Typed reversible filesystem/registry transactions create durable checkpoints, verify
  state by readback, and roll back on action failure, failed verification, exception, or
  cancellation. A rollback-degraded latch blocks further mutations until recovery.

HITL is not a permission grant, verification is not authentication, and rollback is not
universal. External API calls and other irreversible side effects may not be mechanically
recoverable; least privilege, request-bound approval, idempotency, and audit remain required.

## Migration and rollback notes

Startup migration is idempotent and primarily additive:

1. Create IAM tables and the deterministic legacy-owner user.
2. Create the five built-in preset/version rows and assign `owner` to the legacy user.
3. Add `user_id` to existing personal tables, backfill all pre-migration rows to the legacy
   owner, add tenant indexes, and install triggers that reject empty or unknown users on
   legacy table layouts. Fresh databases use `NOT NULL` foreign keys directly.
4. Rebuild legacy execution playbooks once to add `user_id`, assigning old rows to the
   legacy owner.
5. Synchronize the core, admin, HTTP-route, and tool capability catalogs.

Take a consistent backup of the SQLite database, WAL/SHM state, memory vault, files, and
execution-playbook database before deployment. Do not downgrade an active multi-user data
directory to an old binary: old code does not understand tenant predicates and can mix or
expose data. Rollback should restore the coordinated pre-migration backup, or use a reviewed
forward fix that preserves `user_id`; there is no automatic destructive down-migration.
User deletion is a status transition, not immediate physical erasure.

## Operational requirements and limits

- Set a random `JARVIS_API_TOKEN` of at least 32 characters. Token authentication on
  loopback is enabled by default; disabling it is a legacy development mode only.
- The Windows launcher generates this token automatically. Manual `serve` and Compose
  deployments must set it explicitly; Compose fails closed at configuration time if absent.
- Keep `JARVIS_API_TOKEN`, `JARVIS_TELEGRAM_BRIDGE_SECRET`, `TELEGRAM_BOT_TOKEN`, and
  `JARVIS_UI_SESSION_SECRET` separate. Never expose them through `NEXT_PUBLIC_*`, URLs,
  logs, approval payloads, or model context.
- Restrict CORS to required origins, use HTTPS outside loopback, protect data-directory
  permissions, rotate credentials after suspected disclosure, and restart affected
  processes after rotation.
- Monitor denied authorization decisions, security-audit changes, repeated Telegram replay
  conflicts, throttling, exhausted update leases, approval failures, and rollback state.
- Expired sessions, Telegram replay rows, high-volume authorization decisions, and rate
  windows are pruned periodically. The durable security administration audit is retained.
- Per-user memory-vault handles use a bounded LRU cache; evicting a handle never removes
  tenant data from disk.
- Keep one active owner recovery path and test suspension, session revocation, backup, and
  restore procedures before onboarding users.

There is no application-level user-count limit, but this release uses a single SQLite data
plane, a process-local lock, and a primary-runtime lease. It is suitable for one Jarvis
runtime with paginated users; it does **not** provide horizontally scaled multi-replica
storage. Horizontal deployment requires a deliberate migration to shared transactional
storage plus distributed session/event/locking infrastructure.
