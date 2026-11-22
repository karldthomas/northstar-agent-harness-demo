"""Stable tool observation envelopes shared by the simulator and API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar
from uuid import uuid4

from .models import to_jsonable


T = TypeVar("T")
ToolStatus = Literal["succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolObservation(Generic[T]):
    """A bounded, typed observation returned by a simulator tool."""

    schema_version: str
    tool_name: str
    scenario_run_id: str
    request_id: str
    status: ToolStatus
    data: T | None
    error: ToolError | None
    read_only: bool
    authoritative: bool
    state_revision: int
    observed_at: str

    @classmethod
    def success(
        cls,
        *,
        tool_name: str,
        scenario_run_id: str,
        data: T,
        state_revision: int,
        request_id: str | None = None,
    ) -> "ToolObservation[T]":
        return cls(
            schema_version="northstar.tool-observation.v1",
            tool_name=tool_name,
            scenario_run_id=scenario_run_id,
            request_id=request_id or f"req-{uuid4().hex}",
            status="succeeded",
            data=data,
            error=None,
            read_only=True,
            authoritative=True,
            state_revision=state_revision,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def failure(
        cls,
        *,
        tool_name: str,
        scenario_run_id: str,
        error: ToolError,
        state_revision: int = 0,
        request_id: str | None = None,
    ) -> "ToolObservation[None]":
        return cls(
            schema_version="northstar.tool-observation.v1",
            tool_name=tool_name,
            scenario_run_id=scenario_run_id,
            request_id=request_id or f"req-{uuid4().hex}",
            status="failed",
            data=None,
            error=error,
            read_only=True,
            authoritative=True,
            state_revision=state_revision,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool_name": self.tool_name,
            "scenario_run_id": self.scenario_run_id,
            "request_id": self.request_id,
            "status": self.status,
            "data": to_jsonable(self.data),
            "error": to_jsonable(self.error),
            "read_only": self.read_only,
            "authoritative": self.authoritative,
            "state_revision": self.state_revision,
            "observed_at": self.observed_at,
        }

