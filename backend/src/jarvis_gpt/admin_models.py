from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class UserStatusUpdateRequest(BaseModel):
    status: Literal["active", "suspended", "deleted"]
    reason: str = Field(min_length=1, max_length=500)


class UserPresetAssignmentRequest(BaseModel):
    preset_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_-]*$")
    reason: str = Field(min_length=1, max_length=500)


class UserPermissionUpdateRequest(BaseModel):
    effect: Literal["grant", "deny"] = "grant"
    can_delegate: bool = False
    reason: str = Field(min_length=1, max_length=500)
    valid_until: datetime | None = None

    @field_validator("valid_until")
    @classmethod
    def valid_until_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("valid_until must include a timezone")
        return value


class PermissionPresetCreateRequest(BaseModel):
    key: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    security_ids: list[str] = Field(default_factory=list, max_length=1000)


class PermissionPresetUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    security_ids: list[str] = Field(default_factory=list, max_length=1000)


class TelegramUserPayload(BaseModel):
    id: int = Field(gt=0)
    is_bot: bool = False
    username: str | None = Field(default=None, max_length=160)
    first_name: str | None = Field(default=None, max_length=160)
    last_name: str | None = Field(default=None, max_length=160)
    language_code: str | None = Field(default=None, max_length=32)


class TelegramChatPayload(BaseModel):
    id: int
    type: str = Field(min_length=1, max_length=32)


class TelegramSessionRequest(BaseModel):
    realm_id: str = Field(min_length=1, max_length=120)
    bot_id: int = Field(gt=0)
    update_id: int = Field(ge=0)
    telegram_user: TelegramUserPayload
    chat: TelegramChatPayload


class TelegramSessionResponse(BaseModel):
    realm_id: str
    bot_id: int
    session_token: str
    session_id: str
    expires_at: str
    user: dict[str, object]
