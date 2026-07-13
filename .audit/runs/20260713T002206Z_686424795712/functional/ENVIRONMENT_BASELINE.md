# Functional Environment Baseline

## Scope and isolation

- Functional run: `20260713T002206Z_686424795712`.
- Repository: `D:\jarvis-gpt`; runtime/data root: `D:\jarvis`.
- Source start point: branch `main`, HEAD `3fda655e4f723a0d8f58a4edfb4b3ee7dda079fe`.
- External pre-resume checkpoint: `D:\jarvis\audit-backups\20260713T002206Z_686424795712\pre-functional-resume\checkpoint-20260713T160611Z`.
- The authoritative checkpoint manifest is `run-copy-manifest-sha256-v2.csv`. The original three-byte `run-copy-manifest-sha256.csv` is intentionally preserved.
- Historical live queues, findings indexes, and extended PHASE B/C prompts were not used for this baseline.

This document separates the initial pre-campaign observation from the subsequent functional start. The baseline collector did not start, stop, or reconfigure any process, service, container, WSL distribution, or network.

## Initial pre-campaign state

The first read-only observation completed before the new campaign launcher process was created at `2026-07-13 19:10:43 +03:00`.

### Host

| Item | Observed value |
| --- | --- |
| Host / user | `DESKTOP-ESPSBSE` / `Admin` |
| OS | Windows 11 Pro, `10.0.26200`, build `26200`, x64 |
| Last boot | `2026-07-13 17:14:56 +03:00` |
| PowerShell | Windows PowerShell `5.1.26100.8655`, Desktop, x64 |
| CPU | Intel Core Ultra 9 285K, 24 cores / 24 logical processors |
| CPU load snapshot | 62% |
| RAM | 127.27 GiB visible; 113.41 GiB free; 4 x 32 GiB modules at 4400 MT/s |
| GPU | NVIDIA GeForce RTX 5090, driver `610.47`, 32607 MiB VRAM |
| GPU before dispatcher start | 1144 MiB used, 31043 MiB free, 11% utilization, 57 C |
| Disk `C:` | 299.35 GiB total; 110.83 GiB free |
| Disk `D:` | 1562.72 GiB total; 691.10 GiB free |

The WMI `AdapterRAM` value for the RTX 5090 was truncated; the VRAM figure above comes from `nvidia-smi`.

### Toolchain

| Tool | Observed value |
| --- | --- |
| Global Python | `3.14.5`; launcher also exposes Python `3.11` and Astral CPython `3.14.4` |
| Project virtual environment | `D:\jarvis-gpt\.venv\Scripts\python.exe`, Python `3.14.4` |
| Node.js | `v24.16.0` |
| npm | `11.13.0` |
| uv | `0.11.23` |
| Git | `2.54.0.windows.1` |
| Docker client | `29.6.1`, API `1.55`, context `desktop-linux` |
| Docker Compose | `v5.3.0` |
| WSL | `2.7.8.0`; kernel `6.18.33.1-1`; default version 2 |

### Runtime and model paths

| Path | Observation |
| --- | --- |
| `D:\jarvis` | Exists; configured host runtime root |
| `D:\jarvis\cache\jarvis-gpt` | 335 files, 31.91 MiB; latest write before campaign `2026-07-12 22:58:27 +03:00` |
| `D:\jarvis\data\jarvis-gpt` | 1669 files, 60.92 MiB |
| `D:\jarvis\logs` | 20 files, approximately 0.05 MiB |
| `D:\jarvis\data\jarvis-gpt\state` | SQLite state, launcher state, primary-runtime lock, and execution checkpoints present |
| `D:\jarvis\data\models\gemma4-26b-a4b-nvfp4` | 12 files, 17.53 GiB |
| `D:\jarvis\data\models\gemma4-31b-it-nvfp4` | 15 files, 30.42 GiB |
| `D:\jarvis\data\models\ui-tars` | 15 files, 9.11 GiB |
| `D:\jarvis\data\models\ui-tars-1.5-7b-awq` | 15 files, 6.46 GiB |

### Network, Docker, and health

- Active host IPv4: `10.194.175.68`; gateway and DNS: `10.194.175.232`.
- Hyper-V Default Switch host address: `172.30.96.1`.
- The initial socket inventory contained 41 listening rows. Ports `8765`, `3000`, `8000`, and `8001` were not listening.
- Notable unrelated listeners included `xray.exe` on `127.0.0.1:10808` and `127.0.0.1:10809`.
- `Ubuntu-24.04` and `docker-desktop` were both `Stopped`.
- `com.docker.service` was `Stopped`; `WSLService`, `hns`, and `vmcompute` were running.
- No Python, Node, vLLM, or JARVIS runtime process was present in the initial process snapshot.
- Docker containers and networks could not be enumerated because the Linux engine pipe did not exist. This is an unavailable inventory, not evidence of zero containers.
- Direct probes of `http://127.0.0.1:3000/`, `http://127.0.0.1:8000/health`, and `http://127.0.0.1:8001/health` returned curl exit 28 after approximately two seconds.

Exact initial Docker error:

```text
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

Exact initial HTTP error class:

```text
curl: (28) Connection timed out after 2007 milliseconds
```

### Stale launcher metadata

Before the new start, `launcher-state.json` described a launch at `2026-07-12T22:57:50.7747765+03:00` with profile `gemma4-turbo`. Its recorded backend PID `10040`, frontend PID `33252`, bridge PID `13568`, and primary-runtime lock PID `27260` were not live. This establishes stale metadata only; it does not establish an old audit-owned process.

## Parallel new campaign start

While the read-only baseline was being recorded, another campaign worker started the new cold-start scenario. The transition is retained rather than folded into the initial state.

| Time (+03:00) | New evidence |
| --- | --- |
| `19:10:43` | New campaign PowerShell PID `24744` created; its command line names this functional namespace and `startup-gemma4-turbo-cold.*` evidence files. |
| `19:11:15` | Windows RPC bridge leaf PID `29348` created and bound `127.0.0.1:8765`. |
| `19:11:17` | Backend leaf PID `30052` created and bound `127.0.0.1:8000`. |
| `19:11:18` | Frontend leaf PID `30512` created and bound `127.0.0.1:3000`. |
| `19:11:05` | Docker dispatcher start timestamp, reported in UTC as `2026-07-13T16:11:05.893467407Z`. |

Observed start commands:

```text
py.exe -3.11 D:\jarvis-gpt\scripts\windows_rpc_bridge.py --host 127.0.0.1 --port 8765 --token-file D:\jarvis\.jarvis\bridge.token
py.exe -3.11 D:\jarvis-gpt\jarvis.py --profile gemma4-turbo serve --host 127.0.0.1 --port 8000
npm.cmd run start -- --hostname 127.0.0.1
```

Early post-start health:

- Frontend `http://127.0.0.1:3000/`: HTTP 200.
- Backend `http://127.0.0.1:8000/health`: HTTP 200 with `{"ok":true,"profile":"gemma4-turbo","home":"D:\\jarvis"}`.
- Dispatcher container `5fb6c625fbbb6b7bfc291bcc1d460ec972610bbe1f93ab3fa9a12295c2204154`: running, health `starting`, port publication `127.0.0.1:8001->8001/tcp`.
- Dispatcher `/health` was not yet accepting connections; container health logs showed `ConnectionRefusedError: [Errno 111] Connection refused`, and the host curl probe returned exit 56 with `Recv failure: Connection was reset`.
- GPU usage during model loading rose to 20243 MiB, leaving 11944 MiB free.
- Docker networks after the engine became available: `bridge`, `host`, `jarvis3_default`, `jarvis-gpt_default`, and `none`.

The dispatcher object predates this campaign: it was created at `2026-07-12T19:57:46.702797309Z`. The current campaign owns the observed activation attempt, not the historical creation of that container.

## Ownership boundary

The detailed classification is in `machine/PROCESS_ATTRIBUTION.md`. In summary, the campaign claims only process trees and lifecycle actions whose start command, namespace, creation time, and lineage are directly observed. Pre-existing, ambiguous, host-infrastructure, and Codex worker processes remain externally owned and must not be stopped by campaign cleanup.

## Reproduction commands

```powershell
Get-CimInstance Win32_OperatingSystem
Get-CimInstance Win32_Processor
Get-CimInstance Win32_PhysicalMemory
Get-CimInstance Win32_Process
Get-NetTCPConnection -State Listen
Get-NetIPConfiguration -All
docker version
docker ps -a --no-trunc
docker network ls
docker inspect jarvis-gpt-dispatcher
wsl.exe --list --verbose
nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader
curl.exe --noproxy '*' --connect-timeout 2 --max-time 5 http://127.0.0.1:8000/health
```
