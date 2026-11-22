"""Operation ledger primitive for safe, idempotent future state mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from copy import deepcopy
from threading import RLock
from typing import Any
from uuid import uuid4


class OperationError(RuntimeError):
    pass


class OperationConflictError(OperationError):
    pass


class StaleStateError(OperationError):
    """Raised when an approved mutation no longer matches current state."""

    code = "STALE_STATE"


class ApprovalError(OperationError):
    """Base error for approval lifecycle violations."""

    code = "APPROVAL_ERROR"


class ApprovalNotFoundError(ApprovalError):
    code = "APPROVAL_NOT_FOUND"


class ApprovalExpiredError(ApprovalError):
    code = "APPROVAL_EXPIRED"


class ApprovalRequiredError(ApprovalError):
    code = "APPROVAL_REQUIRED"


@dataclass(slots=True)
class OperationRecord:
    operation_id: str
    idempotency_key: str
    action: str
    target_ids: tuple[str, ...]
    request_fingerprint: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "started"
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "action": self.action,
            "target_ids": list(self.target_ids),
            "request_fingerprint": self.request_fingerprint,
            "payload": deepcopy(self.payload),
            "status": self.status,
            "result": deepcopy(self.result),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ApprovalRecord:
    """Immutable-scope approval that can be replayed but not expanded."""

    approval_id: str
    operation_id: str
    idempotency_key: str
    action: str
    run_id: str
    requested_by: str
    target_ids: tuple[str, ...]
    payload: dict[str, Any]
    parameters_hash: str
    expires_at: str
    status: str = "requested"
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: str | None = None
    executed_at: str | None = None
    verified_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def create(
        cls,
        *,
        operation_id: str,
        idempotency_key: str,
        action: str,
        run_id: str,
        requested_by: str,
        target_ids: list[str] | tuple[str, ...],
        payload: dict[str, Any],
        parameters_hash: str,
        expires_at: str,
    ) -> "ApprovalRecord":
        return cls(
            approval_id=f"APR-{uuid4().hex}",
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            action=action,
            run_id=run_id,
            requested_by=requested_by,
            target_ids=tuple(sorted(target_ids)),
            payload=deepcopy(payload),
            parameters_hash=parameters_hash,
            expires_at=expires_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "action": self.action,
            "run_id": self.run_id,
            "requested_by": self.requested_by,
            "target_ids": list(self.target_ids),
            "payload": deepcopy(self.payload),
            "parameters_hash": self.parameters_hash,
            "expires_at": self.expires_at,
            "status": self.status,
            "decided_by": self.decided_by,
            "decision_reason": self.decision_reason,
            "decided_at": self.decided_at,
            "executed_at": self.executed_at,
            "verified_at": self.verified_at,
            "created_at": self.created_at,
        }


class OperationLedger:
    """Claim operations once and return the prior record for safe retries."""

    def __init__(self) -> None:
        self._records: dict[str, OperationRecord] = {}
        self._lock = RLock()

    @staticmethod
    def fingerprint(action: str, target_ids: list[str] | tuple[str, ...], payload: dict[str, Any]) -> str:
        canonical = repr((action, tuple(sorted(target_ids)), sorted(payload.items())))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def begin(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        action: str,
        target_ids: list[str] | tuple[str, ...],
        payload: dict[str, Any],
    ) -> tuple[OperationRecord, bool]:
        fingerprint = self.fingerprint(action, target_ids, payload)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise OperationConflictError(
                        f"idempotency key {idempotency_key} was already used for a different request"
                    )
                return existing, False
            record = OperationRecord(
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                action=action,
                target_ids=tuple(sorted(target_ids)),
                request_fingerprint=fingerprint,
                payload=deepcopy(payload),
            )
            self._records[idempotency_key] = record
            return record, True

    def complete(self, idempotency_key: str, result: dict[str, Any]) -> OperationRecord:
        with self._lock:
            record = self._records[idempotency_key]
            record.status = "completed"
            record.result = dict(result)
            record.updated_at = datetime.now(timezone.utc).isoformat()
            return record

    def fail(self, idempotency_key: str, error: str) -> OperationRecord:
        with self._lock:
            record = self._records[idempotency_key]
            record.status = "failed"
            record.error = error
            record.updated_at = datetime.now(timezone.utc).isoformat()
            return record

    def get(self, idempotency_key: str) -> OperationRecord | None:
        with self._lock:
            return self._records.get(idempotency_key)

    def list(self) -> list[OperationRecord]:
        with self._lock:
            return list(self._records.values())
