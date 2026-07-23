from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from .config import JarvisSettings
from .model_catalog import ModelCatalog
from .storage import JarvisStorage

DISPATCHER_SERVICE = "dispatcher"
DISPATCHER_CONTAINER = "jarvis-gpt-dispatcher"
DISPATCHER_OPERATION_NONCE_ENV = "JARVIS_DISPATCHER_OPERATION_NONCE"
DISPATCHER_OPERATION_NONCE_LABEL = "com.jarvis-gpt.dispatcher.operation-nonce"
DISPATCHER_OWNERSHIP_JOURNAL = "dispatcher-ownership-journal.json"
DISPATCHER_OPERATION_LOCK_TIMEOUT_SECONDS = 5.0
LAUNCHER_STATE_LOCK_TIMEOUT_SECONDS = 5.0
_COMPOSE_PROCESS_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "USERNAME",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "COMPUTERNAME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CLI_PLUGIN_EXTRA_DIRS",
    "JARVIS_API_TOKEN",
    "NVIDIA_VISIBLE_DEVICES",
    "NVIDIA_DRIVER_CAPABILITIES",
    "CUDA_VISIBLE_DEVICES",
    "CUDA_HOME",
    "NCCL_BN_DISABLE",
    "TEMP",
    "TMP",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
}
_FULL_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_NONCE = re.compile(r"^[0-9a-f]{32}$")
RUNTIME_COMPATIBILITY_FIELDS = (
    "model_path",
    "model_id",
    "served_model_name",
    "dtype",
    "enforce_eager",
    "max_model_len",
    "gpu_memory_utilization",
    "kv_cache_dtype",
    "max_num_seqs",
    "cpu_offload_gb",
    "kv_offloading_gb",
    "kv_offloading_backend",
    "tokenizer_mode",
    "safetensors_load_strategy",
    "prefix_caching",
    "language_model_only",
    "skip_mm_profiling",
    "mm_processor_cache_gb",
    "max_num_batched_tokens",
    "host",
    "port",
)
LOCAL_DERIVED_IMAGE_BUILDS = {
    "jarvis/vllm-openai:v0.25.1-asyncio-e4f88a8": {
        "base_image": (
            "vllm/vllm-openai:v0.25.1@sha256:"
            "e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089"
        ),
        "dockerfile": "docker/vllm-asyncio/Dockerfile",
    }
}


@contextmanager
def _launcher_state_lock(lock_path: Path, *, timeout_seconds: float) -> Iterator[None]:
    """Serialize launcher-state writers across the launcher and the backend process."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        while True:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("launcher state lock acquisition timed out") from exc
                time.sleep(0.05)
        yield
    finally:
        if locked:
            with suppress(OSError):
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _dispatcher_operation_lock(lock_path: Path, *, timeout_seconds: float) -> Iterator[None]:
    """Serialize every dispatcher mutation across launcher/backend processes."""

    with _launcher_state_lock(lock_path, timeout_seconds=timeout_seconds):
        yield


def _full_container_id(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if _FULL_CONTAINER_ID.fullmatch(normalized) else ""


def _operation_nonce(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if _OPERATION_NONCE.fullmatch(normalized) else ""


class DispatcherManager:
    def __init__(
        self,
        settings: JarvisSettings,
        repo_root: Path | None = None,
        storage: JarvisStorage | None = None,
    ) -> None:
        self.settings = settings
        self.repo_root = repo_root or Path.cwd()
        self.storage = storage

    def status(self) -> dict[str, Any]:
        catalog = ModelCatalog(self.settings, self.storage).response()
        docker = shutil.which("docker")
        container = self._container_status(docker)
        desired_env = self.compose_env()
        desired_runtime = _runtime_from_env(desired_env)
        runtime = _runtime_from_container(container)
        runtime_mismatches = _runtime_mismatches(runtime, desired_runtime)
        actual_image = str(container.get("image") or "") if isinstance(container, dict) else ""
        actual_image_id = (
            str(container.get("image_id") or "") if isinstance(container, dict) else ""
        )
        desired_image = desired_env["JARVIS_VLLM_IMAGE"]
        desired_image_inspect = (
            self._inspect_local_image(docker, desired_image)
            if docker and container and container.get("exists")
            else {"ok": False, "image_id": ""}
        )
        desired_image_id = str(desired_image_inspect.get("image_id") or "")
        if container and container.get("exists") and actual_image != desired_image:
            runtime_mismatches["image"] = {
                "actual": actual_image,
                "desired": desired_image,
            }
        elif container and container.get("exists") and (
            not actual_image_id
            or not desired_image_id
            or actual_image_id != desired_image_id
        ):
            runtime_mismatches["image_id"] = {
                "actual": actual_image_id or "unavailable",
                "desired": desired_image_id or "unavailable",
            }
        active_model = _model_for_runtime(catalog, runtime) or catalog["active_model"]
        return {
            "service": DISPATCHER_SERVICE,
            "container": DISPATCHER_CONTAINER,
            "docker_available": docker is not None,
            "docker_path": docker,
            "port": 8001,
            "port_open": _port_open("127.0.0.1", 8001, timeout=0.5),
            "base_url": self.settings.llm_base_url,
            "model": self.settings.llm_model,
            "active_model": active_model,
            "desired_model": catalog["active_model"],
            "runtime": runtime,
            "desired_runtime": desired_runtime,
            "actual_image": actual_image,
            "actual_image_id": actual_image_id,
            "desired_image": desired_image,
            "desired_image_id": desired_image_id,
            "runtime_matches_desired": runtime is not None and not runtime_mismatches,
            "runtime_mismatches": runtime_mismatches,
            "compose": self.compose_command("up"),
            "container_status": container,
            "env": _public_env(desired_env),
        }

    def compose_env(self) -> dict[str, str]:
        dispatcher = ModelCatalog(self.settings, self.storage).dispatcher_config()
        env = {
            "JARVIS_HOST_HOME": str(self.settings.home),
            "JARVIS_MODEL_ROOT": str(self.settings.model_root),
            "JARVIS_VLLM_IMAGE": os.environ.get(
                "JARVIS_VLLM_IMAGE",
                self.settings.profile.vllm_image,
            ),
            "VLLM_USE_V2_MODEL_RUNNER": os.environ.get("VLLM_USE_V2_MODEL_RUNNER", "0"),
            "VLLM_WEIGHT_OFFLOADING_DISABLE_UVA": os.environ.get(
                "VLLM_WEIGHT_OFFLOADING_DISABLE_UVA",
                "1",
            ),
            "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
            "CUDA_DEVICE_ORDER": os.environ.get("CUDA_DEVICE_ORDER", "PCI_BUS_ID"),
            "CUDA_DISABLE_P2P": os.environ.get("CUDA_DISABLE_P2P", "1"),
            "NCCL_P2P_DISABLE": os.environ.get("NCCL_P2P_DISABLE", "1"),
            "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
        }
        env.update({key: str(value) for key, value in dispatcher["env"].items()})
        return env

    def compose_command(self, action: str = "up") -> list[str]:
        base = ["docker", "compose", "--profile", "llm"]
        if action == "up":
            return [*base, "up", "-d", DISPATCHER_SERVICE]
        if action == "down":
            return ["docker", "rm", "-f", "<expected-full-container-id>"]
        if action == "logs":
            return [*base, "logs", "--tail", "80", DISPATCHER_SERVICE]
        return [*base, "ps", DISPATCHER_SERVICE]

    @property
    def _ownership_journal_path(self) -> Path:
        return self.settings.state_dir / DISPATCHER_OWNERSHIP_JOURNAL

    def _read_launcher_state_entry(self) -> dict[str, Any] | None:
        state_path = self.settings.state_dir / "launcher-state.json"
        if not state_path.exists():
            return None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        services = state.get("services") if isinstance(state, dict) else None
        entry = services.get("dispatcher") if isinstance(services, dict) else None
        return entry if isinstance(entry, dict) else None

    def _read_ownership_journal(self) -> dict[str, Any] | None:
        path = self._ownership_journal_path
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        phase = str(value.get("phase") or "")
        if phase not in {
            "intent",
            "candidate",
            "rollback-intent",
            "rollback-candidate",
            "stop-intent",
            "stopped",
        }:
            return None
        if not _operation_nonce(value.get("operation_nonce")):
            return None
        if not isinstance(value.get("launcher_owned"), bool) or not isinstance(
            value.get("requires_state_sync"), bool
        ):
            return None
        raw_container_id = str(value.get("container_id") or "")
        raw_previous_id = str(value.get("previous_container_id") or "")
        raw_state_expected_id = str(value.get("state_expected_container_id") or "")
        raw_state_expected_nonce = str(
            value.get("state_expected_operation_nonce") or ""
        )
        container_id = _full_container_id(raw_container_id)
        previous_id = _full_container_id(raw_previous_id)
        state_expected_id = _full_container_id(raw_state_expected_id)
        state_expected_nonce = _operation_nonce(raw_state_expected_nonce)
        if (raw_container_id and not container_id) or (raw_previous_id and not previous_id):
            return None
        if (raw_state_expected_id and not state_expected_id) or (
            raw_state_expected_nonce and not state_expected_nonce
        ):
            return None
        if value.get("requires_state_sync") and (
            not state_expected_id or not state_expected_nonce
        ):
            return None
        if phase in {"candidate", "rollback-candidate", "stop-intent"} and not container_id:
            return None
        if phase == "stopped" and not previous_id:
            return None
        return value

    def _atomic_write_json_locked(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _write_ownership_journal(self, journal: dict[str, Any]) -> dict[str, Any]:
        lock_path = self.settings.state_dir / "launcher-state.lock"
        try:
            with _launcher_state_lock(
                lock_path,
                timeout_seconds=LAUNCHER_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                self._atomic_write_json_locked(self._ownership_journal_path, journal)
        except (OSError, TimeoutError) as exc:
            return {
                "ok": False,
                "summary": f"Dispatcher ownership journal write failed: {exc.__class__.__name__}",
            }
        return {"ok": True, "journal": _redact_private_fields(journal)}

    def _clear_ownership_journal(
        self,
        *,
        expected_operation_nonce: str | None = None,
    ) -> dict[str, Any]:
        expected_nonce = (
            _operation_nonce(expected_operation_nonce)
            if expected_operation_nonce is not None
            else ""
        )
        lock_path = self.settings.state_dir / "launcher-state.lock"
        try:
            with _launcher_state_lock(
                lock_path,
                timeout_seconds=LAUNCHER_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                current = self._read_ownership_journal()
                if current is None:
                    return {"ok": True, "removed": False, "reason": "journal-absent"}
                current_nonce = _operation_nonce(current.get("operation_nonce"))
                if expected_nonce and current_nonce != expected_nonce:
                    return {
                        "ok": False,
                        "removed": False,
                        "reason": "journal-operation-cas-mismatch",
                    }
                self._ownership_journal_path.unlink(missing_ok=True)
        except (OSError, TimeoutError) as exc:
            return {
                "ok": False,
                "removed": False,
                "reason": f"journal-remove-failed:{exc.__class__.__name__}",
            }
        return {"ok": True, "removed": True, "reason": "journal-removed"}

    def _launcher_ownership_context(
        self,
        *,
        previous_container_id: str,
        previous_operation_nonce: str,
        explicit_launcher_intent: bool,
    ) -> dict[str, Any]:
        previous_id = _full_container_id(previous_container_id)
        previous_nonce = _operation_nonce(previous_operation_nonce)
        entry = self._read_launcher_state_entry()
        recorded_id = (
            _full_container_id(entry.get("container_id"))
            if isinstance(entry, dict) and entry.get("started_by_launcher") is True
            else ""
        )
        recorded_nonce = (
            _operation_nonce(entry.get("operation_nonce"))
            if isinstance(entry, dict) and entry.get("started_by_launcher") is True
            else ""
        )
        state_owns_previous = bool(
            previous_id
            and previous_nonce
            and recorded_id == previous_id
            and recorded_nonce == previous_nonce
        )

        pending = self._read_ownership_journal()
        pending_id = (
            _full_container_id(pending.get("container_id"))
            if isinstance(pending, dict) and pending.get("launcher_owned") is True
            else ""
        )
        pending_nonce = (
            _operation_nonce(pending.get("operation_nonce"))
            if isinstance(pending, dict) and pending.get("launcher_owned") is True
            else ""
        )
        pending_carries_ownership = bool(
            isinstance(pending, dict)
            and pending.get("launcher_owned") is True
            and previous_id
            and previous_nonce
            and pending_id == previous_id
            and pending_nonce == previous_nonce
        )
        launcher_owned = bool(
            explicit_launcher_intent or state_owns_previous or pending_carries_ownership
        )
        state_expected_id = recorded_id if state_owns_previous else ""
        return {
            "launcher_owned": launcher_owned,
            "requires_state_sync": bool(state_owns_previous),
            "state_expected_container_id": state_expected_id,
            "state_expected_operation_nonce": recorded_nonce if state_owns_previous else "",
            "explicit_launcher_intent": bool(explicit_launcher_intent),
            "source": (
                "launcher-intent"
                if explicit_launcher_intent
                else "launcher-state"
                if state_owns_previous
                else "pending-journal"
                if pending_carries_ownership
                else "unowned"
            ),
        }

    def _new_ownership_journal(
        self,
        *,
        phase: str,
        operation_nonce: str,
        previous_container_id: str,
        container_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "phase": phase,
            "profile": self.settings.profile.name,
            "launcher_owned": bool(context.get("launcher_owned")),
            "requires_state_sync": bool(context.get("requires_state_sync")),
            "state_expected_container_id": _full_container_id(
                context.get("state_expected_container_id")
            ),
            "state_expected_operation_nonce": _operation_nonce(
                context.get("state_expected_operation_nonce")
            ),
            "previous_container_id": _full_container_id(previous_container_id),
            "container_id": _full_container_id(container_id),
            "operation_nonce": _operation_nonce(operation_nonce),
            "ownership_source": str(context.get("source") or "unowned"),
            "written_at_unix_ns": time.time_ns(),
        }

    def _reconcile_ownership_journal_locked(self) -> dict[str, Any]:
        journal = self._read_ownership_journal()
        if journal is None:
            if self._ownership_journal_path.exists():
                return {
                    "ok": False,
                    "reconciled": False,
                    "reason": "ownership-journal-invalid",
                }
            return {"ok": True, "reconciled": False, "reason": "journal-absent"}
        docker = shutil.which("docker")
        current = self._container_status(docker)
        if not isinstance(current, dict) or current.get("ok") is not True:
            return {
                "ok": False,
                "reconciled": False,
                "reason": "docker-identity-unknown",
            }
        operation_nonce = _operation_nonce(journal.get("operation_nonce"))
        current_exists = current.get("exists") is True
        current_id = _full_container_id(current.get("id")) if current_exists else ""
        current_nonce = (
            _operation_nonce(current.get("operation_nonce")) if current_exists else ""
        )
        journal_id = _full_container_id(journal.get("container_id"))
        previous_id = _full_container_id(journal.get("previous_container_id"))
        phase = str(journal.get("phase") or "")
        if phase == "stop-intent":
            stopped_id = journal_id or previous_id
            if current_exists and current_id == stopped_id:
                if journal.get("launcher_owned") and not journal.get(
                    "requires_state_sync"
                ):
                    if operation_nonce and current_nonce == operation_nonce:
                        return {
                            "ok": True,
                            "reconciled": False,
                            "pending_launcher_stop": True,
                            "reason": "journal-only-stop-intent-retained",
                            "container_id": current_id,
                        }
                    return {
                        "ok": False,
                        "reconciled": False,
                        "reason": "journal-only-stop-provenance-mismatch",
                        "current_container_id": current_id,
                    }
                cleared = self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce or None
                )
                return {
                    "ok": bool(cleared.get("ok")),
                    "reconciled": bool(cleared.get("ok")),
                    "reason": "aborted-stop-intent-cleared",
                    "container_id": current_id,
                }
            if not current_exists:
                cleared = self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce or None
                )
                return {
                    "ok": bool(cleared.get("ok")),
                    "reconciled": bool(cleared.get("ok")),
                    "reason": "completed-stop-intent-cleared",
                }
            if journal.get("launcher_owned"):
                return {
                    "ok": False,
                    "reconciled": False,
                    "reason": "launcher-owned-stop-provenance-mismatch",
                    "current_container_id": current_id,
                }
            cleared = self._clear_ownership_journal(
                expected_operation_nonce=operation_nonce or None
            )
            return {
                "ok": bool(cleared.get("ok")),
                "reconciled": bool(cleared.get("ok")),
                "reason": "stale-unowned-stop-intent-cleared",
            }
        if phase == "stopped":
            # Successful launcher-owned stop left a tombstone so concurrent start
            # cannot reclaim provenance until absence (or foreign replacement) is clear.
            if not current_exists:
                cleared = self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce or None
                )
                return {
                    "ok": bool(cleared.get("ok")),
                    "reconciled": bool(cleared.get("ok")),
                    "reason": "completed-stop-tombstone-cleared",
                }
            if (
                previous_id
                and current_id == previous_id
                and operation_nonce
                and current_nonce == operation_nonce
            ):
                return {
                    "ok": True,
                    "reconciled": False,
                    "pending_launcher_stop": True,
                    "reason": "aborted-stop-tombstone-retained",
                    "container_id": current_id,
                }
            if journal.get("launcher_owned"):
                # Foreign container after a proven stop: clear tombstone so
                # mutations are not permanently blocked by stale stop provenance.
                cleared = self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce or None
                )
                return {
                    "ok": bool(cleared.get("ok")),
                    "reconciled": bool(cleared.get("ok")),
                    "reason": "stop-tombstone-superseded-cleared",
                    "current_container_id": current_id,
                }
            cleared = self._clear_ownership_journal(
                expected_operation_nonce=operation_nonce or None
            )
            return {
                "ok": bool(cleared.get("ok")),
                "reconciled": bool(cleared.get("ok")),
                "reason": "stale-unowned-stop-tombstone-cleared",
            }
        candidate_matches = bool(
            current_exists
            and operation_nonce
            and current_nonce == operation_nonce
            and (not journal_id or current_id == journal_id)
        )
        if candidate_matches:
            if not journal.get("launcher_owned"):
                cleared = self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce
                )
                return {
                    "ok": bool(cleared.get("ok")),
                    "reconciled": bool(cleared.get("ok")),
                    "reason": "unowned-candidate-journal-cleared",
                    "container_id": current_id,
                }
            if journal.get("requires_state_sync"):
                sync = self.sync_launcher_state_container_id(
                    expected_previous_id=str(
                        journal.get("state_expected_container_id") or ""
                    ),
                    expected_previous_operation_nonce=str(
                        journal.get("state_expected_operation_nonce") or ""
                    ),
                    expected_current_id=current_id,
                    operation_nonce=operation_nonce,
                )
                if not (
                    sync.get("ok")
                    and (sync.get("updated") or sync.get("reason") == "already-current")
                ):
                    return {
                        "ok": False,
                        "reconciled": False,
                        "reason": "launcher-state-sync-failed",
                        "launcher_state_sync": sync,
                    }
                cleared = self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce
                )
                return {
                    "ok": bool(cleared.get("ok")),
                    "reconciled": bool(cleared.get("ok")),
                    "reason": "launcher-state-synchronized",
                    "launcher_state_sync": sync,
                    "container_id": current_id,
                }
            return {
                "ok": True,
                "reconciled": False,
                "pending_launcher_commit": True,
                "reason": "launcher-owned-candidate-retained",
                "container_id": current_id,
                "operation_nonce": operation_nonce,
            }

        if not current_exists or (previous_id and current_id == previous_id):
            cleared = self._clear_ownership_journal(
                expected_operation_nonce=operation_nonce or None
            )
            return {
                "ok": bool(cleared.get("ok")),
                "reconciled": bool(cleared.get("ok")),
                "reason": "abandoned-operation-journal-cleared",
            }
        if journal.get("launcher_owned"):
            return {
                "ok": False,
                "reconciled": False,
                "reason": "launcher-owned-journal-provenance-mismatch",
                "current_container_id": current_id,
            }
        cleared = self._clear_ownership_journal(
            expected_operation_nonce=operation_nonce or None
        )
        return {
            "ok": bool(cleared.get("ok")),
            "reconciled": bool(cleared.get("ok")),
            "reason": "stale-unowned-journal-cleared",
        }

    def _commit_candidate_ownership(
        self,
        journal: dict[str, Any],
    ) -> dict[str, Any]:
        operation_nonce = _operation_nonce(journal.get("operation_nonce"))
        candidate_id = _full_container_id(journal.get("container_id"))
        if not operation_nonce or not candidate_id:
            return {"ok": False, "reason": "candidate-journal-proof-invalid"}
        if not journal.get("launcher_owned"):
            cleared = self._clear_ownership_journal(
                expected_operation_nonce=operation_nonce
            )
            return {
                "ok": bool(cleared.get("ok")),
                "updated": False,
                "reason": "unowned-operation-complete",
                "journal_cleanup": cleared,
            }
        if not journal.get("requires_state_sync"):
            return {
                "ok": True,
                "updated": False,
                "pending_launcher_commit": True,
                "reason": "launcher-journal-awaits-full-state",
            }
        sync = self.sync_launcher_state_container_id(
            expected_previous_id=str(journal.get("state_expected_container_id") or ""),
            expected_previous_operation_nonce=str(
                journal.get("state_expected_operation_nonce") or ""
            ),
            expected_current_id=candidate_id,
            operation_nonce=operation_nonce,
        )
        ok = bool(
            sync.get("ok")
            and (sync.get("updated") or sync.get("reason") == "already-current")
        )
        cleanup = (
            self._clear_ownership_journal(expected_operation_nonce=operation_nonce)
            if ok
            else None
        )
        return {
            **sync,
            "ok": bool(ok and cleanup and cleanup.get("ok")),
            "journal_cleanup": cleanup,
        }

    def run_compose(self, action: str) -> dict[str, Any]:
        if action in {"up", "down"}:
            return self.run_compose_verified(action)
        return self._run_compose_with_env(action, self.compose_env())

    def _run_compose_with_env(
        self,
        action: str,
        compose_env: dict[str, str],
    ) -> dict[str, Any]:
        if action == "down":
            return {
                "ok": False,
                "summary": "Dispatcher stop requires an exact immutable container ID.",
                "returncode": None,
                "command": [],
            }
        docker = shutil.which("docker")
        if docker is None:
            return {"ok": False, "summary": "Docker is not available in PATH.", "returncode": None}
        command = self.compose_command(action)
        command[0] = docker
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _COMPOSE_PROCESS_ENV_KEYS
        }
        env.update(compose_env)
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "summary": f"Dispatcher compose {action} failed: {exc}",
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
                "command": command,
            }
        return {
            "ok": result.returncode == 0,
            "summary": f"Dispatcher compose {action} exited with {result.returncode}.",
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "command": command,
        }

    def run_compose_verified(
        self,
        action: str,
        *,
        timeout_seconds: float | None = None,
        expected_container_id: str | None = None,
        expected_operation_nonce: str | None = None,
        ownership_intent: str | None = None,
    ) -> dict[str, Any]:
        """Mutate the dispatcher under one cross-process lock and immutable-ID CAS."""

        lock_path = self.settings.state_dir / "dispatcher-operation.lock"
        try:
            with _dispatcher_operation_lock(
                lock_path,
                timeout_seconds=DISPATCHER_OPERATION_LOCK_TIMEOUT_SECONDS,
            ):
                if action == "down":
                    reconciliation = self._reconcile_ownership_journal_locked()
                    if not reconciliation.get("ok"):
                        return {
                            "ok": False,
                            "summary": (
                                "Dispatcher ownership journal could not be reconciled safely."
                            ),
                            "returncode": None,
                            "ownership_reconciliation": reconciliation,
                        }
                    return self._stop_dispatcher_locked(
                        expected_container_id=expected_container_id,
                        expected_operation_nonce=expected_operation_nonce,
                        timeout_seconds=float(timeout_seconds or 15.0),
                    )
                if action != "up":
                    return {
                        "ok": False,
                        "summary": f"Unsupported dispatcher mutation: {action}.",
                        "returncode": None,
                    }
                return self._run_compose_verified_locked(
                    "up",
                    timeout_seconds=timeout_seconds,
                    explicit_launcher_intent=ownership_intent == "launcher",
                )
        except TimeoutError:
            return {
                "ok": False,
                "summary": "Dispatcher mutation lock is held by another process.",
                "returncode": None,
                "verification": {"ok": False, "skipped": True},
            }

    def restart_verified(
        self,
        expected_container_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Atomically replace one proven container; never restart by mutable name."""

        expected_id = _full_container_id(expected_container_id)
        if not expected_id:
            return {
                "ok": False,
                "summary": "Dispatcher restart requires a full 64-hex container ID.",
                "returncode": None,
                "verification": {"ok": False, "skipped": True},
            }
        lock_path = self.settings.state_dir / "dispatcher-operation.lock"
        try:
            with _dispatcher_operation_lock(
                lock_path,
                timeout_seconds=DISPATCHER_OPERATION_LOCK_TIMEOUT_SECONDS,
            ):
                return self._run_compose_verified_locked(
                    "up",
                    timeout_seconds=timeout_seconds,
                    force_replace_expected_id=expected_id,
                    explicit_launcher_intent=False,
                )
        except TimeoutError:
            return {
                "ok": False,
                "summary": "Dispatcher mutation lock is held by another process.",
                "returncode": None,
                "verification": {"ok": False, "skipped": True},
            }

    def _run_compose_verified_locked(
        self,
        action: str,
        *,
        timeout_seconds: float | None = None,
        force_replace_expected_id: str | None = None,
        explicit_launcher_intent: bool = False,
    ) -> dict[str, Any]:
        effective_timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else (
                self.settings.profile.readiness_deadline_sec
                if action == "up"
                else 15.0
            )
        )
        reconciliation = self._reconcile_ownership_journal_locked()
        if not reconciliation.get("ok"):
            return {
                "ok": False,
                "summary": "Dispatcher ownership journal could not be reconciled safely.",
                "returncode": None,
                "ownership_reconciliation": reconciliation,
                "verification": {"ok": False, "skipped": True},
            }

        replacement: dict[str, Any] | None = None
        operation_nonce = secrets.token_hex(16)
        intent_journal: dict[str, Any] | None = None
        if action == "up":
            initial_snapshot = self.status()
            initial_container = initial_snapshot.get("container_status")
            if (
                not isinstance(initial_container, dict)
                or initial_container.get("ok") is not True
            ):
                return {
                    "ok": False,
                    "summary": "Dispatcher mutation refused because Docker identity is unknown.",
                    "returncode": None,
                    "verification": {"ok": False, "skipped": True},
                }
            initial_id = (
                _full_container_id(initial_container.get("id"))
                if initial_container.get("exists") is True
                else ""
            )
            if initial_container.get("exists") is True and not initial_id:
                return {
                    "ok": False,
                    "summary": "Dispatcher mutation refused because its full ID is unknown.",
                    "returncode": None,
                    "verification": {"ok": False, "skipped": True},
                }
            initial_running = bool(
                initial_container.get("exists") is True
                and str(initial_container.get("status") or "")
                .casefold()
                .startswith("up")
            )
            initial_reusable = bool(
                not force_replace_expected_id
                and initial_running
                and initial_snapshot.get("runtime_matches_desired") is True
            )
            if not initial_reusable:
                ownership_context = self._launcher_ownership_context(
                    previous_container_id=initial_id,
                    previous_operation_nonce=str(
                        initial_container.get("operation_nonce") or ""
                    ),
                    explicit_launcher_intent=explicit_launcher_intent,
                )
                intent_journal = self._new_ownership_journal(
                    phase="intent",
                    operation_nonce=operation_nonce,
                    previous_container_id=initial_id,
                    container_id="",
                    context=ownership_context,
                )
                journal_write = self._write_ownership_journal(intent_journal)
                if not journal_write.get("ok"):
                    return {
                        "ok": False,
                        "summary": "Dispatcher mutation journal could not be written safely.",
                        "returncode": None,
                        "ownership_journal": journal_write,
                        "verification": {"ok": False, "skipped": True},
                    }
            replacement = (
                self._replace_mismatched_container(
                    force_expected_container_id=force_replace_expected_id
                )
                if force_replace_expected_id
                else self._replace_mismatched_container()
            )
            replacement["_ownership_context"] = (
                {
                    "launcher_owned": bool(intent_journal.get("launcher_owned")),
                    "requires_state_sync": bool(intent_journal.get("requires_state_sync")),
                    "state_expected_container_id": str(
                        intent_journal.get("state_expected_container_id") or ""
                    ),
                    "state_expected_operation_nonce": str(
                        intent_journal.get("state_expected_operation_nonce") or ""
                    ),
                    "explicit_launcher_intent": explicit_launcher_intent,
                    "source": str(intent_journal.get("ownership_source") or "unowned"),
                }
                if intent_journal is not None
                else self._launcher_ownership_context(
                    previous_container_id=initial_id,
                    previous_operation_nonce=str(
                        initial_container.get("operation_nonce") or ""
                    ),
                    explicit_launcher_intent=explicit_launcher_intent,
                )
            )
            replacement["_ownership_journal"] = intent_journal
            if replacement.get("required") and not replacement.get("ok"):
                rollback = (
                    self._rollback_replacement(
                        replacement,
                        timeout_seconds=effective_timeout,
                    )
                    if replacement.get("cutover_started")
                    else None
                )
                journal_cleanup = (
                    self._clear_ownership_journal(
                        expected_operation_nonce=operation_nonce
                    )
                    if not replacement.get("cutover_started") and intent_journal is not None
                    else None
                )
                rollback_image_cleanup = (
                    self._cleanup_rollback_image(replacement)
                    if not replacement.get("cutover_started")
                    else None
                )
                return {
                    "ok": False,
                    "summary": (
                        "Existing dispatcher cutover failed; previous runtime was restored."
                        if rollback and rollback.get("ok")
                        else "Existing dispatcher container could not be replaced safely."
                    ),
                    "returncode": replacement.get("returncode"),
                    "stdout": replacement.get("stdout", ""),
                    "stderr": replacement.get("stderr", ""),
                    "command": replacement.get("command", []),
                    "replacement": self._public_replacement(replacement),
                    "rollback": rollback,
                    "rollback_image_cleanup": rollback_image_cleanup,
                    "ownership_journal_cleanup": journal_cleanup,
                    "verification": {"ok": False, "skipped": True},
                }
            if replacement.get("reused"):
                journal_cleanup = (
                    self._clear_ownership_journal(
                        expected_operation_nonce=operation_nonce
                    )
                    if intent_journal is not None
                    else None
                )
                verification = self.verify_state(
                    running=True,
                    timeout_seconds=effective_timeout,
                )
                reusable_container = verification.get("container_status")
                reusable_id = (
                    _full_container_id(reusable_container.get("id"))
                    if isinstance(reusable_container, dict)
                    else ""
                )
                reusable_nonce = (
                    _operation_nonce(reusable_container.get("operation_nonce"))
                    if isinstance(reusable_container, dict)
                    else ""
                )
                reuse_ok = bool(
                    verification["ok"]
                    and (journal_cleanup is None or journal_cleanup.get("ok"))
                )
                return {
                    "ok": reuse_ok,
                    "summary": (
                        "Dispatcher already runs the exact desired runtime."
                        if reuse_ok
                        else "Reusable dispatcher failed independent state verification."
                    ),
                    "returncode": 0 if reuse_ok else None,
                    "stdout": "",
                    "stderr": "",
                    "command": [],
                    "container_id": reusable_id,
                    "operation_nonce": reusable_nonce,
                    "replacement": self._public_replacement(replacement),
                    "ownership_reconciliation": reconciliation,
                    "ownership_journal_cleanup": journal_cleanup,
                    "verification": verification,
                }
            if not replacement.get("required"):
                desired_image = str(self.compose_env().get("JARVIS_VLLM_IMAGE") or "")
                image_preflight = self._ensure_desired_image_available(
                    str(shutil.which("docker") or "docker"),
                    desired_image,
                )
                replacement["startup_image"] = image_preflight
                if not image_preflight.get("ok"):
                    journal_cleanup = (
                        self._clear_ownership_journal(
                            expected_operation_nonce=operation_nonce
                        )
                        if intent_journal is not None
                        else None
                    )
                    return {
                        "ok": False,
                        "summary": (
                            "Dispatcher image is unavailable; startup was not attempted."
                        ),
                        "returncode": image_preflight.get("returncode"),
                        "stdout": image_preflight.get("stdout", ""),
                        "stderr": image_preflight.get("stderr", ""),
                        "command": image_preflight.get("command", []),
                        "replacement": self._public_replacement(replacement),
                        "ownership_journal_cleanup": journal_cleanup,
                        "verification": {"ok": False, "skipped": True},
                    }
        operation_env = self.compose_env()
        operation_env[DISPATCHER_OPERATION_NONCE_ENV] = operation_nonce
        if replacement is not None:
            replacement["operation_nonce"] = operation_nonce
        result = self._run_compose_with_env(action, operation_env)
        candidate_id = (
            self._record_candidate_container_id(replacement, operation_nonce)
            if replacement is not None
            else ""
        )
        candidate_journal_write: dict[str, Any] | None = None
        if candidate_id and replacement is not None:
            context = replacement.get("_ownership_context")
            previous_id = _full_container_id(
                (replacement.get("preflight") or {}).get("previous_container_id")
                if isinstance(replacement.get("preflight"), dict)
                else ""
            )
            if not previous_id and intent_journal is not None:
                previous_id = _full_container_id(
                    intent_journal.get("previous_container_id")
                )
            candidate_journal = self._new_ownership_journal(
                phase="candidate",
                operation_nonce=operation_nonce,
                previous_container_id=previous_id,
                container_id=candidate_id,
                context=context if isinstance(context, dict) else {},
            )
            candidate_journal_write = self._write_ownership_journal(candidate_journal)
            replacement["_ownership_journal"] = candidate_journal
            if not candidate_journal_write.get("ok"):
                if replacement.get("cutover_started"):
                    rollback = self._rollback_replacement(
                        replacement,
                        timeout_seconds=effective_timeout,
                    )
                    candidate_cleanup = None
                else:
                    rollback = None
                    candidate_cleanup = self._remove_proven_candidate(replacement)
                return {
                    **result,
                    "ok": False,
                    "summary": "Dispatcher candidate journal could not be persisted safely.",
                    "replacement": self._public_replacement(replacement),
                    "ownership_journal": candidate_journal_write,
                    "rollback": rollback,
                    "candidate_cleanup": candidate_cleanup,
                    "verification": {"ok": False, "skipped": True},
                }
        if not result.get("ok"):
            rollback = (
                self._rollback_replacement(replacement, timeout_seconds=effective_timeout)
                if replacement and replacement.get("cutover_started")
                else None
            )
            candidate_cleanup = (
                self._remove_proven_candidate(replacement)
                if replacement and not replacement.get("cutover_started") and candidate_id
                else None
            )
            journal_cleanup = (
                self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce
                )
                if not replacement.get("cutover_started")
                and (not candidate_id or (candidate_cleanup and candidate_cleanup.get("ok")))
                else None
            )
            return {
                **result,
                "summary": (
                    f"{result.get('summary', 'Dispatcher compose failed')} "
                    "Previous runtime restored."
                    if rollback and rollback.get("ok")
                    else result.get("summary", "Dispatcher compose failed.")
                ),
                "replacement": self._public_replacement(replacement),
                "rollback": rollback,
                "candidate_cleanup": candidate_cleanup,
                "ownership_journal": candidate_journal_write,
                "ownership_journal_cleanup": journal_cleanup,
                "verification": {"ok": False, "skipped": True},
            }

        if not candidate_id:
            rollback = (
                self._rollback_replacement(replacement, timeout_seconds=effective_timeout)
                if replacement and replacement.get("cutover_started")
                else None
            )
            journal_cleanup = (
                self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce
                )
                if not replacement.get("cutover_started")
                else None
            )
            return {
                **result,
                "ok": False,
                "summary": (
                    "Dispatcher compose candidate failed immutable-ID/nonce provenance; "
                    "previous runtime restored."
                    if rollback and rollback.get("ok")
                    else "Dispatcher compose candidate failed immutable-ID/nonce provenance."
                ),
                "replacement": self._public_replacement(replacement),
                "rollback": rollback,
                "ownership_journal": candidate_journal_write,
                "ownership_journal_cleanup": journal_cleanup,
                "verification": {"ok": False, "skipped": True},
            }

        verification = self.verify_state(
            running=True,
            timeout_seconds=effective_timeout,
        )
        docker = shutil.which("docker")
        provenance_still_current = bool(
            docker
            and self._candidate_matches_current(
                docker,
                container_id=candidate_id,
                operation_nonce=operation_nonce,
            )
        )
        if verification.get("ok") and not provenance_still_current:
            verification = {
                **verification,
                "ok": False,
                "provenance_still_current": False,
            }
        ownership_commit: dict[str, Any] | None = None
        if verification.get("ok") and replacement is not None:
            journal = replacement.get("_ownership_journal")
            ownership_commit = (
                self._commit_candidate_ownership(journal)
                if isinstance(journal, dict)
                else {"ok": False, "reason": "candidate-journal-missing"}
            )
            if not ownership_commit.get("ok"):
                verification = {
                    **verification,
                    "ok": False,
                    "ownership_commit": ownership_commit,
                }
        rollback = None
        finalized = None
        candidate_cleanup = None
        if action == "up" and replacement and replacement.get("cutover_started"):
            if verification["ok"]:
                finalized = self._cleanup_rollback_image(replacement)
            else:
                rollback = self._rollback_replacement(
                    replacement,
                    timeout_seconds=effective_timeout,
                )
        elif replacement and not verification.get("ok"):
            candidate_cleanup = self._remove_proven_candidate(replacement)
            if candidate_cleanup.get("ok"):
                self._clear_ownership_journal(
                    expected_operation_nonce=operation_nonce
                )
        return {
            **result,
            "ok": bool(result.get("ok") and verification["ok"]),
            "container_id": candidate_id,
            "operation_nonce": operation_nonce,
            "provenance_still_current": provenance_still_current,
            "replacement": self._public_replacement(replacement),
            "summary": (
                f"Dispatcher compose {action} completed and independent state checks passed."
                if verification["ok"]
                else (
                    f"Dispatcher compose {action} verification failed; previous runtime restored."
                    if rollback and rollback.get("ok")
                    else f"Dispatcher compose {action} completed but state verification failed."
                )
            ),
            "verification": verification,
            "ownership_journal": candidate_journal_write,
            "ownership_commit": ownership_commit,
            "rollback": rollback,
            "candidate_cleanup": candidate_cleanup,
            "rollback_image_cleanup": finalized,
        }

    def _stop_dispatcher_locked(
        self,
        *,
        expected_container_id: str | None,
        expected_operation_nonce: str | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        docker = shutil.which("docker")
        if docker is None:
            return {
                "ok": False,
                "summary": "Docker is not available in PATH.",
                "returncode": None,
            }
        current = self._container_status(docker)
        if not isinstance(current, dict) or current.get("ok") is not True:
            return {
                "ok": False,
                "summary": "Dispatcher stop refused because Docker identity is unknown.",
                "returncode": None,
            }
        if current.get("exists") is False:
            return {
                "ok": True,
                "summary": "Dispatcher is already absent.",
                "returncode": 0,
                "command": [],
                "verification": {"ok": True, "container_absent": True},
            }
        current_id = _full_container_id(current.get("id"))
        current_nonce = _operation_nonce(current.get("operation_nonce"))
        expected_id = (
            _full_container_id(expected_container_id)
            if expected_container_id is not None
            else current_id
        )
        expected_nonce = (
            _operation_nonce(expected_operation_nonce)
            if expected_operation_nonce is not None
            else ""
        )
        # Ownership-proof stops must supply both full ID and nonce. Bare-id stops
        # (CLI "down current") still require the live container to carry a nonce so
        # provenance-less containers cannot be removed as if they were owned.
        if expected_operation_nonce is not None and not expected_nonce:
            return {
                "ok": False,
                "summary": (
                    "Dispatcher stop refused: ownership proof has an invalid operation nonce."
                ),
                "returncode": None,
                "expected_container_id": expected_id,
                "current_container_id": current_id,
                "command": [],
            }
        nonce_matches = (
            bool(current_nonce)
            and (
                not expected_nonce
                or expected_nonce == current_nonce
            )
        )
        if (
            not expected_id
            or not current_id
            or expected_id != current_id
            or not nonce_matches
        ):
            return {
                "ok": False,
                "summary": (
                    "Dispatcher stop refused: full immutable-ID/nonce CAS did not match."
                ),
                "returncode": None,
                "expected_container_id": expected_id,
                "current_container_id": current_id,
                "expected_operation_nonce": expected_nonce or None,
                "current_operation_nonce": current_nonce or None,
                "command": [],
            }
        ownership_context = self._launcher_ownership_context(
            previous_container_id=current_id,
            previous_operation_nonce=current_nonce,
            explicit_launcher_intent=False,
        )
        stop_nonce = current_nonce
        stop_journal = self._new_ownership_journal(
            phase="stop-intent",
            operation_nonce=stop_nonce,
            previous_container_id=current_id,
            container_id=current_id,
            context=ownership_context,
        )
        journal_write = self._write_ownership_journal(stop_journal)
        if not journal_write.get("ok"):
            return {
                "ok": False,
                "summary": "Dispatcher stop journal could not be written safely.",
                "returncode": None,
                "expected_container_id": expected_id,
                "ownership_journal": journal_write,
                "command": [],
            }
        command = [docker, "rm", "-f", expected_id]
        try:
            removed = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=max(1.0, min(float(timeout_seconds), 30.0)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "summary": f"Exact-ID dispatcher removal failed: {exc}",
                "returncode": None,
                "expected_container_id": expected_id,
                "ownership_journal": journal_write,
                "command": command,
            }
        after = self._container_status(docker)
        absent = bool(
            isinstance(after, dict)
            and after.get("ok") is True
            and after.get("exists") is False
        )
        ok = absent
        journal_result: dict[str, Any]
        if ok:
            if ownership_context.get("launcher_owned"):
                stopped_journal = self._new_ownership_journal(
                    phase="stopped",
                    operation_nonce=stop_nonce,
                    previous_container_id=current_id,
                    container_id="",
                    context=ownership_context,
                )
                journal_result = self._write_ownership_journal(stopped_journal)
                ok = bool(journal_result.get("ok"))
            else:
                journal_result = self._clear_ownership_journal(
                    expected_operation_nonce=stop_nonce
                )
                ok = bool(journal_result.get("ok"))
        else:
            journal_result = {
                "ok": True,
                "removed": False,
                "reason": "stop-unconfirmed-journal-retained",
            }
        return {
            "ok": ok,
            "summary": (
                "Dispatcher exact-ID removal completed and absence was verified."
                if ok
                else "Dispatcher exact-ID removal could not be verified safely."
            ),
            "returncode": removed.returncode,
            "stdout": removed.stdout.strip(),
            "stderr": removed.stderr.strip(),
            "expected_container_id": expected_id,
            "ownership_journal": journal_result,
            "command": command,
            "verification": {"ok": absent, "container_status": after},
        }

    def _replace_mismatched_container(
        self,
        *,
        force_expected_container_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.status()
        container = snapshot.get("container_status")
        if not isinstance(container, dict) or container.get("ok") is not True:
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "cutover_started": False,
                "stderr": "Dispatcher Docker identity is unknown; mutation refused.",
            }
        if container.get("exists") is not True:
            if force_expected_container_id:
                return {
                    "required": True,
                    "ok": False,
                    "removed": False,
                    "reused": False,
                    "cutover_started": False,
                    "stderr": "Expected dispatcher container is absent during restart CAS.",
                }
            return {
                "required": False,
                "ok": True,
                "removed": False,
                "reused": False,
            }
        container_running = str(container.get("status") or "").casefold().startswith("up")
        snapshot_container_id = _full_container_id(container.get("id"))
        if force_expected_container_id and snapshot_container_id != force_expected_container_id:
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "cutover_started": False,
                "stderr": (
                    "Dispatcher changed before restart preflight; immutable-ID CAS failed."
                ),
                "expected_container_id": force_expected_container_id,
                "current_container_id": snapshot_container_id,
            }
        if (
            not force_expected_container_id
            and container_running
            and snapshot.get("runtime_matches_desired") is True
        ):
            return {
                "required": False,
                "ok": True,
                "removed": False,
                "reused": True,
            }

        docker = shutil.which("docker")
        if docker is None:
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "returncode": None,
                "stderr": "Docker is not available in PATH.",
                "mismatches": snapshot.get("runtime_mismatches", {}),
            }
        preflight = self._cutover_preflight(snapshot, docker)
        if not preflight.get("ok"):
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "returncode": None,
                "stdout": "",
                "stderr": str(preflight.get("summary") or "Cutover preflight failed."),
                "command": [],
                "mismatches": snapshot.get("runtime_mismatches", {}),
                "preflight": preflight,
            }
        expected_container_id = _full_container_id(preflight.get("previous_container_id"))
        current_container = self._container_status(docker)
        current_container_id = (
            _full_container_id(current_container.get("id"))
            if isinstance(current_container, dict)
            else ""
        )
        if (
            not expected_container_id
            or (
                force_expected_container_id
                and expected_container_id != force_expected_container_id
            )
            or not isinstance(current_container, dict)
            or current_container.get("ok") is not True
            or current_container.get("exists") is not True
            or current_container_id != expected_container_id
        ):
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "returncode": None,
                "stdout": "",
                "stderr": (
                    "Dispatcher changed during cutover preflight; refusing to remove "
                    "a container without immutable-ID ownership proof."
                ),
                "command": [],
                "mismatches": snapshot.get("runtime_mismatches", {}),
                "preflight": preflight,
                "cutover_started": False,
                "expected_container_id": expected_container_id,
                "current_container_id": current_container_id,
            }
        command = [docker, "rm", "-f", expected_container_id]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc) or exc.__class__.__name__,
                "command": command,
                "mismatches": snapshot.get("runtime_mismatches", {}),
                "preflight": preflight,
                # Docker may have accepted the exact-ID removal before the client
                # timed out, so rollback remains necessary for this indeterminate case.
                "cutover_started": True,
            }
        except OSError as exc:
            # Process creation failed, so Docker never received the destructive
            # command. Preserve the proven-good container instead of invoking a
            # rollback that would remove it.
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc) or exc.__class__.__name__,
                "command": command,
                "mismatches": snapshot.get("runtime_mismatches", {}),
                "preflight": preflight,
                "cutover_started": False,
            }
        return {
            "required": True,
            "ok": result.returncode == 0,
            "removed": result.returncode == 0,
            "reused": False,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "command": command,
            "mismatches": snapshot.get("runtime_mismatches", {}),
            "preflight": preflight,
            "cutover_started": result.returncode == 0,
        }

    def _record_candidate_container_id(
        self,
        replacement: dict[str, Any],
        operation_nonce: str,
    ) -> str:
        """Accept a compose candidate only when full ID and unique nonce both match."""

        docker = shutil.which("docker")
        if docker is None:
            return ""
        current = self._container_status(docker)
        candidate_id = (
            _full_container_id(current.get("id"))
            if isinstance(current, dict)
            and current.get("ok") is True
            and current.get("exists") is True
            else ""
        )
        candidate_nonce = (
            _operation_nonce(current.get("operation_nonce"))
            if isinstance(current, dict)
            else ""
        )
        expected_nonce = _operation_nonce(operation_nonce)
        previous_id = _full_container_id(
            (replacement.get("preflight") or {}).get("previous_container_id")
            if isinstance(replacement.get("preflight"), dict)
            else ""
        )
        provenance_ok = bool(
            candidate_id
            and expected_nonce
            and candidate_nonce == expected_nonce
            and candidate_id != previous_id
        )
        replacement["candidate_provenance"] = {
            "ok": provenance_ok,
            "container_id": candidate_id,
            "expected_nonce": expected_nonce,
            "actual_nonce": candidate_nonce,
        }
        if provenance_ok:
            replacement["candidate_container_id"] = candidate_id
            replacement["candidate_operation_nonce"] = expected_nonce
            return candidate_id
        return ""

    def _candidate_matches_current(
        self,
        docker: str,
        *,
        container_id: str,
        operation_nonce: str,
    ) -> bool:
        expected_id = _full_container_id(container_id)
        expected_nonce = _operation_nonce(operation_nonce)
        current = self._container_status(docker)
        return bool(
            expected_id
            and expected_nonce
            and isinstance(current, dict)
            and current.get("ok") is True
            and current.get("exists") is True
            and _full_container_id(current.get("id")) == expected_id
            and _operation_nonce(current.get("operation_nonce")) == expected_nonce
        )

    def _remove_proven_candidate(self, replacement: dict[str, Any]) -> dict[str, Any]:
        """Remove only the exact full-ID candidate carrying this operation's nonce."""

        docker = shutil.which("docker")
        candidate_id = _full_container_id(replacement.get("candidate_container_id"))
        candidate_nonce = _operation_nonce(replacement.get("candidate_operation_nonce"))
        if (
            docker is None
            or not candidate_id
            or not candidate_nonce
            or not self._candidate_matches_current(
                docker,
                container_id=candidate_id,
                operation_nonce=candidate_nonce,
            )
        ):
            return {
                "ok": False,
                "removed": False,
                "summary": "Candidate cleanup refused: immutable-ID/nonce CAS failed.",
            }
        command = [docker, "rm", "-f", candidate_id]
        try:
            removed = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "removed": False,
                "summary": str(exc) or exc.__class__.__name__,
                "command": command,
            }
        after = self._container_status(docker)
        absent = bool(
            isinstance(after, dict)
            and after.get("ok") is True
            and after.get("exists") is False
        )
        return {
            "ok": removed.returncode == 0 and absent,
            "removed": removed.returncode == 0 and absent,
            "returncode": removed.returncode,
            "command": command,
            "post_cleanup_container": after,
        }

    def _inspect_local_image(self, docker: str, image: str) -> dict[str, Any]:
        command = [docker, "image", "inspect", "--format", "{{.Id}}", image]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "image_id": "",
                "returncode": None,
                "stdout": "",
                "stderr": str(exc) or exc.__class__.__name__,
                "command": command,
            }
        image_id = result.stdout.strip() if result.returncode == 0 else ""
        return {
            "ok": result.returncode == 0 and bool(image_id),
            "image_id": image_id,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "command": command,
        }

    def _ensure_desired_image_available(self, docker: str, image: str) -> dict[str, Any]:
        """Resolve the exact local image before any destructive dispatcher cutover."""

        if not image:
            return {
                "ok": False,
                "available": False,
                "pulled": False,
                "returncode": None,
                "stdout": "",
                "stderr": "Desired dispatcher image is empty.",
                "command": [],
            }

        inspected = self._inspect_local_image(docker, image)
        if inspected.get("ok"):
            return {
                **inspected,
                "ok": True,
                "available": True,
                "built": False,
                "pulled": False,
                "image": image,
            }

        local_build = LOCAL_DERIVED_IMAGE_BUILDS.get(image)
        if local_build is not None:
            dockerfile = self.repo_root / str(local_build["dockerfile"])
            base_image = str(local_build["base_image"])
            base = self._inspect_local_image(docker, base_image)
            build_command = [
                docker,
                "build",
                "--pull=false",
                "-f",
                str(dockerfile),
                "-t",
                image,
                str(self.repo_root),
            ]
            if not dockerfile.is_file() or not base.get("ok"):
                return {
                    "ok": False,
                    "available": False,
                    "built": False,
                    "pulled": False,
                    "returncode": None,
                    "stdout": "",
                    "stderr": (
                        f"Required local base image is unavailable: {base_image}"
                        if dockerfile.is_file()
                        else f"Local derivative Dockerfile is missing: {dockerfile}"
                    ),
                    "command": build_command,
                    "inspect": inspected,
                    "base": base,
                    "image": image,
                }
            try:
                built = subprocess.run(
                    build_command,
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {
                    "ok": False,
                    "available": False,
                    "built": False,
                    "pulled": False,
                    "returncode": None,
                    "stdout": "",
                    "stderr": str(exc) or exc.__class__.__name__,
                    "command": build_command,
                    "inspect": inspected,
                    "base": base,
                    "image": image,
                }
            verified = (
                self._inspect_local_image(docker, image)
                if built.returncode == 0
                else {"ok": False, "image_id": "", "stderr": ""}
            )
            ok = built.returncode == 0 and verified.get("ok") is True
            return {
                "ok": ok,
                "available": ok,
                "built": ok,
                "pulled": False,
                "image_id": str(verified.get("image_id") or ""),
                "returncode": built.returncode,
                "stdout": built.stdout.strip(),
                "stderr": built.stderr.strip() or str(verified.get("stderr") or ""),
                "command": build_command,
                "inspect": inspected,
                "base": base,
                "verification": verified,
                "image": image,
            }

        pull_command = [docker, "pull", image]
        try:
            pulled = subprocess.run(
                pull_command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "available": False,
                "pulled": False,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc) or exc.__class__.__name__,
                "command": pull_command,
                "inspect": inspected,
                "image": image,
            }
        verified = (
            self._inspect_local_image(docker, image)
            if pulled.returncode == 0
            else {"ok": False, "image_id": "", "stderr": ""}
        )
        ok = pulled.returncode == 0 and verified.get("ok") is True
        return {
            "ok": ok,
            "available": ok,
            "built": False,
            "pulled": ok,
            "image_id": str(verified.get("image_id") or ""),
            "returncode": pulled.returncode,
            "stdout": pulled.stdout.strip(),
            "stderr": pulled.stderr.strip() or str(verified.get("stderr") or ""),
            "command": pull_command,
            "inspect": inspected,
            "verification": verified,
            "image": image,
        }

    def _inspect_container_for_cutover(self, docker: str) -> dict[str, Any]:
        command = [docker, "inspect", DISPATCHER_CONTAINER]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "summary": str(exc) or exc.__class__.__name__, "command": command}
        if result.returncode != 0:
            return {
                "ok": False,
                "summary": result.stderr.strip() or "docker inspect failed",
                "returncode": result.returncode,
                "command": command,
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "summary": "docker inspect returned invalid JSON",
                "command": command,
            }
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            return {
                "ok": False,
                "summary": "docker inspect returned no exact container",
                "command": command,
            }
        return {"ok": True, "document": payload[0], "command": command}

    def _validate_model_artifacts(self, model_root: str, docker_model_path: str) -> dict[str, Any]:
        normalized = str(docker_model_path or "").replace("\\", "/")
        prefix = "/models/"
        if not normalized.startswith(prefix):
            return {"ok": False, "summary": "Model path is outside the /models bind."}
        relative = PurePosixPath(normalized[len(prefix) :])
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return {"ok": False, "summary": "Model path is empty or contains traversal."}
        host_path = Path(model_root).joinpath(*relative.parts)
        if not host_path.is_dir():
            return {
                "ok": False,
                "summary": f"Model directory is unavailable: {host_path}",
                "host_path": str(host_path),
            }
        config_path = host_path / "config.json"
        if not config_path.is_file() or not os.access(config_path, os.R_OK):
            return {
                "ok": False,
                "summary": f"Readable model config is missing: {config_path}",
                "host_path": str(host_path),
            }
        def readable_nonempty(path: Path) -> bool:
            try:
                return path.is_file() and os.access(path, os.R_OK) and path.stat().st_size > 0
            except OSError:
                return False

        index_files = sorted(host_path.glob("*.safetensors.index.json"))
        indexed_shards: set[Path] = set()
        for index_path in index_files:
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return {
                    "ok": False,
                    "summary": f"Safetensors index is unreadable or invalid: {index_path}",
                    "host_path": str(host_path),
                }
            weight_map = payload.get("weight_map") if isinstance(payload, dict) else None
            if not isinstance(weight_map, dict) or not weight_map:
                return {
                    "ok": False,
                    "summary": f"Safetensors index has no weight_map: {index_path}",
                    "host_path": str(host_path),
                }
            for raw_name in weight_map.values():
                if not isinstance(raw_name, str) or not raw_name.strip():
                    return {
                        "ok": False,
                        "summary": f"Safetensors index has an invalid shard name: {index_path}",
                        "host_path": str(host_path),
                    }
                relative_shard = PurePosixPath(raw_name.replace("\\", "/"))
                if relative_shard.is_absolute() or any(
                    part in {"", ".", ".."} for part in relative_shard.parts
                ):
                    return {
                        "ok": False,
                        "summary": f"Safetensors index shard escapes the model: {raw_name}",
                        "host_path": str(host_path),
                    }
                shard = host_path.joinpath(*relative_shard.parts)
                if not readable_nonempty(shard):
                    return {
                        "ok": False,
                        "summary": f"Referenced model shard is missing or empty: {shard}",
                        "host_path": str(host_path),
                    }
                indexed_shards.add(shard)

        direct_weights = {
            path
            for pattern in ("*.safetensors", "pytorch_model*.bin", "*.gguf")
            for path in host_path.glob(pattern)
            if readable_nonempty(path)
        }
        if not indexed_shards and not direct_weights:
            return {
                "ok": False,
                "summary": f"No readable non-empty model weight artifact exists in {host_path}",
                "host_path": str(host_path),
            }
        return {
            "ok": True,
            "host_path": str(host_path),
            "config": str(config_path),
            "weight_files": len(indexed_shards | direct_weights),
        }

    def _rollback_compose_environment(
        self,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        container_id = _full_container_id(document.get("Id"))
        if not container_id:
            return {
                "ok": False,
                "summary": "Previous container lacks a full immutable 64-hex ID.",
            }
        config = document.get("Config")
        host_config = document.get("HostConfig")
        mounts = document.get("Mounts")
        if (
            not isinstance(config, dict)
            or not isinstance(host_config, dict)
            or not isinstance(mounts, list)
        ):
            return {
                "ok": False,
                "summary": "Previous container inspect lacks config/host/mount data.",
            }
        raw_command = config.get("Cmd")
        if not isinstance(raw_command, list) or not raw_command:
            return {"ok": False, "summary": "Previous container command is unavailable."}
        command = [str(item) for item in raw_command]
        previous_runtime = _runtime_from_command(command)
        if not previous_runtime.get("model_path"):
            return {"ok": False, "summary": "Previous runtime model path is unavailable."}
        if previous_runtime.get("prefix_caching") is not True:
            return {
                "ok": False,
                "summary": (
                    "Previous runtime cannot be represented by current compose "
                    "prefix-caching contract."
                ),
            }
        if previous_runtime.get("host") != "0.0.0.0" or previous_runtime.get("port") != 8001:
            return {
                "ok": False,
                "summary": "Previous runtime host/port cannot be represented by current compose.",
            }

        mount_by_target = {
            str(item.get("Destination") or ""): item for item in mounts if isinstance(item, dict)
        }
        model_mount = mount_by_target.get("/models")
        cache_mount = mount_by_target.get("/root/.cache")
        if not isinstance(model_mount, dict) or not isinstance(cache_mount, dict):
            return {
                "ok": False,
                "summary": "Previous container model/cache bind mounts are missing.",
            }
        model_root = str(model_mount.get("Source") or "")
        cache_root = str(cache_mount.get("Source") or "")
        if not model_root or not cache_root or model_mount.get("RW") is not False:
            return {
                "ok": False,
                "summary": "Previous container bind mount contract is unsafe or incomplete.",
            }
        if not Path(model_root).is_dir() or not Path(cache_root).is_dir():
            return {
                "ok": False,
                "summary": "Previous container bind source is no longer available.",
            }

        port_bindings = host_config.get("PortBindings")
        bound = port_bindings.get("8001/tcp") if isinstance(port_bindings, dict) else None
        if not isinstance(bound, list) or not any(
            isinstance(item, dict)
            and str(item.get("HostIp") or "") == "127.0.0.1"
            and str(item.get("HostPort") or "") == "8001"
            for item in bound
        ):
            return {
                "ok": False,
                "summary": "Previous container does not own the required loopback port binding.",
            }
        device_requests = host_config.get("DeviceRequests")
        has_gpu_request = any(
            isinstance(item, dict)
            and (
                str(item.get("Driver") or "").casefold() == "nvidia"
                or "gpu" in json.dumps(item.get("Capabilities") or []).casefold()
            )
            for item in (device_requests if isinstance(device_requests, list) else [])
        )
        if not has_gpu_request:
            return {"ok": False, "summary": "Previous container GPU device request is unavailable."}
        if str(host_config.get("IpcMode") or "") != "host":
            return {"ok": False, "summary": "Previous container IPC mode is not reproducible."}

        env = self.compose_env()
        env.update(
            {
                "JARVIS_MODEL_ROOT": model_root,
                "JARVIS_HOST_HOME": str(Path(cache_root).parent),
                "JARVIS_QWEN_MODEL_PATH": str(previous_runtime["model_path"]),
                "JARVIS_QWEN_MODEL_NAME": str(previous_runtime["served_model_name"]),
                "JARVIS_QWEN_DTYPE": str(previous_runtime["dtype"]),
                "JARVIS_QWEN_ENFORCE_EAGER": (
                    "--enforce-eager" if previous_runtime["enforce_eager"] else ""
                ),
                "JARVIS_QWEN_MAX_LEN": str(previous_runtime["max_model_len"]),
                "JARVIS_QWEN_GPU_UTIL": str(previous_runtime["gpu_memory_utilization"]),
                "JARVIS_QWEN_KV_DTYPE": str(previous_runtime["kv_cache_dtype"]),
                "JARVIS_QWEN_MAX_NUM_SEQS": str(previous_runtime["max_num_seqs"]),
                "JARVIS_QWEN_CPU_OFFLOAD_ARGS": (
                    f"--cpu-offload-gb {previous_runtime['cpu_offload_gb']}"
                    if previous_runtime["cpu_offload_gb"] not in {None, 0}
                    else ""
                ),
                "JARVIS_QWEN_KV_OFFLOAD_ARGS": (
                    "--kv-offloading-size "
                    f"{previous_runtime['kv_offloading_gb']} --kv-offloading-backend "
                    f"{previous_runtime['kv_offloading_backend']}"
                    if previous_runtime["kv_offloading_gb"] not in {None, 0}
                    else ""
                ),
                "JARVIS_QWEN_TOKENIZER_MODE": str(previous_runtime["tokenizer_mode"]),
                "JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY": str(
                    previous_runtime["safetensors_load_strategy"]
                ),
                "JARVIS_QWEN_EXTRA_ARGS": _rollback_extra_args(command),
            }
        )
        actual_env = _environment_pairs(config.get("Env"))
        for compose_key, container_key in (
            ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"),
            ("VLLM_USE_V2_MODEL_RUNNER", "VLLM_USE_V2_MODEL_RUNNER"),
            ("VLLM_WEIGHT_OFFLOADING_DISABLE_UVA", "VLLM_WEIGHT_OFFLOADING_DISABLE_UVA"),
            ("CUDA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"),
            ("CUDA_DEVICE_ORDER", "CUDA_DEVICE_ORDER"),
            ("CUDA_DISABLE_P2P", "CUDA_DISABLE_P2P"),
            ("NCCL_P2P_DISABLE", "NCCL_P2P_DISABLE"),
        ):
            if container_key in actual_env:
                env[compose_key] = actual_env[container_key]
        round_trip = _runtime_from_env(env)
        mismatches = _runtime_mismatches(previous_runtime, round_trip)
        if mismatches:
            return {
                "ok": False,
                "summary": "Previous runtime cannot be represented exactly by current compose.",
                "mismatches": mismatches,
            }
        artifacts = self._validate_model_artifacts(model_root, str(previous_runtime["model_path"]))
        if not artifacts.get("ok"):
            return {
                "ok": False,
                "summary": artifacts.get("summary", "Previous model is unavailable."),
            }
        return {
            "ok": True,
            "runtime": previous_runtime,
            "image_id": str(document.get("Image") or ""),
            "container_id": container_id,
            "model_artifacts": artifacts,
            "_env": env,
        }

    def _cutover_preflight(
        self,
        snapshot: dict[str, Any],
        docker: str,
    ) -> dict[str, Any]:
        desired_env = self.compose_env()
        desired_image = str(
            snapshot.get("desired_image") or desired_env.get("JARVIS_VLLM_IMAGE") or ""
        ).strip()
        image = self._ensure_desired_image_available(docker, desired_image)
        if not image.get("ok"):
            return {"ok": False, "summary": "Desired image is unavailable.", "image": image}
        compose_command = [docker, "compose", "--profile", "llm", "config", "--quiet"]
        try:
            configured = subprocess.run(
                compose_command,
                cwd=self.repo_root,
                env={**os.environ, **desired_env},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "summary": str(exc) or exc.__class__.__name__, "image": image}
        if configured.returncode != 0:
            return {
                "ok": False,
                "summary": configured.stderr.strip() or "docker compose config validation failed",
                "image": image,
            }
        desired_artifacts = self._validate_model_artifacts(
            desired_env.get("JARVIS_MODEL_ROOT", ""),
            desired_env.get("JARVIS_QWEN_MODEL_PATH", ""),
        )
        if not desired_artifacts.get("ok"):
            return {"ok": False, "summary": desired_artifacts.get("summary"), "image": image}
        cache_path = Path(desired_env.get("JARVIS_HOST_HOME", "")) / "cache"
        if not cache_path.is_dir():
            return {
                "ok": False,
                "summary": f"Dispatcher cache bind is unavailable: {cache_path}",
                "image": image,
            }

        inspected = self._inspect_container_for_cutover(docker)
        if not inspected.get("ok"):
            return {"ok": False, "summary": inspected.get("summary"), "image": image}
        rollback = self._rollback_compose_environment(inspected["document"])
        if (
            not rollback.get("ok")
            or not rollback.get("image_id")
            or not _full_container_id(rollback.get("container_id"))
        ):
            return {
                "ok": False,
                "summary": rollback.get("summary", "Previous runtime rollback is not provable."),
                "image": image,
            }
        state = inspected["document"].get("State")
        old_running = bool(isinstance(state, dict) and state.get("Running"))
        if not old_running and _port_open("127.0.0.1", 8001, timeout=0.25):
            return {
                "ok": False,
                "summary": "TCP 8001 is owned outside the stopped old container.",
                "image": image,
            }

        gpu_probe = self._gpu_preflight(docker)
        if not gpu_probe.get("ok"):
            return {"ok": False, "summary": gpu_probe.get("summary"), "image": image}
        rollback_tag = (
            "jarvis-gpt-dispatcher-rollback:"
            f"{str(rollback['container_id'])[:12]}-{time.time_ns()}"
        )
        tag_command = [docker, "image", "tag", str(rollback["image_id"]), rollback_tag]
        try:
            tagged = subprocess.run(
                tag_command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "summary": str(exc) or exc.__class__.__name__, "image": image}
        if tagged.returncode != 0:
            return {
                "ok": False,
                "summary": tagged.stderr.strip() or "Could not pin rollback image.",
                "image": image,
            }
        rollback_env = dict(rollback["_env"])
        rollback_env["JARVIS_VLLM_IMAGE"] = rollback_tag
        return {
            "ok": True,
            "summary": "Compose, artifacts, binds, port, GPU, and rollback runtime validated.",
            "image": image,
            "desired_artifacts": desired_artifacts,
            "gpu": gpu_probe,
            "rollback_image_tag": rollback_tag,
            "previous_container_id": rollback["container_id"],
            "previous_image_id": rollback["image_id"],
            "previous_runtime": rollback["runtime"],
            "_rollback_env": rollback_env,
        }

    def _gpu_preflight(self, docker: str) -> dict[str, Any]:
        nvidia_smi = shutil.which("nvidia-smi")
        command = (
            [nvidia_smi, "--query-gpu=index", "--format=csv,noheader"]
            if nvidia_smi
            else [docker, "info", "--format", "{{json .Runtimes}}"]
        )
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "summary": str(exc) or exc.__class__.__name__}
        output = result.stdout.strip()
        available = (
            result.returncode == 0
            and bool(output)
            and (nvidia_smi is not None or "nvidia" in output.casefold())
        )
        return {
            "ok": available,
            "summary": "GPU runtime is available."
            if available
            else "GPU runtime preflight failed.",
            "probe": "nvidia-smi" if nvidia_smi else "docker-runtimes",
        }

    def _public_replacement(self, replacement: dict[str, Any] | None) -> dict[str, Any] | None:
        if replacement is None:
            return None
        return _redact_private_fields(replacement)

    def _cleanup_rollback_image(self, replacement: dict[str, Any]) -> dict[str, Any]:
        preflight = replacement.get("preflight")
        tag = str(preflight.get("rollback_image_tag") or "") if isinstance(preflight, dict) else ""
        docker = shutil.which("docker")
        if not tag or docker is None:
            return {"ok": not tag, "removed": False, "tag": tag}
        command = [docker, "image", "rm", tag]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "removed": False, "tag": tag, "summary": str(exc)}
        return {
            "ok": result.returncode == 0,
            "removed": result.returncode == 0,
            "tag": tag,
            "summary": result.stderr.strip(),
        }

    def _live_completion_probe(self, *, timeout_seconds: float) -> dict[str, Any]:
        """Prove that the configured model can produce a deterministic completion."""

        endpoint = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "max_tokens": 16,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "user", "content": "Reply with only the digit 4. What is 2+2?"}
            ],
        }
        try:
            with httpx.Client(trust_env=False) as client:
                response = client.post(
                    endpoint,
                    json=payload,
                    timeout=max(0.25, min(float(timeout_seconds), 20.0)),
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "terminal": False,
                "error": f"live completion transport failed: {exc.__class__.__name__}",
                "status_code": None,
            }
        if response.status_code != 200:
            terminal_contract_or_auth = response.status_code in {
                400,
                401,
                403,
                404,
                405,
                406,
                407,
                410,
                413,
                414,
                415,
                422,
            }
            return {
                "ok": False,
                "terminal": terminal_contract_or_auth,
                "error": f"live completion returned HTTP {response.status_code}",
                "status_code": response.status_code,
            }
        try:
            body = response.json()
            content = str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return {
                "ok": False,
                "terminal": True,
                "error": f"live completion response is malformed: {exc.__class__.__name__}",
                "status_code": response.status_code,
            }
        normalized = " ".join(content.split()).strip()
        if normalized != "4":
            return {
                "ok": False,
                "terminal": True,
                "error": "live completion returned an unexpected answer to 2+2",
                "status_code": response.status_code,
                "normalized_content": normalized[:80],
            }
        return {
            "ok": True,
            "terminal": False,
            "error": "",
            "status_code": response.status_code,
            "normalized_content": normalized,
        }

    def _verify_runtime(
        self,
        expected_runtime: dict[str, Any],
        expected_image_id: str,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        completion: dict[str, Any] = {"ok": False, "skipped": True}
        while True:
            snapshot = self.status()
            container = snapshot.get("container_status")
            running = bool(
                isinstance(container, dict)
                and container.get("ok") is True
                and container.get("exists") is True
                and str(container.get("status") or "").casefold().startswith("up")
            )
            actual_runtime = snapshot.get("runtime")
            if not isinstance(actual_runtime, dict):
                actual_runtime = _runtime_from_container(container)
            mismatches = _runtime_mismatches(actual_runtime, expected_runtime)
            actual_image_id = str(
                snapshot.get("actual_image_id")
                or (container.get("image_id") if isinstance(container, dict) else "")
                or ""
            )
            image_matches = bool(expected_image_id) and actual_image_id == expected_image_id
            port_open = bool(snapshot.get("port_open"))
            base_ready = running and not mismatches and image_matches and port_open
            now = time.monotonic()
            if base_ready:
                completion = self._live_completion_probe(
                    timeout_seconds=max(0.1, deadline - now)
                )
                now = time.monotonic()
            verified = base_ready and completion.get("ok") is True
            terminal_failure = bool(completion.get("terminal"))
            if verified or terminal_failure or now >= deadline:
                return {
                    "ok": verified,
                    "container_running": running,
                    "runtime_matches": not mismatches,
                    "runtime_mismatches": mismatches,
                    "image_matches": image_matches,
                    "actual_image_id": actual_image_id,
                    "expected_image_id": expected_image_id,
                    "port_open": port_open,
                    "live_completion": completion,
                }
            time.sleep(min(1.0, max(0.01, deadline - time.monotonic())))

    def _rollback_replacement(
        self,
        replacement: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        preflight = replacement.get("preflight")
        if not isinstance(preflight, dict):
            return {"ok": False, "summary": "Rollback plan is unavailable."}
        rollback_env = preflight.get("_rollback_env")
        previous_runtime = preflight.get("previous_runtime")
        previous_image_id = str(preflight.get("previous_image_id") or "")
        if not isinstance(rollback_env, dict) or not isinstance(previous_runtime, dict):
            return {"ok": False, "summary": "Rollback plan is incomplete."}
        docker = shutil.which("docker")
        if docker is None:
            return {"ok": False, "summary": "Docker is unavailable during rollback."}

        candidate_id = _full_container_id(replacement.get("candidate_container_id"))
        candidate_nonce = _operation_nonce(replacement.get("candidate_operation_nonce"))
        current = self._container_status(docker)
        current_known = bool(isinstance(current, dict) and current.get("ok") is True)
        current_exists = bool(current_known and current.get("exists") is True)
        current_id = _full_container_id(current.get("id")) if current_exists else ""
        current_nonce = (
            _operation_nonce(current.get("operation_nonce")) if current_exists else ""
        )
        if not current_known:
            return {
                "ok": False,
                "summary": "Rollback refused because current Docker identity is unknown.",
                "candidate_container_id": candidate_id,
                "current_container_id": current_id,
            }
        if current_exists and (
            not candidate_id
            or not candidate_nonce
            or current_id != candidate_id
            or current_nonce != candidate_nonce
        ):
            return {
                "ok": False,
                "summary": (
                    "Rollback refused because immutable-ID/nonce provenance belongs to "
                    "another dispatcher."
                ),
                "candidate_container_id": candidate_id,
                "current_container_id": current_id,
                "candidate_operation_nonce": candidate_nonce,
                "current_operation_nonce": current_nonce,
            }

        cleanup: dict[str, Any]
        if current_exists:
            remove_command = [docker, "rm", "-f", candidate_id]
            try:
                removed_failed_candidate = subprocess.run(
                    remove_command,
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                cleanup = {
                    "ok": removed_failed_candidate.returncode == 0,
                    "returncode": removed_failed_candidate.returncode,
                    "stderr": removed_failed_candidate.stderr.strip(),
                    "command": remove_command,
                }
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup = {
                    "ok": False,
                    "returncode": None,
                    "stderr": str(exc),
                    "command": remove_command,
                }
            if not cleanup["ok"]:
                return {
                    "ok": False,
                    "summary": "Failed candidate could not be removed by immutable ID.",
                    "failed_candidate_cleanup": cleanup,
                }
        else:
            cleanup = {
                "ok": True,
                "returncode": 0,
                "stderr": "",
                "command": [],
                "already_absent": True,
            }
        post_cleanup = self._container_status(docker)
        if (
            not isinstance(post_cleanup, dict)
            or post_cleanup.get("ok") is not True
            or post_cleanup.get("exists") is not False
        ):
            return {
                "ok": False,
                "summary": (
                    "Rollback refused because dispatcher absence could not be proven "
                    "after candidate cleanup."
                ),
                "failed_candidate_cleanup": cleanup,
                "post_cleanup_container": post_cleanup,
            }
        rollback_nonce = secrets.token_hex(16)
        restore_env = {str(k): str(v) for k, v in rollback_env.items()}
        restore_env[DISPATCHER_OPERATION_NONCE_ENV] = rollback_nonce
        ownership_context = replacement.get("_ownership_context")
        rollback_intent = self._new_ownership_journal(
            phase="rollback-intent",
            operation_nonce=rollback_nonce,
            previous_container_id=str(preflight.get("previous_container_id") or ""),
            container_id="",
            context=(ownership_context if isinstance(ownership_context, dict) else {}),
        )
        rollback_intent_write = self._write_ownership_journal(rollback_intent)
        if not rollback_intent_write.get("ok"):
            return {
                "ok": False,
                "summary": (
                    "Rollback restore was not started because its write-ahead "
                    "ownership intent could not be persisted."
                ),
                "failed_candidate_cleanup": cleanup,
                "ownership_intent_journal": rollback_intent_write,
            }
        restored = self._run_compose_with_env("up", restore_env)
        restoration: dict[str, Any] = {
            "preflight": {
                "previous_container_id": _full_container_id(
                    preflight.get("previous_container_id")
                )
            }
        }
        restored_id = (
            self._record_candidate_container_id(restoration, rollback_nonce)
            if restored.get("ok")
            else ""
        )
        rollback_journal = (
            self._new_ownership_journal(
                phase="rollback-candidate",
                operation_nonce=rollback_nonce,
                previous_container_id=str(preflight.get("previous_container_id") or ""),
                container_id=restored_id,
                context=(
                    ownership_context if isinstance(ownership_context, dict) else {}
                ),
            )
            if restored_id
            else None
        )
        rollback_journal_write = (
            self._write_ownership_journal(rollback_journal)
            if isinstance(rollback_journal, dict)
            else {"ok": False, "summary": "Rollback candidate provenance is unavailable."}
        )
        verification = (
            self._verify_runtime(
                previous_runtime,
                previous_image_id,
                timeout_seconds=timeout_seconds,
            )
            if restored.get("ok") and restored_id and rollback_journal_write.get("ok")
            else {"ok": False, "skipped": True}
        )
        provenance_still_current = bool(
            restored_id
            and self._candidate_matches_current(
                docker,
                container_id=restored_id,
                operation_nonce=rollback_nonce,
            )
        )
        ownership_commit = (
            self._commit_candidate_ownership(rollback_journal)
            if (
                isinstance(rollback_journal, dict)
                and verification.get("ok")
                and provenance_still_current
            )
            else {"ok": False, "skipped": True}
        )
        ok = bool(
            restored.get("ok")
            and restored_id
            and rollback_journal_write.get("ok")
            and verification.get("ok")
            and provenance_still_current
            and ownership_commit.get("ok")
        )
        image_cleanup = self._cleanup_rollback_image(replacement) if ok else None
        return {
            "ok": ok,
            "summary": (
                "Previous dispatcher runtime was restored and verified running."
                if ok
                else "Previous dispatcher runtime could not be restored and verified."
            ),
            "failed_candidate_cleanup": cleanup,
            "compose": restored,
            "container_id": restored_id,
            "operation_nonce": rollback_nonce,
            "provenance_still_current": provenance_still_current,
            "ownership_intent_journal": rollback_intent_write,
            "ownership_journal": rollback_journal_write,
            "ownership_commit": ownership_commit,
            "verification": verification,
            "rollback_image_cleanup": image_cleanup,
        }

    def verify_state(
        self,
        *,
        running: bool,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        snapshot: dict[str, Any] = {}
        completion: dict[str, Any] = {"ok": False, "skipped": True}
        while True:
            snapshot = self.status()
            container = snapshot.get("container_status")
            container_status = (
                str(container.get("status") or "") if isinstance(container, dict) else ""
            )
            container_known = bool(
                isinstance(container, dict)
                and container.get("ok") is True
                and isinstance(container.get("exists"), bool)
            )
            container_running = bool(
                container_known
                and container.get("exists")
                and container_status.casefold().startswith("up")
            )
            port_open = bool(snapshot.get("port_open"))
            runtime_matches_desired = snapshot.get("runtime_matches_desired") is True
            base_running_ready = bool(
                container_known
                and container_running
                and runtime_matches_desired
                and port_open
            )
            now = time.monotonic()
            if running and base_running_ready:
                completion = self._live_completion_probe(
                    timeout_seconds=max(0.1, deadline - now)
                )
                now = time.monotonic()
            verified = (
                base_running_ready and completion.get("ok") is True
                if running
                else container_known and not container_running and not port_open
            )
            terminal_failure = bool(running and completion.get("terminal"))
            if verified or terminal_failure or now >= deadline:
                return {
                    "ok": verified,
                    "expected_running": running,
                    "container_known": container_known,
                    "container_running": container_running,
                    "port_open": port_open,
                    "runtime_matches_desired": runtime_matches_desired,
                    "runtime_mismatches": snapshot.get("runtime_mismatches", {}),
                    "port": int(snapshot.get("port") or 8001),
                    "container_status": container,
                    "live_completion": completion,
                }
            time.sleep(min(1.0, max(0.01, deadline - time.monotonic())))

    def sync_launcher_state_container_id(
        self,
        *,
        expected_previous_id: str,
        expected_previous_operation_nonce: str,
        expected_current_id: str,
        operation_nonce: str,
    ) -> dict[str, Any]:
        """CAS launcher ownership from one proven full ID to our nonce-bound candidate."""

        previous_id = _full_container_id(expected_previous_id)
        previous_nonce = _operation_nonce(expected_previous_operation_nonce)
        expected_id = _full_container_id(expected_current_id)
        expected_nonce = _operation_nonce(operation_nonce)
        if (
            not previous_id
            or not previous_nonce
            or not expected_id
            or not expected_nonce
            or previous_id == expected_id
        ):
            return {
                "ok": False,
                "updated": False,
                "reason": "invalid-ownership-cas-proof",
                "previous_id": previous_id,
                "container_id": expected_id,
            }

        snapshot = self.status()
        container = snapshot.get("container_status")
        current_id = (
            _full_container_id(container.get("id")) if isinstance(container, dict) else ""
        )
        current_nonce = (
            _operation_nonce(container.get("operation_nonce"))
            if isinstance(container, dict)
            else ""
        )
        running = bool(
            isinstance(container, dict)
            and container.get("exists") is True
            and str(container.get("status") or "").casefold().startswith("up")
        )
        if (
            not running
            or current_id != expected_id
            or current_nonce != expected_nonce
        ):
            return {
                "ok": False,
                "updated": False,
                "reason": "docker-candidate-provenance-mismatch",
                "container_id": current_id,
            }

        state_path = self.settings.state_dir / "launcher-state.json"
        lock_path = state_path.with_name("launcher-state.lock")
        try:
            with _launcher_state_lock(
                lock_path,
                timeout_seconds=LAUNCHER_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                # Docker may have recreated the container while this writer waited for the
                # launcher. Re-read the source of truth under the lock before publishing.
                try:
                    locked_snapshot = self.status()
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "updated": False,
                        "reason": f"docker-state-recheck-failed:{exc.__class__.__name__}",
                        "container_id": current_id,
                    }
                locked_container = locked_snapshot.get("container_status")
                locked_id = (
                    _full_container_id(locked_container.get("id"))
                    if isinstance(locked_container, dict)
                    else ""
                )
                locked_nonce = (
                    _operation_nonce(locked_container.get("operation_nonce"))
                    if isinstance(locked_container, dict)
                    else ""
                )
                locked_running = bool(
                    isinstance(locked_container, dict)
                    and locked_container.get("exists") is True
                    and str(locked_container.get("status") or "").casefold().startswith("up")
                )
                if (
                    not locked_running
                    or locked_id != expected_id
                    or locked_nonce != expected_nonce
                ):
                    return {
                        "ok": False,
                        "updated": False,
                        "reason": "docker-container-id-changed-or-unavailable",
                        "container_id": locked_id,
                    }
                return self._sync_launcher_state_container_id_locked(
                    state_path,
                    expected_previous_id=previous_id,
                    expected_previous_operation_nonce=previous_nonce,
                    current_id=locked_id,
                    operation_nonce=expected_nonce,
                )
        except TimeoutError:
            return {
                "ok": False,
                "updated": False,
                "reason": "launcher-state-lock-timeout",
                "container_id": current_id,
            }
        except OSError as exc:
            return {
                "ok": False,
                "updated": False,
                "reason": f"launcher-state-lock-failed:{exc.__class__.__name__}",
                "container_id": current_id,
            }

    def _sync_launcher_state_container_id_locked(
        self,
        state_path: Path,
        *,
        expected_previous_id: str,
        expected_previous_operation_nonce: str,
        current_id: str,
        operation_nonce: str,
    ) -> dict[str, Any]:
        if not state_path.exists():
            return {
                "ok": True,
                "updated": False,
                "reason": "launcher-state-absent",
                "container_id": current_id,
            }
        try:
            original = state_path.read_bytes()
            state = json.loads(original.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "updated": False,
                "reason": f"launcher-state-invalid:{exc.__class__.__name__}",
                "container_id": current_id,
            }
        if not isinstance(state, dict):
            return {
                "ok": False,
                "updated": False,
                "reason": "launcher-state-invalid:root-not-object",
                "container_id": current_id,
            }
        services = state.get("services")
        entry = services.get("dispatcher") if isinstance(services, dict) else None
        if not isinstance(entry, dict):
            return {
                "ok": True,
                "updated": False,
                "reason": "launcher-dispatcher-entry-absent",
                "container_id": current_id,
            }
        if entry.get("started_by_launcher") is not True:
            return {
                "ok": True,
                "updated": False,
                "reason": "dispatcher-not-owned-by-launcher",
                "container_id": current_id,
            }
        recorded_profile = str(entry.get("profile") or "")
        if recorded_profile and recorded_profile != self.settings.profile.name:
            return {
                "ok": True,
                "updated": False,
                "reason": "launcher-profile-mismatch",
                "container_id": current_id,
            }
        previous_id = _full_container_id(entry.get("container_id"))
        previous_nonce = _operation_nonce(entry.get("operation_nonce"))
        if previous_id == current_id:
            if previous_nonce != operation_nonce:
                return {
                    "ok": False,
                    "updated": False,
                    "reason": "launcher-current-operation-nonce-cas-mismatch",
                    "previous_id": previous_id,
                    "container_id": current_id,
                }
            entry["phase"] = "ready"
            return self._write_launcher_state_document(
                state_path,
                state,
                original,
                previous_id=previous_id,
                current_id=current_id,
                updated=False,
                reason="already-current",
            )
        if (
            not previous_id
            or previous_id != expected_previous_id
            or not previous_nonce
            or previous_nonce != expected_previous_operation_nonce
        ):
            return {
                "ok": False,
                "updated": False,
                "reason": "launcher-container-id-or-nonce-cas-mismatch",
                "previous_id": previous_id,
                "expected_previous_id": expected_previous_id,
                "container_id": current_id,
            }
        entry["container_id"] = current_id
        entry["operation_nonce"] = operation_nonce
        entry["phase"] = "ready"
        return self._write_launcher_state_document(
            state_path,
            state,
            original,
            previous_id=previous_id,
            current_id=current_id,
            updated=True,
            reason="docker-id-synchronized",
        )

    def _write_launcher_state_document(
        self,
        state_path: Path,
        state: dict[str, Any],
        original: bytes,
        *,
        previous_id: str,
        current_id: str,
        updated: bool,
        reason: str,
    ) -> dict[str, Any]:
        newline = "\r\n" if b"\r\n" in original else "\n"
        rendered = json.dumps(state, ensure_ascii=False, indent=2).replace("\n", newline)
        payload = rendered.encode("utf-8") + newline.encode("ascii")
        if original.startswith(b"\xef\xbb\xbf"):
            payload = b"\xef\xbb\xbf" + payload
        temporary = state_path.with_name(f".{state_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state_path)
        except OSError as exc:
            return {
                "ok": False,
                "updated": False,
                "reason": f"launcher-state-write-failed:{exc.__class__.__name__}",
                "previous_id": previous_id,
                "container_id": current_id,
            }
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return {
            "ok": True,
            "updated": updated,
            "reason": reason,
            "previous_id": previous_id,
            "container_id": current_id,
        }

    def _container_status(self, docker: str | None) -> dict[str, Any] | None:
        if docker is None:
            return None
        try:
            result = subprocess.run(
                [
                    docker,
                    "ps",
                    "-a",
                    "--filter",
                    f"name={DISPATCHER_CONTAINER}",
                    "--format",
                    "{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "exists": None, "error": str(exc)}
        if result.returncode != 0:
            return {"ok": False, "exists": None, "error": result.stderr.strip()}
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        exact = next(
            (line for line in lines if line.split("\t", 1)[0].strip() == DISPATCHER_CONTAINER),
            None,
        )
        if exact is None:
            return {"ok": True, "exists": False}
        parts = exact.split("\t")
        details = self._container_details(docker)
        if details.get("ok") is not True or not _full_container_id(details.get("id")):
            return {
                "ok": False,
                "exists": True,
                "error": str(details.get("error") or "docker inspect identity is unavailable"),
            }
        command = self._container_command(docker)
        return {
            "ok": True,
            "exists": True,
            "name": parts[0] if len(parts) > 0 else DISPATCHER_CONTAINER,
            "status": parts[1] if len(parts) > 1 else "",
            "ports": parts[2] if len(parts) > 2 else "",
            "image": parts[3] if len(parts) > 3 else "",
            "image_id": details.get("image_id", ""),
            "inspect_ok": details.get("ok") is True,
            "id": details.get("id", ""),
            "health": details.get("health", ""),
            "started_at": details.get("started_at", ""),
            "operation_nonce": details.get("operation_nonce", ""),
            "command": command,
        }

    def _container_details(self, docker: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [
                    docker,
                    "inspect",
                    DISPATCHER_CONTAINER,
                    "--format",
                    "{{.Id}}\t{{.Image}}\t{{json .State}}\t{{json .Config.Labels}}",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}
        fields = result.stdout.strip().split("\t", 3)
        if len(fields) != 4:
            return {"ok": False, "error": "docker inspect state output is malformed"}
        container_id, image_id, state_json, labels_json = fields
        try:
            state = json.loads(state_json)
            labels = json.loads(labels_json)
        except json.JSONDecodeError:
            return {"ok": False, "error": "docker inspect state/labels are not JSON"}
        if not isinstance(state, dict) or not isinstance(labels, dict):
            return {"ok": False, "error": "docker inspect state/labels are malformed"}
        health = state.get("Health")
        return {
            "ok": True,
            "id": container_id,
            "image_id": image_id,
            "started_at": str(state.get("StartedAt") or ""),
            "health": (str(health.get("Status") or "") if isinstance(health, dict) else ""),
            "operation_nonce": str(labels.get(DISPATCHER_OPERATION_NONCE_LABEL) or ""),
        }

    def _container_command(self, docker: str) -> list[str]:
        try:
            result = subprocess.run(
                [
                    docker,
                    "inspect",
                    DISPATCHER_CONTAINER,
                    "--format",
                    "{{json .Config.Cmd}}",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        try:
            value = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []


def _port_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _public_env(env: dict[str, str]) -> dict[str, str]:
    """Return dispatcher configuration without exposing credential values."""
    secret_keys = {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"}
    return {
        key: "[configured]" if key in secret_keys and value else value for key, value in env.items()
    }


def _redact_private_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_private_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_redact_private_fields(item) for item in value]
    return value


def _runtime_from_container(container: dict[str, Any] | None) -> dict[str, Any] | None:
    if not container or not container.get("exists"):
        return None
    command = container.get("command")
    if not isinstance(command, list):
        return None
    runtime = _runtime_from_command([str(item) for item in command])
    return runtime if runtime.get("model_path") else None


def _runtime_from_env(env: dict[str, str]) -> dict[str, Any]:
    return {
        "source": "desired-env",
        "model_path": env.get("JARVIS_QWEN_MODEL_PATH", ""),
        "model_id": _model_id(env.get("JARVIS_QWEN_MODEL_PATH", "")),
        "served_model_name": env.get("JARVIS_QWEN_MODEL_NAME", ""),
        "dtype": env.get("JARVIS_QWEN_DTYPE", ""),
        "enforce_eager": bool(env.get("JARVIS_QWEN_ENFORCE_EAGER", "").strip()),
        "max_model_len": _int_or_none(env.get("JARVIS_QWEN_MAX_LEN")),
        "gpu_memory_utilization": _float_or_none(env.get("JARVIS_QWEN_GPU_UTIL")),
        "kv_cache_dtype": env.get("JARVIS_QWEN_KV_DTYPE", ""),
        "max_num_seqs": _int_or_none(env.get("JARVIS_QWEN_MAX_NUM_SEQS")),
        "cpu_offload_gb": _float_arg_value_from_string(
            env.get("JARVIS_QWEN_CPU_OFFLOAD_ARGS"),
            "cpu-offload-gb",
        ),
        "kv_offloading_gb": _int_arg_value_from_string(
            env.get("JARVIS_QWEN_KV_OFFLOAD_ARGS"),
            "kv-offloading-size",
        ),
        "kv_offloading_backend": _flag_value_from_string(
            env.get("JARVIS_QWEN_KV_OFFLOAD_ARGS"),
            "kv-offloading-backend",
        ),
        "tokenizer_mode": env.get("JARVIS_QWEN_TOKENIZER_MODE", ""),
        "safetensors_load_strategy": env.get("JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY", ""),
        "prefix_caching": True,
        "language_model_only": _bool_flag_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "language-model-only"
        ),
        "skip_mm_profiling": _bool_flag_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "skip-mm-profiling"
        ),
        "mm_processor_cache_gb": _float_arg_value_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "mm-processor-cache-gb"
        ),
        "max_num_batched_tokens": _int_arg_value_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "max-num-batched-tokens"
        ),
        "host": "0.0.0.0",
        "port": 8001,
    }


def _runtime_from_command(command: list[str]) -> dict[str, Any]:
    if len(command) == 1 and " --" in command[0]:
        command = [item for item in command[0].split() if item]
    flags = _parse_flags(command)
    model_path = str(flags.get("model") or "")
    return {
        "source": "container-command",
        "model_path": model_path,
        "model_id": _model_id(model_path),
        "served_model_name": str(flags.get("served-model-name") or ""),
        "dtype": str(flags.get("dtype") or ""),
        "enforce_eager": "enforce-eager" in flags,
        "max_model_len": _int_or_none(flags.get("max-model-len")),
        "gpu_memory_utilization": _float_or_none(flags.get("gpu-memory-utilization")),
        "kv_cache_dtype": str(flags.get("kv-cache-dtype") or ""),
        "max_num_seqs": _int_or_none(flags.get("max-num-seqs")),
        "cpu_offload_gb": _float_or_none(flags.get("cpu-offload-gb")),
        "kv_offloading_gb": _int_or_none(flags.get("kv-offloading-size")),
        "kv_offloading_backend": (str(flags.get("kv-offloading-backend") or "") or None),
        "tokenizer_mode": str(flags.get("tokenizer-mode") or ""),
        "safetensors_load_strategy": str(flags.get("safetensors-load-strategy") or ""),
        "prefix_caching": "enable-prefix-caching" in flags,
        "language_model_only": "language-model-only" in flags,
        "skip_mm_profiling": "skip-mm-profiling" in flags,
        "mm_processor_cache_gb": _float_or_none(flags.get("mm-processor-cache-gb")),
        "max_num_batched_tokens": _int_or_none(flags.get("max-num-batched-tokens")),
        "host": str(flags.get("host") or ""),
        "port": _int_or_none(flags.get("port")),
    }


def _runtime_mismatches(
    actual: dict[str, Any] | None,
    desired: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    mismatches: dict[str, dict[str, Any]] = {}
    for field in RUNTIME_COMPATIBILITY_FIELDS:
        actual_value = actual.get(field) if actual is not None else None
        desired_value = desired.get(field)
        if field == "model_path":
            actual_value = _normalized_model_path(actual_value)
            desired_value = _normalized_model_path(desired_value)
        if isinstance(actual_value, float) and isinstance(desired_value, float):
            matches = abs(actual_value - desired_value) <= 1e-9
        else:
            matches = actual_value == desired_value
        if not matches:
            mismatches[field] = {"actual": actual_value, "desired": desired_value}
    return mismatches


def _normalized_model_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/")


def _parse_flags(command: list[str]) -> dict[str, str | bool]:
    flags: dict[str, str | bool] = {}
    index = 0
    while index < len(command):
        token = command[index]
        if not token.startswith("--"):
            index += 1
            continue
        key = token[2:]
        if "=" in key:
            key, value = key.split("=", 1)
            flags[key] = value
            index += 1
            continue
        if index + 1 < len(command) and not command[index + 1].startswith("--"):
            flags[key] = command[index + 1]
            index += 2
            continue
        flags[key] = True
        index += 1
    return flags


def _environment_pairs(value: Any) -> dict[str, str]:
    pairs: dict[str, str] = {}
    if not isinstance(value, list):
        return pairs
    for item in value:
        key, separator, raw = str(item).partition("=")
        if separator and key:
            pairs[key] = raw
    return pairs


def _rollback_extra_args(command: list[str]) -> str:
    tokens = command
    if len(tokens) == 1 and " --" in tokens[0]:
        tokens = [item for item in tokens[0].split() if item]
    fixed_flags = {
        "model",
        "served-model-name",
        "dtype",
        "enforce-eager",
        "max-model-len",
        "gpu-memory-utilization",
        "kv-cache-dtype",
        "max-num-seqs",
        "cpu-offload-gb",
        "kv-offloading-size",
        "kv-offloading-backend",
        "tokenizer-mode",
        "safetensors-load-strategy",
        "enable-prefix-caching",
        "host",
        "port",
    }
    extra: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            index += 1
            continue
        key = token[2:].split("=", 1)[0]
        has_separate_value = (
            "=" not in token and index + 1 < len(tokens) and not tokens[index + 1].startswith("--")
        )
        if key not in fixed_flags:
            extra.append(token)
            if has_separate_value:
                extra.append(tokens[index + 1])
        index += 2 if has_separate_value else 1
    return " ".join(extra)


def _model_for_runtime(
    catalog: dict[str, Any],
    runtime: dict[str, Any] | None,
) -> dict[str, Any] | None:
    model_id = runtime.get("model_id") if runtime else None
    if not model_id:
        return None
    for model in catalog.get("models", []):
        if isinstance(model, dict) and model.get("id") == model_id:
            return model
    return None


def _model_id(path: str | None) -> str:
    if not path:
        return ""
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1]


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _int_arg_value_from_string(raw: str | None, key: str) -> int | None:
    if not raw:
        return None
    flags = _parse_flags([item for item in raw.split() if item])
    return _int_or_none(flags.get(key))


def _float_arg_value_from_string(raw: str | None, key: str) -> float | None:
    if not raw:
        return None
    flags = _parse_flags([item for item in raw.split() if item])
    return _float_or_none(flags.get(key))


def _bool_flag_from_string(raw: str | None, key: str) -> bool:
    if not raw:
        return False
    flags = _parse_flags([item for item in raw.split() if item])
    return key in flags


def _flag_value_from_string(raw: str | None, key: str) -> str | None:
    if not raw:
        return None
    flags = _parse_flags([item for item in raw.split() if item])
    value = flags.get(key)
    if value is True or value is None:
        return None
    text = str(value).strip()
    return text or None
