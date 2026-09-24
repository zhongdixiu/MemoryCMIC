from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reject_explicit_null(self):
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} must be omitted instead of null")
        return self


class ToolCall(StrictRequestModel):
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def validate_arguments_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _json_size(value) > 8 * 1024:
            raise ValueError("tool call arguments exceed 8 KiB")
        return value


class AttachmentRef(StrictRequestModel):
    file_id: str = Field(min_length=1, max_length=256)
    mime_type: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=256)
    sha256: str | None = None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class MessageMetadata(StrictRequestModel):
    channel: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp_origin: Literal["source", "adapter_observed"] = "source"
    tool_name: str | None = Field(default=None, min_length=1, max_length=128)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    tool_status: Literal["succeeded", "failed", "unknown"] | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=20)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_sizes(self):
        if _json_size(self.extensions) > 8 * 1024:
            raise ValueError("metadata.extensions exceeds 8 KiB")
        if _json_size(self.model_dump(exclude_none=True)) > 16 * 1024:
            raise ValueError("metadata exceeds 16 KiB")
        return self


class Message(StrictRequestModel):
    message_id: str = Field(min_length=1, max_length=256)
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    occurred_at: datetime
    author_id: str | None = Field(default=None, min_length=1, max_length=128)
    turn_id: str | None = Field(default=None, min_length=1, max_length=128)
    reply_to_message_id: str | None = Field(default=None, min_length=1, max_length=256)
    metadata: MessageMetadata = Field(default_factory=MessageMetadata)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value:
            raise ValueError("content must not be empty")
        if len(value.encode()) > 64 * 1024:
            raise ValueError("content exceeds 64 KiB")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_role_metadata(self):
        tool_fields = (
            self.metadata.tool_name,
            self.metadata.tool_call_id,
            self.metadata.tool_status,
        )
        if self.role == "tool":
            if any(value is None for value in tool_fields):
                raise ValueError("tool messages require tool_name, tool_call_id and tool_status")
        elif any(value is not None for value in tool_fields):
            raise ValueError("tool metadata is only allowed for tool messages")
        if self.role != "assistant" and self.metadata.tool_calls:
            raise ValueError("tool_calls is only allowed for assistant messages")
        return self


class AddRequest(StrictRequestModel):
    source_system: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    messages: list[Message] = Field(min_length=1, max_length=100)
    history_messages: list[Message] = Field(default_factory=list, max_length=100)
    wait_for_result: bool = False
    wait_timeout_ms: int | None = Field(default=None, ge=1, le=30_000)

    @model_validator(mode="after")
    def validate_request(self):
        if not self.wait_for_result and self.wait_timeout_ms is not None:
            raise ValueError("wait_timeout_ms requires wait_for_result=true")
        ids = [message.message_id for message in [*self.history_messages, *self.messages]]
        if len(ids) != len(set(ids)):
            raise ValueError("message_id must be unique within a request")
        for message in [*self.history_messages, *self.messages]:
            if message.role == "user" and message.author_id not in {None, self.user_id}:
                raise ValueError("user message author_id must equal user_id")
        if self.history_messages:
            latest_history = max(message.occurred_at for message in self.history_messages)
            earliest_target = min(message.occurred_at for message in self.messages)
            if latest_history > earliest_target:
                raise ValueError("history_messages must not contain future messages")
        return self


class TaskStatus(StrEnum):
    pending = "pending"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class MemoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    memory: str
    event: Literal["ADD"] = "ADD"


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: TaskStatus
    results: list[MemoryResult] | None
    error: ErrorInfo | None


class ErrorBody(BaseModel):
    error: ErrorInfo
