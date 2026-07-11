from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from .execution_actions import (
    AtomicAction,
    CopyFileAction,
    CreateDirectoryAction,
    DeleteFileAction,
    ListDirectoryAction,
    MoveFileAction,
    ProcessAction,
    ProcessSignal,
    ReadFileAction,
    RegistryDeleteValueAction,
    RegistryGetAction,
    RegistryHive,
    RegistrySetAction,
    RegistryValueKind,
    ResolveHostAction,
    StatPathAction,
    TcpProbeAction,
    TerminateOwnedProcessAction,
    WriteFileAction,
)
from .execution_models import ActionFeedback
from .execution_process import ProcessRequest

PROTOCOL_VERSION = "jarvis.execution.v1"
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class ActionClass(StrEnum):
    READ_ONLY = "read_only"
    MUTATION = "mutation"
    PROCESS = "process"
    CONTROL = "control"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ActionModel(_StrictModel):
    action_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("action_id")
    @classmethod
    def validate_action_id(cls, value: str | None) -> str | None:
        if value is not None and not _ID_RE.fullmatch(value):
            raise ValueError("action_id contains unsupported characters")
        return value


class FsStatSpec(_ActionModel):
    kind: Literal["fs.stat"]
    path: str = Field(min_length=1, max_length=32_768)


class FsListSpec(_ActionModel):
    kind: Literal["fs.list"]
    path: str = Field(min_length=1, max_length=32_768)
    max_entries: StrictInt = Field(default=1000, ge=1, le=10_000)


class FsReadSpec(_ActionModel):
    kind: Literal["fs.read"]
    path: str = Field(min_length=1, max_length=32_768)
    offset: StrictInt = Field(default=0, ge=0)
    max_bytes: StrictInt = Field(default=1024 * 1024, ge=1, le=16 * 1024 * 1024)


class FsMkdirSpec(_ActionModel):
    kind: Literal["fs.mkdir"]
    path: str = Field(min_length=1, max_length=32_768)
    parents: StrictBool = True


class FsWriteSpec(_ActionModel):
    kind: Literal["fs.write"]
    path: str = Field(min_length=1, max_length=32_768)
    content_base64: str = Field(max_length=24 * 1024 * 1024)
    create_parents: StrictBool = False
    require_absent: StrictBool = False
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    mode: StrictInt | None = Field(default=None, ge=0, le=0o777)


class FsCopySpec(_ActionModel):
    kind: Literal["fs.copy"]
    source: str = Field(min_length=1, max_length=32_768)
    destination: str = Field(min_length=1, max_length=32_768)
    overwrite: StrictBool = False
    create_parents: StrictBool = False
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class FsMoveSpec(FsCopySpec):
    kind: Literal["fs.move"]


class FsDeleteSpec(_ActionModel):
    kind: Literal["fs.delete"]
    path: str = Field(min_length=1, max_length=32_768)
    missing_ok: StrictBool = False
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class ProcessRunSpec(_ActionModel):
    kind: Literal["process.run"]
    executable: str = Field(min_length=1, max_length=32_768)
    arguments: tuple[str, ...] = Field(default=(), max_length=512)
    cwd: str | None = Field(default=None, max_length=32_768)
    environment: dict[str, str] = Field(default_factory=dict, max_length=512)
    inherit_environment: StrictBool = False
    timeout_seconds: StrictFloat | StrictInt | None = Field(default=300.0, gt=0, le=86_400)
    stall_timeout_seconds: StrictFloat | StrictInt | None = Field(default=None, gt=0, le=86_400)
    interrupt_grace_seconds: StrictFloat | StrictInt = Field(default=3.0, gt=0, le=60)
    kill_grace_seconds: StrictFloat | StrictInt = Field(default=3.0, gt=0, le=60)
    max_output_bytes: StrictInt = Field(default=2 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    observe_paths: tuple[str, ...] = Field(default=(), max_length=32)
    max_observed_entries: StrictInt = Field(default=4096, ge=1, le=100_000)
    sensitive_argument_indices: frozenset[StrictInt] = Field(default_factory=frozenset)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class ProcessTerminateSpec(_ActionModel):
    kind: Literal["process.terminate"]
    session_id: str = Field(min_length=1, max_length=128)
    pid: StrictInt = Field(gt=0)
    signal: ProcessSignal = ProcessSignal.TERMINATE


class NetworkResolveSpec(_ActionModel):
    kind: Literal["network.resolve"]
    host: str = Field(min_length=1, max_length=253)
    port: StrictInt = Field(default=443, ge=1, le=65535)


class NetworkTcpProbeSpec(_ActionModel):
    kind: Literal["network.tcp_probe"]
    host: str = Field(min_length=1, max_length=253)
    port: StrictInt = Field(ge=1, le=65535)
    timeout_seconds: StrictFloat | StrictInt = Field(default=5.0, ge=0.05, le=60)


class RegistryGetSpec(_ActionModel):
    kind: Literal["registry.get"]
    hive: RegistryHive
    key: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=16_383)


class RegistrySetSpec(RegistryGetSpec):
    kind: Literal["registry.set"]
    value_kind: RegistryValueKind
    value: str | StrictInt | None = None
    value_base64: str | None = Field(default=None, max_length=24 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_value_representation(self) -> RegistrySetSpec:
        if self.value_kind is RegistryValueKind.BINARY:
            if self.value_base64 is None or self.value is not None:
                raise ValueError("binary values require only value_base64")
        elif self.value is None or self.value_base64 is not None:
            raise ValueError("non-binary values require only value")
        return self


class RegistryDeleteSpec(RegistryGetSpec):
    kind: Literal["registry.delete"]
    missing_ok: StrictBool = False


ActionSpec = Annotated[
    FsStatSpec
    | FsListSpec
    | FsReadSpec
    | FsMkdirSpec
    | FsWriteSpec
    | FsCopySpec
    | FsMoveSpec
    | FsDeleteSpec
    | ProcessRunSpec
    | ProcessTerminateSpec
    | NetworkResolveSpec
    | NetworkTcpProbeSpec
    | RegistryGetSpec
    | RegistrySetSpec
    | RegistryDeleteSpec,
    Field(discriminator="kind"),
]


class ActionEnvelope(_StrictModel):
    protocol: Literal["jarvis.execution.v1"]
    action: ActionSpec


class ResultEnvelope(_StrictModel):
    protocol: Literal["jarvis.execution.v1"] = PROTOCOL_VERSION
    ok: StrictBool
    action_id: str
    result: ActionFeedback


def parse_action(payload: str | bytes | Mapping[str, Any]) -> AtomicAction:
    if isinstance(payload, str | bytes):
        envelope = ActionEnvelope.model_validate_json(payload)
    elif isinstance(payload, Mapping):
        envelope = ActionEnvelope.model_validate(dict(payload))
    else:
        raise TypeError("action payload must be JSON text or a mapping")
    spec = envelope.action
    common = {"action_id": spec.action_id} if spec.action_id else {}
    if isinstance(spec, FsStatSpec):
        return StatPathAction(path=_absolute_path(spec.path), **common)
    if isinstance(spec, FsListSpec):
        return ListDirectoryAction(
            path=_absolute_path(spec.path), max_entries=spec.max_entries, **common
        )
    if isinstance(spec, FsReadSpec):
        return ReadFileAction(
            path=_absolute_path(spec.path),
            offset=spec.offset,
            max_bytes=spec.max_bytes,
            **common,
        )
    if isinstance(spec, FsMkdirSpec):
        return CreateDirectoryAction(path=_absolute_path(spec.path), parents=spec.parents, **common)
    if isinstance(spec, FsWriteSpec):
        return WriteFileAction(
            path=_absolute_path(spec.path),
            content=_decode_base64(spec.content_base64, "content_base64"),
            create_parents=spec.create_parents,
            require_absent=spec.require_absent,
            expected_sha256=spec.expected_sha256,
            mode=spec.mode,
            **common,
        )
    if isinstance(spec, FsMoveSpec):
        return MoveFileAction(
            source=_absolute_path(spec.source),
            destination=_absolute_path(spec.destination),
            overwrite=spec.overwrite,
            create_parents=spec.create_parents,
            expected_sha256=spec.expected_sha256,
            **common,
        )
    if isinstance(spec, FsCopySpec):
        return CopyFileAction(
            source=_absolute_path(spec.source),
            destination=_absolute_path(spec.destination),
            overwrite=spec.overwrite,
            create_parents=spec.create_parents,
            expected_sha256=spec.expected_sha256,
            **common,
        )
    if isinstance(spec, FsDeleteSpec):
        return DeleteFileAction(
            path=_absolute_path(spec.path),
            missing_ok=spec.missing_ok,
            expected_sha256=spec.expected_sha256,
            **common,
        )
    if isinstance(spec, ProcessRunSpec):
        request = ProcessRequest(
            executable=spec.executable,
            arguments=spec.arguments,
            cwd=_absolute_path(spec.cwd) if spec.cwd else None,
            environment=spec.environment,
            inherit_environment=spec.inherit_environment,
            timeout_seconds=(
                float(spec.timeout_seconds) if spec.timeout_seconds is not None else None
            ),
            stall_timeout_seconds=(
                float(spec.stall_timeout_seconds)
                if spec.stall_timeout_seconds is not None
                else None
            ),
            interrupt_grace_seconds=float(spec.interrupt_grace_seconds),
            kill_grace_seconds=float(spec.kill_grace_seconds),
            max_output_bytes=spec.max_output_bytes,
            observe_paths=tuple(_absolute_path(path) for path in spec.observe_paths),
            max_observed_entries=spec.max_observed_entries,
            sensitive_argument_indices=frozenset(spec.sensitive_argument_indices),
        )
        return ProcessAction(request=request, session_id=spec.session_id, **common)
    if isinstance(spec, ProcessTerminateSpec):
        return TerminateOwnedProcessAction(
            session_id=spec.session_id, pid=spec.pid, signal=spec.signal, **common
        )
    if isinstance(spec, NetworkResolveSpec):
        return ResolveHostAction(host=spec.host, port=spec.port, **common)
    if isinstance(spec, NetworkTcpProbeSpec):
        return TcpProbeAction(
            host=spec.host, port=spec.port, timeout_seconds=float(spec.timeout_seconds), **common
        )
    if isinstance(spec, RegistrySetSpec):
        value: str | int | bytes
        if spec.value_kind is RegistryValueKind.BINARY:
            if spec.value_base64 is None:
                raise ValueError("binary registry values require value_base64")
            value = _decode_base64(spec.value_base64, "value_base64")
        else:
            if spec.value_base64 is not None:
                raise ValueError("value_base64 is valid only for binary registry values")
            value = spec.value
            if value is None:
                raise ValueError("registry value is required")
        return RegistrySetAction(
            hive=spec.hive,
            key=spec.key,
            name=spec.name,
            value=value,
            value_kind=spec.value_kind,
            **common,
        )
    if isinstance(spec, RegistryDeleteSpec):
        return RegistryDeleteValueAction(
            hive=spec.hive,
            key=spec.key,
            name=spec.name,
            missing_ok=spec.missing_ok,
            **common,
        )
    if isinstance(spec, RegistryGetSpec):
        return RegistryGetAction(hive=spec.hive, key=spec.key, name=spec.name, **common)
    raise TypeError(f"unsupported action spec: {type(spec).__name__}")


def serialize_result(feedback: ActionFeedback) -> str:
    envelope = ResultEnvelope(
        ok=feedback.ok,
        action_id=feedback.action_id,
        result=feedback,
    )
    return envelope.model_dump_json()


def action_json_schema() -> dict[str, Any]:
    return ActionEnvelope.model_json_schema()


def classify_action(action: AtomicAction) -> ActionClass:
    if isinstance(
        action,
        StatPathAction
        | ListDirectoryAction
        | ReadFileAction
        | ResolveHostAction
        | TcpProbeAction
        | RegistryGetAction,
    ):
        return ActionClass.READ_ONLY
    if isinstance(
        action,
        CreateDirectoryAction
        | WriteFileAction
        | CopyFileAction
        | MoveFileAction
        | DeleteFileAction
        | RegistrySetAction
        | RegistryDeleteValueAction,
    ):
        return ActionClass.MUTATION
    if isinstance(action, ProcessAction):
        return ActionClass.PROCESS
    if isinstance(action, TerminateOwnedProcessAction):
        return ActionClass.CONTROL
    raise TypeError(f"unsupported action: {type(action).__name__}")


def classify_payload(payload: str | bytes | Mapping[str, Any]) -> ActionClass:
    return classify_action(parse_action(payload))


def is_mutating(action: AtomicAction) -> bool:
    return classify_action(action) in {
        ActionClass.MUTATION,
        ActionClass.PROCESS,
        ActionClass.CONTROL,
    }


def canonical_action_json(payload: str | bytes | Mapping[str, Any]) -> str:
    if isinstance(payload, str | bytes):
        envelope = ActionEnvelope.model_validate_json(payload)
    else:
        envelope = ActionEnvelope.model_validate(dict(payload))
    return json.dumps(envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _absolute_path(raw: str) -> Path:
    if "\x00" in raw:
        raise ValueError("paths cannot contain NUL")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("protocol paths must be absolute")
    return path


def _decode_base64(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be canonical Base64") from exc
