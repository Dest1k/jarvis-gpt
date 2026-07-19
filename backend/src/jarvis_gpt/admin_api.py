from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .admin_models import (
    PermissionPresetCreateRequest,
    PermissionPresetUpdateRequest,
    TelegramSessionRequest,
    TelegramSessionResponse,
    UserPermissionUpdateRequest,
    UserPresetAssignmentRequest,
    UserStatusUpdateRequest,
)
from .authorization import (
    OWNER_RECOVERY_SECURITY_IDS,
    AuthorizationDecision,
    AuthorizationError,
    AuthorizationService,
    CapabilityDefinition,
    ConcurrentPolicyUpdateError,
    current_actor,
)

ADMIN_API_CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        "admin.users.list",
        "List registered users",
        "admin",
        2,
        source="admin_api",
        default_presets=("moderator", "admin"),
    ),
    CapabilityDefinition(
        "admin.users.permissions.list",
        "Inspect a user's effective permissions",
        "admin",
        3,
        source="admin_api",
    ),
    CapabilityDefinition(
        "admin.users.status.update",
        "Suspend, reactivate, or delete a user",
        "admin",
        3,
        True,
        source="admin_api",
    ),
    CapabilityDefinition(
        "admin.users.preset.assign",
        "Assign a permission preset to a user",
        "admin",
        4,
        True,
        source="admin_api",
    ),
    CapabilityDefinition(
        "admin.users.permission.set",
        "Set a direct user permission",
        "admin",
        4,
        True,
        source="admin_api",
    ),
    CapabilityDefinition(
        "admin.users.permission.revoke",
        "Revoke a direct user permission",
        "admin",
        4,
        True,
        source="admin_api",
    ),
    CapabilityDefinition(
        "admin.security_ids.list",
        "List registered security identifiers",
        "admin",
        2,
        source="admin_api",
        default_presets=("moderator", "admin"),
    ),
    CapabilityDefinition(
        "admin.presets.list",
        "List permission presets",
        "admin",
        2,
        source="admin_api",
        default_presets=("moderator", "admin"),
    ),
    CapabilityDefinition(
        "admin.audit.list",
        "Read security administration audit",
        "admin",
        3,
        source="admin_api",
    ),
    CapabilityDefinition(
        "admin.presets.create",
        "Create a custom permission preset",
        "admin",
        4,
        True,
        source="admin_api",
    ),
    CapabilityDefinition(
        "admin.presets.update",
        "Publish a new custom preset version",
        "admin",
        4,
        True,
        source="admin_api",
    ),
)


router = APIRouter()
_ROUTE_APPROVAL_TTL = timedelta(minutes=10)


def _authorization(request: Request) -> AuthorizationService:
    service = getattr(request.app.state, "authorization", None)
    if not isinstance(service, AuthorizationService):
        raise HTTPException(status_code=503, detail="Authorization service is unavailable")
    return service


def require_security_id(
    security_id: str,
) -> Callable[[Request], Any]:
    """FastAPI dependency that enforces and records one explicit route capability."""

    # Validate route declarations at import time, rather than failing open at request time.
    CapabilityDefinition(security_id, security_id, "http").validate()

    async def dependency(request: Request) -> AuthorizationDecision:
        actor = getattr(request.state, "actor", None)
        if actor is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        decision = _authorization(request).authorize(
            actor.user_id,
            security_id,
            identity_id=actor.identity_id,
            request_id=getattr(request.state, "request_id", None),
            context={"method": request.method, "path": request.url.path},
        )
        request.state.authorization_decision = decision
        if not decision.allowed:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "security_id": security_id,
                    "reason": decision.reason_code,
                    "decision_id": decision.decision_id,
                },
            )
        await _enforce_declared_hitl(request, service=_authorization(request), decision=decision)
        return decision

    return dependency


async def _enforce_declared_hitl(
    request: Request,
    *,
    service: AuthorizationService,
    decision: AuthorizationDecision,
) -> None:
    """Turn capability HITL metadata into a one-use, request-bound gate.

    Admin routes use explicit capability dependencies instead of the general HTTP-route
    catalog.  They still need the same fail-closed approval semantics before their handler
    can mutate users, presets, or permissions.
    """

    with service.storage.locked_connection() as conn:
        capability = conn.execute(
            "SELECT default_requires_hitl FROM security_ids WHERE security_id = ?",
            (decision.security_id,),
        ).fetchone()
    if capability is None or not bool(capability["default_requires_hitl"]):
        return

    body = await request.body()
    body_sha256 = hashlib.sha256(body).hexdigest()
    target_state = _admin_target_state_digest(request, service=service, body=body)
    fingerprint = hashlib.sha256(
        "\n".join(
            (
                request.method.upper(),
                request.url.path,
                request.url.query,
                decision.security_id,
                body_sha256,
                target_state,
            )
        ).encode("utf-8")
    ).hexdigest()
    supplied_approval_id = str(
        request.headers.get("x-jarvis-approval-id") or ""
    ).strip()
    if supplied_approval_id:
        approval = service.storage.get_approval(supplied_approval_id)
        payload = approval.get("payload") if isinstance(approval, dict) else None
        valid = bool(
            isinstance(approval, dict)
            and approval.get("status") == "approved"
            and approval.get("requested_action") == "admin.route.authorize"
            and isinstance(payload, dict)
            and payload.get("protocol") == "jarvis.admin-route-approval.v1"
            and payload.get("security_id") == decision.security_id
            and payload.get("request_fingerprint") == fingerprint
            and int(payload.get("policy_epoch") or -1) == decision.policy_epoch
            and payload.get("target_state_sha256") == target_state
            and _approval_is_fresh(approval, payload)
        )
        if valid and service.storage.claim_approval_execution(supplied_approval_id) is not None:
            request.state.http_approval_id = supplied_approval_id
            return
        raise HTTPException(
            status_code=409,
            detail={
                "error": "invalid_approval",
                "message": "Approval is stale, mismatched, or already used.",
                "security_id": decision.security_id,
            },
        )

    approval = service.storage.create_approval(
        title=f"Confirm {request.method.upper()} {request.url.path}",
        description="A security administration change requires one-use human approval.",
        requested_action="admin.route.authorize",
        risk="danger",
        payload={
            "protocol": "jarvis.admin-route-approval.v1",
            "security_id": decision.security_id,
            "request_fingerprint": fingerprint,
            "body_sha256": body_sha256,
            "policy_epoch": decision.policy_epoch,
            "target_state_sha256": target_state,
            "expires_at": (datetime.now(UTC) + _ROUTE_APPROVAL_TTL).isoformat(
                timespec="seconds"
            ),
        },
    )
    raise HTTPException(
        status_code=428,
        detail={
            "error": "approval_required",
            "message": "Human approval is required before this operation.",
            "approval_id": approval["id"],
            "security_id": decision.security_id,
        },
    )


def _approval_is_fresh(approval: dict[str, Any], payload: dict[str, Any]) -> bool:
    try:
        created_at = datetime.fromisoformat(str(approval["created_at"]))
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    if created_at.tzinfo is None or expires_at.tzinfo is None:
        return False
    now = datetime.now(UTC)
    return created_at <= now <= expires_at and expires_at - created_at <= _ROUTE_APPROVAL_TTL


def _admin_target_state_digest(
    request: Request,
    *,
    service: AuthorizationService,
    body: bytes,
) -> str:
    """Bind an IAM approval to the exact target policy state it reviewed."""

    target: dict[str, Any] = {"path": request.url.path}
    user_id = str(request.path_params.get("user_id") or "").strip()
    preset_key = str(request.path_params.get("preset_key") or "").strip()
    if not preset_key and request.url.path.rstrip("/") == "/api/admin/presets":
        try:
            parsed = json.loads(body or b"{}")
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            preset_key = str(parsed.get("key") or "").strip()
    with service.storage.locked_connection() as conn:
        if user_id:
            user = conn.execute(
                """
                SELECT u.status, u.policy_epoch, u.row_version, p.preset_key,
                       p.active_version_id
                FROM users u
                LEFT JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                LEFT JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()
            overrides = conn.execute(
                """
                SELECT s.security_id, up.effect, up.can_delegate, up.valid_until
                FROM user_permissions up
                JOIN security_ids s ON s.id = up.security_id_id
                WHERE up.user_id = ? AND up.revoked_at IS NULL
                ORDER BY s.security_id
                """,
                (user_id,),
            ).fetchall()
            target["user"] = dict(user) if user is not None else None
            target["overrides"] = [dict(row) for row in overrides]
            if user is not None:
                request.state.admin_target_user = {
                    "user_id": user_id,
                    "row_version": int(user["row_version"]),
                }
        if preset_key:
            preset = conn.execute(
                """
                SELECT p.id, p.kind, p.active_version_id, p.updated_at, pv.version
                FROM permission_presets p
                LEFT JOIN permission_preset_versions pv ON pv.id = p.active_version_id
                WHERE p.preset_key = ?
                """,
                (preset_key,),
            ).fetchone()
            target["preset"] = dict(preset) if preset is not None else None
            request.state.admin_target_preset = {
                "preset_key": preset_key,
                "active_version_id": (
                    str(preset["active_version_id"])
                    if preset is not None and preset["active_version_id"]
                    else None
                ),
            }
    canonical = json.dumps(
        target,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _expected_user_row_version(request: Request, user_id: str) -> int | None:
    snapshot = getattr(request.state, "admin_target_user", None)
    if not isinstance(snapshot, dict) or snapshot.get("user_id") != user_id:
        return None
    value = snapshot.get("row_version")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _expected_preset_version(request: Request, preset_key: str) -> str | None:
    snapshot = getattr(request.state, "admin_target_preset", None)
    if not isinstance(snapshot, dict) or snapshot.get("preset_key") != preset_key:
        return None
    value = snapshot.get("active_version_id")
    return str(value) if value else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _user_exists(service: AuthorizationService, user_id: str) -> bool:
    with service.storage.locked_connection() as conn:
        return conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None


def _target_preset(service: AuthorizationService, user_id: str) -> str | None:
    with service.storage.locked_connection() as conn:
        row = conn.execute(
            """
            SELECT p.preset_key
            FROM users u
            LEFT JOIN user_preset_assignments upa
              ON upa.user_id = u.id AND upa.revoked_at IS NULL
            LEFT JOIN permission_presets p ON p.id = upa.preset_id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return str(row["preset_key"]) if row["preset_key"] else None


def _assert_target_manageable(service: AuthorizationService, user_id: str) -> None:
    actor = service.actor_for_user(
        current_actor().user_id, source=current_actor().source
    )
    if actor is None:
        raise HTTPException(status_code=403, detail="Actor is inactive")
    if not actor.is_owner and _target_preset(service, user_id) == "owner":
        raise HTTPException(status_code=403, detail="Only an owner may modify another owner")
    if not _user_exists(service, user_id):
        raise HTTPException(status_code=404, detail="User not found")


def _delegable_security_ids(service: AuthorizationService, security_ids: list[str]) -> list[str]:
    ordered = list(dict.fromkeys(item.strip() for item in security_ids if item.strip()))
    actor = service.actor_for_user(
        current_actor().user_id, source=current_actor().source
    )
    if actor is None:
        raise HTTPException(status_code=403, detail="Actor is inactive")
    if actor.is_owner or not ordered:
        return ordered
    with service.storage.locked_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.security_id, MAX(r.can_delegate) AS can_delegate
            FROM security_ids s
            LEFT JOIN (
                SELECT psi.security_id_id, psi.can_delegate
                FROM user_preset_assignments upa
                JOIN permission_presets p ON p.id = upa.preset_id
                JOIN preset_security_ids psi ON psi.preset_version_id = p.active_version_id
                WHERE upa.user_id = ? AND upa.revoked_at IS NULL AND psi.effect = 'grant'
                UNION ALL
                SELECT up.security_id_id, up.can_delegate
                FROM user_permissions up
                WHERE up.user_id = ? AND up.revoked_at IS NULL AND up.effect = 'grant'
                  AND (up.valid_until IS NULL OR up.valid_until > ?)
            ) r ON r.security_id_id = s.id
            WHERE s.security_id IN ({}) AND s.status = 'active'
            GROUP BY s.id
            """.format(",".join("?" for _ in ordered)),
            (actor.user_id, actor.user_id, _now(), *ordered),
        ).fetchall()
    allowed = {str(row["security_id"]) for row in rows if int(row["can_delegate"] or 0)}
    allowed = {
        item
        for item in allowed
        if service.authorize(actor.user_id, item, record=False).allowed
    }
    missing = sorted(set(ordered) - allowed)
    if missing:
        raise HTTPException(
            status_code=403,
            detail={"error": "not_delegable", "security_ids": missing},
        )
    return ordered


def _validate_security_ids(
    service: AuthorizationService, security_ids: list[str]
) -> list[tuple[str, str]]:
    ordered = _delegable_security_ids(service, security_ids)
    if not ordered:
        return []
    with service.storage.locked_connection() as conn:
        rows = conn.execute(
            "SELECT id, security_id FROM security_ids "
            f"WHERE status = 'active' AND security_id IN ({','.join('?' for _ in ordered)})",
            tuple(ordered),
        ).fetchall()
    by_name = {str(row["security_id"]): str(row["id"]) for row in rows}
    missing = [item for item in ordered if item not in by_name]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_security_ids", "security_ids": missing},
        )
    return [(item, by_name[item]) for item in ordered]


def _preset_payload(service: AuthorizationService, preset_key: str) -> dict[str, Any]:
    with service.storage.locked_connection() as conn:
        row = conn.execute(
            """
            SELECT p.id, p.preset_key, p.display_name, p.kind, p.active_version_id,
                   pv.version, pv.published_at, pv.change_reason
            FROM permission_presets p
            JOIN permission_preset_versions pv ON pv.id = p.active_version_id
            WHERE p.preset_key = ? AND p.archived_at IS NULL
            """,
            (preset_key,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        permissions = conn.execute(
            """
            SELECT s.security_id
            FROM preset_security_ids psi
            JOIN security_ids s ON s.id = psi.security_id_id
            WHERE psi.preset_version_id = ? AND psi.effect = 'grant'
            ORDER BY s.security_id
            """,
            (row["active_version_id"],),
        ).fetchall()
    return {
        **dict(row),
        "description": str(row["change_reason"] or ""),
        "security_ids": [str(item["security_id"]) for item in permissions],
    }


@router.get(
    "/api/admin/users",
    dependencies=[Depends(require_security_id("admin.users.list"))],
)
def list_users(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default="", max_length=160),
) -> dict[str, Any]:
    return _authorization(request).list_users_page(
        limit=limit,
        offset=offset,
        search=search,
    )


@router.get(
    "/api/admin/users/{user_id}/permissions",
    dependencies=[Depends(require_security_id("admin.users.permissions.list"))],
)
def user_permissions(request: Request, user_id: str) -> list[dict[str, Any]]:
    service = _authorization(request)
    if not _user_exists(service, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return [
        {**item, "allowed": item.get("effect") == "allow"}
        for item in service.effective_permissions(user_id)
    ]


@router.patch(
    "/api/admin/users/{user_id}/status",
    dependencies=[Depends(require_security_id("admin.users.status.update"))],
)
def update_user_status(
    request: Request, user_id: str, payload: UserStatusUpdateRequest
) -> dict[str, Any]:
    service = _authorization(request)
    _assert_target_manageable(service, user_id)
    try:
        return service.set_user_status(
            user_id=user_id,
            status=payload.status,
            reason=payload.reason,
            expected_row_version=_expected_user_row_version(request, user_id),
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put(
    "/api/admin/users/{user_id}/preset",
    dependencies=[Depends(require_security_id("admin.users.preset.assign"))],
)
def assign_user_preset(
    request: Request, user_id: str, payload: UserPresetAssignmentRequest
) -> dict[str, Any]:
    service = _authorization(request)
    _assert_target_manageable(service, user_id)
    actor = service.actor_for_user(
        current_actor().user_id, source=current_actor().source
    )
    if actor is None:
        raise HTTPException(status_code=403, detail="Actor is inactive")
    if not actor.is_owner:
        preset = _preset_payload(service, payload.preset_key)
        _delegable_security_ids(service, list(preset["security_ids"]))
    try:
        return service.assign_preset(
            user_id=user_id,
            preset_key=payload.preset_key,
            assigned_by=current_actor().user_id,
            reason=payload.reason,
            expected_row_version=_expected_user_row_version(request, user_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentPolicyUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.put(
    "/api/admin/users/{user_id}/permissions/{security_id}",
    dependencies=[Depends(require_security_id("admin.users.permission.set"))],
)
def set_user_permission(
    request: Request,
    user_id: str,
    security_id: str,
    payload: UserPermissionUpdateRequest,
) -> dict[str, Any]:
    service = _authorization(request)
    _assert_target_manageable(service, user_id)
    _delegable_security_ids(service, [security_id])
    try:
        return service.set_user_permission(
            user_id=user_id,
            security_id=security_id,
            effect=payload.effect,
            can_delegate=payload.can_delegate,
            granted_by=current_actor().user_id,
            reason=payload.reason,
            valid_until=(
                payload.valid_until.astimezone(UTC).isoformat(timespec="seconds")
                if payload.valid_until is not None
                else None
            ),
            expected_row_version=_expected_user_row_version(request, user_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentPolicyUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete(
    "/api/admin/users/{user_id}/permissions/{security_id}",
    dependencies=[Depends(require_security_id("admin.users.permission.revoke"))],
)
def revoke_user_permission(
    request: Request,
    user_id: str,
    security_id: str,
) -> dict[str, Any]:
    service = _authorization(request)
    _assert_target_manageable(service, user_id)
    _delegable_security_ids(service, [security_id])
    now = _now()
    with service.storage.transaction(immediate=True) as conn:
        target = conn.execute(
            """
            SELECT u.row_version, p.preset_key
            FROM users u
            LEFT JOIN user_preset_assignments upa
              ON upa.user_id = u.id AND upa.revoked_at IS NULL
            LEFT JOIN permission_presets p ON p.id = upa.preset_id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        expected_row_version = _expected_user_row_version(request, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if (
            expected_row_version is not None
            and int(target["row_version"]) != expected_row_version
        ):
            raise HTTPException(status_code=409, detail="Target user changed")
        if target["preset_key"] == "owner":
            try:
                service.assert_actor_is_active_owner(conn, current_actor().user_id)
            except AuthorizationError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        capability = conn.execute(
            "SELECT id FROM security_ids WHERE security_id = ?", (security_id,)
        ).fetchone()
        if capability is None:
            raise HTTPException(status_code=404, detail="Unknown security_id")
        cursor = conn.execute(
            """
            UPDATE user_permissions SET revoked_at = ?
            WHERE user_id = ? AND security_id_id = ? AND revoked_at IS NULL
            """,
            (now, user_id, capability["id"]),
        )
        if (
            target["preset_key"] == "owner"
            and security_id in OWNER_RECOVERY_SECURITY_IDS
        ):
            service.assert_owner_recovery_invariant(conn)
        conn.execute(
            """
            UPDATE users SET policy_epoch = policy_epoch + 1,
                row_version = row_version + 1, updated_at = ? WHERE id = ?
            """,
            (now, user_id),
        )
        conn.execute(
            "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, user_id),
        )
        service.append_security_audit(
            conn,
            action="user.permission.revoke",
            target_type="security_id",
            target_id=security_id,
            target_user_id=user_id,
            reason="Direct permission override revoked",
            after={"revoked": cursor.rowcount > 0},
        )
    return {"ok": True, "revoked": cursor.rowcount > 0, "security_id": security_id}


@router.get(
    "/api/admin/security-ids",
    dependencies=[Depends(require_security_id("admin.security_ids.list"))],
)
def list_security_ids(request: Request) -> list[dict[str, Any]]:
    return _authorization(request).list_security_ids()


@router.get(
    "/api/admin/audit",
    dependencies=[Depends(require_security_id("admin.audit.list"))],
)
def list_security_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return _authorization(request).list_security_audit(limit=limit, offset=offset)


@router.get(
    "/api/admin/presets",
    dependencies=[Depends(require_security_id("admin.presets.list"))],
)
def list_presets(request: Request) -> list[dict[str, Any]]:
    service = _authorization(request)
    return [_preset_payload(service, item["preset_key"]) for item in service.list_presets()]


@router.post(
    "/api/admin/presets",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_security_id("admin.presets.create"))],
)
def create_preset(request: Request, payload: PermissionPresetCreateRequest) -> dict[str, Any]:
    service = _authorization(request)
    capabilities = _validate_security_ids(service, payload.security_ids)
    now = _now()
    preset_id = _new_id("preset")
    version_id = _new_id("presetv")
    try:
        with service.storage.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO permission_presets(
                    id, preset_key, display_name, kind, active_version_id,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, 'custom', NULL, ?, ?, ?)
                """,
                (
                    preset_id,
                    payload.key,
                    payload.name,
                    current_actor().user_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO permission_preset_versions(
                    id, preset_id, version, state, created_by, created_at,
                    published_at, change_reason
                ) VALUES (?, ?, 1, 'published', ?, ?, ?, ?)
                """,
                (
                    version_id,
                    preset_id,
                    current_actor().user_id,
                    now,
                    now,
                    payload.description,
                ),
            )
            conn.executemany(
                """
                INSERT INTO preset_security_ids(
                    preset_version_id, security_id_id, effect, can_delegate
                ) VALUES (?, ?, 'grant', 0)
                """,
                [(version_id, capability_id) for _, capability_id in capabilities],
            )
            conn.execute(
                "UPDATE permission_presets SET active_version_id = ? WHERE id = ?",
                (version_id, preset_id),
            )
            service.append_security_audit(
                conn,
                action="preset.create",
                target_type="permission_preset",
                target_id=preset_id,
                target_user_id=None,
                reason=payload.description,
                after={
                    "preset_key": payload.key,
                    "version": 1,
                    "security_ids": [item for item, _ in capabilities],
                },
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Preset key already exists") from exc
    return _preset_payload(service, payload.key)


@router.put(
    "/api/admin/presets/{preset_key}",
    dependencies=[Depends(require_security_id("admin.presets.update"))],
)
def update_preset(
    request: Request, preset_key: str, payload: PermissionPresetUpdateRequest
) -> dict[str, Any]:
    service = _authorization(request)
    capabilities = _validate_security_ids(service, payload.security_ids)
    now = _now()
    with service.storage.transaction(immediate=True) as conn:
        preset = conn.execute(
            """
            SELECT id, kind, active_version_id
            FROM permission_presets
            WHERE preset_key = ? AND archived_at IS NULL
            """,
            (preset_key,),
        ).fetchone()
        if preset is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        expected_version = _expected_preset_version(request, preset_key)
        if expected_version is not None and str(preset["active_version_id"]) != expected_version:
            raise HTTPException(status_code=409, detail="Preset changed after authorization")
        if preset["kind"] != "custom":
            raise HTTPException(status_code=409, detail="Built-in presets are immutable")
        version = int(
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version "
                "FROM permission_preset_versions WHERE preset_id = ?",
                (preset["id"],),
            ).fetchone()["version"]
        )
        version_id = _new_id("presetv")
        conn.execute(
            "UPDATE permission_preset_versions SET state = 'retired' "
            "WHERE id = ? AND state = 'published'",
            (preset["active_version_id"],),
        )
        conn.execute(
            """
            INSERT INTO permission_preset_versions(
                id, preset_id, version, state, created_by, created_at,
                published_at, change_reason
            ) VALUES (?, ?, ?, 'published', ?, ?, ?, ?)
            """,
            (
                version_id,
                preset["id"],
                version,
                current_actor().user_id,
                now,
                now,
                payload.description,
            ),
        )
        conn.executemany(
            """
            INSERT INTO preset_security_ids(
                preset_version_id, security_id_id, effect, can_delegate
            ) VALUES (?, ?, 'grant', 0)
            """,
            [(version_id, capability_id) for _, capability_id in capabilities],
        )
        conn.execute(
            """
            UPDATE permission_presets
            SET display_name = ?, active_version_id = ?, updated_at = ? WHERE id = ?
            """,
            (payload.name, version_id, now, preset["id"]),
        )
        affected = conn.execute(
            """
            SELECT user_id FROM user_preset_assignments
            WHERE preset_id = ? AND revoked_at IS NULL
            """,
            (preset["id"],),
        ).fetchall()
        affected_ids = [str(row["user_id"]) for row in affected]
        if affected_ids:
            placeholders = ",".join("?" for _ in affected_ids)
            conn.execute(
                f"UPDATE users SET policy_epoch = policy_epoch + 1, "
                f"row_version = row_version + 1, updated_at = ? WHERE id IN ({placeholders})",
                (now, *affected_ids),
            )
            conn.execute(
                f"UPDATE user_sessions SET revoked_at = ? "
                f"WHERE revoked_at IS NULL AND user_id IN ({placeholders})",
                (now, *affected_ids),
            )
        service.append_security_audit(
            conn,
            action="preset.update",
            target_type="permission_preset",
            target_id=str(preset["id"]),
            target_user_id=None,
            reason=payload.description,
            before={"active_version_id": preset["active_version_id"]},
            after={
                "version": version,
                "security_ids": [item for item, _ in capabilities],
                "affected_user_ids": affected_ids,
            },
        )
    return _preset_payload(service, preset_key)


def _canonical_telegram_realm_id(bot_id: int) -> str:
    if isinstance(bot_id, bool) or bot_id <= 0:
        raise ValueError("Telegram bot id must be a positive integer")
    return f"telegram:{bot_id}"


def _bind_telegram_realm(
    conn: sqlite3.Connection,
    *,
    realm_id: str,
    bot_id: int,
    now: str,
) -> None:
    by_realm = conn.execute(
        "SELECT bot_id FROM telegram_realms WHERE realm_id = ?",
        (realm_id,),
    ).fetchone()
    if by_realm is not None and int(by_realm["bot_id"]) != bot_id:
        raise HTTPException(
            status_code=409,
            detail="Telegram realm is already bound to another bot",
        )
    by_bot = conn.execute(
        "SELECT realm_id FROM telegram_realms WHERE bot_id = ?",
        (bot_id,),
    ).fetchone()
    if by_bot is not None and str(by_bot["realm_id"]) != realm_id:
        raise HTTPException(
            status_code=409,
            detail="Telegram bot is already bound to another realm",
        )
    if by_realm is None:
        conn.execute(
            """
            INSERT INTO telegram_realms(realm_id, bot_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (realm_id, bot_id, now, now),
        )
    else:
        conn.execute(
            "UPDATE telegram_realms SET last_seen_at = ? WHERE realm_id = ?",
            (now, realm_id),
        )


def _telegram_session_ttl() -> int:
    try:
        value = int(os.environ.get("JARVIS_TELEGRAM_SESSION_TTL_SECONDS", "900"))
    except ValueError:
        value = 900
    return max(300, min(value, 86_400))


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _enforce_telegram_ingress_limit(
    service: AuthorizationService,
    *,
    realm_id: str,
    telegram_user_id: int,
) -> None:
    per_user = service.consume_rate_limit(
        scope="telegram.user",
        subject=f"{realm_id}:{telegram_user_id}",
        limit=_bounded_env_int(
            "JARVIS_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE",
            30,
            minimum=1,
            maximum=10_000,
        ),
    )
    if not bool(per_user["allowed"]):
        retry_after = int(per_user["retry_after"])
        raise HTTPException(
            status_code=429,
            detail="Telegram user rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
    global_budget = service.consume_rate_limit(
        scope="telegram.global",
        subject=realm_id,
        limit=_bounded_env_int(
            "JARVIS_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE",
            1_200,
            minimum=1,
            maximum=1_000_000,
        ),
    )
    if not bool(global_budget["allowed"]):
        retry_after = int(global_budget["retry_after"])
        raise HTTPException(
            status_code=429,
            detail="Telegram ingress is temporarily saturated",
            headers={"Retry-After": str(retry_after)},
        )


def _finalize_telegram_update_lease(
    service: AuthorizationService,
    *,
    realm_id: str,
    update_id: int,
    lease_token: str,
    status: str,
    user_id: str | None = None,
    last_error: str | None = None,
) -> bool:
    """Finalize only the attempt that still owns the replay-ledger lease."""

    if status not in {"completed", "failed"}:
        raise ValueError("invalid Telegram update final status")
    with service.storage.transaction(immediate=True) as conn:
        cursor = conn.execute(
            """
            UPDATE telegram_updates
            SET user_id = COALESCE(?, user_id), status = ?, last_error = ?, updated_at = ?
            WHERE realm_id = ? AND update_id = ?
              AND status = 'processing' AND lease_token = ?
            """,
            (
                user_id,
                status,
                last_error,
                _now(),
                realm_id,
                update_id,
                lease_token,
            ),
        )
    return cursor.rowcount == 1


def _discard_unpublished_telegram_session(
    service: AuthorizationService,
    session_id: str | None,
) -> None:
    """Remove a session created by an attempt that lost its lease before publication."""

    if not session_id:
        return
    try:
        with service.storage.transaction(immediate=True) as conn:
            conn.execute(
                "DELETE FROM user_sessions WHERE id = ? AND auth_method = 'telegram-bridge'",
                (session_id,),
            )
    except Exception:
        # The token was never returned. Even if cleanup is temporarily unavailable, the
        # losing request still fails closed and retention removes the unpublished row.
        return


def _telegram_session(request: Request, payload: TelegramSessionRequest) -> TelegramSessionResponse:
    if payload.telegram_user.is_bot:
        raise HTTPException(status_code=403, detail="Telegram bots cannot become users")
    if payload.chat.type != "private" or payload.chat.id != payload.telegram_user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the Telegram sender's private chat may create a session",
        )
    service = _authorization(request)
    service.prune_ephemeral_security_state()
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    bot_id = payload.bot_id
    realm_id = _canonical_telegram_realm_id(bot_id)
    if payload.realm_id != realm_id:
        raise HTTPException(
            status_code=409,
            detail="Telegram realm is not canonical for the authenticated bot identity",
        )
    _enforce_telegram_ingress_limit(
        service,
        realm_id=realm_id,
        telegram_user_id=payload.telegram_user.id,
    )
    now = _now()
    lease_token = _new_id("tglease")
    stale_processing_before = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    with service.storage.transaction(immediate=True) as conn:
        _bind_telegram_realm(conn, realm_id=realm_id, bot_id=bot_id, now=now)
        existing = conn.execute(
            """
            SELECT payload_sha256, status, attempt_count, updated_at
            FROM telegram_updates WHERE realm_id = ? AND update_id = ?
            """,
            (realm_id, payload.update_id),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise HTTPException(status_code=409, detail="Telegram update replay mismatch")
            attempts = int(existing["attempt_count"] or 1)
            # A matching completed registration may be retried after a bridge crash
            # between session issuance and the durable agent turn. Reissuing a scoped
            # session is bounded; the chat layer's exact-request effect ledger prevents
            # duplicate mutations when the prior turn actually completed.
            retryable = existing["status"] in {"failed", "completed"} or (
                existing["status"] == "processing"
                and str(existing["updated_at"]) < stale_processing_before
            )
            # A completed registration is only an identity/session handshake. The
            # durable chat request id is enforced separately by the agent effect
            # ledger, so an exact completed replay must remain available across a
            # model/container outage of arbitrary length. Failed or abandoned
            # registration attempts keep their poison-message bound.
            retry_budget_exhausted = (
                attempts >= 3 and existing["status"] != "completed"
            )
            if not retryable or retry_budget_exhausted:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Telegram update retry budget exhausted"
                        if retry_budget_exhausted
                        else "Telegram update was already processed"
                    ),
                )
            claimed = conn.execute(
                """
                UPDATE telegram_updates
                SET status = 'processing',
                    attempt_count = CASE
                        WHEN status = 'completed' THEN attempt_count
                        ELSE attempt_count + 1
                    END,
                    lease_token = ?, last_error = NULL, updated_at = ?
                WHERE realm_id = ? AND update_id = ?
                  AND payload_sha256 = ? AND status = ? AND attempt_count = ?
                """,
                (
                    lease_token,
                    now,
                    realm_id,
                    payload.update_id,
                    payload_sha256,
                    str(existing["status"]),
                    attempts,
                ),
            )
            if claimed.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Telegram update processing lease changed during claim",
                )
        else:
            conn.execute(
                """
                INSERT INTO telegram_updates(
                    realm_id, update_id, payload_sha256, status, attempt_count,
                    lease_token, received_at, updated_at
                ) VALUES (?, ?, ?, 'processing', 1, ?, ?, ?)
                """,
                (
                    realm_id,
                    payload.update_id,
                    payload_sha256,
                    lease_token,
                    now,
                    now,
                ),
            )
    created_session_id: str | None = None
    try:
        identity = service.upsert_external_identity(
            provider="telegram",
            realm_id=realm_id,
            provider_subject_id=payload.telegram_user.id,
            username=payload.telegram_user.username,
            first_name=payload.telegram_user.first_name,
            last_name=payload.telegram_user.last_name,
            locale=payload.telegram_user.language_code,
        )
        raw_existing_session = str(
            request.headers.get("x-jarvis-user-session") or ""
        ).strip()
        session: dict[str, Any] | None = None
        if raw_existing_session and len(raw_existing_session) <= 1024:
            digest = hashlib.sha256(raw_existing_session.encode("utf-8")).hexdigest()
            with service.storage.locked_connection() as conn:
                reusable = conn.execute(
                    """
                    SELECT s.id, s.expires_at
                    FROM user_sessions s
                    JOIN users u ON u.id = s.user_id AND u.status = 'active'
                    WHERE s.token_sha256 = ? AND s.user_id = ? AND s.identity_id = ?
                      AND s.revoked_at IS NULL AND s.expires_at > ?
                    """,
                    (
                        digest,
                        identity["user_id"],
                        identity["identity_id"],
                        _now(),
                    ),
                ).fetchone()
            if reusable is not None:
                session = {
                    "session_token": raw_existing_session,
                    "session_id": str(reusable["id"]),
                    "expires_at": str(reusable["expires_at"]),
                }
        if session is None:
            session = service.create_user_session(
                user_id=str(identity["user_id"]),
                identity_id=str(identity["identity_id"]),
                auth_method="telegram-bridge",
                ttl_seconds=_telegram_session_ttl(),
            )
            created_session_id = str(session["session_id"])
    except AuthorizationError as exc:
        finalized = _finalize_telegram_update_lease(
            service,
            realm_id=realm_id,
            update_id=payload.update_id,
            lease_token=lease_token,
            status="failed",
            last_error=type(exc).__name__,
        )
        if not finalized:
            raise HTTPException(
                status_code=409,
                detail="Telegram update processing lease was superseded",
            ) from exc
        raise HTTPException(status_code=403, detail="Telegram user is inactive") from exc
    except Exception as exc:
        finalized = _finalize_telegram_update_lease(
            service,
            realm_id=realm_id,
            update_id=payload.update_id,
            lease_token=lease_token,
            status="failed",
            last_error=type(exc).__name__,
        )
        if not finalized:
            raise HTTPException(
                status_code=409,
                detail="Telegram update processing lease was superseded",
            ) from exc
        raise
    finalized = _finalize_telegram_update_lease(
        service,
        realm_id=realm_id,
        update_id=payload.update_id,
        lease_token=lease_token,
        status="completed",
        user_id=str(identity["user_id"]),
    )
    if not finalized:
        _discard_unpublished_telegram_session(service, created_session_id)
        raise HTTPException(
            status_code=409,
            detail="Telegram update processing lease was superseded",
        )
    return TelegramSessionResponse(
        realm_id=realm_id,
        bot_id=bot_id,
        session_token=str(session["session_token"]),
        session_id=str(session["session_id"]),
        expires_at=str(session["expires_at"]),
        user={
            "id": str(identity["user_id"]),
            "identity_id": str(identity["identity_id"]),
            "status": str(identity["status"]),
            "preset_key": str(identity["preset_key"]),
            "created": bool(identity["created"]),
        },
    )


@router.post(
    "/api/integrations/telegram/session",
    response_model=TelegramSessionResponse,
    dependencies=[Depends(require_security_id("integration.telegram.session.create"))],
)
def create_telegram_session(
    request: Request, payload: TelegramSessionRequest
) -> TelegramSessionResponse:
    return _telegram_session(request, payload)


@router.post(
    "/api/integrations/telegram/register-session",
    response_model=TelegramSessionResponse,
    include_in_schema=False,
    dependencies=[Depends(require_security_id("integration.telegram.session.create"))],
)
def create_telegram_session_legacy_alias(
    request: Request, payload: TelegramSessionRequest
) -> TelegramSessionResponse:
    return _telegram_session(request, payload)
