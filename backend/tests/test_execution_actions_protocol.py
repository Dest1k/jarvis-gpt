from __future__ import annotations

import asyncio
import base64

import pytest
from jarvis_gpt.execution_actions import (
    AtomicActionExecutor,
    ListDirectoryAction,
    PathPolicy,
    ReadFileAction,
    StatPathAction,
    WriteFileAction,
)
from jarvis_gpt.execution_protocol import (
    ActionClass,
    action_json_schema,
    classify_payload,
    parse_action,
)
from pydantic import ValidationError


def test_typed_filesystem_actions_round_trip_without_shell(tmp_path):
    executor = AtomicActionExecutor(path_policy=PathPolicy((tmp_path,)))
    target = tmp_path / "nested" / "value.bin"

    written = asyncio.run(
        executor.execute(
            WriteFileAction(path=target, content=b"abc", create_parents=True)
        )
    )
    read = asyncio.run(executor.execute(ReadFileAction(path=target, max_bytes=2)))
    listed = asyncio.run(executor.execute(ListDirectoryAction(path=target.parent)))
    metadata = asyncio.run(executor.execute(StatPathAction(path=target)))

    assert written.ok is True
    assert base64.b64decode(read.after["content_base64"]) == b"ab"
    assert read.after["truncated"] is True
    assert listed.after["entries"][0]["name"] == "value.bin"
    assert metadata.after["sha256"]


def test_path_policy_rejects_escape_and_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    policy = PathPolicy((root,))

    with pytest.raises(ValueError, match="escapes"):
        policy.resolve(tmp_path / "outside.txt")

    link = root / "link"
    try:
        link.symlink_to(tmp_path)
    except OSError:
        pytest.skip("symlinks are unavailable for this user")
    with pytest.raises(ValueError, match="symbolic"):
        policy.resolve(link)


def test_protocol_is_strict_versioned_and_discriminated(tmp_path):
    target = tmp_path / "value.txt"
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": "write-1",
            "path": str(target),
            "content_base64": "YWJj",
            "create_parents": False,
        },
    }

    action = parse_action(payload)

    assert isinstance(action, WriteFileAction)
    assert action.content == b"abc"
    assert classify_payload(payload) is ActionClass.MUTATION
    assert "discriminator" in str(action_json_schema())

    invalid = {**payload, "unexpected": True}
    with pytest.raises(ValidationError):
        parse_action(invalid)

    with pytest.raises(ValidationError):
        parse_action({"action": payload["action"]})

    del invalid["unexpected"]
    invalid["action"] = {**payload["action"], "path": "relative.txt"}
    with pytest.raises(ValueError, match="absolute"):
        parse_action(invalid)


def test_protocol_rejects_invalid_base64(tmp_path):
    with pytest.raises(ValueError, match="Base64"):
        parse_action(
            {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "kind": "fs.write",
                    "path": str(tmp_path / "value"),
                    "content_base64": "!!!",
                },
            }
        )
