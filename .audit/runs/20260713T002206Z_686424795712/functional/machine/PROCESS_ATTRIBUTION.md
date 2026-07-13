# Process Attribution and Runtime Ownership

## Evidence boundary

This attribution uses two read-only observations:

1. The external checkpoint at `D:\jarvis\audit-backups\20260713T002206Z_686424795712\pre-functional-resume\checkpoint-20260713T160611Z`.
2. Live PID, PPID, command-line, creation-time, listening-port, launcher-state, Docker, and WSL observations taken immediately before and during the new functional cold start.

Historical live queues and findings were not used. Name-only, age-only, and working-directory guesses are insufficient to assign audit ownership.

## Ownership model

- **Campaign-owned process:** the command line names the current functional namespace or the process is a directly observed descendant of a campaign start command, with compatible creation time and role.
- **Campaign-owned activation:** the campaign demonstrably started or resumed a resource, but the underlying resource existed before the campaign. Cleanup may reverse only the campaign activation after checking for another active owner.
- **External/shared:** host infrastructure, Docker Desktop, WSL services, Codex workers, and unrelated user processes. The campaign does not stop them.
- **Unknown:** evidence is insufficient. Unknown is treated as external for cleanup.
- **Exited/stale:** the recorded PID no longer exists. Stale metadata is not a live ownership claim.

Campaign cleanup must use exact recorded identifiers and verify PID creation time or container identity immediately before acting. It must never terminate a process solely because its name contains `python`, `node`, `docker`, `jarvis`, or `audit`.

## Initial pre-campaign attribution

The external checkpoint's broad candidate filter recorded the checkpoint writer itself (PID `552`), three Codex `node_repl.exe` workers (PIDs `1900`, `5784`, and `6848`), and `wslservice.exe` PID `5376`. None is evidence of a surviving old audit runtime:

| Resource | Evidence | Classification | Cleanup rule |
| --- | --- | --- | --- |
| PID `552` | Command line is the checkpoint construction script itself. | Checkpoint-owned, transient; not an old campaign runtime. | Do not use as old-audit evidence. |
| PIDs `1900`, `5784`, `6848` | Generic Codex `node_repl.exe` workers under Codex app-server PID `11332`. | External/shared Codex infrastructure. | Never stop for JARVIS cleanup. |
| PID `5376` | `wslservice.exe`, no audit-specific command line. | External/shared host infrastructure. | Never stop for campaign cleanup. |
| PID `9336` | Created `2026-07-13 17:59:29 +03:00`; parent Windows Terminal PID `27028`; command line is only `powershell.exe`. | Unknown/external. It may be an interactive shell, but old-audit ownership is unprovable. | Do not stop. |
| Launcher-state PIDs `10040`, `33252`, `13568` | Recorded in the 2026-07-12 launcher state; not live in the initial snapshot. | Exited/stale. | No process cleanup possible or required. |
| Primary-runtime lock PID `27260` | Recorded in the stale lock; not live. | Exited/stale. | Do not infer a live owner. |

The initial live query found no Python, Node, vLLM, or JARVIS runtime process and no non-collector process whose command line proved membership in an old `.audit` attempt. Therefore the number of proven surviving old-audit processes is **zero**.

## New functional campaign attribution

### Campaign root

PID `24744`, created `2026-07-13 19:10:43 +03:00`, is the only observed process whose command line explicitly names an audit namespace. It names the current namespace, not a historical one:

```text
.audit/runs/20260713T002206Z_686424795712/functional/evidence/startup-gemma4-turbo-cold.log
.audit/runs/20260713T002206Z_686424795712/functional/evidence/startup-gemma4-turbo-cold.json
jarvis.cmd start -Profile gemma4-turbo
```

Classification: **campaign-owned process**.

### Descendant runtime tree

The transient common launcher parent PID `27860` had already exited when ancestry was inspected. Its descendants and their lineage were directly observed:

| Role | Wrapper lineage | Leaf PID and command | Port | Classification |
| --- | --- | --- | --- | --- |
| Windows RPC bridge | `30156 py.exe <- 27860` | `29348 python.exe ... scripts\windows_rpc_bridge.py --host 127.0.0.1 --port 8765 ...` | `127.0.0.1:8765` | Campaign-owned process tree |
| Backend | `30232 py.exe <- 27860` | `30052 python.exe ... jarvis.py --profile gemma4-turbo serve --host 127.0.0.1 --port 8000` | `127.0.0.1:8000` | Campaign-owned process tree |
| Frontend | `26912 node.exe <- 27860` via `cmd.exe`/npm | `30512 node.exe ... next ... start --hostname 127.0.0.1` | `127.0.0.1:3000` | Campaign-owned process tree |

All three leaf processes were created between `19:11:15` and `19:11:18 +03:00`, after the campaign root and after the initial no-runtime snapshot.

### Dispatcher and infrastructure

| Resource | Evidence | Classification | Cleanup rule |
| --- | --- | --- | --- |
| Container `5fb6c625fbbb...` (`jarvis-gpt-dispatcher`) | Created `2026-07-12T19:57:46Z`; current start `2026-07-13T16:11:05Z`; same ID was present in stale launcher metadata. | Historical/pre-existing container with a campaign-owned activation. | Do not claim historical object ownership. Stop only as part of verified current launcher cleanup and only if no other owner is active. Do not remove it. |
| Docker Desktop process/engine | Appeared while another campaign worker executed the cold start; generic host infrastructure. | External/shared host infrastructure. | Do not terminate Docker Desktop or its services. |
| `docker-desktop` WSL distribution | Changed from `Stopped` to `Running` during the cold start. | External/shared host infrastructure with campaign-triggered use. | Do not terminate WSL globally; use only product-supported scoped cleanup. |
| `jarvis-gpt_default` network | Docker Compose network used by dispatcher. | Shared runtime resource. | Do not delete during functional cleanup. |
| `jarvis3_default` network | Present after Docker engine startup, with no current-campaign lineage evidence. | External/unknown historical resource. | Do not delete. |

## Current ownership conclusion

- Proven surviving processes from a prior audit attempt: **0**.
- Proven current functional audit root: PID `24744` in namespace `20260713T002206Z_686424795712/functional`.
- Proven current campaign runtime descendants: bridge, backend, and frontend trees listed above.
- Dispatcher ownership is limited to the current activation; the container object predates this campaign.
- PID `9336`, Codex workers, Docker Desktop, WSL services, and unrelated listeners remain external or unknown and are outside campaign cleanup authority.

This classification must be refreshed before any later stop/cleanup because PIDs are reusable and the recorded observations are time-bounded.
