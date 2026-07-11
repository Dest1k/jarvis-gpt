from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import jarvis_gpt.cognitive_memory as memory_module
import pytest
from jarvis_gpt.cognitive_memory import (
    HOST_PROFILE_SCHEMA,
    ExecutionPlaybookStore,
    HostProfileManager,
)


def _facts() -> dict[str, object]:
    return {
        "os": {"system": "TestOS", "release": "1", "version": "1.2"},
        "architecture": {
            "machine": "test64",
            "bits": 64,
            "python_implementation": "CPython",
            "python_version": "3.11",
        },
        "cpu": {"processor": "Unit CPU", "logical_cores": 8},
        "memory": {"total_bytes": 1024},
        "accelerators": {
            "gpu": [{"vendor": "test", "name": "GPU"}],
            "cuda": {
                "available": False,
                "nvcc_path": None,
                "roots": [],
                "version": None,
            },
            "npu": [],
        },
        "active_network_interfaces": [
            {"name": "loopback", "addresses": ["127.0.0.1"], "is_up": True}
        ],
        "tools": {"linters": [], "compilers": [], "python": "/test/python"},
    }


def test_host_profile_refresh_is_atomic_verifiable_and_hash_is_stable(tmp_path):
    path = tmp_path / "state" / "host_profile.json"
    times = iter(("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"))
    manager = HostProfileManager(path, collector=_facts, clock=lambda: next(times))

    first = manager.refresh()
    second = manager.refresh()

    assert first["schema"] == HOST_PROFILE_SCHEMA
    assert first["fingerprint_sha256"] == second["fingerprint_sha256"]
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert first["collected_at"] != second["collected_at"]
    assert manager.load_verified() == second
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert not list(path.parent.glob("*.tmp"))


def test_host_capability_fingerprint_ignores_volatile_interface_addresses(tmp_path):
    path = tmp_path / "host_profile.json"
    facts = _facts()
    manager = HostProfileManager(path, collector=lambda: facts)
    first = manager.refresh()
    facts["active_network_interfaces"] = [
        {"name": "wifi", "addresses": ["2001:db8::1234"], "is_up": True}
    ]
    second = manager.refresh()

    assert first["fingerprint_sha256"] == second["fingerprint_sha256"]
    assert first["snapshot_sha256"] != second["snapshot_sha256"]
    assert manager.load_verified() == second


def test_host_profile_rejects_tampering_and_incomplete_collector(tmp_path):
    path = tmp_path / "host_profile.json"
    manager = HostProfileManager(path, collector=_facts)
    manager.refresh()
    path.write_text(path.read_text(encoding="utf-8").replace("Unit CPU", "Other CPU"))
    assert manager.load_verified() is None

    previous = path.read_bytes()
    incomplete = HostProfileManager(path, collector=lambda: {"os": {}})
    with pytest.raises(ValueError, match="missing fields"):
        incomplete.refresh()
    assert path.read_bytes() == previous


def test_host_profile_fallback_rejects_stale_snapshot(tmp_path):
    path = tmp_path / "host_profile.json"
    manager = HostProfileManager(
        path,
        collector=_facts,
        clock=lambda: "2000-01-01T00:00:00+00:00",
    )
    manager.refresh()

    assert manager.load_verified(max_age_seconds=60) is None


def test_intel_only_gpu_does_not_claim_cuda_capability(monkeypatch):
    monkeypatch.setattr(memory_module.os, "name", "nt")
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.delenv("CUDA_HOME", raising=False)
    monkeypatch.setattr(memory_module, "_safe_resolved_executable", lambda _name: None)
    monkeypatch.setattr(memory_module, "_run_fixed_probe", lambda *_args, **_kwargs: "")

    def fake_cim(command: str, *, timeout: float):
        del timeout
        if "Win32_VideoController" in command:
            return [
                {
                    "Name": "Intel Arc Graphics",
                    "DriverVersion": "1.2.3",
                    "AdapterRAM": 1024 * 1024 * 1024,
                }
            ]
        return []

    monkeypatch.setattr(memory_module, "_windows_cim_json", fake_cim)

    accelerators = memory_module._accelerators(probe_timeout_sec=0.1)

    assert accelerators["gpu"][0]["vendor"] == "intel"
    assert accelerators["cuda"]["available"] is False


def test_playbooks_normalize_deduplicate_score_and_count_usage(tmp_path):
    with ExecutionPlaybookStore(tmp_path / "memory" / "playbooks.sqlite3") as store:
        first = store.record(
            symptom="  Dependency   resolver CONFLICT ",
            solution="Pin package A to version 2",
            verification="Run dependency check",
            outcome="success",
        )
        second = store.record(
            symptom="dependency resolver conflict",
            solution="pin package a to version 2",
            verification="run dependency check",
            outcome="failure",
        )

        assert first.id == second.id
        assert second.success_count == 1
        assert second.failure_count == 1
        assert second.confidence == pytest.approx(0.5)
        assert store.stats() == {"entries": 1, "successes": 1, "failures": 1, "uses": 0}

        matches = store.lookup("resolver dependency", limit=999)
        assert len(matches) == 1
        assert matches[0].id == first.id
        assert matches[0].relevance > 0
        assert matches[0].use_count == 1
        assert store.stats()["uses"] == 1


def test_playbook_read_only_lookup_does_not_mutate_rank_and_redacts_secrets(tmp_path):
    with ExecutionPlaybookStore(tmp_path / "playbooks.sqlite3") as store:
        stored = store.record(
            symptom="proxy access_token=TOPSECRET OPENAI_API_KEY=OPENAIHIDDEN",
            solution=(
                "use postgresql://user:password@example.test and "
                "proxy_password=PROXYPASS client_secret: CLIENTHIDDEN"
            ),
            verification=(
                "Authorization: Token ABCDEFGHIJKLMNOP\n"
                "Proxy-Authorization: Digest username=user,response=DIGESTSECRET\n"
                "Cookie: session=COOKIESECRET\n"
                "HTTPError: Authorization: ApiKey EMBEDDEDAUTH\n"
                "request failed Cookie: sid=EMBEDDEDCOOKIE"
            ),
            outcome="success",
        )

        matches = store.lookup("proxy", mark_used=False)

        serialized = str(stored.to_dict()) + str(matches[0].to_dict())
        assert "TOPSECRET" not in serialized
        assert "OPENAIHIDDEN" not in serialized
        assert "password@example" not in serialized
        assert "PROXYPASS" not in serialized
        assert "CLIENTHIDDEN" not in serialized
        assert "ABCDEFGHIJKLMNOP" not in serialized
        assert "DIGESTSECRET" not in serialized
        assert "COOKIESECRET" not in serialized
        assert "EMBEDDEDAUTH" not in serialized
        assert "EMBEDDEDCOOKIE" not in serialized
        assert store.stats()["uses"] == 0


def test_playbooks_are_thread_safe_and_fail_closed_on_bad_input(tmp_path):
    store = ExecutionPlaybookStore(tmp_path / "playbooks.sqlite3")

    def record(_index: int) -> int:
        return store.record(
            symptom="compiler cannot find header",
            solution="install matching SDK",
            verification="compile minimal source",
            outcome="success",
        ).success_count

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(24)))

    assert store.stats() == {"entries": 1, "successes": 24, "failures": 0, "uses": 0}
    assert store.lookup("unrelated symptom") == []
    with pytest.raises(ValueError, match="outcome"):
        store.record(
            symptom="x", solution="y", verification="z", outcome="unknown"  # type: ignore[arg-type]
        )
    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.stats()


def test_playbooks_persist_across_store_restarts(tmp_path):
    path = tmp_path / "playbooks.sqlite3"
    first = ExecutionPlaybookStore(path)
    saved = first.record(
        symptom="service port is closed",
        solution="start the bounded service",
        verification="probe the TCP port",
        outcome="success",
    )
    first.close()

    with ExecutionPlaybookStore(path) as reopened:
        matches = reopened.lookup("service port")
        assert matches[0].fingerprint_sha256 == saved.fingerprint_sha256
        assert reopened.stats()["entries"] == 1
