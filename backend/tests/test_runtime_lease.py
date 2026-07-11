from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest
from jarvis_gpt.cli import _primary_runtime, _runtime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.runtime_lease import PrimaryRuntimeLease, RuntimeLeaseError
from jarvis_gpt.storage import JarvisStorage


def test_primary_runtime_lease_is_exclusive_and_crash_safe(tmp_path):
    path = tmp_path / "primary-runtime.lock"
    first = PrimaryRuntimeLease(path)
    second = PrimaryRuntimeLease(path)

    first.acquire()
    first.acquire()
    assert first.acquired
    with pytest.raises(RuntimeLeaseError, match="another Jarvis primary runtime"):
        second.acquire()

    first.release()
    first.release()
    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata["protocol"] == "jarvis.primary-runtime-lease.v1"
    assert metadata["pid"] == os.getpid()
    second.acquire()
    assert second.acquired
    second.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory flock hardening")
def test_primary_runtime_lease_survives_lockfile_unlink_and_recreate(tmp_path):
    path = tmp_path / "primary-runtime.lock"
    first = PrimaryRuntimeLease(path)
    second = PrimaryRuntimeLease(path)

    first.acquire()
    path.unlink()
    path.write_text("replacement", encoding="utf-8")
    with pytest.raises(RuntimeLeaseError, match="another Jarvis primary runtime"):
        second.acquire()

    first.release()
    second.acquire()
    second.release()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux abstract socket")
def test_primary_runtime_lease_survives_state_directory_replacement(tmp_path):
    state = tmp_path / "state"
    path = state / "primary-runtime.lock"
    first = PrimaryRuntimeLease(path)

    first.acquire()
    state.rename(tmp_path / "old-state")
    state.mkdir()
    second = PrimaryRuntimeLease(path)
    with pytest.raises(RuntimeLeaseError, match="another Jarvis primary runtime"):
        second.acquire()

    first.release()
    second.acquire()
    second.release()


def test_mutating_cli_fails_closed_while_api_primary_is_live(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    api_lease = PrimaryRuntimeLease(settings.state_dir / "primary-runtime.lock")
    api_lease.acquire()
    try:
        with (
            pytest.raises(SystemExit, match="API currently owns executive state"),
            _primary_runtime(),
        ):
            pytest.fail("mutating CLI acquired the API runtime lease")
    finally:
        api_lease.release()


def test_mutating_cli_acquires_lease_before_storage_migration(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    initial = JarvisStorage(settings.database_path)
    initial.initialize()
    initial.close()
    owner = PrimaryRuntimeLease(settings.state_dir / "primary-runtime.lock")
    owner.acquire()
    monkeypatch.setattr(
        "jarvis_gpt.cli.JarvisStorage.initialize",
        lambda _self: pytest.fail("storage migrated before primary lease acquisition"),
    )
    try:
        with (
            pytest.raises(SystemExit, match="API currently owns executive state"),
            _primary_runtime(),
        ):
            pytest.fail("second primary runtime acquired live state")
    finally:
        owner.release()


def test_read_only_cli_runtime_skips_migration_and_rejects_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    initial = JarvisStorage(settings.database_path)
    initial.initialize()
    initial.add_memory(content="read-only sentinel", namespace="tests")
    initial.close()
    monkeypatch.setattr(
        "jarvis_gpt.storage.MemoryVault.sync",
        lambda _self, _memories: pytest.fail("read-only runtime rewrote memory vault"),
    )

    _settings, storage, _llm, _agent = _runtime()
    try:
        assert storage.search_memory("sentinel")[0]["content"] == "read-only sentinel"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            storage.add_event(kind="forbidden", title="must not persist")
    finally:
        storage.close()


def test_mutating_cli_bootstraps_primary_executive_and_kernel_recovery(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": "b" * 64,
        "host": {},
    }
    monkeypatch.setattr(
        "jarvis_gpt.cli.HostProfileManager.refresh", lambda _self: profile
    )

    with _primary_runtime() as (_settings, storage, _llm, agent):
        assert agent.executive is not None
        assert storage.get_runtime_value("environment.host_profile", None) == profile
        assert agent.tools.execution.recovered_checkpoints == ()
