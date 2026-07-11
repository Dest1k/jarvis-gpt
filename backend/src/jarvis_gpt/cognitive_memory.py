from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .redaction import redact_text

HOST_PROFILE_SCHEMA = "jarvis.host-profile.v1"
PLAYBOOK_SCHEMA = "jarvis.execution-playbook.v1"
_MAX_PROFILE_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARS = 32_768
_MAX_LOOKUP_LIMIT = 20
_MAX_QUERY_TERMS = 12
_TOKEN_RE = re.compile(r"[\w.+#-]+", flags=re.UNICODE)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_text(value: object, *, field: str) -> str:
    text = " ".join(str(value).split()).strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > _MAX_TEXT_CHARS:
        raise ValueError(f"{field} exceeds {_MAX_TEXT_CHARS} characters")
    return text


def _normalize_text(value: object, *, field: str) -> tuple[str, str]:
    text = _bounded_text(unicodedata.normalize("NFKC", str(value)), field=field)
    return text, text.casefold()


def _safe_resolved_executable(name: str) -> str | None:
    located = shutil.which(name)
    if not located:
        return None
    try:
        path = Path(located).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return str(path) if path.is_file() else None


def _run_fixed_probe(executable: str, arguments: tuple[str, ...], *, timeout: float) -> str:
    """Run one fixed, non-shell inspection probe with strict time/output bounds."""
    path = _safe_resolved_executable(executable)
    if path is None:
        return ""
    try:
        completed = subprocess.run(  # noqa: S603 - canonical executable and fixed argv only
            [path, *arguments],
            capture_output=True,
            check=False,
            shell=False,
            timeout=max(0.1, min(timeout, 2.0)),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    output = completed.stdout[:65_536].decode("utf-8", errors="replace")
    if not output.strip():
        output = completed.stderr[:65_536].decode("utf-8", errors="replace")
    return output.strip()[:65_536]


def _memory_total_bytes() -> int | None:
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total_physical)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = pages * page_size
        return total if total > 0 else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _active_interfaces() -> list[dict[str, Any]]:
    """Return active interfaces without making an outbound network request."""
    try:
        import psutil  # type: ignore[import-not-found]

        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        result: list[dict[str, Any]] = []
        for name, state in stats.items():
            if not state.isup:
                continue
            interface_addresses = sorted(
                {
                    str(item.address).split("%", 1)[0]
                    for item in addresses.get(name, ())
                    if item.family in {socket.AF_INET, socket.AF_INET6} and item.address
                }
            )
            result.append(
                {
                    "name": str(name),
                    "addresses": interface_addresses[:32],
                    "is_up": True,
                }
            )
        return sorted(result, key=lambda item: item["name"].casefold())[:128]
    except Exception:  # optional psutil inspection must degrade to the stdlib fallback
        # Continue with the deterministic stdlib/sysfs collector below.
        result = []

    # Portable fallback: interface names plus locally resolved addresses. On Linux,
    # operstate lets us exclude known-down adapters; unknown states are retained.
    resolved_addresses: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            address = str(item[4][0]).split("%", 1)[0]
            if address:
                resolved_addresses.add(address)
    except OSError:
        resolved_addresses.clear()
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        interfaces = []
    result = []
    for index, name in interfaces[:128]:
        state = "unknown"
        operstate = Path("/sys/class/net") / name / "operstate"
        with suppress(OSError):
            state = operstate.read_text(encoding="ascii", errors="replace").strip().casefold()
        if state in {"down", "dormant", "notpresent", "lowerlayerdown"}:
            continue
        result.append(
            {
                "name": str(name),
                "index": int(index),
                "addresses": [],
                "is_up": state == "up" if state != "unknown" else None,
            }
        )
    if resolved_addresses:
        result.append(
            {
                "name": "host-resolver",
                "addresses": sorted(resolved_addresses)[:32],
                "is_up": True,
            }
        )
    return sorted(result, key=lambda item: item["name"].casefold())[:128]


_LINTERS = (
    "biome",
    "eslint",
    "flake8",
    "golangci-lint",
    "mypy",
    "pylint",
    "ruff",
    "shellcheck",
)
_COMPILERS = (
    "clang",
    "clang++",
    "cl",
    "cmake",
    "dotnet",
    "g++",
    "gcc",
    "go",
    "javac",
    "msbuild",
    "ninja",
    "rustc",
)


def _installed_tools(names: tuple[str, ...]) -> list[dict[str, str]]:
    tools: list[dict[str, str]] = []
    for name in names:
        path = _safe_resolved_executable(name)
        if path:
            tools.append({"name": name, "path": path})
    return sorted(tools, key=lambda item: item["name"])


def _accelerators(*, probe_timeout_sec: float) -> dict[str, Any]:
    gpu: list[dict[str, Any]] = []
    nvidia_output = _run_fixed_probe(
        "nvidia-smi",
        (
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ),
        timeout=probe_timeout_sec,
    )
    for line in nvidia_output.splitlines()[:16]:
        parts = [part.strip() for part in line.split(",")]
        if not parts or not parts[0]:
            continue
        item: dict[str, Any] = {"vendor": "nvidia", "name": parts[0][:512]}
        if len(parts) > 1 and parts[1]:
            item["driver_version"] = parts[1][:128]
        if len(parts) > 2:
            with suppress(ValueError):
                item["memory_mib"] = max(0, int(parts[2]))
        gpu.append(item)

    if os.name == "nt":
        video_controllers = _windows_cim_json(
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,DriverVersion,AdapterRAM | ConvertTo-Json -Compress",
            timeout=probe_timeout_sec,
        )
        known_names = {str(item.get("name") or "").casefold() for item in gpu}
        for item in video_controllers[:32]:
            name = str(item.get("Name") or "").strip()
            if not name or name.casefold() in known_names:
                continue
            lowered = name.casefold()
            vendor = (
                "nvidia"
                if "nvidia" in lowered
                else "amd"
                if "amd" in lowered or "radeon" in lowered
                else "intel"
                if "intel" in lowered
                else "unknown"
            )
            controller: dict[str, Any] = {"vendor": vendor, "name": name[:512]}
            driver = str(item.get("DriverVersion") or "").strip()
            if driver:
                controller["driver_version"] = driver[:128]
            try:
                memory_bytes = int(item.get("AdapterRAM") or 0)
            except (TypeError, ValueError):
                memory_bytes = 0
            if memory_bytes > 0:
                controller["memory_mib"] = memory_bytes // (1024 * 1024)
            gpu.append(controller)
            known_names.add(name.casefold())

    nvcc_path = _safe_resolved_executable("nvcc")
    cuda_roots: list[str] = []
    for name in ("CUDA_PATH", "CUDA_HOME"):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            root = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir() and str(root) not in cuda_roots:
            cuda_roots.append(str(root))
    cuda_roots.sort()
    nvcc_output = (
        _run_fixed_probe("nvcc", ("--version",), timeout=probe_timeout_sec) if nvcc_path else ""
    )
    version_match = re.search(r"\brelease\s+([0-9]+(?:\.[0-9]+)*)", nvcc_output, re.IGNORECASE)
    nvidia_gpu_present = any(
        str(item.get("vendor") or "").casefold() == "nvidia" for item in gpu
    )
    cuda = {
        "available": bool(nvidia_gpu_present or nvcc_path or cuda_roots),
        "nvcc_path": nvcc_path,
        "roots": cuda_roots[:8],
        "version": version_match.group(1) if version_match else None,
    }

    npu: list[dict[str, str]] = []
    for command in ("npu-smi", "intel_npu_top"):
        path = _safe_resolved_executable(command)
        if path:
            npu.append({"provider": command, "path": path})
    if os.name == "nt":
        devices = _windows_cim_json(
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.Name -match 'NPU|Neural Processing' } | "
            "Select-Object Name,Manufacturer,PNPDeviceID | ConvertTo-Json -Compress",
            timeout=probe_timeout_sec,
        )
        for item in devices[:32]:
            name = str(item.get("Name") or "").strip()
            if name:
                npu.append(
                    {
                        "provider": str(item.get("Manufacturer") or "windows-pnp")[:256],
                        "name": name[:512],
                        "device_id": str(item.get("PNPDeviceID") or "")[:512],
                    }
                )
    return {"gpu": gpu, "cuda": cuda, "npu": npu}


def _windows_cim_json(command: str, *, timeout: float) -> list[dict[str, Any]]:
    output = _run_fixed_probe(
        "powershell.exe",
        ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command),
        timeout=timeout,
    )
    if not output:
        return []
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return []
    values = value if isinstance(value, list) else [value]
    return [item for item in values if isinstance(item, dict)][:64]


def collect_host_facts(*, probe_timeout_sec: float = 0.8) -> dict[str, Any]:
    """Collect a bounded, best-effort host snapshot using only local inspection."""
    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "architecture": {
            "machine": platform.machine(),
            "bits": struct.calcsize("P") * 8,
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
        },
        "cpu": {
            "processor": platform.processor(),
            "logical_cores": os.cpu_count(),
        },
        "memory": {"total_bytes": _memory_total_bytes()},
        "accelerators": _accelerators(probe_timeout_sec=probe_timeout_sec),
        "active_network_interfaces": _active_interfaces(),
        "tools": {
            "linters": _installed_tools(_LINTERS),
            "compilers": _installed_tools(_COMPILERS),
            "python": str(Path(sys.executable).resolve(strict=False)),
        },
    }


def _validate_host_facts(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("host facts collector must return an object")
    facts = dict(value)
    required = {
        "os",
        "architecture",
        "cpu",
        "memory",
        "accelerators",
        "active_network_interfaces",
        "tools",
    }
    missing = sorted(required.difference(facts))
    if missing:
        raise ValueError(f"host facts are missing fields: {', '.join(missing)}")
    encoded = _canonical_json(facts)
    if len(encoded) > _MAX_PROFILE_BYTES:
        raise ValueError("host facts exceed the profile size limit")
    return json.loads(encoded.decode("utf-8"))


def _capability_fingerprint(facts: Mapping[str, Any]) -> str:
    stable_facts = {
        key: value for key, value in facts.items() if key != "active_network_interfaces"
    }
    payload = {"schema": HOST_PROFILE_SCHEMA, "host": stable_facts}
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _snapshot_fingerprint(facts: Mapping[str, Any]) -> str:
    payload = {"schema": HOST_PROFILE_SCHEMA, "host": dict(facts)}
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"refusing to replace symlink: {path}")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            with suppress(OSError):
                os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


class HostProfileManager:
    """Create the authoritative host_profile.json snapshot on every cold start."""

    def __init__(
        self,
        path: Path,
        *,
        collector: Callable[[], Mapping[str, Any]] | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self._collector = collector or collect_host_facts
        self._clock = clock

    def refresh(self) -> dict[str, Any]:
        facts = _validate_host_facts(self._collector())
        stable_payload = {"schema": HOST_PROFILE_SCHEMA, "host": facts}
        profile = {
            **stable_payload,
            "collected_at": self._clock(),
            "fingerprint_sha256": _capability_fingerprint(facts),
            "snapshot_sha256": _snapshot_fingerprint(facts),
        }
        _atomic_json_write(self.path, profile)
        return profile

    def load_verified(self, *, max_age_seconds: float | None = None) -> dict[str, Any] | None:
        try:
            if self.path.is_symlink() or self.path.stat().st_size > _MAX_PROFILE_BYTES:
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("schema") != HOST_PROFILE_SCHEMA:
            return None
        try:
            collected_at = datetime.fromisoformat(str(value.get("collected_at") or ""))
            if collected_at.tzinfo is None:
                return None
            age_seconds = (datetime.now(UTC) - collected_at.astimezone(UTC)).total_seconds()
            if age_seconds < -300:
                return None
            if max_age_seconds is not None and age_seconds > max(0.0, max_age_seconds):
                return None
        except (TypeError, ValueError):
            return None
        host = value.get("host")
        try:
            facts = _validate_host_facts(host)
        except (TypeError, ValueError):
            return None
        expected_capability = _capability_fingerprint(facts)
        expected_snapshot = _snapshot_fingerprint(facts)
        stored_snapshot = value.get("snapshot_sha256")
        if stored_snapshot is None:
            # Backward-compatible validation of pre-capability-fingerprint v1 files.
            if value.get("fingerprint_sha256") != expected_snapshot:
                return None
        elif (
            value.get("fingerprint_sha256") != expected_capability
            or stored_snapshot != expected_snapshot
        ):
            return None
        return value


@dataclass(frozen=True)
class PlaybookRecord:
    id: int
    schema: str
    fingerprint_sha256: str
    symptom: str
    solution: str
    verification: str
    success_count: int
    failure_count: int
    use_count: int
    confidence: float
    last_outcome: str
    created_at: str
    updated_at: str
    relevance: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field in ("symptom", "solution", "verification"):
            value[field] = redact_text(value[field])
        return value


class ExecutionPlaybookStore:
    """Thread-safe local store for reusable symptom -> solution -> verification lessons."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
            timeout=5.0,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS execution_playbooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema TEXT NOT NULL,
                fingerprint_sha256 TEXT NOT NULL UNIQUE,
                symptom TEXT NOT NULL,
                symptom_normalized TEXT NOT NULL,
                solution TEXT NOT NULL,
                solution_normalized TEXT NOT NULL,
                verification TEXT NOT NULL,
                verification_normalized TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0 CHECK(success_count >= 0),
                failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
                use_count INTEGER NOT NULL DEFAULT 0 CHECK(use_count >= 0),
                confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                last_outcome TEXT NOT NULL CHECK(last_outcome IN ('success', 'failure')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_execution_playbooks_rank
                ON execution_playbooks(confidence DESC, updated_at DESC);
            COMMIT;
            """
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("execution playbook store is closed")

    @staticmethod
    def _from_row(row: sqlite3.Row, *, relevance: float = 0.0) -> PlaybookRecord:
        return PlaybookRecord(
            id=int(row["id"]),
            schema=str(row["schema"]),
            fingerprint_sha256=str(row["fingerprint_sha256"]),
            symptom=redact_text(row["symptom"]),
            solution=redact_text(row["solution"]),
            verification=redact_text(row["verification"]),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
            use_count=int(row["use_count"]),
            confidence=float(row["confidence"]),
            last_outcome=str(row["last_outcome"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            relevance=float(relevance),
        )

    def record(
        self,
        *,
        symptom: str,
        solution: str,
        verification: str,
        outcome: Literal["success", "failure"],
    ) -> PlaybookRecord:
        if outcome not in {"success", "failure"}:
            raise ValueError("outcome must be 'success' or 'failure'")
        symptom_text, symptom_normalized = _normalize_text(
            redact_text(symptom), field="symptom"
        )
        solution_text, solution_normalized = _normalize_text(
            redact_text(solution), field="solution"
        )
        verification_text, verification_normalized = _normalize_text(
            redact_text(verification), field="verification"
        )
        digest = hashlib.sha256(
            "\x1f".join(
                (symptom_normalized, solution_normalized, verification_normalized)
            ).encode("utf-8")
        ).hexdigest()
        now = _utc_now()
        success_delta = int(outcome == "success")
        failure_delta = int(outcome == "failure")
        with self._lock:
            self._require_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    INSERT INTO execution_playbooks (
                        schema, fingerprint_sha256,
                        symptom, symptom_normalized,
                        solution, solution_normalized,
                        verification, verification_normalized,
                        success_count, failure_count, use_count,
                        confidence, last_outcome, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    ON CONFLICT(fingerprint_sha256) DO UPDATE SET
                        success_count = success_count + excluded.success_count,
                        failure_count = failure_count + excluded.failure_count,
                        confidence = (
                            success_count + excluded.success_count + 1.0
                        ) / (
                            success_count + excluded.success_count
                            + failure_count + excluded.failure_count + 2.0
                        ),
                        last_outcome = excluded.last_outcome,
                        updated_at = excluded.updated_at
                    """,
                    (
                        PLAYBOOK_SCHEMA,
                        digest,
                        symptom_text,
                        symptom_normalized,
                        solution_text,
                        solution_normalized,
                        verification_text,
                        verification_normalized,
                        success_delta,
                        failure_delta,
                        (success_delta + 1.0) / (success_delta + failure_delta + 2.0),
                        outcome,
                        now,
                        now,
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM execution_playbooks WHERE fingerprint_sha256 = ?", (digest,)
                ).fetchone()
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("failed to persist execution playbook")
        return self._from_row(row)

    def lookup(
        self,
        query: str,
        *,
        limit: int = 5,
        mark_used: bool = True,
    ) -> list[PlaybookRecord]:
        query_text = _bounded_text(query, field="query")
        bounded_limit = max(1, min(int(limit), _MAX_LOOKUP_LIMIT))
        terms: list[str] = []
        seen: set[str] = set()
        for token in _TOKEN_RE.findall(unicodedata.normalize("NFKC", query_text).casefold()):
            if token in seen:
                continue
            terms.append(token[:256])
            seen.add(token)
            if len(terms) >= _MAX_QUERY_TERMS:
                break

        with self._lock:
            self._require_open()
            if terms:
                score_parts: list[str] = []
                score_arguments: list[str] = []
                where_parts: list[str] = []
                where_arguments: list[str] = []
                for term in terms:
                    score_parts.append(
                        "(CASE WHEN instr(symptom_normalized, ?) > 0 THEN 4 ELSE 0 END + "
                        "CASE WHEN instr(solution_normalized, ?) > 0 THEN 2 ELSE 0 END + "
                        "CASE WHEN instr(verification_normalized, ?) > 0 THEN 1 ELSE 0 END)"
                    )
                    score_arguments.extend((term, term, term))
                    where_parts.append(
                        "(instr(symptom_normalized, ?) > 0 OR "
                        "instr(solution_normalized, ?) > 0 OR "
                        "instr(verification_normalized, ?) > 0)"
                    )
                    where_arguments.extend((term, term, term))
                score_sql = " + ".join(score_parts)
                rows = self._connection.execute(
                    f"""
                    SELECT *, ({score_sql}) AS relevance_score
                    FROM execution_playbooks
                    WHERE {" OR ".join(where_parts)}
                    ORDER BY relevance_score DESC, confidence DESC,
                             use_count DESC, updated_at DESC, id ASC
                    LIMIT ?
                    """,  # noqa: S608 - structure is generated only from fixed fragments
                    (*score_arguments, *where_arguments, bounded_limit),
                ).fetchall()
            else:  # pragma: no cover - non-empty text always yields a token
                rows = []
            if not rows:
                return []
            if mark_used:
                ids = [int(row["id"]) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._connection.execute(
                        f"UPDATE execution_playbooks SET use_count = use_count + 1 "
                        f"WHERE id IN ({placeholders})",  # noqa: S608 - placeholders only
                        ids,
                    )
                    self._connection.execute("COMMIT")
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
            return [
                replace(
                    self._from_row(row, relevance=float(row["relevance_score"])),
                    use_count=int(row["use_count"]) + int(mark_used),
                )
                for row in rows
            ]

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS entries,
                       COALESCE(SUM(success_count), 0) AS successes,
                       COALESCE(SUM(failure_count), 0) AS failures,
                       COALESCE(SUM(use_count), 0) AS uses
                FROM execution_playbooks
                """
            ).fetchone()
        assert row is not None
        return {
            "entries": int(row["entries"]),
            "successes": int(row["successes"]),
            "failures": int(row["failures"]),
            "uses": int(row["uses"]),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> ExecutionPlaybookStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
