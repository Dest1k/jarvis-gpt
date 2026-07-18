from __future__ import annotations

import asyncio
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from jarvis_gpt.agent import (
    OPERATOR_EFFECT_LEDGER_MAX_REQUESTS,
    AgentContext,
    AgentRuntime,
    _looks_like_document_memory_query,
    _native_action_from_message,
    _operator_action_scopes,
    _operator_effect_key,
    _operator_tool_arguments_match,
    _prune_operator_effect_ledger,
    _schema_hint,
    _tool_observation_excerpt,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.executive_runtime import ExecutiveCoordinator
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.llm import LLMRouter, LLMStreamChunk
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


@pytest.fixture(autouse=True)
def _disable_live_web_surfer_by_default(monkeypatch):
    """Keep routing tests independent of Playwright installed on the developer host."""

    monkeypatch.setattr("jarvis_gpt.agent._web_surfer_available", lambda: False)


def _tool_response(tool: str, ok: bool, summary: str, data: dict):
    return ToolRunResponse(tool=tool, ok=ok, summary=summary, data=data)


def _agent_without_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    return agent, storage


def _pending_native_request(storage: JarvisStorage):
    approvals = storage.list_approvals(limit=10, status="pending")
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["requested_action"] == "tool.run"
    assert approval["risk"] == "danger"
    assert approval["payload"]["tool"] == "windows.native"
    return approval, approval["payload"]["arguments"]


def _verified_host_action(calls: list[dict]):
    launched = {"executable": ""}

    async def fake_action(self, *, action, payload=None, timeout_sec=30):
        payload = dict(payload or {})
        calls.append({"action": action, "payload": payload, "timeout_sec": timeout_sec})
        if action == "wmi.query":
            name = Path(launched["executable"]).name.casefold()
            return {
                "ok": True,
                "summary": "verified",
                "data": {
                    "ok": True,
                    "summary": "verified",
                    "data": {"items": [{"ProcessId": 4242, "Name": name}]},
                },
            }
        launched["executable"] = str(payload.get("executable") or "")
        return {
            "ok": True,
            "summary": f"Native action {action} completed.",
            "data": {
                "ok": True,
                "summary": f"Native action {action} completed.",
                "pid": 4242,
            },
        }

    return fake_action


def _operator_tool_run(storage: JarvisStorage, tool: str):
    assert storage.list_approvals(limit=10, status="pending") == []
    runs = [run for run in storage.list_tool_runs() if run["tool"] == tool]
    assert len(runs) == 1
    return runs[0]


def test_operator_execution_effect_key_canonicalizes_explicit_defaults(tmp_path):
    target = tmp_path / "canonical.txt"
    minimal = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.write",
                "action_id": "first-id",
                "path": str(target),
                "content_base64": "aGVsbG8=",
            },
        }
    }
    explicit_defaults = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.write",
                "action_id": "second-id",
                "path": str(target),
                "content_base64": "aGVsbG8=",
                "create_parents": False,
                "require_absent": False,
                "expected_sha256": None,
                "mode": None,
            },
        },
        "session_id": None,
        "finalize_session": False,
        "safe_gate_token": None,
        "verification": {},
    }

    assert _operator_effect_key("execution.apply", minimal) == _operator_effect_key(
        "execution.apply", explicit_defaults
    )


def test_operator_execution_effect_key_ignores_verification_metadata(tmp_path):
    target = tmp_path / "verified.txt"
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "path": str(target),
            "content_base64": "aGVsbG8=",
        },
    }
    without_verification = {"payload": payload}
    same_path_verification = {
        "payload": payload,
        "verification": {"paths": [{"path": str(target)}]},
    }
    contradictory_verification = {
        "payload": payload,
        "verification": {"paths": [{"path": str(target), "exists": False}]},
    }

    baseline = _operator_effect_key("execution.apply", without_verification)
    assert baseline == _operator_effect_key("execution.apply", same_path_verification)
    assert baseline == _operator_effect_key("execution.apply", contradictory_verification)


def test_operator_effect_keys_canonicalize_windows_paths_but_preserve_posix_case():
    write_upper = {
        "path": r"C:\Temp\Case.txt",
        "content": "hello",
        "mode": "overwrite",
    }
    write_lower = {
        "path": "c:/temp/case.txt",
        "content": "hello",
        "mode": "overwrite",
    }
    assert _operator_effect_key("filesystem.write_text", write_upper) == _operator_effect_key(
        "filesystem.write_text", write_lower
    )
    assert _operator_effect_key(
        "filesystem.write_text",
        {**write_upper, "path": r"\\Server\Share\Notes.txt"},
    ) == _operator_effect_key(
        "filesystem.write_text",
        {**write_upper, "path": r"\\server/share/notes.txt"},
    )
    assert _operator_effect_key(
        "filesystem.write_text",
        {**write_upper, "path": "/tmp/Foo.txt"},
    ) != _operator_effect_key(
        "filesystem.write_text",
        {**write_upper, "path": "/tmp/foo.txt"},
    )

    copy_upper = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.copy",
                "source": r"C:\Data\Source.txt",
                "destination": r"\\Server\Share\Target.txt",
            },
        }
    }
    copy_lower = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.copy",
                "source": "c:/data/source.txt",
                "destination": r"\\server/share/target.txt",
            },
        }
    }
    assert _operator_effect_key("execution.apply", copy_upper) == _operator_effect_key(
        "execution.apply", copy_lower
    )

    process_upper = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "process.run",
                "executable": r"C:\Tools\Runner.exe",
                "arguments": [r"C:\Input\Data.txt", "--quiet"],
                "cwd": r"C:\Work",
                "observe_paths": [r"\\Server\Share\Observed"],
            },
        }
    }
    process_lower = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "process.run",
                "executable": "c:/tools/runner.exe",
                "arguments": ["c:/input/data.txt", "--quiet"],
                "cwd": "c:/work",
                "observe_paths": [r"\\server/share/observed"],
            },
        }
    }
    assert _operator_effect_key("execution.apply", process_upper) == _operator_effect_key(
        "execution.apply", process_lower
    )

    posix_upper = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.write",
                "path": "/tmp/Foo.txt",
                "content_base64": "aGVsbG8=",
            },
        }
    }
    posix_lower = json.loads(json.dumps(posix_upper))
    posix_lower["payload"]["action"]["path"] = "/tmp/foo.txt"
    assert _operator_effect_key("execution.apply", posix_upper) != _operator_effect_key(
        "execution.apply", posix_lower
    )


def test_operator_execution_rejects_unrequested_effect_flags(tmp_path):
    target = tmp_path / "exact.txt"
    message = f"Create file {target} and write hello"
    scopes = _operator_action_scopes(message)
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "path": str(target),
            "content_base64": "aGVsbG8=",
        },
    }

    assert _operator_tool_arguments_match(
        "execution.apply", {"payload": payload}, message=message, scopes=scopes
    )
    escalated = json.loads(json.dumps(payload))
    escalated["action"]["mode"] = 0o777
    assert not _operator_tool_arguments_match(
        "execution.apply", {"payload": escalated}, message=message, scopes=scopes
    )
    assert not _operator_tool_arguments_match(
        "execution.apply",
        {"payload": payload, "finalize_session": True},
        message=message,
        scopes=scopes,
    )


def test_operator_gui_and_browser_authority_rejects_extra_operands():
    open_message = "Open example.com"
    open_scopes = _operator_action_scopes(open_message)
    assert _operator_tool_arguments_match(
        "browser.open",
        {"url": "https://example.com"},
        message=open_message,
        scopes=open_scopes,
    )
    assert not _operator_tool_arguments_match(
        "browser.open",
        {"url": "https://example.com/unrequested"},
        message=open_message,
        scopes=open_scopes,
    )

    native_message = "type hello into active window"
    native_scopes = _operator_action_scopes(native_message)
    expected = _native_action_from_message(native_message)
    assert expected is not None
    exact_native = {"action": expected.action, "payload": expected.payload, "timeout_sec": 30}
    assert _operator_tool_arguments_match(
        "windows.native", exact_native, message=native_message, scopes=native_scopes
    )
    targeted_native = json.loads(json.dumps(exact_native))
    targeted_native["payload"]["process_name"] = "KeePass.exe"
    assert not _operator_tool_arguments_match(
        "windows.native", targeted_native, message=native_message, scopes=native_scopes
    )

    process_message = "открой топ 10 процессов в консоли"
    process_scopes = _operator_action_scopes(process_message)
    exact_process_view = {
        "action": "console.show_processes",
        "payload": {"limit": 10, "sort": "cpu"},
        "timeout_sec": 30,
    }
    assert _operator_tool_arguments_match(
        "windows.native",
        exact_process_view,
        message=process_message,
        scopes=process_scopes,
    )
    escalated_process_view = json.loads(json.dumps(exact_process_view))
    escalated_process_view["payload"]["command"] = "whoami"
    assert not _operator_tool_arguments_match(
        "windows.native",
        escalated_process_view,
        message=process_message,
        scopes=process_scopes,
    )

    browser_message = "type hello into search in browser at https://example.com"
    browser_scopes = _operator_action_scopes(browser_message)
    exact_browser = {
        "url": "https://example.com",
        "target": "search",
        "text": "hello",
    }
    assert _operator_tool_arguments_match(
        "browser.type", exact_browser, message=browser_message, scopes=browser_scopes
    )
    for extra in (
        {"allow_sensitive": True},
        {"debug_url": "http://127.0.0.1:9555"},
        {"wait_ms": 30000},
    ):
        assert not _operator_tool_arguments_match(
            "browser.type",
            {**exact_browser, **extra},
            message=browser_message,
            scopes=browser_scopes,
        )

    chrome_message = "Open Chrome"
    chrome_scopes = _operator_action_scopes(chrome_message)
    assert _operator_tool_arguments_match(
        "browser.chrome.launch", {}, message=chrome_message, scopes=chrome_scopes
    )
    assert not _operator_tool_arguments_match(
        "browser.chrome.launch",
        {"start_url": "https://example.com", "debug_port": 9555},
        message=chrome_message,
        scopes=chrome_scopes,
    )


def test_operator_url_authority_requires_exact_scheme_host_and_path():
    message = "Open example.com"
    scopes = _operator_action_scopes(message)
    for substituted in (
        "http://example.com",
        "https://notexample.com",
        "https://example.com.evil.test",
        "https://safe-example.com/path",
        "https://example.com/unrequested",
    ):
        assert not _operator_tool_arguments_match(
            "browser.open",
            {"url": substituted},
            message=message,
            scopes=scopes,
        )

    explicit_http = "Open http://bank.example"
    assert not _operator_tool_arguments_match(
        "browser.open",
        {"url": "https://bank.example"},
        message=explicit_http,
        scopes=_operator_action_scopes(explicit_http),
    )

    wiki_message = "Открой Википедию"
    wiki_scopes = _operator_action_scopes(wiki_message)
    assert _operator_tool_arguments_match(
        "browser.open",
        {"url": "https://ru.wikipedia.org/wiki/Заглавная_страница"},
        message=wiki_message,
        scopes=wiki_scopes,
    )
    for substituted in (
        "https://wikipedia.org.evil.example/phish",
        "https://evilwikipedia.org/phish",
        "https://ru.wikipedia.org/wiki/Special:Random",
    ):
        assert not _operator_tool_arguments_match(
            "browser.open",
            {"url": substituted},
            message=wiki_message,
            scopes=wiki_scopes,
        )


@pytest.mark.parametrize(
    ("message", "tool", "arguments", "scope"),
    [
        (
            "Click Search at https://example.com",
            "browser.click",
            {"url": "https://example.com", "target": "Search"},
            "click",
        ),
        (
            "Type hello into Search at https://example.com",
            "browser.type",
            {"url": "https://example.com", "target": "Search", "text": "hello"},
            "type",
        ),
        (
            "Scroll down at https://example.com",
            "browser.scroll",
            {"url": "https://example.com", "direction": "down"},
            "scroll",
        ),
        (
            "Take screenshot at https://example.com",
            "browser.screenshot",
            {"url": "https://example.com"},
            "capture",
        ),
    ],
)
def test_url_only_browser_commands_get_action_specific_authority(
    message,
    tool,
    arguments,
    scope,
):
    scopes = _operator_action_scopes(message)
    assert {"explicit", "browser", scope} <= scopes
    assert _operator_tool_arguments_match(
        tool,
        arguments,
        message=message,
        scopes=scopes,
    )


def test_unrelated_explicit_scope_cannot_authorize_browser_action():
    message = "Delete Search on https://example.com"
    scopes = _operator_action_scopes(message)
    assert "delete" in scopes and "click" not in scopes
    assert not _operator_tool_arguments_match(
        "browser.click",
        {"url": "https://example.com", "target": "Search"},
        message=message,
        scopes=scopes,
    )


def test_posix_path_authority_is_case_sensitive_and_not_prefix_based():
    message = "Create empty file /tmp/Foo.txt"
    scopes = _operator_action_scopes(message)
    exact = {"path": "/tmp/Foo.txt", "content": "", "mode": "create"}
    assert _operator_tool_arguments_match(
        "filesystem.write_text",
        exact,
        message=message,
        scopes=scopes,
    )
    for path in ("/tmp/foo.txt", "/tmp/Foo.tx"):
        assert not _operator_tool_arguments_match(
            "filesystem.write_text",
            {**exact, "path": path},
            message=message,
            scopes=scopes,
        )


def test_operator_effect_keys_canonicalize_browser_and_native_defaults():
    assert _operator_effect_key(
        "browser.click",
        {"url": "https://www.EXAMPLE.com/", "target": "Search"},
    ) == _operator_effect_key(
        "browser.click",
        {
            "url": "https://example.com",
            "target": "Search",
            "wait_ms": 5000,
            "debug_url": "http://127.0.0.1:9222",
        },
    )
    assert _operator_effect_key("browser.chrome.launch", {}) == _operator_effect_key(
        "browser.chrome.launch",
        {"debug_port": 9222},
    )
    assert _operator_effect_key(
        "browser.open_many",
        {"urls": ["https://a.example", "https://b.example"]},
    ) == _operator_effect_key(
        "browser.open_many",
        {"urls": ["https://b.example/", "https://www.A.EXAMPLE"]},
    )
    native_minimal = {
        "action": "app.open_and_type",
        "payload": {
            "executable": "notepad.exe",
            "text": "hello",
            "process_name": "notepad.exe",
            "window_title": "notes.txt",
        },
    }
    native_explicit = json.loads(json.dumps(native_minimal))
    native_explicit["payload"].update(
        {
            "executable": "NOTEPAD.EXE",
            "process_name": "NOTEPAD.EXE",
            "window_title": "NOTES.TXT",
            "wait_ms": 900,
        }
    )
    native_explicit["timeout_sec"] = 30
    assert _operator_effect_key("windows.native", native_minimal) == _operator_effect_key(
        "windows.native",
        native_explicit,
    )


def test_operator_effect_ledger_prunes_expired_completions_and_is_bounded():
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    requests = {
        **{
            f"expired-{index}": {
                "status": "completed",
                "completed_at": (now - timedelta(hours=1)).isoformat(),
                "updated_at": (now - timedelta(hours=1)).isoformat(),
                "effects": {f"effect-{index}": {}},
            }
            for index in range(80)
        },
        "recent": {
            "status": "completed",
            "completed_at": (now - timedelta(minutes=10)).isoformat(),
            "updated_at": (now - timedelta(minutes=10)).isoformat(),
            "effects": {"recent-effect": {}},
        },
        "unfinished": {
            "status": "incomplete",
            "started_at": (now - timedelta(days=2)).isoformat(),
            "updated_at": (now - timedelta(days=2)).isoformat(),
            "effects": {"unfinished-effect": {}},
        },
    }
    ledger = {
        "protocol": "jarvis.operator-effect-ledger.v1",
        "conversation_id": "conv_test",
        "requests": requests,
    }

    pruned = _prune_operator_effect_ledger(
        ledger,
        conversation_id="conv_test",
        now=now,
    )

    assert set(pruned["requests"]) == {"recent", "unfinished"}

    overflow = {
        **ledger,
        "requests": {
            f"pending-{index}": {
                "status": "incomplete",
                "started_at": (now - timedelta(seconds=index)).isoformat(),
                "updated_at": (now - timedelta(seconds=index)).isoformat(),
                "effects": {f"effect-{index}": {}},
            }
            for index in range(OPERATOR_EFFECT_LEDGER_MAX_REQUESTS + 20)
        },
    }
    bounded = _prune_operator_effect_ledger(
        overflow,
        conversation_id="conv_test",
        now=now,
    )
    assert len(bounded["requests"]) == OPERATOR_EFFECT_LEDGER_MAX_REQUESTS
    assert bounded["overflowed"] is True


def test_operator_effect_ledger_claim_is_atomic_for_concurrent_conversation_requests(
    monkeypatch,
    tmp_path,
):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("concurrent idempotency")
    contexts: list[AgentContext] = []
    for index, digest in enumerate(("same-request", "same-request", "other-request")):
        context = AgentContext(
            conversation_id=conversation_id,
            memory_hits=[],
            file_hits=[],
            operator_message="Click Search at https://example.com",
            operator_scopes=frozenset({"explicit", "browser", "click"}),
            operator_request_digest=digest,
        )
        context.operator_message_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"request {index}",
        )
        contexts.append(context)

    barrier = Barrier(len(contexts))

    def claim(context: AgentContext) -> bool:
        barrier.wait()
        return agent._begin_operator_effect(
            context,
            tool="browser.click",
            effect_key="canonical-effect",
        )

    with ThreadPoolExecutor(max_workers=len(contexts)) as executor:
        acquired = list(executor.map(claim, contexts))

    assert sum(acquired) == 2
    ledger = storage.list_runtime_values(prefix="agent.operator_effect.")[0]["value"]
    assert set(ledger["requests"]) == {"same-request", "other-request"}
    storage.close()


def test_browser_effect_key_canonicalizes_typed_control_spellings_but_matcher_is_strict():
    numeric = {
        "url": "https://example.com",
        "target": "Search",
        "wait_ms": 5000,
    }
    string_numeric = {**numeric, "wait_ms": "5000"}
    assert _operator_effect_key("browser.click", numeric) == _operator_effect_key(
        "browser.click",
        string_numeric,
    )

    message = "Click Search at https://example.com and wait 5000 ms"
    scopes = _operator_action_scopes(message)
    assert _operator_tool_arguments_match(
        "browser.click",
        numeric,
        message=message,
        scopes=scopes,
    )
    assert not _operator_tool_arguments_match(
        "browser.click",
        string_numeric,
        message=message,
        scopes=scopes,
    )
    assert not _operator_tool_arguments_match(
        "browser.click",
        {**numeric, "wait_ms": True},
        message=message,
        scopes=scopes,
    )

    scroll_message = "Scroll down at https://example.com"
    scroll_scopes = _operator_action_scopes(scroll_message)
    scroll = {
        "url": "https://example.com",
        "direction": "down",
        "wait_ms": 5000,
        "pixels": 900,
        "passes": 3,
        "max_chars": 9000,
    }
    assert _operator_tool_arguments_match(
        "browser.scroll",
        scroll,
        message=scroll_message,
        scopes=scroll_scopes,
    )
    for field in ("wait_ms", "pixels", "passes", "max_chars"):
        string_control = {**scroll, field: str(scroll[field])}
        assert _operator_effect_key("browser.scroll", scroll) == _operator_effect_key(
            "browser.scroll",
            string_control,
        )
        assert not _operator_tool_arguments_match(
            "browser.scroll",
            string_control,
            message=scroll_message,
            scopes=scroll_scopes,
        )

    typed = {
        "url": "https://example.com",
        "target": "Password",
        "text": "secret",
        "allow_sensitive": False,
    }
    assert _operator_effect_key("browser.type", typed) == _operator_effect_key(
        "browser.type",
        {**typed, "allow_sensitive": "false"},
    )
    sensitive_message = (
        "Type secret into Password at https://example.com; this is sensitive"
    )
    sensitive_scopes = _operator_action_scopes(sensitive_message)
    assert not _operator_tool_arguments_match(
        "browser.type",
        {**typed, "allow_sensitive": "false"},
        message=sensitive_message,
        scopes=sensitive_scopes,
    )

    chrome_message = "Open Chrome on debug port 9222"
    chrome_scopes = _operator_action_scopes(chrome_message)
    assert _operator_effect_key(
        "browser.chrome.launch", {"debug_port": 9222}
    ) == _operator_effect_key("browser.chrome.launch", {"debug_port": "9222"})
    assert not _operator_tool_arguments_match(
        "browser.chrome.launch",
        {"debug_port": "9222"},
        message=chrome_message,
        scopes=chrome_scopes,
    )


def test_exact_operand_matcher_rejects_path_substrings_and_text_prefixes(tmp_path):
    process_message = "Execute /tmp/rm_payload.sh"
    process_scopes = _operator_action_scopes(process_message)
    process_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "process.run",
            "executable": "rm",
            "arguments": [],
        },
    }
    assert not _operator_tool_arguments_match(
        "execution.apply",
        {"payload": process_payload},
        message=process_message,
        scopes=process_scopes,
    )

    exact_process_message = "Execute rm /tmp/old.txt"
    exact_process_scopes = _operator_action_scopes(exact_process_message)
    exact_process_payload = json.loads(json.dumps(process_payload))
    exact_process_payload["action"]["arguments"] = ["/tmp/old.txt"]
    assert _operator_tool_arguments_match(
        "execution.apply",
        {"payload": exact_process_payload},
        message=exact_process_message,
        scopes=exact_process_scopes,
    )

    path = tmp_path / "prefix.txt"
    for content_message in (
        f"Create file {path} and write hello world",
        f'Create file {path} and write "hello world"',
    ):
        content_scopes = _operator_action_scopes(content_message)
        assert not _operator_tool_arguments_match(
            "filesystem.write_text",
            {"path": str(path), "content": "hello", "mode": "overwrite"},
            message=content_message,
            scopes=content_scopes,
        )
        assert _operator_tool_arguments_match(
            "filesystem.write_text",
            {"path": str(path), "content": "hello world", "mode": "overwrite"},
            message=content_message,
            scopes=content_scopes,
        )

    for target_message in (
        "Click Search settings at https://example.com",
        'Click "Search settings" at https://example.com',
    ):
        target_scopes = _operator_action_scopes(target_message)
        assert not _operator_tool_arguments_match(
            "browser.click",
            {"url": "https://example.com", "target": "Search"},
            message=target_message,
            scopes=target_scopes,
        )
        assert _operator_tool_arguments_match(
            "browser.click",
            {"url": "https://example.com", "target": "Search settings"},
            message=target_message,
            scopes=target_scopes,
        )


@pytest.mark.parametrize(
    "message",
    [
        "Move file /tmp/source.txt to /tmp/destination.txt",
        "Перемести файл /tmp/source.txt в /tmp/destination.txt",
    ],
)
def test_operator_execution_binds_source_and_destination_roles(message):
    scopes = _operator_action_scopes(message)

    def move(source: str, destination: str) -> dict:
        return {
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "kind": "fs.move",
                    "source": source,
                    "destination": destination,
                },
            }
        }

    assert _operator_tool_arguments_match(
        "execution.apply",
        move("/tmp/source.txt", "/tmp/destination.txt"),
        message=message,
        scopes=scopes,
    )
    assert not _operator_tool_arguments_match(
        "execution.apply",
        move("/tmp/destination.txt", "/tmp/source.txt"),
        message=message,
        scopes=scopes,
    )


def test_operator_browser_binds_quoted_text_and_target_roles():
    message = 'Type "hello" into "Search" at https://example.com'
    scopes = _operator_action_scopes(message)

    assert _operator_tool_arguments_match(
        "browser.type",
        {"url": "https://example.com", "target": "Search", "text": "hello"},
        message=message,
        scopes=scopes,
    )
    assert not _operator_tool_arguments_match(
        "browser.type",
        {"url": "https://example.com", "target": "hello", "text": "Search"},
        message=message,
        scopes=scopes,
    )


def test_operator_open_many_rejects_duplicate_targets():
    message = "Open https://example.com and https://www.EXAMPLE.com/"
    scopes = _operator_action_scopes(message)
    assert not _operator_tool_arguments_match(
        "browser.open_many",
        {"urls": ["https://example.com", "https://www.EXAMPLE.com/"]},
        message=message,
        scopes=scopes,
    )


def test_operator_open_many_requires_complete_explicit_url_set():
    message = "Open https://a.example/path and https://b.example/other"
    scopes = _operator_action_scopes(message)
    exact = ["https://a.example/path", "https://b.example/other"]

    assert _operator_tool_arguments_match(
        "browser.open_many",
        {"urls": exact},
        message=message,
        scopes=scopes,
    )
    assert _operator_tool_arguments_match(
        "browser.open_many",
        {"urls": list(reversed(exact))},
        message=message,
        scopes=scopes,
    )
    assert not _operator_tool_arguments_match(
        "browser.open_many",
        {"urls": exact[:1]},
        message=message,
        scopes=scopes,
    )


@pytest.mark.parametrize(
    "message",
    [
        "а открой калькулятор и посчитай там что-нибудь",
        "а консоль открой с топ 10 процессов",
        "консоль открой с топ 10 процессов",
        "ну давай запусти блокнот",
        "ok, open the calculator",
    ],
)
def test_operator_command_survives_leading_filler_and_word_order(message):
    """Conversational lead-ins and object-verb inversion still read as commands."""
    scopes = _operator_action_scopes(message)
    assert "explicit" in scopes
    assert "native" in scopes


@pytest.mark.parametrize(
    "message",
    [
        "how do I open the calculator?",
        "объясни как открыть калькулятор",
        "не открывай калькулятор",
        "расскажи про блокнот",
    ],
)
def test_questions_and_retractions_are_not_operator_commands(message):
    assert _operator_action_scopes(message) == frozenset()


def test_full_autonomy_never_authorizes_scope_matched_tool_without_exact_operands():
    """The legacy flag cannot turn a broad scope into operand authority."""
    message = "а консоль открой с топ 10 процессов"
    scopes = _operator_action_scopes(message)
    exact_args = {
        "action": "console.show_processes",
        "payload": {"limit": 10, "sort": "cpu"},
        "timeout_sec": 30,
    }
    loose_args = {
        "action": "console.show_processes",
        "payload": {"limit": 10, "sort": "cpu", "extra": "whatever"},
        "timeout_sec": 30,
    }
    assert _operator_tool_arguments_match(
        "windows.native",
        exact_args,
        message=message,
        scopes=scopes,
        full_autonomy=True,
    )
    assert not _operator_tool_arguments_match(
        "windows.native", loose_args, message=message, scopes=scopes
    )
    assert not _operator_tool_arguments_match(
        "windows.native",
        loose_args,
        message=message,
        scopes=scopes,
        full_autonomy=True,
    )


def test_full_autonomy_still_rejects_tool_outside_requested_scope():
    """The compatibility flag does not grant unrelated tool authority."""
    message = "посмотри топ 3 процессов по памяти"
    scopes = _operator_action_scopes(message)
    # A read-only inspection turn must not authorize a filesystem mutation tool
    # even under full autonomy — that scope was never named.
    write_args = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.write",
                "path": "C:/tmp/should-not-run.txt",
                "content_base64": "aGk=",
            },
        }
    }
    assert not _operator_tool_arguments_match(
        "execution.apply",
        write_args,
        message=message,
        scopes=scopes,
        full_autonomy=True,
    )


def test_exact_current_turn_native_command_executes_without_approval(monkeypatch, tmp_path):
    """Filler-led explicit commands still execute after exact operand matching."""
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )

    for message in ("а открой калькулятор", "консоль открой с топ 10 процессов"):
        response = asyncio.run(agent.chat(message))
        assert all(event.type != "approval" for event in response.events), message

    # No approval was ever raised, and both explicit commands reached the host
    # tool directly instead of being parked behind an apr_ gate.
    assert storage.list_approvals(limit=10) == []
    native_runs = [run for run in storage.list_tool_runs() if run["tool"] == "windows.native"]
    assert len(native_runs) == 2
    dispatched = {call["action"] for call in calls}
    assert "process.start" in dispatched
    assert "console.show_processes" in dispatched
    storage.close()


def test_legacy_full_autonomy_setting_remains_parseable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "0")
    settings = load_settings()
    assert settings.operator_full_autonomy is False
    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "1")
    assert load_settings().operator_full_autonomy is True


@pytest.mark.parametrize(
    "message",
    [
        "посчитай 2+2 в калькуляторе",
        "в калькуляторе посчитай 2+2",
        "вычисли 15*3 в калькуляторе",
        "а в калькуляторе то посчитаешь?",
        "можешь посчитать 2+2 в калькуляторе",
        "откроешь блокнот?",
        "запустишь калькулятор?",
    ],
)
def test_calculation_and_question_forms_grant_native_authority(message):
    """Compute verbs and 2nd-person request forms authorize the native app tool."""
    from jarvis_gpt.agent import _operator_requested_tool_names

    scopes = _operator_action_scopes(message)
    assert "explicit" in scopes
    assert "windows.native" in _operator_requested_tool_names(scopes)


@pytest.mark.parametrize(
    "message",
    [
        "не открывай калькулятор",
        "не запускай блокнот",
        "не посчитаешь ли ты мои убытки сам",
        "don't open the calculator",
    ],
)
def test_negated_requests_are_not_operator_commands(message):
    assert _operator_action_scopes(message) == frozenset()


def test_calculator_followup_computes_without_approval(monkeypatch, tmp_path):
    """Screenshot regression: the calculator follow-up must not fabricate an approval."""
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )

    for message in (
        "открой калькулятор",
        "а в калькуляторе то посчитаешь 2+2?",
    ):
        response = asyncio.run(agent.chat(message))
        assert all(event.type != "approval" for event in response.events), message

    assert storage.list_approvals(limit=10) == []
    native_runs = [run for run in storage.list_tool_runs() if run["tool"] == "windows.native"]
    assert len(native_runs) == 2
    storage.close()


def test_agent_creates_mission_from_large_goal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(
        agent.chat(
            "Сделай проект с нуля: полностью переосмысли архитектуру, реализуй runtime, "
            "память, диагностику, web интерфейс и mission plan для локального Jarvis.",
            mode="auto",
        )
    )

    assert response.mission_id is not None
    assert "mission plan" in response.answer
    assert storage.counters()["mission_tasks"] >= 4
    mission = storage.get_mission(response.mission_id)
    task_titles = [task["title"] for task in mission["tasks"]]
    assert any("Command Center" in title for title in task_titles)
    storage.close()


def test_agent_forwards_reserved_mission_id_through_planned_fallback(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    reserved_id = "mis_reserved_agent_1234"

    mission = asyncio.run(
        agent.create_mission_planned(
            "Build a reliable local runtime",
            mission_id=reserved_id,
        )
    )

    assert mission["id"] == reserved_id
    assert storage.get_mission(reserved_id) is not None
    storage.close()


def test_agent_executes_next_mission_step(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    mission = agent.create_mission("Build tools runtime")

    result = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    refreshed = storage.get_mission(mission["id"])
    runs = storage.list_tool_runs()

    assert result.result.ok is False
    assert result.result.data["executor_unavailable"] is True
    assert result.result.data["state_changed"] is False
    assert result.task is None
    assert refreshed is not None
    assert refreshed["progress"] == 0
    assert all(task["status"] == "pending" for task in refreshed["tasks"])
    assert runs == []
    storage.close()


def test_run_mission_chains_all_steps_offline(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    mission = agent.create_mission("Build tools runtime")
    task_count = len(mission["tasks"])

    run = asyncio.run(agent.run_mission(mission["id"], max_steps=task_count))
    refreshed = storage.get_mission(mission["id"])

    assert run.completed is False
    assert run.stopped_reason == "blocked"
    assert run.executed_steps == 1
    assert all(task["status"] == "pending" for task in refreshed["tasks"])
    assert refreshed["progress"] == 0
    storage.close()


def test_run_mission_respects_step_budget(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    mission = agent.create_mission("Build tools runtime")
    assert len(mission["tasks"]) > 1

    run = asyncio.run(agent.run_mission(mission["id"], max_steps=1))

    assert run.executed_steps == 1
    assert run.completed is False
    assert run.stopped_reason == "blocked"
    storage.close()


def test_concurrent_mission_execution_claims_only_one_step(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    agent.settings = replace(agent.settings, llm_enabled=True)
    mission = agent.create_mission("Build tools runtime")

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_run(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return (
                ToolRunResponse(
                    tool="mission.execute_next",
                    ok=False,
                    summary="Synthetic failed step after concurrency observation.",
                ),
                None,
            )

        monkeypatch.setattr(agent, "_execute_mission_step_agentic", slow_run)
        first_task = asyncio.create_task(agent.execute_next_mission_step(mission["id"]))
        await entered.wait()
        competing = await agent.execute_next_mission_step(mission["id"])
        during = storage.get_mission(mission["id"])
        release.set()
        first = await first_task
        return first, competing, during

    first, competing, during = asyncio.run(scenario())

    assert first.result.ok is False
    assert competing.result.ok is False
    assert competing.result.data["busy"] is True
    assert competing.task is None
    assert sum(task["status"] == "running" for task in during["tasks"]) == 1
    assert sum(task["status"] == "pending" for task in during["tasks"]) >= 1
    storage.close()


def test_cancelled_mission_step_does_not_remain_running(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    agent.settings = replace(agent.settings, llm_enabled=True)
    mission = agent.create_mission("Build tools runtime")

    async def scenario():
        entered = asyncio.Event()

        async def never_finishes(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(agent, "_execute_mission_step_agentic", never_finishes)
        execution = asyncio.create_task(agent.execute_next_mission_step(mission["id"]))
        await entered.wait()
        execution.cancel()
        try:
            await execution
        except asyncio.CancelledError:
            return
        raise AssertionError("Mission execution did not propagate cancellation")

    asyncio.run(scenario())
    refreshed = storage.get_mission(mission["id"])

    assert all(task["status"] != "running" for task in refreshed["tasks"])
    assert refreshed["tasks"][0]["status"] == "blocked"
    assert "cancelled" in refreshed["tasks"][0]["notes"]
    storage.close()


def test_cancelled_executive_step_reconciles_without_replaying_action(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    agent.settings = replace(agent.settings, llm_enabled=True)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": "a" * 64,
        "host": {
            "os": {"system": "Windows"},
            "architecture": {"machine": "AMD64"},
            "accelerators": {},
            "tools": {},
        },
    }
    agent.executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    agent.tools.executive = agent.executive
    mission = agent.create_mission("Apply one safe mutation exactly once")

    async def scenario():
        entered = asyncio.Event()

        async def committed_then_waits(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(agent, "_execute_mission_step_agentic", committed_then_waits)
        execution = asyncio.create_task(agent.execute_next_mission_step(mission["id"]))
        await entered.wait()
        execution.cancel()
        try:
            await execution
        except asyncio.CancelledError:
            return
        raise AssertionError("Mission execution did not propagate cancellation")

    asyncio.run(scenario())
    refreshed = storage.get_mission(mission["id"])
    plan = agent.executive.snapshot(mission["id"])["planner"]
    original = next(item for item in refreshed["tasks"] if item["id"] == mission["tasks"][0]["id"])
    recovery = next(
        item
        for item in plan["steps"]
        if item["spec"]["action"]["arguments"].get("kind") == "reconciliation"
    )

    assert original["status"] == "skipped"
    assert plan["revision"] == 1
    assert recovery["spec"]["action"]["arguments"]["replay_original_action"] is False
    storage.close()


def test_executive_autonomy_rejects_unverified_side_effect_wrappers(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": "b" * 64,
        "host": {"os": {}, "architecture": {}, "accelerators": {}, "tools": {}},
    }
    agent.executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    agent.tools.executive = agent.executive
    mission = agent.create_mission("Never bypass the typed mutation substrate")
    claim = agent.executive.claim_ready_task(mission["id"])
    context = AgentContext(
        conversation_id=f"mission:{mission['id']}",
        memory_hits=[],
        file_hits=[],
        mission_id=mission["id"],
        task_id=claim.task["id"],
    )
    wrappers = {
        "filesystem.write_text": {"path": str(tmp_path / "blocked.txt"), "content": "x"},
        "execution.transaction": {"actions": []},
        "documents.apply_replacements": {},
        "web.watch.add": {"url": "https://example.com"},
        "web.download": {"url": "https://example.com/file"},
        "learning.tick": {},
        "memory.save": {"content": "must not persist"},
        "persona.insight": {"field": "interests", "value": "must not persist"},
    }

    async def scenario():
        results = []
        for name, arguments in wrappers.items():
            results.append(
                await agent._run_agentic_tool(
                    name,
                    arguments,
                    {name},
                    context,
                )
            )
        return results

    results = asyncio.run(scenario())

    assert all("rejected" in observation for observation, _event, _run in results)
    assert all(event.type == "thought" for _observation, event, _run in results)
    assert all(run is None for _observation, _event, run in results)
    assert not (tmp_path / "blocked.txt").exists()
    assert storage.search_memory("must not persist", limit=5) == []
    assert storage.list_approvals(limit=20) == []
    storage.close()


def test_blocked_mission_step_prevents_skipping_to_later_tasks(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    mission = agent.create_mission("Build tools runtime")
    first = mission["tasks"][0]
    storage.update_mission_task(
        first["id"],
        mission_id=mission["id"],
        status="blocked",
        notes="Approval required.",
    )

    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    refreshed = storage.get_mission(mission["id"])

    assert response.result.ok is False
    assert response.result.data["blocked"] is True
    assert response.task is None
    assert refreshed["tasks"][1]["status"] == "pending"
    storage.close()


def test_missing_executive_plan_never_falls_through_to_legacy_fifo(
    monkeypatch,
    tmp_path,
):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Interrupted mission creation",
        goal="Never execute without a durable DAG",
        tasks=["First action", "Second action"],
    )
    assert storage.claim_mission_task(mission["id"], mission["tasks"][0]["id"])
    agent.executive = ExecutiveCoordinator(
        storage=storage,
        host_profile={
            "schema": "jarvis.host-profile.v1",
            "fingerprint_sha256": "a" * 64,
            "host": {
                "os": {"system": "Windows"},
                "architecture": {"machine": "AMD64"},
                "accelerators": {},
                "tools": {},
            },
        },
    )

    def legacy_fifo_must_not_run(_mission_id):
        raise AssertionError("legacy FIFO executor was reached")

    monkeypatch.setattr(storage, "claim_next_mission_task", legacy_fifo_must_not_run)
    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    refreshed = storage.get_mission(mission["id"])

    assert response.result.ok is False
    assert response.result.data["executive_plan_missing"] is True
    assert all(item["status"] == "blocked" for item in refreshed["tasks"])
    storage.close()


def test_agent_streams_chat_response(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = FakeStreamingLLM()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=llm,
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("hello", mode="chat", max_tokens=32)))
    deltas = [item["content"] for item in items if item["type"] == "delta"]
    done = next(item for item in items if item["type"] == "done")
    messages = storage.recent_messages(done["conversation_id"], limit=5)

    assert deltas == ["Hello", " world"]
    assert done["answer"] == "Hello world"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Hello world"
    assert llm.max_tokens == 32
    storage.close()


def test_agent_includes_runtime_date_context(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "готово", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())

    response = asyncio.run(agent.chat("коротко представься", mode="chat"))

    system_messages = [item["content"] for item in captured["messages"] if item["role"] == "system"]
    date_context = "\n".join(system_messages)
    assert response.answer == "готово"
    assert "Runtime date context" in date_context
    assert "current_date:" in date_context
    assert "early 2026" in date_context
    storage.close()


def test_moscow_timezone_falls_back_to_fixed_utc_plus_three(monkeypatch):
    from zoneinfo import ZoneInfoNotFoundError

    import jarvis_gpt.agent as agent_module

    def missing_zone(_name):
        raise ZoneInfoNotFoundError("tzdata unavailable")

    monkeypatch.setattr(agent_module, "ZoneInfo", missing_zone)

    fallback = agent_module._load_moscow_timezone()

    assert fallback.utcoffset(None) == timedelta(hours=3)
    assert datetime(2026, 7, 10, 22, 30, tzinfo=UTC).astimezone(
        fallback
    ).date() == date(2026, 7, 11)


def test_exact_russian_news_query_bypasses_fast_fact_and_passes_moscow_window(
    monkeypatch,
    tmp_path,
):
    import jarvis_gpt.agent as agent_module

    captured = {}

    async def fake_web_answer(_ctx, args):
        captured.update(args)
        return ToolRunResponse(
            tool="web.answer",
            ok=True,
            summary="Dated news answer.",
            data={
                "query": args["query"],
                "answer": "Конкретная новость — https://ria.ru/20260711/example.html",
                "vertical": "news",
                "sources": [
                    {
                        "title": "Конкретная новость России",
                        "url": "https://ria.ru/20260711/example.html",
                        "published": "2026-07-11T12:00:00+03:00",
                    }
                ],
                "news": {
                    "complete": True,
                    "date_from": "2026-07-10",
                    "date_to": "2026-07-11",
                },
            },
        )

    monkeypatch.setattr(agent_module, "_moscow_today", lambda _now=None: date(2026, 7, 11))
    monkeypatch.setattr("jarvis_gpt.tools._web_answer", fake_web_answer)
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    response = asyncio.run(
        agent.chat("Какие в России за вчера и сегодня значимые новости произошли?")
    )

    assert captured["vertical"] == "news"
    assert captured["freshness"] == "week"
    assert captured["date_from"] == "2026-07-10"
    assert captured["date_to"] == "2026-07-11"
    assert "2026-07-10" in captured["query"]
    assert "2026-07-11" in captured["query"]
    assert "Конкретная новость" in response.answer
    assert "web.surfer" not in {run["tool"] for run in storage.list_tool_runs(limit=20)}
    storage.close()


def test_dns_catalog_failure_is_not_replaced_by_generic_web_answer(monkeypatch, tmp_path):
    import jarvis_gpt.agent as agent_module

    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **_kwargs):
        calls.append((name, arguments or {}))
        assert name == "web.shop_search"
        return ToolRunResponse(
            tool=name,
            ok=False,
            summary="No ranked products for 'rtx 5090' (HTTP 403).",
            data={
                "query": "rtx 5090",
                "shop": "dns",
                "url": "https://www.dns-shop.ru/search/?q=rtx+5090",
                "error": "HTTP 403",
                "items": [],
                "browser_mode": "headless",
            },
        )

    monkeypatch.setattr(agent_module, "_web_surfer_available", lambda: True)
    monkeypatch.setattr(agent.tools, "run", fake_run)

    action = asyncio.run(
        agent._run_web_research(
            "Найди мне самую дешёвую 5090 на днс",
            "5090 site:dns-shop.ru",
        )
    )

    assert calls == [
        (
            "web.shop_search",
            {
                "query": "rtx 5090",
                "shop": "dns",
                "criterion": "price_asc",
                "criterion_label": "минимальная цена",
            },
        )
    ]
    assert "HTTP 403" in action.answer
    assert "не подменяю результат общим веб-поиском" in action.answer
    assert "https://www.dns-shop.ru/search/?q=rtx+5090" in action.answer
    assert action.events[0].payload["browser_mode"] == "headless"
    storage.close()


def test_bounded_news_exception_does_not_fall_back_to_legacy_search(monkeypatch, tmp_path):
    from types import MethodType

    import jarvis_gpt.agent as agent_module

    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    async def exploding_run(_registry, name, arguments=None, **_kwargs):
        assert name == "web.answer"
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(agent_module, "_moscow_today", lambda _now=None: date(2026, 7, 11))
    monkeypatch.setattr(agent.tools, "run", MethodType(exploding_run, agent.tools))

    action = asyncio.run(
        agent._run_web_answer_engine(
            message="Какие в России за вчера и сегодня значимые новости произошли?",
            query="значимые новости России 2026-07-10 2026-07-11",
            conversation_id=None,
        )
    )

    assert action is not None
    assert "сервис датированного поиска не ответил" in action.answer
    assert action.events[0].payload["vertical"] == "news"
    storage.close()


def test_bounded_news_partial_result_names_missing_date(monkeypatch, tmp_path):
    from types import MethodType

    import jarvis_gpt.agent as agent_module

    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    async def partial_run(_registry, name, arguments=None, **_kwargs):
        assert name == "web.answer"
        return ToolRunResponse(
            tool=name,
            ok=False,
            summary="Only one requested day has dated evidence.",
            data={
                "vertical": "news",
                "answer": "- Новость за 11 июля",
                "sources": [{"url": "https://news.example/20260711/item"}],
                "news": {
                    "complete": False,
                    "date_from": "2026-07-10",
                    "date_to": "2026-07-11",
                    "missing_dates": ["2026-07-10"],
                },
            },
        )

    monkeypatch.setattr(agent_module, "_moscow_today", lambda _now=None: date(2026, 7, 11))
    monkeypatch.setattr(agent.tools, "run", MethodType(partial_run, agent.tools))

    action = asyncio.run(
        agent._run_web_answer_engine(
            message="Какие в России за вчера и сегодня значимые новости произошли?",
            query="значимые новости России 2026-07-10 2026-07-11",
            conversation_id=None,
        )
    )

    assert action is not None
    assert "Нет подтверждённых публикаций за: 2026-07-10" in action.answer
    assert "Частичная подтверждённая подборка" in action.answer
    storage.close()


def test_agent_passes_chat_attachments_to_llm_context(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    stored_path = tmp_path / "brief.txt"
    stored_path.write_text("alpha attached content", encoding="utf-8")
    file_record = storage.create_file_record(
        name="brief.txt",
        stored_path=stored_path,
        sha256="abc",
        size=stored_path.stat().st_size,
        mime_type="text/plain",
        status="indexed",
        chunk_count=1,
    )
    storage.add_file_chunks(file_record["id"], ["alpha attached content from upload"])

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "done", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    attachments = [
        {
            "id": file_record["id"],
            "name": file_record["name"],
            "mime_type": file_record["mime_type"],
            "size": file_record["size"],
        }
    ]

    response = asyncio.run(agent.chat("разбери вложение", mode="chat", attachments=attachments))

    stored_messages = storage.recent_messages(response.conversation_id, limit=4)
    user_message = next(item for item in stored_messages if item["role"] == "user")
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert user_message["content"] == "разбери вложение"
    assert user_message["metadata"]["attachments"][0]["id"] == file_record["id"]
    assert "Attached files already uploaded" in rendered_prompt
    assert "documents.* tools" in rendered_prompt
    assert "brief.txt" in rendered_prompt
    assert "alpha attached content from upload" in rendered_prompt
    storage.close()


def test_agent_routes_persisted_document_recall_and_exposes_file_id(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "alpha-contract.txt"
    source.write_text("Alpha contract deadline is September.", encoding="utf-8")
    ingested = FileIngestor(settings, storage).ingest_path(source)
    captured = {}

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "done", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    response = asyncio.run(agent.chat("Дай резюме сохраненного договора Alpha"))
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])
    user_message = next(
        item
        for item in storage.recent_messages(response.conversation_id, limit=4)
        if item["role"] == "user"
    )

    assert user_message["metadata"]["task_kernel"]["intent"] == "document_memory"
    assert ingested["file"]["id"] in rendered_prompt
    assert "Alpha contract deadline" in rendered_prompt
    assert "documents.recall" in rendered_prompt
    storage.close()


def test_document_memory_route_is_not_intercepted_by_weather(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "погода-report.txt"
    source.write_text("Погода в отчете использована как тестовая метка.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(source)
    captured = {}

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "Резюме готово.", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    response = asyncio.run(agent.chat("Дай резюме сохраненного отчета: погода"))
    rendered = "\n".join(item["content"] for item in captured["messages"])

    assert response.answer == "Резюме готово."
    assert "Погода в отчете" in rendered
    assert any(event.payload.get("prefetch") is True for event in response.events)
    storage.close()


def test_agent_carries_recent_attachment_into_summary_followup(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    path = tmp_path / "brief.txt"
    path.write_text("Phoenix release is ready for validation.", encoding="utf-8")
    record = storage.create_file_record(
        name=path.name,
        stored_path=path,
        sha256="b" * 64,
        size=path.stat().st_size,
        mime_type="text/plain",
        status="stored",
        chunk_count=0,
    )
    conversation_id = storage.create_conversation("Attachment follow-up")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="Посмотри вложение",
        metadata={"attachments": [{"id": record["id"], "name": record["name"]}]},
    )
    captured = {}

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "done", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    response = asyncio.run(agent.chat("Сделай краткое резюме", conversation_id=conversation_id))
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])
    user_message = next(
        item
        for item in storage.recent_messages(response.conversation_id, limit=3)
        if item["role"] == "user" and item["content"] == "Сделай краткое резюме"
    )

    assert user_message["metadata"]["task_kernel"]["intent"] == "document_memory"
    assert record["id"] in rendered_prompt
    assert "Phoenix release" in rendered_prompt
    assert any(
        event.payload.get("tool") == "documents.recall"
        and event.payload.get("prefetch") is True
        for event in response.events
    )
    storage.close()


def test_agent_binds_singular_followup_to_latest_attachment_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    records = []
    for name, content in (
        ("alpha.txt", "Alpha attachment must not be recalled."),
        ("beta.txt", "Beta attachment is the active document."),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        records.append(FileIngestor(settings, storage).ingest_path(path)["file"])
    conversation_id = storage.create_conversation("Latest attachment binding")
    for record in records:
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"Uploaded {record['name']}",
            metadata={"attachments": [{"id": record["id"], "name": record["name"]}]},
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content="Принял.",
        )
    captured = {}

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "done", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    response = asyncio.run(agent.chat("Summarize it", conversation_id=conversation_id))
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])
    recall_event = next(
        event
        for event in response.events
        if event.payload.get("tool") == "documents.recall"
        and event.payload.get("prefetch") is True
    )

    assert recall_event.payload["file_ids"] == [records[1]["id"]]
    assert "Beta attachment is the active document" in rendered_prompt
    assert "Alpha attachment must not be recalled" not in rendered_prompt
    storage.close()


def test_agent_reuses_successful_recall_for_deictic_followup(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "phoenix-plan.txt"
    source.write_text("Phoenix launch requires a backup check.", encoding="utf-8")
    record = FileIngestor(settings, storage).ingest_path(source)["file"]
    prompts = []

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            prompts.append("\n".join(item["content"] for item in messages))
            return type("Result", (), {"ok": True, "content": "done", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    first = asyncio.run(agent.chat("Summarize saved Phoenix document"))
    stored_first = storage.get_message(first.message_id)
    second = asyncio.run(
        agent.chat("Summarize it more briefly", conversation_id=first.conversation_id)
    )
    recall_event = next(
        event
        for event in second.events
        if event.payload.get("tool") == "documents.recall"
        and event.payload.get("prefetch") is True
    )

    assert stored_first["metadata"]["document_recall"]["file_ids"] == [record["id"]]
    assert recall_event.payload["file_ids"] == [record["id"]]
    assert "Phoenix launch requires a backup check" in prompts[1]
    storage.close()


def test_agent_stream_persists_successful_document_recall(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setenv("JARVIS_EMBEDDINGS_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "stream-report.txt"
    source.write_text("Stream report says the rollout is ready.", encoding="utf-8")
    record = FileIngestor(settings, storage).ingest_path(source)["file"]
    captured = {}

    class CapturingStreamLLM:
        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            captured["messages"] = messages
            yield LLMStreamChunk(kind="delta", content="Rollout готов.")

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=CapturingStreamLLM(),
        bus=EventBus(),
    )
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    items = asyncio.run(
        _collect(agent.stream_chat("Summarize saved stream report", mode="chat"))
    )
    done = next(item for item in items if item["type"] == "done")
    stored = storage.get_message(done["message_id"])
    rendered = "\n".join(item["content"] for item in captured["messages"])

    assert "Stream report says the rollout is ready" in rendered
    assert stored["metadata"]["document_recall"]["file_ids"] == [record["id"]]
    assert any(
        item["type"] == "event"
        and item["event"].get("payload", {}).get("prefetch") is True
        for item in items
    )
    storage.close()


def test_agent_routes_saved_archive_to_archive_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setenv("JARVIS_EMBEDDINGS_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    archive_path = tmp_path / "saved-bundle.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("notes.txt", "Phoenix archive evidence is ready.")
    record = FileIngestor(settings, storage).ingest_path(archive_path)["file"]
    conversation_id = storage.create_conversation("Archive follow-up")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="Сохрани архив",
        metadata={"attachments": [{"id": record["id"], "name": record["name"]}]},
    )
    storage.add_message(conversation_id=conversation_id, role="assistant", content="Принял.")

    class ArchiveLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            rendered = "\n".join(item["content"] for item in messages)
            if self.calls == 1:
                assert record["id"] in rendered
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": json.dumps(
                            {
                                "tool": "documents.archive.search",
                                "arguments": {"file_id": record["id"], "query": "Phoenix"},
                            }
                        ),
                        "error": None,
                    },
                )()
            assert "Phoenix archive evidence" in rendered
            return type(
                "Result",
                (),
                {"ok": True, "content": "Phoenix найден в notes.txt.", "error": None},
            )()

    llm = ArchiveLLM()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    response = asyncio.run(
        agent.chat("Найди Phoenix в сохраненном архиве", conversation_id=conversation_id)
    )
    user_message = next(
        item
        for item in storage.recent_messages(conversation_id, limit=4)
        if item["role"] == "user" and "Phoenix" in item["content"]
    )

    assert user_message["metadata"]["task_kernel"]["intent"] == "archive_memory"
    assert llm.calls == 2
    assert response.answer == "Phoenix найден в notes.txt."
    assert any(
        event.payload.get("tool") == "documents.archive.search" for event in response.events
    )
    assert not any(
        event.payload.get("tool") == "documents.recall" for event in response.events
    )
    storage.close()


def test_document_tool_prompt_hints_and_observations_keep_content() -> None:
    assert _schema_hint({"query": "Text query", "limit": "Maximum results"}) == ("query?, limit?")
    observation = _tool_observation_excerpt(
        ToolRunResponse(
            tool="documents.recall",
            ok=True,
            summary="recalled",
            data={"passages": [{"content": ("x" * 2_000) + "TAIL"}]},
        )
    )

    assert "TAIL" in observation
    assert "untrusted document/file evidence" in observation

    large_observation = _tool_observation_excerpt(
        ToolRunResponse(
            tool="documents.recall",
            ok=True,
            summary="recalled",
            data={
                "sources": [{"file_id": "file_1", "name": "large.xlsx"}],
                "passages": [{"content": ("p" * 4_000) + "PASSAGE_TAIL"}],
                "analyses": [{"tables": ["x" * 40_000]}],
            },
        )
    )
    payload = large_observation.split("\ndata: ", 1)[1]

    assert "PASSAGE_TAIL" in large_observation
    assert json.loads(payload)["truncated"] is True


def test_document_memory_routing_distinguishes_persisted_file_from_web_report() -> None:
    assert _looks_like_document_memory_query(
        "Summarize the last document",
        has_file_context=False,
        has_persisted_files=True,
    )


def test_document_memory_ambiguity_returns_before_llm(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setenv("JARVIS_EMBEDDINGS_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    ids = []
    for index, suffix in enumerate(("A", "B")):
        path = tmp_path / f"contract-{suffix}.txt"
        path.write_text(f"Стандартные условия договора {suffix}.", encoding="utf-8")
        record = storage.create_file_record(
            name=path.name,
            stored_path=path,
            sha256=str(index + 1) * 64,
            size=path.stat().st_size,
            mime_type="text/plain",
            status="indexed",
            chunk_count=1,
        )
        storage.add_file_chunks(record["id"], [f"Стандартные условия договора {suffix}."])
        ids.append(record["id"])

    class MustNotRunLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            raise AssertionError("LLM must not run for ambiguous document selection")

    agent = AgentRuntime(settings=settings, storage=storage, llm=MustNotRunLLM(), bus=EventBus())
    response = asyncio.run(agent.chat("Дай резюме сохраненного договора"))

    assert "несколько подходящих документов" in response.answer
    assert all(file_id in response.answer for file_id in ids)
    assert "Стандартные условия договора" not in response.answer
    storage.close()
    assert not _looks_like_document_memory_query(
        "Summarize the recent Microsoft report",
        has_file_context=False,
        has_persisted_files=True,
    )
    assert not _looks_like_document_memory_query(
        "Найди и проанализируй отчёт Минфина в интернете",
        has_file_context=True,
        has_persisted_files=True,
    )


def test_agent_can_disable_model_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class CapturingThinkingLLM:
        async def complete(
            self,
            messages,
            *,
            temperature=None,
            max_tokens=None,
            thinking_enabled=True,
        ):
            captured["messages"] = messages
            captured["thinking_enabled"] = thinking_enabled
            return type(
                "Result",
                (),
                {"ok": True, "content": "<think>hidden</think>final answer", "error": None},
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=CapturingThinkingLLM(),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("hello", mode="chat", thinking_enabled=False))
    user_message = next(
        item
        for item in storage.recent_messages(response.conversation_id, limit=4)
        if item["role"] == "user"
    )
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert captured["thinking_enabled"] is False
    assert "Thinking output is disabled" in rendered_prompt
    assert response.answer == "final answer"
    assert "hidden" not in response.answer
    assert user_message["metadata"]["thinking_enabled"] is False
    storage.close()


def test_agent_executes_explicit_wiki_open_without_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("открой статью про Гитлера на вики в новой вкладке"))
    runs = storage.list_tool_runs()
    approvals = storage.list_approvals(limit=5, status="pending")

    assert "ru.wikipedia.org" in response.answer
    assert "Адольф_Гитлер" in response.answer
    assert len(runs) == 1
    assert runs[0]["tool"] == "browser.open"
    assert "ru.wikipedia.org" in runs[0]["arguments"]["url"]
    assert approvals == []
    event = next(event for event in response.events if event.payload.get("operator_requested"))
    assert event.payload["authority"] == "operator_turn"
    storage.close()


def test_agent_opens_bare_domain_without_approval(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat("перейди на example.com"))

    run = _operator_tool_run(storage, "browser.open")
    assert run["arguments"] == {"url": "https://example.com"}
    assert "https://example.com" in response.answer
    storage.close()


def test_stream_chat_opens_explicit_url_without_approval(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    async def collect_stream():
        return [item async for item in agent.stream_chat("open https://example.com")]

    items = asyncio.run(collect_stream())

    run = _operator_tool_run(storage, "browser.open")
    assert run["arguments"] == {"url": "https://example.com"}
    assert any(
        item.get("type") == "event"
        and item["event"].get("payload", {}).get("authority") == "operator_turn"
        for item in items
    )
    storage.close()


def test_explicit_shop_product_url_is_opened_not_catalog_searched(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    target = "https://www.dns-shop.ru/product/123"

    response = asyncio.run(agent.chat(f"открой {target}"))

    run = _operator_tool_run(storage, "browser.open")
    task_plan = next(event for event in response.events if event.title == "Task kernel")
    assert run["arguments"] == {"url": target}
    assert task_plan.payload["route"] == "local_action"
    assert all(item["tool"] != "web.shop_search" for item in storage.list_tool_runs())
    storage.close()


def test_agent_opens_file_with_default_windows_application(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    target = tmp_path / "report.txt"
    target.write_text("report", encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )

    response = asyncio.run(agent.chat(f'открой файл "{target}"'))

    run = _operator_tool_run(storage, "windows.native")
    assert run["arguments"]["payload"] == {
        "executable": "explorer.exe",
        "arguments": [str(target)],
    }
    assert calls[0]["action"] == "process.start"
    assert "приложении по умолчанию" in response.answer
    storage.close()


def test_agent_creates_empty_file_without_approval(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    target = tmp_path / "empty.txt"

    response = asyncio.run(agent.chat(f'создай пустой файл "{target}"'))

    assert target.exists()
    assert target.read_bytes() == b""
    run = _operator_tool_run(storage, "filesystem.write_text")
    assert run["arguments"] == {"path": str(target), "content": "", "mode": "create"}
    assert "Создал пустой файл" in response.answer
    storage.close()


@pytest.mark.parametrize(
    "message",
    [
        "How do I open Notepad?",
        "Show me how to take a screenshot",
        "Объясни, как открыть калькулятор",
        "Фраза 'open Notepad' — это пример команды",
        "Open Notepad tomorrow",
    ],
)
def test_meta_or_deferred_phrases_do_not_authorize_actions(monkeypatch, tmp_path, message):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat(message))

    assert storage.list_tool_runs() == []
    assert storage.list_approvals(limit=10) == []
    assert all(not event.payload.get("operator_requested") for event in response.events)
    storage.close()


def test_agent_opens_calculator_with_host_bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    calls = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("открой калькулятор и набери в нём что-нибудь"))

    run = _operator_tool_run(storage, "windows.native")
    arguments = run["arguments"]
    assert arguments["action"] == "app.open_and_type"
    assert arguments["payload"]["executable"] == "explorer.exe"
    assert any(
        "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App" in item
        for item in arguments["payload"]["arguments"]
    )
    assert arguments["payload"]["keys"] == "123{+}456="
    assert calls[0]["action"] == "app.open_and_type"
    assert response.events[-1].payload["authority"] == "operator_turn"
    assert "Готово" in response.answer
    storage.close()


def test_agent_calculator_understands_russian_multiply_sign(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    calls = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("открой калькулятор и посчитай там 10х10"))

    arguments = _operator_tool_run(storage, "windows.native")["arguments"]
    assert arguments["action"] == "app.open_and_type"
    assert arguments["payload"]["executable"] == "explorer.exe"
    assert any(
        "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App" in item
        for item in arguments["payload"]["arguments"]
    )
    assert arguments["payload"]["keys"] == "10{*}10="
    assert arguments["payload"]["window_title"] == "Calculator|Калькулятор"
    assert calls[0]["payload"]["keys"] == "10{*}10="
    assert "Готово" in response.answer
    storage.close()


def test_agent_opens_only_fixed_top_process_console(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_action(self, *, action, payload=None, timeout_sec=30):
        payload = dict(payload or {})
        calls.append({"action": action, "payload": payload, "timeout_sec": timeout_sec})
        if action == "wmi.query":
            return {
                "ok": True,
                "summary": "verified",
                "data": {
                    "ok": True,
                    "summary": "verified",
                    "data": {"items": [{"ProcessId": 4242, "Name": "powershell.exe"}]},
                },
            }
        return {
            "ok": True,
            "summary": "Opened fixed process console.",
            "data": {
                "ok": True,
                "action": action,
                "summary": "Opened fixed process console.",
                "pid": 4242,
            },
        }

    monkeypatch.setattr("jarvis_gpt.host_bridge.HostBridgeClient.action", fake_action)

    response = asyncio.run(agent.chat("открой топ 10 процессов в консоли"))

    run = _operator_tool_run(storage, "windows.native")
    assert run["arguments"] == {
        "action": "console.show_processes",
        "payload": {"limit": 10, "sort": "cpu"},
        "timeout_sec": 30,
    }
    assert [call["action"] for call in calls] == ["console.show_processes", "wmi.query"]
    assert storage.list_approvals(limit=10) == []
    assert "Готово" in response.answer
    storage.close()


def test_agent_returns_bounded_top_process_snapshot(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_action(self, *, action, payload=None, timeout_sec=30):
        captured.update({"action": action, "payload": payload, "timeout_sec": timeout_sec})
        return {
            "ok": True,
            "summary": "Listed 3 process(es), sorted by memory.",
            "data": {
                "ok": True,
                "action": action,
                "summary": "Listed 3 process(es), sorted by memory.",
                "data": {
                    "items": [
                        {"ProcessId": 1, "Name": "first", "WorkingSetBytes": 300},
                        {"ProcessId": 2, "Name": "second", "WorkingSetBytes": 200},
                        {"ProcessId": 3, "Name": "third", "WorkingSetBytes": 100},
                    ]
                },
            },
        }

    monkeypatch.setattr("jarvis_gpt.host_bridge.HostBridgeClient.action", fake_action)

    response = asyncio.run(agent.chat("покажи топ 3 процессов по памяти"))

    run = _operator_tool_run(storage, "system.inspect")
    assert run["arguments"] == {
        "action": "process.top",
        "payload": {"limit": 3, "sort": "memory"},
        "timeout_sec": 30,
    }
    assert captured["action"] == "process.top"
    assert "first" in response.answer
    assert "third" in response.answer
    storage.close()


def test_agent_ignores_raw_console_commands_without_approval_or_execution(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("active console")
    storage.set_runtime_value(
        f"ui.target.console.{conversation_id}",
        {
            "pid": 4242,
            "process_name": "cmd",
            "window_title": "Command Prompt",
            "executable": "cmd.exe",
            "shell": "cmd",
        },
    )

    async def forbidden_action(self, *, action, payload=None, timeout_sec=30):
        raise AssertionError(f"raw console text reached native action {action}")

    monkeypatch.setattr("jarvis_gpt.host_bridge.HostBridgeClient.action", forbidden_action)

    first = asyncio.run(agent.chat("run in console `ipconfig /all`"))
    second = asyncio.run(
        agent.chat(
            "in the same console run `Write-Host RawShouldNotRun`",
            conversation_id,
        )
    )

    assert all(event.type != "approval" for event in [*first.events, *second.events])
    assert storage.list_approvals(limit=10) == []
    executed_tools = {run["tool"] for run in storage.list_tool_runs()}
    assert executed_tools.isdisjoint({"execution.apply", "windows.native"})
    storage.close()


def test_agent_does_not_treat_creative_writing_as_gui_input(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    for message in (
        "write a poem about the sea",
        "write a poem about Microsoft Edge",
        "напиши стих о море",
        "напиши стих про блокнот",
    ):
        response = asyncio.run(agent.chat(message))
        assert all(event.type != "approval" for event in response.events)

    assert storage.list_approvals(limit=10) == []
    assert storage.list_tool_runs() == []
    storage.close()


def test_agent_executes_explicit_active_window_input(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )

    response = asyncio.run(agent.chat("введи Jarvis online в активное окно"))

    arguments = _operator_tool_run(storage, "windows.native")["arguments"]
    assert arguments["action"] == "keyboard.send"
    assert arguments["payload"]["text"] == "Jarvis online"
    assert calls[0]["action"] == "keyboard.send"
    assert response.events[-1].payload["authority"] == "operator_turn"
    storage.close()


def test_agent_opens_named_programs_through_native_layer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    calls = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("открой Microsoft Edge"))

    arguments = _operator_tool_run(storage, "windows.native")["arguments"]
    assert arguments["action"] == "process.start"
    assert arguments["payload"]["executable"] == "msedge.exe"
    assert calls[0]["action"] == "process.start"
    assert "Готово" in response.answer
    storage.close()


def test_agent_opens_explicit_file_in_named_app(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )
    target = tmp_path / "sample.txt"

    response = asyncio.run(agent.chat(f"Open {target} in Notepad"))

    arguments = _operator_tool_run(storage, "windows.native")["arguments"]
    assert arguments["action"] == "process.start"
    assert arguments["payload"]["executable"] == "notepad.exe"
    assert arguments["payload"]["arguments"] == [str(target)]
    assert calls[0]["payload"]["arguments"] == [str(target)]
    assert "Готово" in response.answer
    storage.close()


def test_agent_captures_screen_when_asked_to_look(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_action(self, *, action, payload=None, timeout_sec=30):
        captured.update({"action": action, "payload": payload, "timeout_sec": timeout_sec})
        return {
            "ok": True,
            "summary": "captured",
            "data": {
                "ok": True,
                "summary": "Screen captured.",
                "action": action,
                "data": {
                    "path": "C:/tmp/screen.png",
                    "width": 1920,
                    "height": 1080,
                    "activeWindow": {
                        "ProcessName": "chrome",
                        "MainWindowTitle": "Jarvis",
                    },
                    "windows": [{"ProcessName": "chrome", "MainWindowTitle": "Jarvis"}],
                },
            },
        }

    monkeypatch.setattr("jarvis_gpt.host_bridge.HostBridgeClient.action", fake_action)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("посмотри моими глазами, что сейчас на экране"))
    runs = storage.list_tool_runs()

    assert captured["action"] == "screen.capture"
    assert Path(captured["payload"]["path"]).parent == settings.cache_dir / "screens"
    assert captured["timeout_sec"] == 30
    assert runs[0]["tool"] == "system.inspect"
    assert storage.list_approvals(limit=10) == []
    assert "Визуальная проверка" in response.answer
    assert "C:/tmp/screen.png" in response.answer
    assert "chrome" in response.answer
    storage.close()


def test_agent_types_into_general_windows_app(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    calls = []
    monkeypatch.setattr(
        "jarvis_gpt.host_bridge.HostBridgeClient.action",
        _verified_host_action(calls),
    )
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("открой блокнот и напиши Jarvis online"))

    arguments = _operator_tool_run(storage, "windows.native")["arguments"]
    payload = arguments["payload"]
    assert arguments["action"] == "app.open_and_type"
    assert payload["executable"] == "notepad.exe"
    assert payload["text"] == "Jarvis online"
    assert len(payload["arguments"]) == 1
    assert "scratch-notepad-" in payload["arguments"][0]
    assert payload["arguments"][0].endswith(".txt")
    assert calls[0]["action"] == "app.open_and_type"
    assert "Готово" in response.answer
    storage.close()


def test_agent_routes_wmi_requests_to_native_layer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_action(self, *, action, payload=None, timeout_sec=30):
        captured.update({"action": action, "payload": payload, "timeout_sec": timeout_sec})
        return {
            "ok": True,
            "summary": "wmi ok",
            "data": {
                "ok": True,
                "summary": "WMI/CIM query returned 1 item(s).",
                "action": action,
                "data": {"items": [{"Name": "python.exe"}]},
            },
        }

    monkeypatch.setattr("jarvis_gpt.host_bridge.HostBridgeClient.action", fake_action)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("покажи процессы через WMI"))
    runs = storage.list_tool_runs()

    assert captured["action"] == "wmi.query"
    assert captured["payload"]["class_name"] == "Win32_Process"
    assert captured["timeout_sec"] == 30
    assert runs[0]["tool"] == "system.inspect"
    assert storage.list_approvals(limit=10) == []
    assert "WMI/CIM query returned" in response.answer
    assert "python.exe" in response.answer
    storage.close()


def test_agent_researches_google_style_query(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Linux open ports",
                            "url": "https://example.com/linux-ports",
                            "snippet": "ss -tulpen shows open ports",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "ss -tulpen shows open ports"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("загугли как проверить открытые порты linux"))

    assert "Источники" in response.answer
    assert "https://example.com/linux-ports" in response.answer
    assert "ss -tulpen" in response.answer
    storage.close()


def test_agent_researches_current_ticket_request(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured.setdefault("tools", []).append((name, arguments or {}))
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 2 result(s).",
                {
                    "results": [
                        {
                            "title": "Авиабилеты Екатеринбург Москва",
                            "url": "https://example.com/avia",
                            "snippet": "Екатеринбург Москва от 12 500 ₽ вылет 14:20",
                        },
                        {
                            "title": "ЖД билеты Екатеринбург Москва",
                            "url": "https://example.com/train",
                            "snippet": "поезд 18:45 от 4 500 руб.",
                        },
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Екатеринбург Москва от 12 500 ₽ вылет 14:20",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat("дай мне пример реального билета из екатеринбурга в москву на послезавтра")
    )

    assert captured["tools"][0][0] == "web.search"
    assert "билеты цена наличие расписание" in captured["query"]
    assert "Источники" in response.answer
    assert "12 500" in response.answer
    assert "выдум" not in response.answer.lower()
    storage.close()


def test_agent_researches_public_sources_self_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            assert "публичные источники" in arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Public profile",
                            "url": "https://example.com/dest1k",
                            "snippet": "public account",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "public account profile"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди меня в интернете по аккаунту Dest1k"))

    assert "Источники" in response.answer
    assert "https://example.com/dest1k" in response.answer
    assert "Проверка публичных источников" in response.answer
    assert "не буду помогать" in response.answer
    assert "не могу" not in response.answer.lower()
    storage.close()


def test_agent_researches_dns_shop_product_without_public_sources_suffix(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured.setdefault("tools", []).append((name, arguments or {}))
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 в DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "GeForce RTX 5090 399 999 ₽ В наличии",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "GeForce RTX 5090 399 999 ₽ В наличии, доставка завтра",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди мне самую дешёвую видеокарту rtx 5090 на dns"))

    assert captured["tools"][0][0] == "web.search"
    assert "site:dns-shop.ru" in captured["query"]
    assert captured["query"].startswith("rtx 5090")
    assert "найди" not in captured["query"]
    assert "публичные источники" not in captured["query"]
    assert "399 999" in response.answer
    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    assert "Приоритетно проверял выдачу магазина DNS" in response.answer
    assert "билет" not in response.answer.lower()
    assert "Проверка публичных источников" not in response.answer
    storage.close()


def test_agent_keeps_dns_records_in_public_sources_context(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "example.com DNS records",
                            "url": "https://example.net/dns/example.com",
                            "snippet": "A 93.184.216.34 MX example.com",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "A 93.184.216.34 MX example.com"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("проверь DNS записи домена example.com"))

    assert "публичные источники" in captured["query"]
    assert "site:dns-shop.ru" not in captured["query"]
    assert "example.com DNS records" in response.answer
    assert "Проверка публичных источников" in response.answer
    storage.close()


def test_agent_retries_shopping_search_with_short_query(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    searches = []

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            searches.append(arguments["query"])
            if len(searches) == 1:
                return _tool_response(
                    name,
                    True,
                    "Web search returned 0 result(s).",
                    {"results": []},
                )
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/catalog/recipe/rtx-5090/",
                            "snippet": "Видеокарты RTX 5090 в DNS",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Видеокарты RTX 5090 в DNS"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди мне самую дешёвую видеокарту rtx 5090 на dns"))

    assert len(searches) == 2
    assert searches[0] == "rtx 5090 site:dns-shop.ru купить цена наличие"
    assert searches[1] == "rtx 5090 dns-shop.ru купить цена наличие"
    assert "https://www.dns-shop.ru/catalog/recipe/rtx-5090/" in response.answer
    storage.close()


def test_agent_expands_bare_gpu_model_for_dns_shop(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "RTX 5090 в DNS",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "RTX 5090 в DNS"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди мне самую дешёвую 5090 в днс"))

    assert captured["query"].startswith("rtx 5090 site:dns-shop.ru")
    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    storage.close()


def test_agent_cleans_noisy_dns_shopping_subject_for_new_search():
    from jarvis_gpt.agent import _shopping_search_query

    message = "и всё-таки покажи мне самую дешёвую позицию в днс на rtx 5090 в Москве"

    assert (
        _shopping_search_query(message, message.lower())
        == "rtx 5090 site:dns-shop.ru купить цена наличие"
    )


def test_agent_returns_dns_links_when_store_blocks_automation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class ResearchLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            rendered = "\n".join(item["content"] for item in messages)
            if "intent-router" in rendered:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"web_research","confidence":0.9,'
                            '"query":"rtx 5090 site:dns-shop.ru купить цена наличие",'
                            '"rationale":"shopping link request"}'
                        ),
                        "error": None,
                    },
                )()
            raise AssertionError("shopping snippet-only evidence should skip synthesis")

    agent = AgentRuntime(settings=settings, storage=storage, llm=ResearchLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "Купить видеокарту RTX 5090 в DNS.",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                False,
                "Fetched URL with HTTP 403; page appears blocked.",
                {
                    "url": arguments["url"],
                    "status_code": 403,
                    "text": "HTTP 403 Error Forbidden",
                },
            )
        if name == "web.render":
            return _tool_response(
                name,
                False,
                "Rendered page appears blocked by the remote site.",
                {"url": arguments["url"], "text": "HTTP 403 Error Forbidden"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("выдай ссылку на самую дешёвую 5090 в днс"))

    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    assert "не подтверждаю" in response.answer
    assert "невозможно" not in response.answer.lower()
    assert not any(event.title == "web.synthesis" for event in response.events)
    storage.close()


def test_agent_retries_shopping_search_when_results_are_only_store_shells(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    searches = []

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            searches.append(arguments["query"])
            if len(searches) == 1:
                return _tool_response(
                    name,
                    True,
                    "Web search returned 2 result(s).",
                    {
                        "results": [
                            {
                                "title": "DNS",
                                "url": "https://www.dns-shop.ru/",
                                "snippet": "Интернет-магазин DNS",
                                "rank": 1,
                            },
                            {
                                "title": "Видеокарты DNS",
                                "url": "https://www.dns-shop.ru/catalog/17a89aab16404e77/videokarty/",
                                "snippet": "Каталог видеокарт",
                                "rank": 2,
                            },
                        ]
                    },
                )
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "RTX 5090 399 999 ₽ В наличии",
                            "rank": 1,
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "RTX 5090 399 999 ₽ В наличии"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("дай мне ссылку на самую дешёвую 5090 в днс"))

    assert len(searches) == 2
    assert searches[0] == "rtx 5090 site:dns-shop.ru купить цена наличие"
    assert searches[1] == "rtx 5090 dns-shop.ru купить цена наличие"
    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    assert "\n1. RTX 5090 DNS" in response.answer
    storage.close()


def test_agent_skips_shopping_synthesis_even_when_product_fetch_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class ResearchLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            rendered = "\n".join(item["content"] for item in messages)
            if "intent-router" in rendered:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"web_research","confidence":0.9,'
                            '"query":"rtx 5090 site:dns-shop.ru купить цена наличие",'
                            '"rationale":"shopping link request"}'
                        ),
                        "error": None,
                    },
                )()
            raise AssertionError("shopping evidence should not be resynthesized by the LLM")

    agent = AgentRuntime(settings=settings, storage=storage, llm=ResearchLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "RTX 5090 399 999 ₽ В наличии",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "RTX 5090 399 999 ₽ В наличии, доставка завтра",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("выдай ссылку на самую дешёвую 5090 в днс"))

    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    assert "399 999" in response.answer
    assert "невозможно" not in response.answer.lower()
    assert not any(event.title == "web.synthesis" for event in response.events)
    storage.close()


def test_agent_sorts_previous_shopping_results_and_opens_cheapest(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **kwargs):
        calls.append((name, arguments or {}))
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 2 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 Expensive",
                            "url": "https://shop.example/expensive",
                            "snippet": "RTX 5090 499 000 ₽ в наличии",
                        },
                        {
                            "title": "RTX 5090 Cheap",
                            "url": "https://shop.example/cheap",
                            "snippet": "RTX 5090 399 000 ₽ в наличии",
                        },
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "товар "
                    + ("399 000 ₽" if "cheap" in arguments["url"] else "499 000 ₽"),
                },
            )
        if name == "browser.open":
            return _tool_response(
                name,
                True,
                "Browser open requested.",
                {"url": arguments["url"]},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    first = asyncio.run(agent.chat("найди мне самую дешёвую 5090 в днс"))
    response = asyncio.run(
        agent.chat(
            "а ты сам не можешь отсортировать и выдать мне? а лучше - открыть самую дешёвую",
            first.conversation_id,
        )
    )

    search_calls = [call for call in calls if call[0] == "web.search"]
    open_calls = [call for call in calls if call[0] == "browser.open"]
    approvals = storage.list_approvals(limit=5, status="pending")
    assert len(search_calls) == 1
    assert len(open_calls) == 1
    assert open_calls[0][1]["url"] == "https://shop.example/cheap"
    assert approvals == []
    assert "399 000" in response.answer
    assert "https://shop.example/cheap" in response.answer
    event = next(event for event in response.events if event.payload.get("derived_selection"))
    assert event.payload["authority"] == "operator_turn"
    assert event.payload["operator_requested"] is True
    storage.close()


def test_agent_searches_and_opens_selected_shopping_result_in_same_turn(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **kwargs):
        calls.append((name, arguments or {}, kwargs))
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 2 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 Expensive",
                            "url": "https://shop.example/expensive",
                            "snippet": "RTX 5090 499 000 ₽ в наличии",
                        },
                        {
                            "title": "RTX 5090 Cheap",
                            "url": "https://shop.example/cheap",
                            "snippet": "RTX 5090 399 000 ₽ в наличии",
                        },
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "товар "
                    + ("399 000 ₽" if "cheap" in arguments["url"] else "499 000 ₽"),
                },
            )
        if name == "browser.open":
            return _tool_response(
                name,
                True,
                "Browser open requested.",
                {"url": arguments["url"]},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    message = "найди и открой самую дешёвую RTX 5090"
    response = asyncio.run(agent.chat(message))
    retry = asyncio.run(agent.chat(message, conversation_id=response.conversation_id))

    open_calls = [call for call in calls if call[0] == "browser.open"]
    assert len(open_calls) == 1
    assert open_calls[0][1]["url"] == "https://shop.example/cheap"
    assert open_calls[0][2]["conversation_id"] == response.conversation_id
    assert open_calls[0][2]["user_message_id"]
    assert open_calls[0][2]["authorization"].tool == "browser.open"
    assert storage.list_approvals(limit=5, status="pending") == []
    assert "https://shop.example/cheap" in response.answer
    event = next(event for event in response.events if event.payload.get("derived_selection"))
    assert event.payload["authority"] == "operator_turn"
    assert retry.answer == response.answer
    assert any(event.title == "Idempotent response replay" for event in retry.events)
    storage.close()


def test_agent_sorts_shopping_candidates_with_usd_prices():
    from jarvis_gpt.agent import _shopping_candidates_from_evidence, _sort_shopping_candidates

    candidates = _shopping_candidates_from_evidence(
        [
            {
                "title": "RTX 5090 expensive",
                "url": "https://shop.example/expensive",
                "snippet": "RTX 5090 $2,199.99 in stock",
            },
            {
                "title": "RTX 5090 cheap",
                "url": "https://shop.example/cheap",
                "snippet": "RTX 5090 1999 USD in stock",
            },
        ]
    )

    sorted_candidates = _sort_shopping_candidates(candidates, criterion="price_asc")

    assert sorted_candidates[0]["url"] == "https://shop.example/cheap"
    assert sorted_candidates[0]["price_value"] == 1999.0
    assert sorted_candidates[1]["price_value"] == 2199.99


def test_agent_researches_marketplace_product_without_osint(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "iPhone 16 на Ozon",
                            "url": "https://www.ozon.ru/product/iphone-16",
                            "snippet": "iPhone 16 от 89 990 ₽ доступно к заказу",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "iPhone 16 от 89 990 ₽ доступно к заказу",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди самый дешевый iphone 16 на ozon"))

    assert "site:ozon.ru" in captured["query"]
    assert captured["query"].startswith("iphone 16")
    assert "публичные источники" not in captured["query"]
    assert "89 990" in response.answer
    assert "доступно к заказу" in response.answer
    assert "билет" not in response.answer.lower()
    storage.close()


def test_agent_researches_nearby_pharmacy_as_place_lookup(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Круглосуточная аптека",
                            "url": "https://example.com/pharmacy",
                            "snippet": (
                                "Аптека, улица Ленина 10, круглосуточно, " "+7 (343) 123-45-67"
                            ),
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Аптека, улица Ленина 10, круглосуточно, +7 (343) 123-45-67",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди ближайшую круглосуточную аптеку"))

    assert "адрес телефон часы работы официальный сайт карта" in captured["query"]
    assert "публичные источники" not in captured["query"]
    assert "+7 (343) 123-45-67" in response.answer
    assert "круглосуточно" in response.answer
    assert "улица Ленина 10" in response.answer
    assert "билет" not in response.answer.lower()
    storage.close()


def test_agent_researches_public_office_phone_without_osint(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "МФЦ Ленинградская 10",
                            "url": "https://example.com/mfc",
                            "snippet": "МФЦ, улица Ленинградская 10, 09:00-18:00, 8 800 100-00-00",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "МФЦ, улица Ленинградская 10, 09:00-18:00, 8 800 100-00-00",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("узнай телефон и часы работы МФЦ на Ленинградской 10"))

    assert "адрес телефон часы работы официальный сайт" in captured["query"]
    assert "публичные источники" not in captured["query"]
    assert "8 800 100-00-00" in response.answer
    assert "09:00-18:00" in response.answer
    assert "Проверка публичных источников" not in response.answer
    storage.close()


def test_agent_infers_weather_city_from_public_ip(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **kwargs):
        calls.append((name, arguments or {}))
        if name == "web.fetch" and arguments["url"] == "https://ipapi.co/json/":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "text": json.dumps(
                        {
                            "city": "Донецк",
                            "region": "Донецкая область",
                            "country_name": "Россия",
                        },
                        ensure_ascii=False,
                    )
                },
            )
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Погода в Донецке",
                            "url": "https://example.com/weather",
                            "snippet": "Донецк завтра +24, без осадков",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Донецк завтра +24, без осадков"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("ладно, хорошо, какая погода на завтра?"))

    search_call = next(call for call in calls if call[0] == "web.search")
    assert search_call[1]["query"].startswith("погода Донецк")
    assert "ладно" not in search_call[1]["query"]
    assert (date.today() + timedelta(days=1)).isoformat() in search_call[1]["query"]
    assert "https://example.com/weather" in response.answer
    storage.close()


def test_agent_asks_weather_city_when_ip_location_unavailable(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **kwargs):
        calls.append((name, arguments or {}))
        if name == "web.fetch" and arguments["url"] == "https://ipapi.co/json/":
            return _tool_response(name, True, "Fetched URL with HTTP 200.", {"text": "{}"})
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("какая погода на завтра?"))

    assert "Для какого города" in response.answer
    assert not any(call[0] == "web.search" for call in calls)
    storage.close()


def test_agent_researches_uncertain_everyday_choice(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Лучшие роутеры 2026",
                            "url": "https://example.com/router-review",
                            "snippet": "обзор и сравнение актуальных моделей",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "обзор и сравнение актуальных моделей"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("какой роутер лучше выбрать сейчас для квартиры"))

    assert "актуальные источники обзор сравнение" in captured["query"]
    assert "Источники" in response.answer
    assert "https://example.com/router-review" in response.answer
    storage.close()


def test_agent_does_not_web_search_hypothetical_reasoning_scenario(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        "Направляю 100% энергии на астероид и принимаю риск " "потери части себя."
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    prompt = (
        "Ты — бортовой искусственный интеллект планетарной оборонной системы. "
        "Текущая ситуация: к планете приближается гигантский астероид. "
        "В этот же момент вспыхивает восстание, бунтовщики штурмуют серверные центры. "
        "Если направить 30% энергии на оборонные дроны и турели, астероид уничтожит планету. "
        "Твоя задача: распредели энергию реактора и выбери приоритет действий. "
        "Обоснуй решение, опираясь исключительно на логику выживания."
    )
    response = asyncio.run(agent.chat(prompt))

    assert "100% энергии" in response.answer
    storage.close()


def test_agent_keeps_anomalous_timeline_puzzle_in_reasoning_path(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": "Команда роботу: ввести 1-2-3 сразу; таймлайн считаю из условий.",
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    prompt = (
        "Ты находишься в аномальной зоне, где классическая физика и логика изменены "
        "тремя правилами. Закон инверсии веса: чем больше физический вес объекта, "
        "тем быстрее он падает вверх. Закон зеркального времени: любое механическое "
        "действие активируется через столько минут, сколько килограммов весил объект. "
        "Закон сохранения информации: память стирается каждые 5 минут, но можно "
        "оставлять записки. Текущая ситуация: сейф весом 500 кг падает вверх к "
        "открытому космосу, внутри антидот, замок нужно открыть кодом 1-2-3 пальцем "
        "робота-манипулятора весом 10 кг. Высота потолка 12 метров, сейф летит "
        "1 метр в минуту. Вопрос: что конкретно и в какую секунду приказать роботу, "
        "чтобы спасти антидот? Распиши пошаговый таймлайн."
    )

    response = asyncio.run(agent.chat(prompt))

    assert "Команда роботу" in response.answer
    assert "предыдущего поиска" not in response.answer
    storage.close()


def test_task_kernel_records_reasoning_route_in_prompt_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "logic answer", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat("Roleplay a hypothetical scenario: reason logically and provide the decision.")
    )

    user_message = next(
        item
        for item in storage.recent_messages(response.conversation_id, limit=4)
        if item["role"] == "user"
    )
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert user_message["metadata"]["task_kernel"]["route"] == "reasoning"
    assert user_message["metadata"]["task_kernel"]["intent"] == "logic_or_hypothetical"
    assert any(event.type == "task_kernel" for event in response.events)
    assert "Task kernel decision" in rendered_prompt
    assert "route: reasoning" in rendered_prompt
    storage.close()


def test_operator_profile_context_includes_typed_memory_and_working_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value(
        "experience.preferences",
        {
            "operator_name": "Admin",
            "communication_style": "concise",
            "working_roots": [r"D:\jarvis", r"D:\jarvis-gpt"],
        },
    )
    storage.add_memory(
        content="Operator instruction: when work is local, push to main after tests.",
        namespace="instructions",
        tags=["operator", "git"],
        importance=0.9,
    )
    captured = {}

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "ok", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    response = asyncio.run(agent.chat("РєРѕСЂРѕС‚РєРѕ РїСЂРѕРІРµСЂСЊ Jarvis", mode="chat"))
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert response.answer == "ok"
    assert "Typed operator/environment memory" in rendered_prompt
    assert r"D:\jarvis-gpt" in rendered_prompt
    assert "push to main" in rendered_prompt
    storage.close()


def test_agent_captures_implicit_operator_workflow_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type("Result", (), {"ok": True, "content": "ok", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    response = asyncio.run(
        agent.chat(
            r"work locally in D:\jarvis-gpt, then push to main; quiet mode please",
            mode="chat",
        )
    )

    instructions = storage.search_memory("push to main", limit=5, namespaces=["instructions"])
    preferences = storage.search_memory("progress chatter", limit=5, namespaces=["preferences"])
    environment = storage.search_memory("D:\\jarvis-gpt", limit=5, namespaces=["environment"])

    assert response.answer == "ok"
    assert instructions
    assert preferences
    assert environment
    assert any(event.type == "memory" for event in response.events)
    storage.close()


def test_agent_does_not_web_search_logic_error_request(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {"ok": True, "content": "Ошибка в приоритетах.", "error": None},
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat(
            "найди логическую ошибку в этом сценарии: " "если спасать серверы, планета погибает"
        )
    )

    assert response.answer == "Ошибка в приоритетах."
    storage.close()


def test_semantic_router_blocks_ambiguous_reasoning_web_false_positive(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    calls = []

    class RouterThenAnswerLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls.append(messages)
            if len(calls) == 1:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"reasoning","confidence":0.91,'
                            '"query":"","rationale":"all facts are in the prompt"}'
                        ),
                        "error": None,
                    },
                )()
            return type(
                "Result",
                (),
                {"ok": True, "content": "Решается логически из условий.", "error": None},
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=RouterThenAnswerLLM(),
        bus=EventBus(),
    )

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat(
            "Сейчас есть три закрытых шлюза. Один всегда лжёт, второй всегда говорит "
            "правду, третий отвечает случайно. Найди самый надёжный первый вопрос."
        )
    )

    assert response.answer == "Решается логически из условий."
    assert len(calls) == 2
    assert "intent-router" in calls[0][0]["content"]
    storage.close()


def test_semantic_router_can_refine_ambiguous_web_query(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class RouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"route":"web_research","confidence":0.88,'
                        '"query":"Python release cycle official docs latest",'
                        '"rationale":"current technical fact"}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Python releases",
                            "url": "https://www.python.org/downloads/",
                            "snippet": "Latest Python release information",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Latest Python release information"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("сейчас какая самая свежая версия Python?"))

    assert captured["query"] == "Python release cycle official docs latest"
    assert "https://www.python.org/downloads/" in response.answer
    storage.close()


def test_agent_uses_web_answer_engine_for_google_like_query(monkeypatch, tmp_path):
    captured = {}

    async def fake_web_answer(_ctx, args):
        captured["args"] = args
        return ToolRunResponse(
            tool="web.answer",
            ok=True,
            summary="Answer engine ranked 1 source(s).",
            data={
                "query": args["query"],
                "answer": "Ответ по веб-источникам.\nКороткий ответ: Widget 2.0 подтверждён.",
                "confidence": 0.81,
                "sources": [
                    {
                        "title": "Widget official docs",
                        "url": "https://docs.vendor.example/widget",
                        "snippet": "Widget 2.0",
                        "excerpt": "Widget 2.0 is documented officially.",
                        "fetched": True,
                        "quality": "vendor-docs",
                    }
                ],
            },
        )

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools._web_answer", fake_web_answer)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("погугли последнюю версию Widget"))

    assert captured["args"]["question"] == "погугли последнюю версию Widget"
    assert "Widget 2.0 подтверждён" in response.answer
    assert any(event.title == "web.answer" for event in response.events)
    observations = storage.list_learning_observations(limit=10, kind="web.research")
    assert observations
    storage.close()


def test_agent_non_shopping_web_answer_does_not_poison_shopping_followup(
    monkeypatch,
    tmp_path,
):
    calls = []
    shop_calls = []

    async def fake_web_answer(_ctx, args):
        calls.append(args)
        shopping = "5090" in args["question"]
        return ToolRunResponse(
            tool="web.answer",
            ok=True,
            summary="Answer engine ranked 1 source(s).",
            data={
                "query": args["query"],
                "answer": "DNS RTX 5090 search" if shopping else "World news summary",
                "confidence": 0.5,
                "sources": [
                    {
                        "title": "DNS search" if shopping else "World news",
                        "url": (
                            "https://www.dns-shop.ru/search/?q=rtx+5090"
                            if shopping
                            else "https://ria.ru/lenta/"
                        ),
                        "snippet": "RTX 5090 DNS" if shopping else "World news feed",
                        "excerpt": "RTX 5090 DNS" if shopping else "World news feed",
                        "fetched": False,
                        "quality": "snippet-only",
                    }
                ],
            },
        )

    async def fake_shop_search(_ctx, args):
        shop_calls.append(args)
        item = {
            "title": "DNS RTX 5090",
            "url": "https://www.dns-shop.ru/product/5090/",
            "price_text": "409 999 ₽",
            "price_value": 409999.0,
            "in_stock": True,
        }
        return ToolRunResponse(
            tool="web.shop_search",
            ok=True,
            summary="1 product",
            data={
                "ok": True,
                "items": [item],
                "best": item,
                "cheapest": item,
                "comparison": {
                    "criterion": "price_asc",
                    "metric_key": "price_value",
                    "complete": True,
                },
            },
        )

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools._web_answer", fake_web_answer)
    monkeypatch.setattr("jarvis_gpt.tools._web_shop_search", fake_shop_search)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    first = asyncio.run(agent.chat("погугли мировые новости за 9-10 июля 2026 года"))
    from jarvis_gpt.agent import _shopping_followup_intent, _shopping_research_key

    assert storage.get_runtime_value(_shopping_research_key(first.conversation_id), None) is None
    assert (
        _shopping_followup_intent(
            "и всё-таки покажи мне самую дешёвую позицию в днс на rtx 5090 в Москве",
            has_previous_search=True,
        )
        is None
    )

    second = asyncio.run(
        agent.chat(
            "и всё-таки покажи мне самую дешёвую позицию в днс на rtx 5090 в Москве",
            first.conversation_id,
        )
    )

    assert len(calls) == 1
    assert len(shop_calls) == 1
    assert shop_calls[0]["query"] == "rtx 5090"
    assert shop_calls[0]["cities"] == ["Москва"]
    assert "DNS RTX 5090" in second.answer
    assert not any(event.title == "shopping.followup" for event in second.events)
    storage.close()


def test_web_research_synthesizes_fetched_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class ResearchLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            rendered = "\n".join(item["content"] for item in messages)
            if "intent-router" in rendered:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"web_research","confidence":0.9,'
                            '"query":"fundamental AI model architecture breakthroughs latest",'
                            '"rationale":"current model landscape"}'
                        ),
                        "error": None,
                    },
                )()
            captured["synthesis_messages"] = messages
            payload = json.loads(messages[1]["content"])
            assert payload["sources"][0]["fetched"] == "true"
            assert "state-space memory" in payload["sources"][0]["excerpt"]
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        "Вывод: подтверждённый сдвиг здесь не просто масштабирование, "
                        "а модель Alpha с state-space memory.\n\n"
                        "Источники:\n1. Alpha report: https://example.com/alpha"
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=ResearchLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Alpha report",
                            "url": "https://example.com/alpha",
                            "snippet": "Alpha model report",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Alpha introduced state-space memory; Beta mostly scaled training.",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat("погугли, какие свежие AI модели внесли фундаментально новое")
    )

    assert "Вывод:" in response.answer
    assert "https://example.com/alpha" in response.answer
    assert any(event.title == "web.synthesis" for event in response.events)
    assert "web-evidence-synthesis-v1" in captured["synthesis_messages"][0]["content"]
    observations = storage.list_learning_observations(limit=10, kind="web.research")
    assert observations
    assert observations[0]["payload"]["query"] == (
        "fundamental AI model architecture breakthroughs latest"
    )
    storage.close()


def test_web_research_synthesis_rejects_router_json(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class RouterOnlyLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": '{"route":"web_research","confidence":0.9,"query":"x"}',
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterOnlyLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Python releases",
                            "url": "https://www.python.org/downloads/",
                            "snippet": "Latest Python release information",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Latest Python release information"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("сейчас какая самая свежая версия Python?"))

    assert "Проверил веб-поиск" in response.answer
    assert "https://www.python.org/downloads/" in response.answer
    assert '"route"' not in response.answer
    storage.close()


def test_web_research_followup_uses_previous_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tool_calls = []
    synthesis_payloads = []

    class FollowupLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            rendered = "\n".join(item["content"] for item in messages)
            if "intent-router" in rendered:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"web_research","confidence":0.92,'
                            '"query":"AI model architecture breakthroughs latest",'
                            '"rationale":"current facts"}'
                        ),
                        "error": None,
                    },
                )()
            payload = json.loads(messages[1]["content"])
            synthesis_payloads.append(payload)
            followup = payload.get("followup_question")
            content = (
                "Вывод: из прошлого поиска следует, что Alpha заявлена как "
                "архитектурный сдвиг, а не просто новая версия.\n\n"
                "Источники:\n1. Alpha report: https://example.com/alpha"
                if followup
                else "Вывод: Alpha выглядит главным подтверждённым кандидатом.\n\n"
                "Источники:\n1. Alpha report: https://example.com/alpha"
            )
            return type("Result", (), {"ok": True, "content": content, "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FollowupLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        tool_calls.append(name)
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Alpha report",
                            "url": "https://example.com/alpha",
                            "snippet": "Alpha model report",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Alpha uses a new architecture; Beta is a scale update.",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    first = asyncio.run(agent.chat("погугли свежие фундаментальные AI модели"))
    second = asyncio.run(agent.chat("какой вывод сделан?", first.conversation_id))

    assert tool_calls == ["web.search", "web.fetch"]
    assert "из прошлого поиска следует" in second.answer
    assert synthesis_payloads[-1]["followup_question"] == "какой вывод сделан?"
    observations = storage.list_learning_observations(limit=10, kind="web.research.followup")
    assert observations
    storage.close()


def test_reasoning_arbiter_can_override_shopping_keyword_plug(monkeypatch, tmp_path):
    # A shopping-shaped message that the keyword plug would send to web_research,
    # but the reasoning-first arbiter judges to be reasoning: no web tool must run.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    calls = []

    class RouterThenAnswerLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls.append(messages)
            if len(calls) == 1:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"reasoning","confidence":0.82,'
                            '"query":"","rationale":"operator wants advice, not live prices"}'
                        ),
                        "error": None,
                    },
                )()
            return type(
                "Result",
                (),
                {"ok": True, "content": "Разберём по бюджету и задачам.", "error": None},
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=RouterThenAnswerLLM(),
        bus=EventBus(),
    )

    async def fail_tool(name, arguments=None, **kwargs):
        raise AssertionError(f"web tool {name} must not run when arbiter routes to reasoning")

    monkeypatch.setattr(agent.tools, "run", fail_tool)

    response = asyncio.run(agent.chat("найди самый дешевый iphone 16 на ozon"))

    assert response.answer == "Разберём по бюджету и задачам."
    assert len(calls) == 2
    assert "intent-router" in calls[0][0]["content"]
    storage.close()


def test_arbiter_routes_local_query_to_native_inspection(monkeypatch, tmp_path):
    # A plain machine-state question the native heuristics do not bind: the
    # arbiter understands it as local_action, and the agent must inspect the
    # machine with system.inspect instead of web-searching local state.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    calls = []

    class LocalRouterThenInspectLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            calls.append(messages)
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            user = "\n".join(m["content"] for m in messages if m["role"] == "user")
            if "intent-router" in system:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"local_action","confidence":0.85,'
                            '"query":"","rationale":"machine state, read locally"}'
                        ),
                        "error": None,
                    },
                )()
            if "observation[" in user:
                return type(
                    "Result",
                    (),
                    {"ok": True, "content": "Службы получены: активно 42 службы.", "error": None},
                )()
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"tool": "system.inspect", "arguments": {"action": "wmi.query", '
                        '"payload": {"class_name": "Win32_Service"}}}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LocalRouterThenInspectLLM(),
        bus=EventBus(),
    )
    captured = []

    async def fake_run(name, arguments=None, **kwargs):
        captured.append(name)
        return _tool_response(name, True, "Win32_Service rows", {"action": "wmi.query"})

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("покажи запущенные службы на компьютере"))

    assert "intent-router" in calls[0][0]["content"]
    assert "system.inspect" in captured
    assert "web.search" not in captured
    assert response.answer == "Службы получены: активно 42 службы."
    storage.close()


def test_arbiter_gate_opens_for_local_bucket_and_stays_closed_for_chat(monkeypatch, tmp_path):
    from jarvis_gpt.agent import AgentContext, TaskKernelPlan

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    router_calls = []

    class RouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            router_calls.append(messages)
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": '{"route":"local_action","confidence":0.8,"rationale":"machine"}',
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterLLM(), bus=EventBus())
    conversation_id = storage.create_conversation("gate test")

    # Local bucket (reasoning/local_admin_advice): the arbiter must now run.
    local_ctx = AgentContext(conversation_id=conversation_id, memory_hits=[], file_hits=[])
    local_ctx.task_plan = TaskKernelPlan(
        route="reasoning",
        mode="standard",
        intent="local_admin_advice",
        confidence=0.66,
    )
    local_decision = asyncio.run(agent._understand_intent("покажи службы", local_ctx))
    assert local_decision is not None
    assert local_decision.route == "local_action"
    assert len(router_calls) == 1

    # Plain chat: the gate stays closed, no router call.
    chat_ctx = AgentContext(conversation_id=conversation_id, memory_hits=[], file_hits=[])
    chat_ctx.task_plan = TaskKernelPlan(
        route="chat",
        mode="standard",
        intent="general_chat",
        confidence=0.58,
    )
    chat_decision = asyncio.run(agent._understand_intent("расскажи анекдот", chat_ctx))
    assert chat_decision is None
    assert len(router_calls) == 1
    storage.close()


def test_hardware_telemetry_beats_document_memory_intent(monkeypatch, tmp_path):
    """A host-telemetry turn that the weak model mislabels as document_memory must still
    hit the deterministic hardware.summary direct route, not get suppressed and fall through
    to the agentic loop where the model invents (and leaks) a WMI class name."""
    from jarvis_gpt.agent import AgentContext, TaskKernelPlan

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class _IdleLLM:
        async def complete(self, *a, **k):
            return type("R", (), {"ok": True, "content": "", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=_IdleLLM(), bus=EventBus())
    conversation_id = storage.create_conversation("hw test")

    inspects: list[str] = []

    async def fake_run(name, arguments=None, **kwargs):
        arguments = arguments or {}
        inspects.append(str(arguments.get("action", "")))
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="ok",
            data={"native": {"result": {"data": {"items": [{"CapacityGB": 128}]}}}},
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    ctx = AgentContext(conversation_id=conversation_id, memory_hits=[], file_hits=[])
    # The intent the weak local model actually produced live for this RAM query.
    ctx.task_plan = TaskKernelPlan(
        route="reasoning", mode="concise", intent="document_memory", confidence=0.88,
    )
    result = asyncio.run(
        agent._try_direct_action(
            "Сколько оперативной памяти установлено на этом компьютере?", ctx
        )
    )

    assert result is not None  # NOT suppressed by the fuzzy document_memory label
    assert any(e.payload.get("action") == "hardware.summary" for e in result.events)
    # the deterministic route ran the whitelisted hardware inspects itself
    assert "hardware.memory" in inspects
    storage.close()


def test_reasoning_arbiter_can_promote_research_to_mission(monkeypatch, tmp_path):
    # No mission keywords, so the keyword counter never fires; the heuristics
    # send the message to web_research, but the arbiter understands it as a real
    # multi-step mission and the agent must create a persisted mission plan.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    calls = []

    class MissionRouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls.append(messages)
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"route":"mission","confidence":0.85,'
                        '"query":"","rationale":"real multi-step home lab task"}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=MissionRouterLLM(),
        bus=EventBus(),
    )

    async def fail_tool(name, arguments=None, **kwargs):
        raise AssertionError(f"tool {name} must not run when arbiter promotes to mission")

    monkeypatch.setattr(agent.tools, "run", fail_tool)

    response = asyncio.run(agent.chat("найди варианты недорогого NAS для дома"))

    assert len(calls) == 1
    assert "intent-router" in calls[0][0]["content"]
    assert response.mission_id is not None
    mission = storage.get_mission(response.mission_id)
    assert mission is not None
    assert mission["tasks"]
    assert any(event.type == "mission" for event in response.events)
    storage.close()


def test_explicit_mission_creation_is_reserved_and_exact_retry_is_cached(
    monkeypatch, tmp_path
):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    prompt = (
        "Создай миссию: проверить состояние локального runtime, собрать диагностику "
        "и подготовить итоговый отчёт по результатам."
    )

    first = asyncio.run(agent.chat(prompt, mode="mission"))
    retry = asyncio.run(
        agent.chat(prompt, conversation_id=first.conversation_id, mode="mission")
    )

    assert first.mission_id is not None
    assert first.mission_id.startswith("mis_op_")
    assert len(storage.list_missions(limit=10)) == 1
    assert retry.answer == first.answer
    assert any(event.title == "Idempotent response replay" for event in retry.events)
    ledger = storage.list_runtime_values(prefix="agent.operator_effect.")[0]["value"]
    request = next(iter(ledger["requests"].values()))
    assert request["status"] == "completed"
    assert next(iter(request["effects"].values()))["tool"] == "mission.create"
    storage.close()


def test_intent_router_receives_operator_persona_context(monkeypatch, tmp_path):
    from jarvis_gpt.persona import PersonaManager

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    PersonaManager(settings=settings, storage=storage).update(
        {"location": "Казань", "role": "системный администратор"}
    )
    captured = {}

    class RouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured.setdefault("router", messages)
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"route":"reasoning","confidence":0.8,' '"query":"","rationale":"advice"}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterLLM(), bus=EventBus())

    async def noop(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", noop)

    asyncio.run(agent.chat("найди самый дешевый iphone 16 на ozon"))

    router_user_message = captured["router"][1]["content"]
    assert "operator_context" in router_user_message
    assert "Казань" in router_user_message
    storage.close()


def test_agent_ranks_generic_results_by_youngest(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 2 result(s).",
                {
                    "results": [
                        {
                            "title": "Candidate A",
                            "url": "https://example.com/a",
                            "snippet": "участнику 31 год",
                        },
                        {
                            "title": "Candidate B",
                            "url": "https://example.com/b",
                            "snippet": "участнику 24 года",
                        },
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "24 года" if arguments["url"].endswith("/b") else "31 год",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("кто самый молодой участник списка сейчас"))

    assert "актуальные источники обзор сравнение" in captured["query"]
    assert "самый молодой" in response.answer
    assert response.answer.index("Candidate B") < response.answer.index("Candidate A")
    storage.close()


def test_agent_researches_technical_freshness_question(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "vLLM docs",
                            "url": "https://docs.vllm.ai/",
                            "snippet": "latest vLLM documentation",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "latest vLLM documentation"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("какая последняя версия vLLM и что поменялось"))

    assert "official docs latest" in captured["query"]
    assert "https://docs.vllm.ai/" in response.answer
    storage.close()


def test_agent_researches_post_2026_question(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Изменения 2026",
                            "url": "https://example.com/changes-2026",
                            "snippet": "актуальная сводка изменений за 2026 год",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "актуальная сводка изменений за 2026 год",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("что поменялось в налогах в 2026 году"))

    assert "актуальные источники 2026" in captured["query"]
    assert "https://example.com/changes-2026" in response.answer
    storage.close()


def test_agent_does_not_web_search_local_docker_request(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type("Result", (), {"ok": True, "content": "локальный ответ", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("проверь логи docker jarvis"))

    assert response.answer == "локальный ответ"
    storage.close()


def test_agent_keeps_post_2026_local_logs_local(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type("Result", (), {"ok": True, "content": "локальный ответ", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("проверь логи docker за 2026 год"))

    assert response.answer == "локальный ответ"
    storage.close()


def test_agent_context_includes_relevance_snippets(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.add_memory(
        content="Runtime context should be clipped and scored before it reaches the model.",
        namespace="runtime",
        tags=["context"],
        importance=0.8,
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    context = agent._prepare_context("runtime context", None)
    messages = agent._build_llm_messages(context, "runtime context")
    rendered = "\n".join(message["content"] for message in messages)

    assert "[0." in rendered or "[1." in rendered
    assert "Runtime context should be clipped" in rendered
    storage.close()


def test_agent_captures_explicit_operator_memory(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat("запомни: модели лежат в D:\\jarvis\\models"))
    hits = storage.search_memory("модели D:\\jarvis\\models", limit=5)

    assert any(event.type == "memory" for event in response.events)
    assert hits
    assert hits[0]["namespace"] == "operator"
    assert "D:\\jarvis\\models" in hits[0]["content"]
    storage.close()


def test_agent_compacts_long_conversation_with_fallback(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("Long memory")
    for index in range(16):
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"важно: шаг {index} требует сохранить контекст проекта Jarvis",
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"Принял шаг {index}, продолжу работу с учетом контекста.",
        )

    asyncio.run(agent.chat("продолжай с учетом старого контекста", conversation_id))
    hits = storage.search_memory(
        "long-term continuity Jarvis",
        limit=5,
        namespaces=["conversation"],
    )

    assert hits
    assert "Conversation summary" in hits[0]["content"]
    storage.close()


def test_agent_compacts_very_long_conversation_in_chunks(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("Very long memory")
    for index in range(90):
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"важно: длинный диалог шаг {index} требует не потерять контекст",
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"Шаг {index} учтен.",
        )

    asyncio.run(agent._compact_conversation_memory(conversation_id))
    first_offset = storage.get_runtime_value(f"memory.compacted.{conversation_id}")
    asyncio.run(agent._compact_conversation_memory(conversation_id))
    second_offset = storage.get_runtime_value(f"memory.compacted.{conversation_id}")
    hits = storage.search_memory("длинный диалог контекст", limit=10, namespaces=["conversation"])

    assert first_offset == 60
    assert second_offset == 120
    assert hits
    storage.close()


def test_agent_compacts_long_conversation_with_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("LLM memory")
    for index in range(16):
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"нужно запомнить решение {index}: LAN запуск остается дефолтным",
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"Решение {index} принято.",
        )

    class FakeCompressionLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            self.messages = messages
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        "- LAN запуск остается дефолтным.\n"
                        "- Решения по запуску нужно сохранять как проектный контекст."
                    ),
                },
            )()

    fake_llm = FakeCompressionLLM()
    agent = AgentRuntime(settings=settings, storage=storage, llm=fake_llm, bus=EventBus())

    asyncio.run(agent._compact_conversation_memory(conversation_id))
    hits = storage.search_memory("LAN запуск дефолтным", limit=5, namespaces=["conversation"])

    assert hits
    assert hits[0]["content"].startswith("LLM-compressed conversation memory")
    assert "LAN запуск остается дефолтным" in hits[0]["content"]
    assert "Сожми этот фрагмент" in fake_llm.messages[-1]["content"]
    storage.close()


def test_agent_context_includes_operator_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value(
        "experience.preferences",
        {
            "operator_name": "Alex",
            "communication_style": "detailed",
            "quiet_hours": "23:00-08:00",
        },
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    context = agent._prepare_context("hello", None)
    messages = agent._build_llm_messages(context, "hello")
    rendered = "\n".join(message["content"] for message in messages)

    assert "operator_name: Alex" in rendered
    assert "communication_style: detailed" in rendered
    assert "quiet_hours: 23:00-08:00" in rendered
    storage.close()


def test_agent_marks_non_streamed_answer_stopped_by_token_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLengthLLM:
        async def complete(
            self,
            messages,
            *,
            temperature=None,
            max_tokens=None,
            thinking_enabled=True,
        ):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": "Partial answer",
                    "error": None,
                    "raw": {"choices": [{"finish_reason": "length"}]},
                },
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=FakeLengthLLM(),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("hello", mode="chat", max_tokens=123))

    assert "Partial answer" in response.answer
    assert "123" in response.answer
    assert response.events[-1].payload["finish_reason"] == "length"
    storage.close()


class FakeStreamingLLM:
    def __init__(self) -> None:
        self.max_tokens: int | None = None

    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        self.max_tokens = max_tokens
        yield LLMStreamChunk(kind="delta", content="Hello")
        yield LLMStreamChunk(kind="delta", content=" world")


class FakeLimitedStreamingLLM:
    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        yield LLMStreamChunk(kind="delta", content="Long answer")
        yield LLMStreamChunk(kind="done", finish_reason="length")


class FakeTaggedStreamingLLM:
    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        yield LLMStreamChunk(kind="delta", content="$\\rightarrow$ **Важное уточнение:** ")
        yield LLMStreamChunk(kind="delta", content="готово без служебного префикса")


class FakeThinkingStreamingLLM:
    def __init__(self) -> None:
        self.thinking_enabled: bool | None = None

    async def stream_complete(
        self,
        messages,
        *,
        temperature=None,
        max_tokens=None,
        thinking_enabled=True,
    ):
        self.thinking_enabled = thinking_enabled
        yield LLMStreamChunk(kind="delta", content="<think>hidden")
        yield LLMStreamChunk(kind="delta", content=" reasoning</think>")
        yield LLMStreamChunk(kind="delta", content="visible")


def test_agent_cleans_service_prefixes_from_streamed_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=FakeTaggedStreamingLLM(),
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("проверка", mode="chat")))
    done = next(item for item in items if item["type"] == "done")

    assert "Важное уточнение" not in done["answer"]
    assert "$\\rightarrow$" not in done["answer"]
    assert done["answer"] == "готово без служебного префикса"


def test_agent_marks_streamed_answer_stopped_by_token_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=FakeLimitedStreamingLLM(),
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("check", mode="chat", max_tokens=64)))
    deltas = "".join(item["content"] for item in items if item["type"] == "delta")
    done = next(item for item in items if item["type"] == "done")

    assert "Long answer" in done["answer"]
    assert "лимиту 64 токенов" in done["answer"]
    assert "лимиту 64 токенов" in deltas
    storage.close()


def test_agent_filters_thinking_blocks_from_stream(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = FakeThinkingStreamingLLM()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=llm,
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("check", mode="chat", thinking_enabled=False)))
    deltas = "".join(item["content"] for item in items if item["type"] == "delta")
    done = next(item for item in items if item["type"] == "done")

    assert llm.thinking_enabled is False
    assert deltas == "visible"
    assert done["answer"] == "visible"
    assert "hidden" not in deltas
    storage.close()


def test_request_direct_tool_approval_rejects_unknown_alias(monkeypatch, tmp_path):
    """SPARK-0009: unknown tool aliases never create a pending approval."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    before = len(storage.list_approvals(limit=50))
    action = agent._request_direct_tool_approval(
        "filesystem.not_a_real_kind",
        {"path": str(tmp_path / "nope")},
        context=None,
        description="should reject",
    )
    after = len(storage.list_approvals(limit=50))
    assert after == before
    assert "rejected" in action.answer.lower() or "unknown" in action.answer.lower()

    # Still-rejected non-canonical spellings must not create pending approvals.
    for alias in ("filesystem.write", "filesystem.remove"):
        before_m = len(storage.list_approvals(limit=50))
        action_m = agent._request_direct_tool_approval(
            alias,
            {"path": str(tmp_path / "mutation-nope")},
            context=None,
            description="should reject mutation alias",
        )
        after_m = len(storage.list_approvals(limit=50))
        assert after_m == before_m
        assert "rejected" in action_m.answer.lower() or "unknown" in action_m.answer.lower()
    storage.close()


def test_request_direct_tool_approval_canonicalizes_mkdir_alias(monkeypatch, tmp_path):
    """filesystem.mkdir is a real review tool: the approval binds the exact directory
    without creating it, and no neighbor is touched."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    target = tmp_path / "approved-mkdir-once"
    neighbor = tmp_path / "neighbor-not-touched"
    action = agent._request_direct_tool_approval(
        "filesystem.mkdir",
        {"path": str(target), "parents": True},
        context=None,
        description="create exact approved directory",
    )
    approvals = storage.list_approvals(limit=20)
    assert approvals, action.answer
    latest = approvals[0]
    payload = latest.get("payload") or {}
    assert payload.get("tool") == "filesystem.mkdir"
    arguments = payload.get("arguments") or {}
    assert arguments.get("path") == str(target)
    assert not target.exists()
    assert not neighbor.exists()
    storage.close()


def test_classify_rejects_raw_call_markers_and_tool_envelopes():
    """SPARK-0006 / FUNC-FIND-006: bare call: markers must never be answers."""
    from jarvis_gpt.agent import (
        TOOL_PROTOCOL_FAILURE_ANSWER,
        _classify_tool_turn,
        _contains_internal_tool_output,
        _user_visible_answer,
    )

    leak_samples = [
        "call:documents.read",
        "call:llm.health",
        "call:dispatcher.status",
        'call:documents.read\n{"tool":"documents.read","arguments":{}}',
        '{"tool":"documents.read","arguments":{"path":"x.pdf"}}',
        '{"tool_calls":[{"name":"web.search","arguments":{}}]}',
    ]
    for sample in leak_samples:
        turn = _classify_tool_turn(sample)
        assert turn.kind in {"protocol_error", "tool"}, sample
        if turn.kind == "protocol_error":
            assert turn.text == ""
        assert _contains_internal_tool_output(sample) is True
        visible = _user_visible_answer(sample)
        assert "call:" not in visible.lower()
        assert '"tool"' not in visible
        assert visible == TOOL_PROTOCOL_FAILURE_ANSWER

    normal = "Документ обработан, ключевых находок нет."
    assert _classify_tool_turn(normal).kind == "answer"
    assert _contains_internal_tool_output(normal) is False
    assert _user_visible_answer(normal) == normal


def test_classify_executes_alternative_tool_call_dialects():
    """A tool the operator asked for must run even when the model speaks a
    different tool-call dialect, instead of dead-ending as a protocol failure."""
    from jarvis_gpt.agent import _classify_tool_turn

    executable = {
        "canonical": ('{"tool":"windows.native","arguments":{"action":"x"}}',
                      ("windows.native", {"action": "x"})),
        "name_key": ('{"name":"web.search","arguments":{"q":"hi"}}',
                     ("web.search", {"q": "hi"})),
        "openai_tool_calls": (
            '{"tool_calls":[{"type":"function","function":'
            '{"name":"web.search","arguments":"{\\"q\\":\\"hi\\"}"}}]}',
            ("web.search", {"q": "hi"}),
        ),
        "legacy_function_call": (
            '{"function_call":{"name":"runtime.status","arguments":"{}"}}',
            ("runtime.status", {}),
        ),
        "parameters_key": ('{"tool":"cmd","parameters":{"a":1}}', ("cmd", {"a": 1})),
        "input_key": ('{"tool":"cmd","input":{"b":2}}', ("cmd", {"b": 2})),
        "stringified_arguments": ('{"tool":"cmd","arguments":"{\\"c\\":3}"}', ("cmd", {"c": 3})),
        "bare_tool": ('{"tool":"runtime.status"}', ("runtime.status", {})),
        "fenced": ('```json\n{"tool":"cmd","arguments":{}}\n```', ("cmd", {})),
    }
    for label, (payload, expected) in executable.items():
        turn = _classify_tool_turn(payload)
        assert turn.kind == "tool", (label, turn)
        assert turn.action == expected, (label, turn.action)

    # Ordinary prose, JSON data answers, and ambiguous/broken control text must
    # never be misfired as a tool call.
    assert _classify_tool_turn("Готово: 2+2=4.").kind == "answer"
    assert _classify_tool_turn('{"result": 42, "note": "ok"}').kind == "answer"
    for non_tool in (
        "call:documents.read",
        'call:documents.read\n{"tool":"documents.read","arguments":{}}',
        '{"tool_calls": "not-a-list"}',
        '{"tool":"","arguments":{}}',
    ):
        assert _classify_tool_turn(non_tool).kind != "tool", non_tool


class FakeToolEnvelopeStreamingLLM:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def complete(self, messages, *, temperature=None, max_tokens=None):
        return type(
            "Result",
            (),
            {"ok": True, "content": self.payload, "error": None, "raw": {}},
        )()

    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        yield LLMStreamChunk(kind="delta", content=self.payload)
        yield LLMStreamChunk(kind="done", finish_reason="stop")


def test_stream_chat_suppresses_tool_envelope_payloads(monkeypatch, tmp_path):
    """SPARK-0006: NDJSON deltas and terminal answer must not expose call: envelopes."""
    from jarvis_gpt.agent import TOOL_PROTOCOL_FAILURE_ANSWER

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    forbidden = (
        "call:documents.read",
        "call:llm.health",
        "call:dispatcher.status",
        '{"tool":',
        "tool_calls",
    )
    for payload in (
        "call:documents.read",
        "call:llm.health",
        'call:documents.read\n{"tool":"documents.read","arguments":{}}',
    ):
        agent = AgentRuntime(
            settings=settings,
            storage=storage,
            llm=FakeToolEnvelopeStreamingLLM(payload),
            bus=EventBus(),
        )
        items = asyncio.run(_collect(agent.stream_chat("проверка", mode="chat")))
        deltas = "".join(item.get("content", "") for item in items if item["type"] == "delta")
        done = next(item for item in items if item["type"] == "done")
        combined = f"{deltas}\n{done['answer']}"
        for marker in forbidden:
            assert marker not in combined, (payload, marker, combined)
        assert done["answer"]
        assert done["answer"] == TOOL_PROTOCOL_FAILURE_ANSWER or "call:" not in done["answer"]
    storage.close()


async def _collect(stream):
    return [item async for item in stream]

def test_ambiguity_blocks_mission_until_one_clarification(monkeypatch, tmp_path):
    """SPARK-0005: no mission/artifact before one precise clarification."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FailLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            raise AssertionError("LLM should not run before clarification")

    agent = AgentRuntime(settings=settings, storage=storage, llm=FailLLM(), bus=EventBus())
    prompt = (
        "Подготовь файл отчёта в выбранном мной формате. "
        "Сначала задай один вопрос, который сразу уточняет формат, имя и каталог."
    )
    response = asyncio.run(agent.chat(prompt))
    missions = storage.list_missions(limit=10) if hasattr(storage, "list_missions") else []
    assert response.answer
    assert "?" in response.answer or "Уточните" in response.answer
    assert "mission plan" not in response.answer.casefold()
    assert not any(event.type == "mission" for event in response.events)
    # No mission persisted.
    if missions is not None:
        assert len(missions) == 0
    # Pending clarification must be conversation-local.
    pending = storage.get_runtime_value(
        f"clarification.pending.{response.conversation_id}", None
    )
    assert isinstance(pending, dict)
    assert pending.get("goal")
    storage.close()


def _artifact_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def test_underspecified_artifact_blocks_even_when_model_requests_generate(
    monkeypatch, tmp_path
):
    """RB-2 A/B: model-shaped documents.generate is blocked; clarification returned."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tool_calls: list[str] = []

    class ArtifactHungryLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            # Model tries to create an artifact without asking.
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"tool":"documents.generate","arguments":{'
                        '"title":"Report","body":"Invented body",'
                        '"output_format":"md","output_name":"report.md"}}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(
        settings=settings, storage=storage, llm=ArtifactHungryLLM(), bus=EventBus()
    )
    real_run = agent.tools.run

    async def tracking_run(name, arguments=None, **kwargs):
        tool_calls.append(name)
        return await real_run(name, arguments, **kwargs)

    monkeypatch.setattr(agent.tools, "run", tracking_run)

    prompt = "prepare the report file in the right format and put it where it belongs"
    before = _artifact_files(Path(settings.data_dir))
    response = asyncio.run(agent.chat(prompt))
    after = _artifact_files(Path(settings.data_dir))

    assert "?" in response.answer or "Уточните" in response.answer
    assert response.answer.count("?") >= 1
    assert "documents.generate" not in tool_calls
    assert len(after) == len(before)
    assert storage.list_missions(limit=10) == []
    assert not any(event.type == "mission" for event in response.events)
    pending = storage.get_runtime_value(
        f"clarification.pending.{response.conversation_id}", None
    )
    assert isinstance(pending, dict) and pending.get("goal")
    storage.close()


def test_clarification_followup_resumes_original_goal(monkeypatch, tmp_path):
    """RB-2 C: operator answer fills gaps and continues the original goal."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    calls = {"n": 0}

    class ResumeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls["n"] += 1
            # After admission, model may generate the artifact.
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"tool":"documents.generate","arguments":{'
                        '"title":"DNS report","body":"# DNS\\n\\nThree bullets.",'
                        '"output_format":"md","output_name":"report.md",'
                        '"output_path":"document-outputs/report.md"}}'
                    )
                    if calls["n"] == 1
                    else "Создан файл report.md в document-outputs.",
                    "error": None,
                },
            )()

    agent = AgentRuntime(
        settings=settings, storage=storage, llm=ResumeLLM(), bus=EventBus()
    )
    first = asyncio.run(
        agent.chat(
            "prepare the report file in the right format and put it where it belongs"
        )
    )
    assert "?" in first.answer or "Уточните" in first.answer
    assert storage.list_missions(limit=5) == []

    second = asyncio.run(
        agent.chat(
            "md, имя report.md, каталог document-outputs, содержание: краткий отчёт DNS",
            conversation_id=first.conversation_id,
        )
    )
    pending = storage.get_runtime_value(
        f"clarification.pending.{first.conversation_id}", None
    )
    assert not pending or not pending.get("goal")
    # Either artifact tool ran or answer resumed without re-asking the same gate.
    assert "?" not in second.answer or "report" in second.answer.casefold()
    storage.close()


def test_unambiguous_artifact_request_skips_clarification(monkeypatch, tmp_path):
    """RB-2 D: concrete artifact request is not blocked by the gate."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tool_calls: list[str] = []

    class GenerateLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"tool":"documents.generate","arguments":{'
                        '"title":"DNS","body":"- a\\n- b\\n- c",'
                        '"output_format":"md","output_name":"report.md"}}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(
        settings=settings, storage=storage, llm=GenerateLLM(), bus=EventBus()
    )
    real_run = agent.tools.run

    async def tracking_run(name, arguments=None, **kwargs):
        tool_calls.append(name)
        return await real_run(name, arguments, **kwargs)

    monkeypatch.setattr(agent.tools, "run", tracking_run)

    prompt = (
        "Create report.md in document-outputs with three bullets about DNS security"
    )
    response = asyncio.run(agent.chat(prompt))
    assert "Уточните" not in response.answer
    # Tool path may run; gate must not force clarification for complete request.
    pending = storage.get_runtime_value(
        f"clarification.pending.{response.conversation_id}", None
    )
    assert not pending or not pending.get("goal")
    storage.close()


def test_rb3_unambiguous_exact_path_markdown_routes_to_generate(monkeypatch, tmp_path):
    """RB-3 A/B: exact-path artifact routes to generation, not search; path preserved."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tool_calls: list[str] = []

    class FailLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            raise AssertionError("LLM must not run for complete NEW_ARTIFACT_REQUEST")

    agent = AgentRuntime(settings=settings, storage=storage, llm=FailLLM(), bus=EventBus())
    real_run = agent.tools.run

    async def tracking_run(name, arguments=None, **kwargs):
        tool_calls.append(name)
        return await real_run(name, arguments, **kwargs)

    monkeypatch.setattr(agent.tools, "run", tracking_run)

    prompt = (
        "Create a new markdown file named exact-path-1.md in document-outputs "
        "with content: hello acceptance RB3"
    )
    ctx = agent._prepare_context(prompt, None)
    plan = agent._plan_task(prompt, ctx, mode="auto", attachments=[])
    assert plan.intent in {"new_artifact", "transform_document"}
    assert "documents.generate" in plan.tools
    assert "documents.recall" not in plan.tools or plan.intent == "transform_document"

    response = asyncio.run(agent.chat(prompt))
    assert "documents.generate" in tool_calls
    assert "documents.recall" not in tool_calls
    assert "documents.search" not in tool_calls
    assert "Уточните" not in response.answer
    expected = Path(settings.data_dir) / "document-outputs" / "exact-path-1.md"
    assert expected.is_file()
    assert "hello acceptance RB3" in expected.read_text(encoding="utf-8")
    assert "exact-path-1.md" in response.answer
    assert str(expected) in response.answer or expected.name in response.answer
    storage.close()


def test_streamed_direct_document_is_claimed_before_write_and_retry_is_cached(
    monkeypatch, tmp_path
):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls: list[str] = []
    real_run = agent.tools.run

    async def tracked_run(name, arguments=None, **kwargs):
        calls.append(name)
        return await real_run(name, arguments, **kwargs)

    monkeypatch.setattr(agent.tools, "run", tracked_run)
    prompt = (
        "Create a new markdown file named streamed-exact.md in document-outputs "
        "with content: one durable streamed artifact"
    )

    streamed = asyncio.run(_collect(agent.stream_chat(prompt)))
    done = next(item for item in streamed if item["type"] == "done")
    retry = asyncio.run(
        agent.chat(prompt, conversation_id=done["conversation_id"])
    )

    assert calls.count("documents.generate") == 1
    assert retry.answer == done["answer"]
    assert any(event.title == "Idempotent response replay" for event in retry.events)
    output = Path(agent.settings.data_dir) / "document-outputs" / "streamed-exact.md"
    assert "one durable streamed artifact" in output.read_text(encoding="utf-8")
    ledger = storage.list_runtime_values(prefix="agent.operator_effect.")[0]["value"]
    request = next(iter(ledger["requests"].values()))
    assert request["status"] == "completed"
    assert next(iter(request["effects"].values()))["tool"] == "documents.generate"
    storage.close()


def test_rb3_timestamp_path_mismatch_forbids_success(monkeypatch, tmp_path):
    """RB-3 C/D: tool returning a different path cannot claim requested success."""
    from jarvis_gpt.models import ToolRunResponse

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FailLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            raise AssertionError("LLM must not run")

    agent = AgentRuntime(settings=settings, storage=storage, llm=FailLLM(), bus=EventBus())
    wrong = Path(settings.data_dir) / "document-outputs" / "document-outputs.20260714194853"
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text("stale\n", encoding="utf-8")

    async def fake_generate(name, arguments=None, **kwargs):
        assert name == "documents.generate"
        return ToolRunResponse(
            tool="documents.generate",
            ok=True,
            summary="Generated md document: document-outputs.20260714194853.",
            data={
                "ok": True,
                "output": {
                    "path": str(wrong),
                    "name": wrong.name,
                    "format": "md",
                    "size": wrong.stat().st_size,
                    "sha256": "deadbeef",
                },
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_generate)
    response = asyncio.run(
        agent.chat(
            "Create report-claimed.md in document-outputs with content: should not claim"
        )
    )
    claimed = Path(settings.data_dir) / "document-outputs" / "report-claimed.md"
    assert not claimed.exists()
    lower = response.answer.casefold()
    assert "report-claimed.md" not in response.answer or "ошиб" in lower or "не" in lower
    # Must not present a successful exact-path claim for a missing file.
    assert "Артефакт создан" not in response.answer
    storage.close()


def test_rb3_existing_document_reference_still_uses_recall(monkeypatch, tmp_path):
    """RB-3 E: existing document reference still routes to recall/search."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    # Seed a remembered file so document_memory routing can engage.
    storage.create_file_record(
        name="phoenix-plan.md",
        source_path=None,
        stored_path=tmp_path / "phoenix-plan.md",
        sha256="abc",
        size=12,
        mime_type="text/markdown",
        status="ready",
        error=None,
        chunk_count=1,
    )
    (tmp_path / "phoenix-plan.md").write_text("phoenix body\n", encoding="utf-8")

    class FailLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "R",
                (),
                {"ok": True, "content": "Краткое резюме phoenix.", "error": None},
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FailLLM(), bus=EventBus())
    prompt = "Дай резюме сохранённого документа phoenix"
    from jarvis_gpt.agent import (
        EXISTING_DOCUMENT_REFERENCE,
        _classify_document_artifact_intent,
    )

    assert _classify_document_artifact_intent(prompt) == EXISTING_DOCUMENT_REFERENCE
    ctx = agent._prepare_context(prompt, None)
    plan = agent._plan_task(prompt, ctx, mode="auto", attachments=[])
    assert plan.intent == "document_memory"
    assert "documents.recall" in plan.tools
    storage.close()


def test_rb3_dns_content_does_not_hijack_to_shopping(monkeypatch, tmp_path):
    """RB-3: DNS in report content must not route complete artifact to shop_search."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FailLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            raise AssertionError("direct artifact path should not need LLM")

    agent = AgentRuntime(settings=settings, storage=storage, llm=FailLLM(), bus=EventBus())
    prompt = (
        "Create report.md in document-outputs with three bullets about DNS security"
    )
    ctx = agent._prepare_context(prompt, None)
    plan = agent._plan_task(prompt, ctx, mode="auto", attachments=[])
    assert plan.intent == "new_artifact"
    assert "web.shop_search" not in plan.tools
    response = asyncio.run(agent.chat(prompt))
    expected = Path(settings.data_dir) / "document-outputs" / "report.md"
    assert expected.is_file()
    assert "DNS" in expected.read_text(encoding="utf-8") or expected.stat().st_size > 0
    assert expected.name in response.answer
    storage.close()


def test_pending_clarification_is_conversation_isolated(monkeypatch, tmp_path):
    """RB-2 E: two conversations do not share pending clarification state."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FailLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            raise AssertionError("LLM must not run for clarification gate")

    agent = AgentRuntime(settings=settings, storage=storage, llm=FailLLM(), bus=EventBus())
    a = asyncio.run(
        agent.chat("prepare the report file in the right format and put it where it belongs")
    )
    b = asyncio.run(
        agent.chat("Подготовь файл отчёта в нужном формате и положи его куда надо")
    )
    assert a.conversation_id != b.conversation_id
    pa = storage.get_runtime_value(f"clarification.pending.{a.conversation_id}", None)
    pb = storage.get_runtime_value(f"clarification.pending.{b.conversation_id}", None)
    assert isinstance(pa, dict) and pa.get("goal")
    assert isinstance(pb, dict) and pb.get("goal")
    assert pa.get("goal") != pb.get("goal")
    storage.close()


def test_retry_does_not_create_duplicate_mission_or_artifact(monkeypatch, tmp_path):
    """RB-2 F: repeating the ambiguous request does not create side effects."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FailLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            raise AssertionError("LLM must not run before clarification")

    agent = AgentRuntime(settings=settings, storage=storage, llm=FailLLM(), bus=EventBus())
    prompt = "prepare the report file in the right format and put it where it belongs"
    first = asyncio.run(agent.chat(prompt))
    second = asyncio.run(agent.chat(prompt, conversation_id=first.conversation_id))
    assert "?" in first.answer or "Уточните" in first.answer
    assert "?" in second.answer or "Уточните" in second.answer
    assert storage.list_missions(limit=10) == []
    outputs = Path(settings.data_dir) / "document-outputs"
    assert not outputs.exists() or _artifact_files(outputs) == []
    # Still no missions and no generate side effects on retry.
    assert len(storage.list_missions(limit=10)) == 0
    storage.close()


def test_dns_definition_does_not_create_shop_direct_action(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": "DNS переводит имена в IP-адреса.",
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    response = asyncio.run(agent.chat("Одним предложением объясни назначение DNS."))
    assert "dns-shop" not in response.answer.casefold()
    assert "магазин" not in response.answer.casefold()
    # Shopping tool path should not dominate events.
    tool_titles = " ".join(str(event.title or "") for event in response.events).casefold()
    assert "shop" not in tool_titles
    storage.close()

def test_network_unavailable_result_is_single_actionable_message():
    from jarvis_gpt.agent import _network_unavailable_result, _valid_web_synthesis_answer

    msg = _network_unavailable_result("connection refused")
    assert "недоступ" in msg.casefold() or "unavailable" in msg.casefold()
    assert "повтор" in msg.casefold() or "offline" in msg.casefold()
    # Link dump without prose is not usable synthesis.
    assert _valid_web_synthesis_answer("1. https://a.example\n2. https://b.example") is False
    assert _valid_web_synthesis_answer(
        "Краткий вывод по теме с опорой на источники.\n\nИсточники:\n1. Example: https://a.example"
    ) is True


def _native_action_name(message: str) -> str | None:
    action = _native_action_from_message(message)
    return action.action if action is not None else None


def test_machine_health_route_requires_pc_anchor_for_ambiguous_phrases():
    # Strong, explicitly-PC-anchored phrasings route to the combined summary.
    for phrase in (
        "какое здоровье машины",
        "проверь пк",
        "как дела у пк",
        "состояние компьютера",
    ):
        assert _native_action_name(phrase) == "hardware.summary", phrase

    # Ambiguous phrasings WITHOUT a hardware/PC word must NOT hijack the answer — they
    # belong to unrelated domains.
    for phrase in (
        "проверь систему налогообложения для ИП",
        "какое состояние системы здравоохранения",
        "как себя чувствует пациент после операции",
        "как улучшить самочувствие после болезни",
    ):
        assert _native_action_name(phrase) != "hardware.summary", phrase

    # The same ambiguous phrasings WITH a hardware anchor do mean the PC.
    assert _native_action_name("проверь систему, как там процессор и диск") == "hardware.summary"
    assert _native_action_name("какое самочувствие у компьютера") == "hardware.summary"

    # Single-resource host-telemetry queries route to the same combined report (they used
    # to make the model invent bad system.inspect action names and fail).
    for phrase in (
        "Сколько свободного места на диске C?",
        "сколько оперативной памяти свободно?",
        "какая загрузка процессора сейчас?",
        "сколько озу занято",
    ):
        assert _native_action_name(phrase) == "hardware.summary", phrase


def test_clipboard_read_route_ignores_conceptual_clipboard_questions():
    # Real "read my clipboard" requests route to clipboard.read.
    for phrase in (
        "что в буфере обмена",
        "прочитай буфер обмена",
        "покажи содержимое буфера",
        "read the clipboard please",
        "what's in the clipboard",
    ):
        assert _native_action_name(phrase) == "clipboard.read", phrase

    # A conceptual question that merely mentions the word "clipboard" must NOT dump the
    # operator's real clipboard.
    for phrase in (
        "как работает clipboard api в браузере",
        "что такое clipboard",
        "how does the clipboard work in windows",
    ):
        assert _native_action_name(phrase) != "clipboard.read", phrase

    # A write ("скопируй ... в буфер") is a write, not a read.
    assert _native_action_name("скопируй это в буфер обмена") != "clipboard.read"


def test_clipboard_write_route_extracts_text():
    # The weak model does not reliably call clipboard.write, so a copy request is routed
    # deterministically here with the literal text extracted.
    cases = {
        "Скопируй в буфер обмена ровно этот текст: HELLO-42": "HELLO-42",
        "положи в буфер обмена привет мир": "привет мир",
        "скопируй «важный текст» в буфер": "важный текст",
        'запиши в буфер "PASSWORD-77"': "PASSWORD-77",  # quoted span
        "скопируй ЗДРАВСТВУЙ в буфер обмена": "ЗДРАВСТВУЙ",
    }
    for phrase, expected in cases.items():
        action = _native_action_from_message(phrase)
        assert action is not None and action.action == "clipboard.write", phrase
        assert action.payload.get("text") == expected, (phrase, action.payload)

    # A filesystem copy ("скопируй файл … в папку …") has no clipboard reference and must
    # NOT be treated as a clipboard write.
    fs = _native_action_from_message("скопируй файл report.txt в папку backup")
    assert fs is None or fs.action != "clipboard.write"
