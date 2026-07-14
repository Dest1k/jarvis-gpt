"""Offline Git-object verification for immutable remediation inputs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .safe_paths import bounded_file_bytes, canonical_directory, validate_relative_path

OVERLAY_HASH_CONVENTION = "sha256_git_blob_raw_bytes_v1"
OVERLAY_PIN_NAMES = ("state", "findings_index", "queue", "task_schema")
OVERLAY_TASK_ORDER = {
    "WAVE-0": (
        "SPARK-0017",
        "SPARK-0016",
        "SPARK-0006",
        "SPARK-0009",
        "SPARK-0015",
        "SPARK-0011",
    ),
    "WAVE-1": ("SPARK-0014", "SPARK-0007", "SPARK-0003", "SPARK-0008"),
    "WAVE-2": (
        "SPARK-0002",
        "SPARK-0004",
        "SPARK-0005",
        "SPARK-0012",
        "SPARK-0001",
        "SPARK-0010",
    ),
}
_LOWER_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_LOWER_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_OVERLAY_BYTES = 2 * 1024 * 1024
_MAX_SOURCE_BLOB_BYTES = 16 * 1024 * 1024


class _UncommittedStartState(ValueError):
    """Exact reviewed Git objects and local checkout/index state differ."""


@dataclass(frozen=True, slots=True)
class OverlaySourcePin:
    name: str
    source_commit: str
    path: str
    git_blob_oid: str
    sha256: str


@dataclass(frozen=True, slots=True)
class OverlaySourceValidation:
    pins: tuple[OverlaySourcePin, ...]
    matched: int
    task_mappings: int
    task_files_matched: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return (
            not self.errors
            and self.matched == len(self.pins) == len(OVERLAY_PIN_NAMES)
            and self.task_mappings == self.task_files_matched == 17
        )


@dataclass(frozen=True, slots=True)
class ReviewedInputValidation:
    expected: str
    actual: str | None
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors and self.actual == self.expected


@dataclass(frozen=True, slots=True)
class OverlayTaskMapping:
    wave: str
    order: int
    task_id: str
    finding_id: str
    task_path: str


def _quoted_scalar(value: str, *, label: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON-quoted string") from exc
    if not isinstance(parsed, str) or not parsed:
        raise ValueError(f"{label} must be a non-empty string")
    return parsed


@dataclass(frozen=True, slots=True)
class _CanonicalYamlToken:
    indent: int
    list_item: bool
    key: str | None
    has_value: bool
    value: object | None


def _canonical_yaml_scalar(raw: str) -> object:
    if raw.startswith('"'):
        return _quoted_scalar(raw, label="overlay scalar")
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if re.fullmatch(r"0|[1-9][0-9]*", raw):
        return int(raw)
    if re.fullmatch(r"[a-z][a-z0-9_]*", raw):
        return raw
    raise ValueError("overlay uses an unsupported or noncanonical YAML scalar")


def _canonical_yaml_tokens(text: str) -> tuple[_CanonicalYamlToken, ...]:
    if text.startswith("\ufeff"):
        raise ValueError("overlay must not contain a byte-order mark")
    tokens: list[_CanonicalYamlToken] = []
    key_pattern = re.compile(r"([a-z][a-z0-9_]*):(.*)")
    for line in text.splitlines():
        if not line:
            continue
        if line != line.rstrip() or "\t" in line:
            raise ValueError("overlay whitespace is noncanonical")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise ValueError("overlay indentation is noncanonical")
        content = line[indent:]
        list_item = content.startswith("- ")
        if list_item:
            content = content[2:]
            if not content:
                raise ValueError("overlay list item is empty")
        match = key_pattern.fullmatch(content)
        if match:
            key = match.group(1)
            remainder = match.group(2)
            if not remainder:
                tokens.append(
                    _CanonicalYamlToken(indent, list_item, key, False, None)
                )
                continue
            if not remainder.startswith(" ") or remainder.startswith("  "):
                raise ValueError("overlay mapping spacing is noncanonical")
            tokens.append(
                _CanonicalYamlToken(
                    indent,
                    list_item,
                    key,
                    True,
                    _canonical_yaml_scalar(remainder[1:]),
                )
            )
            continue
        if not list_item:
            raise ValueError("overlay line is not a canonical mapping")
        tokens.append(
            _CanonicalYamlToken(
                indent,
                True,
                None,
                True,
                _canonical_yaml_scalar(content),
            )
        )
    if not tokens:
        raise ValueError("overlay is empty")
    return tuple(tokens)


def _parse_canonical_overlay_document(payload: bytes) -> dict[str, object]:
    """Parse the exact dependency-free YAML subset used by the overlay."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("overlay must be UTF-8") from exc
    tokens = _canonical_yaml_tokens(text)

    def parse_mapping_entry(
        token_index: int,
        target: dict[str, object],
        *,
        child_indent: int,
    ) -> int:
        token = tokens[token_index]
        if token.key is None or token.key in target:
            raise ValueError("overlay mapping key is missing or duplicated")
        if token.has_value:
            target[token.key] = token.value
            return token_index + 1
        next_index = token_index + 1
        if next_index >= len(tokens) or tokens[next_index].indent != child_indent:
            raise ValueError("overlay nested mapping value is missing")
        child, next_index = parse_block(next_index, child_indent)
        target[token.key] = child
        return next_index

    def parse_mapping(token_index: int, indent: int) -> tuple[dict[str, object], int]:
        result: dict[str, object] = {}
        while token_index < len(tokens) and tokens[token_index].indent == indent:
            token = tokens[token_index]
            if token.list_item:
                raise ValueError("overlay block mixes mappings and list items")
            token_index = parse_mapping_entry(
                token_index,
                result,
                child_indent=indent + 2,
            )
        if token_index < len(tokens) and tokens[token_index].indent > indent:
            raise ValueError("overlay mapping indentation is unsupported")
        return result, token_index

    def parse_list(token_index: int, indent: int) -> tuple[list[object], int]:
        result: list[object] = []
        while token_index < len(tokens) and tokens[token_index].indent == indent:
            token = tokens[token_index]
            if not token.list_item:
                raise ValueError("overlay block mixes list items and mappings")
            if token.key is None:
                result.append(token.value)
                token_index += 1
            else:
                item: dict[str, object] = {}
                token_index = parse_mapping_entry(
                    token_index,
                    item,
                    child_indent=indent + 4,
                )
                while (
                    token_index < len(tokens)
                    and tokens[token_index].indent == indent + 2
                ):
                    if tokens[token_index].list_item:
                        raise ValueError("overlay list mapping continuation is invalid")
                    token_index = parse_mapping_entry(
                        token_index,
                        item,
                        child_indent=indent + 4,
                    )
                result.append(item)
            if token_index < len(tokens) and tokens[token_index].indent > indent:
                raise ValueError("overlay list indentation is unsupported")
        return result, token_index

    def parse_block(token_index: int, indent: int) -> tuple[object, int]:
        if token_index >= len(tokens) or tokens[token_index].indent != indent:
            raise ValueError("overlay block indentation is incomplete")
        if tokens[token_index].list_item:
            return parse_list(token_index, indent)
        return parse_mapping(token_index, indent)

    document, final_index = parse_block(0, 0)
    if final_index != len(tokens) or not isinstance(document, dict):
        raise ValueError("overlay document structure is incomplete")
    return document


def parse_overlay_source_pins(payload: bytes) -> tuple[OverlaySourcePin, ...]:
    """Parse the deliberately small, dependency-free immutable source block."""

    document = _parse_canonical_overlay_document(payload)
    section = document.get("immutable_sources")
    if not isinstance(section, dict) or tuple(section) != (
        "hash_convention",
        "functional_run",
        *OVERLAY_PIN_NAMES,
    ):
        raise ValueError("immutable_sources fields are incomplete or noncanonical")
    if section["hash_convention"] != OVERLAY_HASH_CONVENTION:
        raise ValueError("overlay hash convention is unsupported")
    if not isinstance(section["functional_run"], str):
        raise ValueError("immutable_sources.functional_run must be a string")
    functional_run = validate_relative_path(
        section["functional_run"], label="immutable_sources.functional_run"
    )

    pins: list[OverlaySourcePin] = []
    seen_paths: set[str] = set()
    expected_fields = {"source_commit", "path", "git_blob_oid", "sha256"}
    expected_paths = {
        "state": f"{functional_run}/FUNCTIONAL_STATE.json",
        "findings_index": f"{functional_run}/FUNCTIONAL_FINDINGS_INDEX.md",
        "queue": f"{functional_run}/spark/QUEUE.csv",
        "task_schema": f"{functional_run}/spark/TASK_SCHEMA.md",
    }
    for name in OVERLAY_PIN_NAMES:
        record = section[name]
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise ValueError(f"immutable source pin {name} has incomplete fields")
        source_commit = record["source_commit"]
        git_blob_oid = record["git_blob_oid"]
        sha256 = record["sha256"]
        if not all(
            isinstance(value, str)
            for value in (source_commit, record["path"], git_blob_oid, sha256)
        ):
            raise ValueError(f"immutable source pin {name} fields must be strings")
        path = validate_relative_path(record["path"], label=f"source pin {name} path")
        if not _LOWER_SHA1_RE.fullmatch(source_commit):
            raise ValueError(f"source pin {name} commit must be a full lowercase SHA")
        if not _LOWER_SHA1_RE.fullmatch(git_blob_oid):
            raise ValueError(f"source pin {name} blob OID must be a full lowercase SHA")
        if not _LOWER_SHA256_RE.fullmatch(sha256):
            raise ValueError(f"source pin {name} SHA-256 is invalid")
        if path != expected_paths[name] or not PurePosixPath(path).is_relative_to(
            PurePosixPath(functional_run)
        ):
            raise ValueError(f"source pin {name} path is noncanonical")
        path_key = path.casefold()
        if path_key in seen_paths:
            raise ValueError("immutable source paths must be unique")
        seen_paths.add(path_key)
        pins.append(
            OverlaySourcePin(
                name=name,
                source_commit=source_commit,
                path=path,
                git_blob_oid=git_blob_oid,
                sha256=sha256,
            )
        )
    if len({pin.source_commit for pin in pins}) != 1:
        raise ValueError("all immutable source pins must use one exact source commit")
    return tuple(pins)


def parse_overlay_task_mappings(
    payload: bytes,
    *,
    functional_run: str,
) -> tuple[OverlayTaskMapping, ...]:
    document = _parse_canonical_overlay_document(payload)
    waves = document.get("waves")
    product = document.get("product_decision_gate")
    if not isinstance(waves, list) or len(waves) != len(OVERLAY_TASK_ORDER):
        raise ValueError("overlay waves structure is incomplete")
    if not isinstance(product, dict):
        raise ValueError("product decision gate structure is incomplete")

    mappings: list[OverlayTaskMapping] = []
    wave_headers: list[str] = []

    for wave in waves:
        if not isinstance(wave, dict):
            raise ValueError("overlay wave must be a mapping")
        wave_id = wave.get("id")
        tasks = wave.get("tasks")
        if not isinstance(wave_id, str) or not isinstance(tasks, list):
            raise ValueError("overlay wave identity or tasks are incomplete")
        wave_headers.append(wave_id)
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("overlay task must be a mapping")
            order = task.get("order")
            task_id = task.get("task_id")
            finding_id = task.get("finding_id")
            task_path = task.get("task_path")
            if (
                type(order) is not int
                or not isinstance(task_id, str)
                or not isinstance(finding_id, str)
                or not isinstance(task_path, str)
            ):
                raise ValueError("overlay task mapping is incomplete")
            mappings.append(
                OverlayTaskMapping(
                    wave=wave_id,
                    order=order,
                    task_id=task_id,
                    finding_id=finding_id,
                    task_path=task_path,
                )
            )

    product_fields = {
        key: product.get(key) for key in ("task_id", "finding_id", "task_path")
    }
    if not all(isinstance(value, str) for value in product_fields.values()):
        raise ValueError("product decision gate mapping is incomplete")
    mappings.append(
        OverlayTaskMapping(
            wave="PRODUCT-DECISION",
            order=1,
            task_id=str(product_fields["task_id"]),
            finding_id=str(product_fields["finding_id"]),
            task_path=str(product_fields["task_path"]),
        )
    )

    if wave_headers != list(OVERLAY_TASK_ORDER):
        raise ValueError("overlay wave identity or order is noncanonical")
    expected_orders = {
        **{wave: len(tasks) for wave, tasks in OVERLAY_TASK_ORDER.items()},
        "PRODUCT-DECISION": 1,
    }
    for wave, count in expected_orders.items():
        orders = [mapping.order for mapping in mappings if mapping.wave == wave]
        if orders != list(range(1, count + 1)):
            raise ValueError(f"{wave} task order is incomplete or noncanonical")
        if wave in OVERLAY_TASK_ORDER:
            task_ids = tuple(
                mapping.task_id for mapping in mappings if mapping.wave == wave
            )
            if task_ids != OVERLAY_TASK_ORDER[wave]:
                raise ValueError(f"{wave} task identity order is noncanonical")
    if len(mappings) != 17:
        raise ValueError("overlay must map exactly 17 tasks")

    seen_tasks: set[str] = set()
    seen_findings: set[str] = set()
    seen_paths: set[str] = set()
    task_re = re.compile(r"^SPARK-[0-9]{4}$")
    finding_re = re.compile(r"^FUNC-FIND-[0-9]{3}$")
    for mapping in mappings:
        if not task_re.fullmatch(mapping.task_id) or not finding_re.fullmatch(
            mapping.finding_id
        ):
            raise ValueError("overlay task or finding identifier is noncanonical")
        path = validate_relative_path(mapping.task_path, label="overlay task path")
        expected_path = f"{functional_run}/spark/tasks/{mapping.task_id}.md"
        if path != expected_path:
            raise ValueError("overlay task path does not match its task identifier")
        if (
            mapping.task_id in seen_tasks
            or mapping.finding_id in seen_findings
            or path.casefold() in seen_paths
        ):
            raise ValueError("overlay task mappings must be one-to-one")
        seen_tasks.add(mapping.task_id)
        seen_findings.add(mapping.finding_id)
        seen_paths.add(path.casefold())
    return tuple(mappings)


def _git_environment() -> dict[str, str]:
    keep = {
        "COMSPEC",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in keep and not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _verified_git_executable(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or candidate.name.casefold() not in {"git", "git.exe"}:
        raise ValueError("Git executable must be an explicit absolute git path")
    parent = canonical_directory(candidate.parent)
    target = parent / candidate.name
    try:
        target_stat = os.lstat(target)
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Git executable is unavailable") from exc
    if (
        not stat.S_ISREG(target_stat.st_mode)
        or stat.S_ISLNK(target_stat.st_mode)
        or bool(int(getattr(target_stat, "st_file_attributes", 0)) & 0x0400)
        or os.path.normcase(str(resolved)) != os.path.normcase(str(target))
    ):
        raise ValueError("Git executable must be a non-reparse regular file")
    return target


def _git(
    git_executable: Path,
    repository_root: Path,
    *arguments: str,
    max_stdout: int = 4096,
    input_bytes: bytes | None = None,
) -> bytes:
    if input_bytes is not None and len(input_bytes) > 4 * 1024 * 1024:
        raise ValueError("isolated Git command input exceeded its bound")
    try:
        completed = subprocess.run(
            [
                str(git_executable),
                "--no-pager",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.preloadIndex=false",
                "-c",
                "maintenance.auto=false",
                "-C",
                str(repository_root),
                *arguments,
            ],
            check=False,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            input=input_bytes,
            capture_output=True,
            env=_git_environment(),
            shell=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("isolated Git command could not complete") from exc
    if completed.returncode != 0:
        raise ValueError("isolated Git command failed")
    if len(completed.stdout) > max_stdout:
        raise ValueError("isolated Git command output exceeded its bound")
    return completed.stdout


def _nul_records(payload: bytes, *, label: str) -> tuple[bytes, ...]:
    if not payload:
        return ()
    if not payload.endswith(b"\0"):
        raise ValueError(f"{label} is not NUL terminated")
    records = tuple(payload[:-1].split(b"\0"))
    if any(not record for record in records):
        raise ValueError(f"{label} contains an empty record")
    return records


def _tracked_path(raw: bytes, *, label: str) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8") from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError(f"{label} contains control characters")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if (
        not path
        or path != path.strip()
        or "\\" in path
        or ":" in path
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or str(posix) != path
    ):
        raise ValueError(f"{label} is not a canonical Git path")
    return path


def _reviewed_tree_entries(
    git_executable: Path,
    repository_root: Path,
    reviewed_input_commit: str,
) -> dict[str, tuple[str, str]]:
    payload = _git(
        git_executable,
        repository_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        reviewed_input_commit,
        max_stdout=16 * 1024 * 1024,
    )
    entries: dict[str, tuple[str, str]] = {}
    for record in _nul_records(payload, label="reviewed tree"):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, oid = metadata.decode("ascii").split(" ")
        except (UnicodeError, ValueError) as exc:
            raise ValueError("reviewed tree entry is malformed") from exc
        path = _tracked_path(raw_path, label="reviewed tree path")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("reviewed tree contains an unsupported tracked entry")
        if not _LOWER_SHA1_RE.fullmatch(oid) or path in entries:
            raise ValueError("reviewed tree entry is noncanonical")
        entries[path] = (mode, oid)
    return entries


def _index_entries(
    git_executable: Path,
    repository_root: Path,
) -> dict[str, tuple[str, str]]:
    staged_payload = _git(
        git_executable,
        repository_root,
        "ls-files",
        "--stage",
        "-z",
        max_stdout=16 * 1024 * 1024,
    )
    entries: dict[str, tuple[str, str]] = {}
    for record in _nul_records(staged_payload, label="index"):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ")
        except (UnicodeError, ValueError) as exc:
            raise ValueError("index entry is malformed") from exc
        path = _tracked_path(raw_path, label="index path")
        if (
            mode not in {"100644", "100755"}
            or not _LOWER_SHA1_RE.fullmatch(oid)
            or stage != "0"
            or path in entries
        ):
            raise ValueError("index entry is noncanonical")
        entries[path] = (mode, oid)

    flag_payload = _git(
        git_executable,
        repository_root,
        "ls-files",
        "-v",
        "-z",
        max_stdout=16 * 1024 * 1024,
    )
    flagged_paths: list[str] = []
    for record in _nul_records(flag_payload, label="index flags"):
        if len(record) < 3 or record[1:2] != b" ":
            raise ValueError("index flag entry is malformed")
        path = _tracked_path(record[2:], label="index flag path")
        if record[:1] != b"H":
            raise _UncommittedStartState(
                "index contains nondefault tracking flags"
            )
        flagged_paths.append(path)
    if flagged_paths != list(entries):
        raise ValueError("index flag paths do not match staged entries")
    return entries


def _worktree_blob_oids(
    git_executable: Path,
    repository_root: Path,
    paths: tuple[str, ...],
) -> tuple[str, ...]:
    if not paths:
        return ()
    path_input = "".join(f"{path}\n" for path in paths).encode("utf-8")
    output = _git(
        git_executable,
        repository_root,
        "hash-object",
        "--filters",
        "--stdin-paths",
        max_stdout=len(paths) * 41 + 1,
        input_bytes=path_input,
    )
    try:
        oids = tuple(output.decode("ascii").splitlines())
    except UnicodeDecodeError as exc:
        raise ValueError("worktree hash output is not ASCII") from exc
    if len(oids) != len(paths) or any(
        not _LOWER_SHA1_RE.fullmatch(oid) for oid in oids
    ):
        raise ValueError("worktree hash output is noncanonical")
    return oids


def _reject_external_content_filters(
    git_executable: Path,
    repository_root: Path,
    paths: tuple[str, ...],
) -> None:
    if not paths:
        return
    path_input = b"\0".join(path.encode("utf-8") for path in paths) + b"\0"
    output = _git(
        git_executable,
        repository_root,
        "check-attr",
        "--stdin",
        "-z",
        "filter",
        max_stdout=len(path_input) + len(paths) * 32,
        input_bytes=path_input,
    )
    records = _nul_records(output, label="Git attributes")
    if len(records) != len(paths) * 3:
        raise ValueError("Git filter attributes are incomplete")
    for index, path in enumerate(paths):
        raw_path, attribute, value = records[index * 3 : index * 3 + 3]
        if (
            _tracked_path(raw_path, label="Git attribute path") != path
            or attribute != b"filter"
        ):
            raise ValueError("Git filter attribute output is noncanonical")
        if value not in {b"unspecified", b"unset"}:
            raise ValueError("external Git content filters are not permitted")


def _verify_exact_reviewed_checkout(
    git_executable: Path,
    repository_root: Path,
    reviewed_input_commit: str,
) -> None:
    reviewed = _reviewed_tree_entries(
        git_executable,
        repository_root,
        reviewed_input_commit,
    )
    index = _index_entries(git_executable, repository_root)
    if index != reviewed:
        raise _UncommittedStartState(
            "index does not exactly match the reviewed tree"
        )
    paths = tuple(index)
    _reject_external_content_filters(git_executable, repository_root, paths)
    expected_oids = tuple(index[path][1] for path in paths)
    first = _worktree_blob_oids(git_executable, repository_root, paths)
    second = _worktree_blob_oids(git_executable, repository_root, paths)
    if first != expected_oids or second != expected_oids or first != second:
        raise _UncommittedStartState(
            "worktree bytes do not exactly match the reviewed index"
        )


def _verified_repository_root(
    git_executable: Path,
    repository_root: str | os.PathLike[str],
) -> Path:
    root = canonical_directory(repository_root)
    reported = _git(git_executable, root, "rev-parse", "--show-toplevel").decode(
        "utf-8"
    ).strip()
    try:
        actual = Path(reported).resolve(strict=True)
    except OSError as exc:
        raise ValueError("Git repository root is unavailable") from exc
    if os.path.normcase(str(actual)) != os.path.normcase(str(root)):
        raise ValueError("repository root does not match Git toplevel")
    for alternate_kind in ("alternates", "http-alternates"):
        alternate_path = _git(
            git_executable,
            root,
            "rev-parse",
            "--git-path",
            f"objects/info/{alternate_kind}",
        ).decode("utf-8").strip()
        alternate = Path(alternate_path)
        if not alternate.is_absolute():
            alternate = root / alternate
        try:
            if alternate.is_file() and alternate.stat().st_size:
                raise ValueError("Git object alternates are not permitted")
        except OSError as exc:
            raise ValueError("Git object alternate state is inaccessible") from exc
    return root


def verified_git_repository_root(
    repository_root: str | os.PathLike[str],
    git_executable: str | os.PathLike[str],
) -> tuple[Path, Path]:
    """Return one explicit Git executable and its exact local worktree root."""

    executable = _verified_git_executable(git_executable)
    return executable, _verified_repository_root(executable, repository_root)


def verify_overlay_source_pins(
    repository_root: str | os.PathLike[str],
    overlay_path: object,
    expected_source_commit: object,
    git_executable: str | os.PathLike[str],
) -> OverlaySourceValidation:
    """Recompute every source pin from exact immutable Git blob bytes."""

    try:
        executable, root = verified_git_repository_root(repository_root, git_executable)
        if not isinstance(expected_source_commit, str) or not _LOWER_SHA1_RE.fullmatch(
            expected_source_commit
        ):
            raise ValueError("expected source commit must be a full lowercase SHA")
        overlay_relative = validate_relative_path(overlay_path, label="overlay path")
        payload = bounded_file_bytes(root, overlay_relative, max_bytes=_MAX_OVERLAY_BYTES)
        pins = parse_overlay_source_pins(payload)
        if any(pin.source_commit != expected_source_commit for pin in pins):
            raise ValueError("overlay source commit does not match the retained input")
        functional_run = str(PurePosixPath(pins[0].path).parent)
        mappings = parse_overlay_task_mappings(payload, functional_run=functional_run)
    except (OSError, UnicodeError, ValueError) as exc:
        return OverlaySourceValidation(
            (), 0, 0, 0, (f"OVERLAY_SOURCE_MANIFEST_INVALID: {exc}",)
        )

    matched = 0
    errors: list[str] = []
    raw_sources: dict[str, bytes] = {}
    for pin in pins:
        try:
            resolved_commit = _git(
                executable,
                root,
                "rev-parse",
                "--verify",
                f"{pin.source_commit}^{{commit}}",
            ).decode("ascii").strip()
            if resolved_commit != pin.source_commit:
                raise ValueError("source commit did not resolve exactly")
            actual_oid = _git(
                executable,
                root,
                "rev-parse",
                "--verify",
                f"{pin.source_commit}:{pin.path}",
            ).decode("ascii").strip()
            if not _LOWER_SHA1_RE.fullmatch(actual_oid):
                raise ValueError("source path did not resolve to a canonical object")
            object_type = _git(executable, root, "cat-file", "-t", actual_oid).decode(
                "ascii"
            ).strip()
            if object_type != "blob":
                raise ValueError("source path is not a Git blob")
            size_text = _git(executable, root, "cat-file", "-s", actual_oid).decode(
                "ascii"
            ).strip()
            size = int(size_text)
            if size < 0 or size > _MAX_SOURCE_BLOB_BYTES:
                raise ValueError("source blob exceeds the verification bound")
            raw = _git(
                executable,
                root,
                "cat-file",
                "blob",
                actual_oid,
                max_stdout=size + 1,
            )
            if len(raw) != size:
                raise ValueError("source blob size changed during verification")
            actual_sha256 = hashlib.sha256(raw).hexdigest()
        except (UnicodeError, ValueError) as exc:
            errors.append(f"{pin.name}: SOURCE_BLOB_UNAVAILABLE: {exc}")
            continue
        if actual_oid != pin.git_blob_oid:
            errors.append(f"{pin.name}: SOURCE_BLOB_OID_MISMATCH")
            continue
        if actual_sha256 != pin.sha256:
            errors.append(f"{pin.name}: SOURCE_BLOB_SHA256_MISMATCH")
            continue
        raw_sources[pin.name] = raw
        matched += 1

    task_files_matched = 0
    if matched == len(OVERLAY_PIN_NAMES):
        try:
            queue_text = raw_sources["queue"].decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(queue_text, newline=""))
            expected_header = [
                "task_id",
                "priority",
                "status",
                "title",
                "finding_id",
                "dependencies",
                "task_path",
                "acceptance",
            ]
            if reader.fieldnames != expected_header:
                raise ValueError("immutable queue header is noncanonical")
            queue_rows = list(reader)
            queue_mappings: set[tuple[str, str, str]] = set()
            for row in queue_rows:
                queue_task_path = validate_relative_path(
                    row["task_path"], label="immutable queue task path"
                )
                expected_queue_path = f"tasks/{row['task_id']}.md"
                if queue_task_path != expected_queue_path:
                    raise ValueError("immutable queue task path is noncanonical")
                queue_mappings.add(
                    (
                        row["task_id"],
                        row["finding_id"],
                        f"{functional_run}/spark/{queue_task_path}",
                    )
                )
            overlay_mappings = {
                (mapping.task_id, mapping.finding_id, mapping.task_path)
                for mapping in mappings
            }
            if len(queue_rows) != 17 or len(queue_mappings) != 17:
                raise ValueError("immutable queue does not contain 17 unique mappings")
            if queue_mappings != overlay_mappings:
                raise ValueError("overlay task mappings do not match the immutable queue")
            findings_text = raw_sources["findings_index"].decode("utf-8")
            indexed_findings = set(re.findall(r"FUNC-FIND-[0-9]{3}", findings_text))
            mapped_findings = {mapping.finding_id for mapping in mappings}
            if indexed_findings != mapped_findings:
                raise ValueError("overlay findings do not match the immutable index")
        except (UnicodeError, ValueError) as exc:
            errors.append(f"OVERLAY_TASK_MAPPING_MISMATCH: {exc}")
        else:
            source_commit = pins[0].source_commit
            for mapping in mappings:
                try:
                    actual_oid = _git(
                        executable,
                        root,
                        "rev-parse",
                        "--verify",
                        f"{source_commit}:{mapping.task_path}",
                    ).decode("ascii").strip()
                    if not _LOWER_SHA1_RE.fullmatch(actual_oid):
                        raise ValueError("task path did not resolve to a canonical object")
                    if (
                        _git(executable, root, "cat-file", "-t", actual_oid)
                        .decode("ascii")
                        .strip()
                        != "blob"
                    ):
                        raise ValueError("task path is not a Git blob")
                    size = int(
                        _git(executable, root, "cat-file", "-s", actual_oid)
                        .decode("ascii")
                        .strip()
                    )
                    if size < 0 or size > _MAX_SOURCE_BLOB_BYTES:
                        raise ValueError("task blob exceeds the verification bound")
                    task_bytes = _git(
                        executable,
                        root,
                        "cat-file",
                        "blob",
                        actual_oid,
                        max_stdout=size + 1,
                    )
                    if len(task_bytes) != size:
                        raise ValueError("task blob size changed during verification")
                    task_text = task_bytes.decode("utf-8")
                    if (
                        mapping.task_id not in task_text
                        or mapping.finding_id not in task_text
                    ):
                        raise ValueError("task blob identity does not match its mapping")
                except (UnicodeError, ValueError) as exc:
                    errors.append(
                        f"{mapping.task_id}: TASK_SOURCE_UNAVAILABLE: {exc}"
                    )
                    continue
                task_files_matched += 1
    return OverlaySourceValidation(
        pins,
        matched,
        len(mappings),
        task_files_matched,
        tuple(errors),
    )


def verify_reviewed_input_head(
    repository_root: str | os.PathLike[str],
    reviewed_input_commit: object,
    git_executable: str | os.PathLike[str],
) -> ReviewedInputValidation:
    """Require exact worktree HEAD equality; ancestry is intentionally insufficient."""

    if not isinstance(reviewed_input_commit, str) or not _LOWER_SHA1_RE.fullmatch(
        reviewed_input_commit
    ):
        return ReviewedInputValidation(str(reviewed_input_commit), None, ("INVALID_COMMIT",))
    try:
        executable, root = verified_git_repository_root(repository_root, git_executable)
        expected = _git(
            executable,
            root,
            "rev-parse",
            "--verify",
            f"{reviewed_input_commit}^{{commit}}",
        ).decode("ascii").strip()
        actual = _git(executable, root, "rev-parse", "HEAD").decode("ascii").strip()
        checkout_dirty = False
        if actual == reviewed_input_commit:
            try:
                _verify_exact_reviewed_checkout(
                    executable,
                    root,
                    reviewed_input_commit,
                )
            except _UncommittedStartState:
                checkout_dirty = True
        status = _git(
            executable,
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "-z",
            max_stdout=1024 * 1024,
        )
    except (UnicodeError, ValueError) as exc:
        return ReviewedInputValidation(
            reviewed_input_commit,
            None,
            (f"REVIEWED_INPUT_UNAVAILABLE: {exc}",),
        )
    errors: list[str] = []
    if expected != reviewed_input_commit:
        errors.append("REVIEWED_INPUT_NOT_EXACT")
    if actual != reviewed_input_commit:
        errors.append("UNREVIEWED_START_HEAD")
    if checkout_dirty or status:
        errors.append("UNCOMMITTED_START_STATE")
    return ReviewedInputValidation(reviewed_input_commit, actual, tuple(errors))
