from __future__ import annotations

import asyncio
import base64
import hashlib
import os

import pytest
from jarvis_gpt.execution_actions import (
    AtomicActionExecutor,
    DeleteFileAction,
    ListDirectoryAction,
    PathPolicy,
    ReadFileAction,
    StatPathAction,
    WriteFileAction,
)
from jarvis_gpt.execution_filesystem import BoundPath
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
        executor.execute(WriteFileAction(path=target, content=b"abc", create_parents=True))
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


def test_expected_digest_delete_never_unlinks_a_raced_replacement(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"verified identity")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"must survive")
    saved_original = tmp_path / "saved-original.txt"
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    original_replace = BoundPath.replace_from
    raced = False

    def race_before_stage(self, source):
        nonlocal raced
        if not raced and self.name.endswith(".jarvis-stage"):
            raced = True
            target.replace(saved_original)
            replacement.replace(target)
        return original_replace(self, source)

    monkeypatch.setattr(BoundPath, "replace_from", race_before_stage)
    executor = AtomicActionExecutor(path_policy=PathPolicy((tmp_path,)))

    result = asyncio.run(executor.execute(DeleteFileAction(path=target, expected_sha256=expected)))

    assert raced is True
    assert result.ok is False
    assert "identity changed" in result.error
    assert target.read_bytes() == b"must survive"
    assert saved_original.read_bytes() == b"verified identity"


def test_bound_temporary_identity_rejects_a_post_close_replacement(tmp_path):
    policy = PathPolicy((tmp_path,))
    target = tmp_path / "target.txt"
    attacker = tmp_path / "attacker.txt"
    attacker.write_bytes(b"substituted")

    with policy.mutation_scope((target,)):
        destination = policy.bind_mutation_path(target)
        temporary = destination.sibling(".target.pinned.tmp")
        descriptor = temporary.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, b"trusted")
        os.close(descriptor)
        temporary.path.unlink()
        attacker.replace(temporary.path)

        with pytest.raises(RuntimeError, match="identity changed"):
            destination.replace_from(temporary)

    assert not target.exists()
    assert temporary.path.read_bytes() == b"substituted"


def test_read_never_follows_a_parent_identity_swap(tmp_path):
    root = tmp_path / "root"
    parent = root / "parent"
    parent.mkdir(parents=True)
    target = parent / "value.txt"
    target.write_bytes(b"allowed")
    relocated = root / "relocated"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("value.txt").write_bytes(b"outside-secret")
    policy = PathPolicy((root,))
    executor = AtomicActionExecutor(path_policy=policy)

    with policy.mutation_scope((target,)):
        try:
            parent.replace(relocated)
        except OSError:
            swap_blocked = True
        else:
            swap_blocked = False
            if os.name == "nt":
                parent.mkdir()
                target.write_bytes(b"replacement-secret")
            else:
                parent.symlink_to(outside, target_is_directory=True)
        result = asyncio.run(executor.execute(ReadFileAction(path=target)))

    if swap_blocked:
        assert result.ok is True
        assert base64.b64decode(result.after["content_base64"]) == b"allowed"
    elif os.name == "nt":
        assert result.ok is False
        assert "parent identity changed" in result.error
    else:
        assert result.ok is True
        assert base64.b64decode(result.after["content_base64"]) == b"allowed"


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
