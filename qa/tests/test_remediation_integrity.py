from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import qa.evidence as evidence_module
from qa.cli import main
from qa.evidence import (
    compare_audit_content_manifests,
    write_audit_content_manifest,
)
from qa.overlay import verify_overlay_source_pins, verify_reviewed_input_head
from qa.safe_paths import SafePathError

OVERLAY_RELATIVE = "docs/assurance/remediation/test/WAVES.yml"
QUEUE_HEADER = "task_id,priority,status,title,finding_id,dependencies,task_path,acceptance\n"
WAVE_TASKS = {
    "WAVE-0": (17, 16, 6, 9, 15, 11),
    "WAVE-1": (14, 7, 3, 8),
    "WAVE-2": (2, 4, 5, 12, 1, 10),
}
ROOT = Path(__file__).resolve().parents[2]
GIT_EXECUTABLE = Path(shutil.which("git") or "git-not-found").resolve()


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env=_git_environment(),
        shell=False,
        text=True,
        timeout=15,
    )
    return completed.stdout.strip()


def _init_repository(repository: Path) -> None:
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "Disposable Fixture")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "commit.gpgsign", "false")


def _write(repository: Path, relative: str, payload: bytes) -> None:
    target = repository / Path(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _overlay_fixture(repository: Path) -> tuple[str, str]:
    functional_run = ".audit/runs/sanitized/functional"
    source_paths = {
        "state": f"{functional_run}/FUNCTIONAL_STATE.json",
        "findings_index": f"{functional_run}/FUNCTIONAL_FINDINGS_INDEX.md",
        "queue": f"{functional_run}/spark/QUEUE.csv",
        "task_schema": f"{functional_run}/spark/TASK_SCHEMA.md",
    }
    _write(repository, source_paths["state"], b'{"status":"SANITIZED"}\r\n')
    findings = "\n".join(f"FUNC-FIND-{index:03d}" for index in range(1, 18)) + "\n"
    _write(repository, source_paths["findings_index"], findings.encode())
    queue_rows = []
    for index in range(1, 18):
        queue_rows.append(
            f"SPARK-{index:04d},P2,READY,Fixture {index},"
            f"FUNC-FIND-{index:03d},,tasks/SPARK-{index:04d}.md,fixture\n"
        )
        _write(
            repository,
            f"{functional_run}/spark/tasks/SPARK-{index:04d}.md",
            (
                f"# Sanitized task SPARK-{index:04d}\n"
                f"Finding: FUNC-FIND-{index:03d}\n"
            ).encode(),
        )
    _write(repository, source_paths["queue"], (QUEUE_HEADER + "".join(queue_rows)).encode())
    _write(repository, source_paths["task_schema"], b"# Sanitized task schema\n")
    _git(repository, "add", "--", ".audit")
    _git(repository, "commit", "--quiet", "-m", "sanitized immutable inputs")
    source_commit = _git(repository, "rev-parse", "HEAD")

    lines = [
        "schema_version: 1",
        "immutable_sources:",
        '  hash_convention: "sha256_git_blob_raw_bytes_v1"',
        f'  functional_run: "{functional_run}"',
    ]
    for name in ("state", "findings_index", "queue", "task_schema"):
        path = source_paths[name]
        oid = _git(repository, "rev-parse", f"{source_commit}:{path}")
        raw = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "blob", oid],
            check=True,
            capture_output=True,
            env=_git_environment(),
            shell=False,
            timeout=15,
        ).stdout
        lines.extend(
            [
                f"  {name}:",
                f'    source_commit: "{source_commit}"',
                f'    path: "{path}"',
                f'    git_blob_oid: "{oid}"',
                f'    sha256: "{hashlib.sha256(raw).hexdigest()}"',
            ]
        )
    lines.append("waves:")
    for wave, task_numbers in WAVE_TASKS.items():
        lines.append(f'  - id: "{wave}"')
        lines.append("    tasks:")
        for order, index in enumerate(task_numbers, start=1):
            lines.extend(
                [
                    f"      - order: {order}",
                    f'        task_id: "SPARK-{index:04d}"',
                    f'        finding_id: "FUNC-FIND-{index:03d}"',
                    "        task_path: "
                    f'"{functional_run}/spark/tasks/SPARK-{index:04d}.md"',
                ]
            )
    lines.extend(
        [
            "product_decision_gate:",
            '  task_id: "SPARK-0013"',
            '  finding_id: "FUNC-FIND-013"',
            f'  task_path: "{functional_run}/spark/tasks/SPARK-0013.md"',
        ]
    )
    _write(repository, OVERLAY_RELATIVE, ("\n".join(lines) + "\n").encode())
    return source_commit, source_paths["state"]


def _manifest_roots(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    backup = tmp_path / "external-backup"
    _init_repository(repository)
    (repository / ".audit" / "nested").mkdir(parents=True)
    backup.mkdir()
    return repository, backup


def test_overlay_sources_use_exact_git_blob_bytes_and_ignore_checkout_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    source_commit, state_path = _overlay_fixture(repository)
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted-empty-path"))

    baseline = verify_overlay_source_pins(
        repository,
        OVERLAY_RELATIVE,
        source_commit,
        GIT_EXECUTABLE,
    )
    assert baseline.ok
    assert baseline.matched == 4
    assert baseline.task_mappings == baseline.task_files_matched == 17

    _write(repository, state_path, b'{"status":"CHECKOUT_MUTATED"}\n')
    after_checkout_mutation = verify_overlay_source_pins(
        repository,
        OVERLAY_RELATIVE,
        source_commit,
        GIT_EXECUTABLE,
    )
    assert after_checkout_mutation.ok


@pytest.mark.parametrize(
    "substitution",
    ["commit", "path", "oid", "hash", "mapping", "order"],
)
def test_overlay_source_or_mapping_substitution_fails(
    tmp_path: Path,
    substitution: str,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    source_commit, state_path = _overlay_fixture(repository)
    overlay = repository / Path(*OVERLAY_RELATIVE.split("/"))
    text = overlay.read_text(encoding="utf-8")
    state_oid = _git(repository, "rev-parse", f"{source_commit}:{state_path}")
    if substitution == "commit":
        text = text.replace(source_commit, "0" * 40, 1)
    elif substitution == "path":
        text = text.replace("FUNCTIONAL_STATE.json", "MISSING_STATE.json", 1)
    elif substitution == "oid":
        text = text.replace(state_oid, "0" * 40, 1)
    elif substitution == "hash":
        raw = b'{"status":"SANITIZED"}\r\n'
        normalized_hash = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        text = text.replace(hashlib.sha256(raw).hexdigest(), normalized_hash, 1)
    elif substitution == "mapping":
        text = text.replace('finding_id: "FUNC-FIND-017"', 'finding_id: "FUNC-FIND-016"', 1)
    else:
        text = text.replace("      - order: 1", "      - order: 2", 1)
    overlay.write_text(text, encoding="utf-8", newline="")

    result = verify_overlay_source_pins(
        repository,
        OVERLAY_RELATIVE,
        source_commit,
        GIT_EXECUTABLE,
    )

    assert not result.ok
    assert result.errors


def test_overlay_source_commit_is_bound_out_of_band(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    reviewed_commit, state_path = _overlay_fixture(repository)
    old_state_oid = _git(repository, "rev-parse", f"{reviewed_commit}:{state_path}")
    old_state_sha = hashlib.sha256(b'{"status":"SANITIZED"}\r\n').hexdigest()
    _write(repository, state_path, b'{"status":"SUBSTITUTED"}\r\n')
    _git(repository, "add", "--", state_path)
    _git(repository, "commit", "--quiet", "-m", "coherent substituted source")
    substituted_commit = _git(repository, "rev-parse", "HEAD")
    new_state_oid = _git(repository, "rev-parse", f"{substituted_commit}:{state_path}")
    new_state_sha = hashlib.sha256(b'{"status":"SUBSTITUTED"}\r\n').hexdigest()
    overlay = repository / Path(*OVERLAY_RELATIVE.split("/"))
    text = overlay.read_text(encoding="utf-8")
    text = text.replace(reviewed_commit, substituted_commit)
    text = text.replace(old_state_oid, new_state_oid)
    text = text.replace(old_state_sha, new_state_sha)
    overlay.write_text(text, encoding="utf-8", newline="")

    result = verify_overlay_source_pins(
        repository,
        OVERLAY_RELATIVE,
        reviewed_commit,
        GIT_EXECUTABLE,
    )

    assert not result.ok
    assert "retained input" in result.errors[0]


def test_overlay_verifier_ignores_git_replace_refs(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    reviewed_commit, state_path = _overlay_fixture(repository)
    _write(repository, state_path, b'{"status":"REPLACEMENT"}\r\n')
    _git(repository, "add", "--", state_path)
    _git(repository, "commit", "--quiet", "-m", "replacement source")
    replacement_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "replace", reviewed_commit, replacement_commit)

    result = verify_overlay_source_pins(
        repository,
        OVERLAY_RELATIVE,
        reviewed_commit,
        GIT_EXECUTABLE,
    )

    assert result.ok


def test_reviewed_input_requires_exact_head_not_ancestor(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _write(repository, "fixture.txt", b"reviewed\n")
    _write(repository, ".gitignore", b"ignored.bin\n")
    _git(repository, "add", "--", "fixture.txt")
    _git(repository, "add", "--", ".gitignore")
    _git(repository, "commit", "--quiet", "-m", "reviewed")
    reviewed = _git(repository, "rev-parse", "HEAD")
    assert verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE).ok

    _write(repository, "fixture.txt", b"dirty tracked bytes\n")
    dirty = verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE)
    assert not dirty.ok
    assert "UNCOMMITTED_START_STATE" in dirty.errors
    _write(repository, "fixture.txt", b"reviewed\n")
    _write(repository, "untracked.txt", b"unexpected\n")
    untracked = verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE)
    assert not untracked.ok
    assert "UNCOMMITTED_START_STATE" in untracked.errors
    (repository / "untracked.txt").unlink()
    _write(repository, "ignored.bin", b"ignored state\n")
    ignored = verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE)
    assert not ignored.ok
    assert "UNCOMMITTED_START_STATE" in ignored.errors
    (repository / "ignored.bin").unlink()

    _write(repository, "extra.txt", b"unreviewed\n")
    _git(repository, "add", "--", "extra.txt")
    _git(repository, "commit", "--quiet", "-m", "unreviewed descendant")
    result = verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE)

    assert not result.ok
    assert result.errors == ("UNREVIEWED_START_HEAD",)


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_reviewed_input_rejects_hidden_tracked_mutation(
    tmp_path: Path,
    index_flag: str,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _write(repository, "fixture.txt", b"reviewed\n")
    _git(repository, "add", "--", "fixture.txt")
    _git(repository, "commit", "--quiet", "-m", "reviewed")
    reviewed = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-index", index_flag, "--", "fixture.txt")
    _write(repository, "fixture.txt", b"hidden mutation\n")

    result = verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE)

    assert not result.ok
    assert result.errors


def test_overlay_rejects_shadow_mappings_outside_executable_waves(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    source_commit, _ = _overlay_fixture(repository)
    overlay = repository / Path(*OVERLAY_RELATIVE.split("/"))
    text = overlay.read_text(encoding="utf-8")
    waves_start = text.index("waves:\n")
    product_start = text.index("product_decision_gate:\n")
    shadow_body = text[waves_start + len("waves:\n") : product_start]
    executable_waves = """waves:
  - id: WAVE-0
    tasks: []
  - id: WAVE-1
    tasks: []
  - id: WAVE-2
    tasks: []
"""
    overlay.write_text(
        text[:waves_start]
        + "shadow_wave_mappings: |\n"
        + shadow_body
        + executable_waves
        + text[product_start:],
        encoding="utf-8",
        newline="",
    )

    result = verify_overlay_source_pins(
        repository,
        OVERLAY_RELATIVE,
        source_commit,
        GIT_EXECUTABLE,
    )

    assert not result.ok
    assert result.errors


def test_reviewed_input_disables_repository_fsmonitor(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _write(repository, "fixture.txt", b"reviewed\n")
    _git(repository, "add", "--", "fixture.txt")
    _git(repository, "commit", "--quiet", "-m", "reviewed")
    reviewed = _git(repository, "rev-parse", "HEAD")
    sentinel = tmp_path / "fsmonitor-invoked"
    if os.name == "nt":
        hook = tmp_path / "fsmonitor.cmd"
        hook.write_text(
            f'@echo off\r\necho invoked>"{sentinel}"\r\n',
            encoding="utf-8",
            newline="",
        )
    else:
        hook = tmp_path / "fsmonitor.sh"
        hook.write_text(
            f"#!/bin/sh\nprintf invoked > '{sentinel}'\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
    _git(repository, "config", "core.fsmonitor", str(hook))

    result = verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE)

    assert result.ok
    assert not sentinel.exists()


def test_reviewed_input_rejects_external_content_filter_without_execution(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _init_repository(repository)
    _write(repository, "fixture.txt", b"reviewed\n")
    _write(repository, ".gitattributes", b"fixture.txt filter=sentinel\n")
    _git(repository, "add", "--", "fixture.txt", ".gitattributes")
    _git(repository, "commit", "--quiet", "-m", "reviewed")
    reviewed = _git(repository, "rev-parse", "HEAD")
    sentinel = tmp_path / "filter-invoked"
    if os.name == "nt":
        hook = tmp_path / "filter.cmd"
        hook.write_text(
            f'@echo off\r\necho invoked>"{sentinel}"\r\nmore\r\n',
            encoding="utf-8",
            newline="",
        )
    else:
        hook = tmp_path / "filter.sh"
        hook.write_text(
            f"#!/bin/sh\nprintf invoked > '{sentinel}'\ncat\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
    _git(repository, "config", "filter.sentinel.clean", str(hook))

    result = verify_reviewed_input_head(repository, reviewed, GIT_EXECUTABLE)

    assert not result.ok
    assert result.errors
    assert not sentinel.exists()


def test_wave_protocol_requires_exact_input_and_anchored_audit_manifests() -> None:
    protocol = (
        ROOT / "docs/audit/10_JARVIS_FUNCTIONAL_REMEDIATION_WAVES_PROMPT.md"
    ).read_text(encoding="utf-8")

    assert "REVIEWED_INPUT_COMMIT=<полный 40-символьный SHA" in protocol
    assert "git rev-parse HEAD == REVIEWED_INPUT_COMMIT" in protocol
    assert "BLOCKED_BY_UNREVIEWED_START_HEAD" in protocol
    assert "audit-manifest-create" in protocol
    assert "audit-manifest-compare" in protocol
    assert "--expected-before-sha256" in protocol
    assert "tracked/untracked" in protocol
    assert "assume-unchanged" in protocol
    assert "два независимых bounded digest pass" in protocol
    assert "BLOCKED_BY_AUDIT_CONTENT_MANIFEST" in protocol


def test_audit_manifest_identical_tree_passes_and_cli_is_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, backup = _manifest_roots(tmp_path)
    (repository / ".audit" / "nested" / "fixture.bin").write_bytes(b"sanitized\n")
    (repository / ".audit" / "a").mkdir()
    (repository / ".audit" / "a" / "child.bin").write_bytes(b"child")
    (repository / ".audit" / "a.txt").write_bytes(b"sibling")

    assert (
        main(
            [
                "audit-manifest-create",
                "--repository-root",
                str(repository),
                "--backup-root",
                str(backup),
                "--output-name",
                "before.json",
                "--git-executable",
                str(GIT_EXECUTABLE),
            ]
        )
        == 0
    )
    before_output = json.loads(capsys.readouterr().out)
    after = write_audit_content_manifest(
        repository, backup, "after.json", GIT_EXECUTABLE
    )
    exit_code = main(
        [
            "audit-manifest-compare",
            "--repository-root",
            str(repository),
            "--backup-root",
            str(backup),
            "--before-name",
            "before.json",
            "--after-name",
            "after.json",
            "--expected-before-sha256",
            before_output["manifest_sha256"],
            "--expected-after-sha256",
            after.sha256,
            "--result-name",
            "comparison.json",
            "--git-executable",
            str(GIT_EXECUTABLE),
        ]
    )
    comparison_output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert comparison_output["ok"] is True
    assert comparison_output["difference_count"] == 0


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("same_size", "FILE_HASH_CHANGED"),
        ("size", "FILE_SIZE_CHANGED"),
        ("add", "PATH_ADDED"),
        ("remove", "PATH_REMOVED"),
        ("type", "FILE_TYPE_CHANGED"),
    ],
)
def test_audit_manifest_detects_every_content_shape_change(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    repository, backup = _manifest_roots(tmp_path)
    target = repository / ".audit" / "nested" / "fixture.bin"
    target.write_bytes(b"1234")
    before = write_audit_content_manifest(
        repository, backup, "before.json", GIT_EXECUTABLE
    )
    if mutation == "same_size":
        target.write_bytes(b"5678")
    elif mutation == "size":
        target.write_bytes(b"12345")
    elif mutation == "add":
        (repository / ".audit" / "nested" / "added.bin").write_bytes(b"added")
    elif mutation == "remove":
        target.unlink()
    else:
        target.unlink()
        target.mkdir()
    after = write_audit_content_manifest(
        repository, backup, "after.json", GIT_EXECUTABLE
    )

    comparison = compare_audit_content_manifests(
        repository,
        backup,
        "before.json",
        "after.json",
        GIT_EXECUTABLE,
        expected_before_sha256=before.sha256,
        expected_after_sha256=after.sha256,
        result_name="comparison.json",
    )

    assert not comparison.ok
    assert expected_code in {item["code"] for item in comparison.differences}


def test_audit_manifest_requires_retained_outer_anchor(tmp_path: Path) -> None:
    repository, backup = _manifest_roots(tmp_path)
    (repository / ".audit" / "nested" / "fixture.bin").write_bytes(b"sanitized")
    before = write_audit_content_manifest(
        repository, backup, "before.json", GIT_EXECUTABLE
    )
    after = write_audit_content_manifest(
        repository, backup, "after.json", GIT_EXECUTABLE
    )
    before_path = backup / "before.json"
    before_path.write_bytes(before_path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="trusted SHA-256 mismatch"):
        compare_audit_content_manifests(
            repository,
            backup,
            "before.json",
            "after.json",
            GIT_EXECUTABLE,
            expected_before_sha256=before.sha256,
            expected_after_sha256=after.sha256,
            result_name="comparison.json",
        )
    assert not (backup / "comparison.json").exists()


def test_audit_manifest_comparison_rejects_stale_after_snapshot(tmp_path: Path) -> None:
    repository, backup = _manifest_roots(tmp_path)
    target = repository / ".audit" / "nested" / "fixture.bin"
    target.write_bytes(b"before")
    before = write_audit_content_manifest(
        repository, backup, "before.json", GIT_EXECUTABLE
    )
    shutil.copyfile(backup / "before.json", backup / "after.json")
    after_sha256 = hashlib.sha256((backup / "after.json").read_bytes()).hexdigest()
    target.write_bytes(b"after!")

    comparison = compare_audit_content_manifests(
        repository,
        backup,
        "before.json",
        "after.json",
        GIT_EXECUTABLE,
        expected_before_sha256=before.sha256,
        expected_after_sha256=after_sha256,
        result_name="comparison.json",
    )

    assert not comparison.ok
    assert "AFTER_MANIFEST_STALE" in {
        str(item["code"]) for item in comparison.differences
    }


def test_audit_manifest_rejects_same_snapshot_or_different_repository(
    tmp_path: Path,
) -> None:
    repository, backup = _manifest_roots(tmp_path)
    (repository / ".audit" / "nested" / "fixture.bin").write_bytes(b"sanitized")
    before = write_audit_content_manifest(
        repository, backup, "before.json", GIT_EXECUTABLE
    )
    after = write_audit_content_manifest(
        repository, backup, "after.json", GIT_EXECUTABLE
    )

    with pytest.raises(ValueError, match="must be distinct"):
        compare_audit_content_manifests(
            repository,
            backup,
            "before.json",
            "before.json",
            GIT_EXECUTABLE,
            expected_before_sha256=before.sha256,
            expected_after_sha256=before.sha256,
            result_name="same.json",
        )

    other_repository = tmp_path / "other-repository"
    _init_repository(other_repository)
    (other_repository / ".audit").mkdir()
    with pytest.raises(ValueError, match="requested repository"):
        compare_audit_content_manifests(
            other_repository,
            backup,
            "before.json",
            "after.json",
            GIT_EXECUTABLE,
            expected_before_sha256=before.sha256,
            expected_after_sha256=after.sha256,
            result_name="other.json",
        )


def test_audit_manifest_rejects_repository_output_and_overwrite(tmp_path: Path) -> None:
    repository, backup = _manifest_roots(tmp_path)
    (repository / ".audit" / "nested" / "fixture.bin").write_bytes(b"sanitized")
    repository_output = repository / "backup"
    repository_output.mkdir()

    with pytest.raises(SafePathError, match="disjoint"):
        write_audit_content_manifest(
            repository, repository_output, "manifest.json", GIT_EXECUTABLE
        )
    with pytest.raises(SafePathError, match="disjoint"):
        compare_audit_content_manifests(
            repository,
            repository_output,
            "missing-before.json",
            "missing-after.json",
            GIT_EXECUTABLE,
            expected_before_sha256="0" * 64,
            expected_after_sha256="1" * 64,
            result_name="comparison.json",
        )
    first = write_audit_content_manifest(
        repository, backup, "manifest.json", GIT_EXECUTABLE
    )
    assert first.path.is_file()
    with pytest.raises(FileExistsError):
        write_audit_content_manifest(
            repository, backup, "manifest.json", GIT_EXECUTABLE
        )


def test_audit_manifest_rejects_non_git_root_before_file_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "not-a-repository"
    backup = tmp_path / "external-backup"
    (repository / ".audit").mkdir(parents=True)
    (repository / ".audit" / "fixture.bin").write_bytes(b"must not be read")
    backup.mkdir()

    def forbidden_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("audit file scan must not start")

    monkeypatch.setattr(evidence_module, "bounded_file_digest", forbidden_read)
    with pytest.raises(ValueError, match="Git command failed"):
        write_audit_content_manifest(
            repository, backup, "manifest.json", GIT_EXECUTABLE
        )
    assert not (backup / "manifest.json").exists()


def test_audit_manifest_never_copies_file_content_or_canary(tmp_path: Path) -> None:
    repository, backup = _manifest_roots(tmp_path)
    canary = "DISPOSABLE_CANARY_VALUE_9f6e"
    raw_content = f"sanitized fixture {canary}".encode()
    (repository / ".audit" / "nested" / "fixture.bin").write_bytes(raw_content)

    artifact = write_audit_content_manifest(
        repository,
        backup,
        "manifest.json",
        GIT_EXECUTABLE,
        canaries=(canary,),
    )
    manifest_bytes = artifact.path.read_bytes()

    assert raw_content not in manifest_bytes
    assert canary.encode() not in manifest_bytes


def test_audit_manifest_does_not_truncate_known_large_path_set(tmp_path: Path) -> None:
    repository, backup = _manifest_roots(tmp_path)
    for index in range(270):
        (repository / ".audit" / "nested" / f"fixture-{index:03d}.bin").write_bytes(b"x")

    artifact = write_audit_content_manifest(
        repository, backup, "manifest.json", GIT_EXECUTABLE
    )
    document = json.loads(artifact.path.read_text(encoding="utf-8"))

    assert artifact.entry_count == 271
    assert document["entry_count"] == len(document["entries"]) == 271


def test_audit_manifest_inaccessible_entry_fails_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, backup = _manifest_roots(tmp_path)
    (repository / ".audit" / "nested" / "fixture.bin").write_bytes(b"sanitized")

    def inaccessible(*args: object, **kwargs: object) -> object:
        raise SafePathError("FILE_INACCESSIBLE", "fixture unavailable")

    monkeypatch.setattr(evidence_module, "bounded_file_digest", inaccessible)
    with pytest.raises(SafePathError, match="fixture unavailable"):
        write_audit_content_manifest(
            repository, backup, "manifest.json", GIT_EXECUTABLE
        )
    assert not (backup / "manifest.json").exists()


def test_audit_manifest_detects_same_size_mutation_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, backup = _manifest_roots(tmp_path)
    target = repository / ".audit" / "nested" / "fixture.bin"
    target.write_bytes(b"1234")
    original = evidence_module.bounded_file_digest
    mutated = False

    def racing_digest(*args: object, **kwargs: object) -> object:
        nonlocal mutated
        result = original(*args, **kwargs)
        if not mutated:
            target.write_bytes(b"5678")
            mutated = True
        return result

    monkeypatch.setattr(evidence_module, "bounded_file_digest", racing_digest)
    with pytest.raises(SafePathError, match="changed during scan"):
        write_audit_content_manifest(
            repository, backup, "manifest.json", GIT_EXECUTABLE
        )
    assert not (backup / "manifest.json").exists()


def test_audit_manifest_detects_timestamp_restored_mutation_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, backup = _manifest_roots(tmp_path)
    target = repository / ".audit" / "nested" / "fixture.bin"
    target.write_bytes(b"1234")
    original_stat = target.stat()
    original = evidence_module.bounded_file_digest
    mutated = False

    def timestamp_restoring_digest(*args: object, **kwargs: object) -> object:
        nonlocal mutated
        result = original(*args, **kwargs)
        if not mutated:
            target.write_bytes(b"5678")
            os.utime(
                target,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            mutated = True
        return result

    monkeypatch.setattr(
        evidence_module,
        "bounded_file_digest",
        timestamp_restoring_digest,
    )
    with pytest.raises(SafePathError, match="changed during scan"):
        write_audit_content_manifest(
            repository, backup, "manifest.json", GIT_EXECUTABLE
        )
    assert not (backup / "manifest.json").exists()


def test_reparse_metadata_is_hashed_without_following_and_retarget_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, backup = _manifest_roots(tmp_path)
    marker = repository / ".audit" / "nested" / "link"
    marker.write_bytes(b"must not be copied")
    original_reparse = evidence_module._audit_reparse
    original_readlink = os.readlink
    target = {"value": "sanitized-target-a"}

    def simulated_reparse(stat_result: os.stat_result) -> bool:
        return (
            stat.S_ISREG(stat_result.st_mode)
            and stat_result.st_size == len(b"must not be copied")
        ) or original_reparse(stat_result)

    def simulated_readlink(path: os.PathLike[str] | str) -> str:
        if Path(path) == marker:
            return target["value"]
        return original_readlink(path)

    monkeypatch.setattr(evidence_module, "_audit_reparse", simulated_reparse)
    monkeypatch.setattr(evidence_module.os, "readlink", simulated_readlink)
    before = write_audit_content_manifest(
        repository, backup, "before.json", GIT_EXECUTABLE
    )
    target["value"] = "sanitized-target-b"
    after = write_audit_content_manifest(
        repository, backup, "after.json", GIT_EXECUTABLE
    )
    comparison = compare_audit_content_manifests(
        repository,
        backup,
        "before.json",
        "after.json",
        GIT_EXECUTABLE,
        expected_before_sha256=before.sha256,
        expected_after_sha256=after.sha256,
        result_name="comparison.json",
    )

    before_document = json.loads((backup / "before.json").read_text(encoding="utf-8"))
    link_entry = next(
        entry for entry in before_document["entries"] if entry["relative_path"].endswith("link")
    )
    assert link_entry["file_type"] == "reparse"
    assert b"must not be copied" not in (backup / "before.json").read_bytes()
    assert "FILE_HASH_CHANGED" in {item["code"] for item in comparison.differences}
