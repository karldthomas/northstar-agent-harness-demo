"""Persistence adapters for run-scoped simulator state.

The local simulator intentionally remains usable without AWS dependencies or
credentials.  The deployed Lambda sets ``NORTHSTAR_STATE_TABLE_NAME`` and
uses the existing DynamoDB state table so the browser/API invocation and the
AgentCore Gateway target share one authoritative run and event sequence.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any, Protocol

from .models import (
    AccessRequest,
    Application,
    Device,
    Employee,
    EnvironmentState,
    Incident,
    KnowledgeArticle,
    Service,
    SoftwareDeployment,
)
from .operations import ApprovalRecord, OperationLedger, OperationRecord


PERSISTENCE_SCHEMA = "northstar.scenario-run.v1"


class PersistenceConflictError(RuntimeError):
    """Raised when an optimistic-concurrency write loses a race."""


class RunPersistence(Protocol):
    """Minimal persistence contract consumed by ``SimulatorStore``."""

    def load(self, run_id: str) -> Any | None:
        """Load one run, or return ``None`` when it does not exist."""

    def save(self, run: Any) -> None:
        """Persist the current run snapshot or raise on a stale version."""

    def list_run_ids(self) -> list[str]:
        """Return persisted run IDs."""


def _native(value: Any) -> Any:
    """Convert DynamoDB ``Decimal`` values back to JSON-native numbers."""

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(key): _native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_native(item) for item in value]
    return value


def _state_from_dict(payload: dict[str, Any]) -> EnvironmentState:
    """Rebuild the typed environment model from its JSON/DynamoDB shape."""

    def mapping(model: Any, key: str) -> dict[str, Any]:
        return {
            str(item_id): model(**dict(item))
            for item_id, item in dict(payload.get(key) or {}).items()
        }

    return EnvironmentState(
        scenario_run_id=str(payload["scenario_run_id"]),
        scenario_id=str(payload["scenario_id"]),
        seed=int(payload["seed"]),
        revision=int(payload["revision"]),
        employees=mapping(Employee, "employees"),
        devices=mapping(Device, "devices"),
        applications=mapping(Application, "applications"),
        services=mapping(Service, "services"),
        deployments=mapping(SoftwareDeployment, "deployments"),
        incidents=mapping(Incident, "incidents"),
        access_requests=mapping(AccessRequest, "access_requests"),
        knowledge_articles=mapping(KnowledgeArticle, "knowledge_articles"),
        metadata=deepcopy(dict(payload.get("metadata") or {})),
    )


def serialize_run(run: Any) -> dict[str, Any]:
    """Serialize a ``ScenarioRun`` without relying on Python object storage."""

    return {
        "schema_version": PERSISTENCE_SCHEMA,
        "run_id": run.run_id,
        "scenario_id": run.scenario_id,
        "initial_state": run.initial_state.to_dict(),
        "state": run.state.to_dict(),
        "owner_actor_id": run.owner_actor_id,
        "tenant_id": run.tenant_id,
        "gateway_context_token": run.gateway_context_token,
        "persistence_version": getattr(run, "persistence_version", None),
        "evaluation_start_sequence": getattr(run, "evaluation_start_sequence", 0),
        "faults": run.faults.to_dict(),
        "events": deepcopy(run.events),
        "observations": deepcopy(run.observations),
        "operations": [record.to_dict() for record in run.operations.list()],
        "approvals": [approval.to_dict() for approval in run.approvals.values()],
        "checkpoints": deepcopy(run.checkpoints),
        "created_at": run.created_at,
    }


def deserialize_run(payload: dict[str, Any]) -> Any:
    """Rebuild a ``ScenarioRun`` from a persisted JSON-compatible object."""

    # Imported lazily to keep the store/persistence modules free of an import
    # cycle while still sharing the domain dataclasses.
    from .store import FaultProfile, ScenarioRun

    normalized = _native(payload)
    if normalized.get("schema_version") != PERSISTENCE_SCHEMA:
        raise ValueError("unsupported persisted scenario-run schema")

    operations = OperationLedger()
    for item in normalized.get("operations") or []:
        record = OperationRecord(
            operation_id=str(item["operation_id"]),
            idempotency_key=str(item["idempotency_key"]),
            action=str(item["action"]),
            target_ids=tuple(str(value) for value in item.get("target_ids") or []),
            request_fingerprint=str(item["request_fingerprint"]),
            payload=deepcopy(dict(item.get("payload") or {})),
            status=str(item.get("status") or "started"),
            result=deepcopy(item.get("result")),
            error=item.get("error"),
            created_at=str(item.get("created_at") or ""),
            updated_at=str(item.get("updated_at") or ""),
        )
        operations._records[record.idempotency_key] = record

    approvals: dict[str, ApprovalRecord] = {}
    for item in normalized.get("approvals") or []:
        approval = ApprovalRecord(
            approval_id=str(item["approval_id"]),
            operation_id=str(item["operation_id"]),
            idempotency_key=str(item["idempotency_key"]),
            action=str(item["action"]),
            run_id=str(item["run_id"]),
            requested_by=str(item["requested_by"]),
            target_ids=tuple(str(value) for value in item.get("target_ids") or []),
            payload=deepcopy(dict(item.get("payload") or {})),
            parameters_hash=str(item["parameters_hash"]),
            expires_at=str(item["expires_at"]),
            status=str(item.get("status") or "requested"),
            decided_by=item.get("decided_by"),
            decision_reason=item.get("decision_reason"),
            decided_at=item.get("decided_at"),
            executed_at=item.get("executed_at"),
            verified_at=item.get("verified_at"),
            created_at=str(item.get("created_at") or ""),
        )
        approvals[approval.approval_id] = approval

    return ScenarioRun(
        run_id=str(normalized["run_id"]),
        scenario_id=str(normalized["scenario_id"]),
        initial_state=_state_from_dict(dict(normalized["initial_state"])),
        state=_state_from_dict(dict(normalized["state"])),
        owner_actor_id=normalized.get("owner_actor_id"),
        tenant_id=str(normalized.get("tenant_id") or "demo"),
        gateway_context_token=normalized.get("gateway_context_token"),
        faults=FaultProfile(
            **{
                key: set(str(value) for value in values)
                for key, values in dict(normalized.get("faults") or {}).items()
            }
        ),
        events=deepcopy(list(normalized.get("events") or [])),
        observations=deepcopy(list(normalized.get("observations") or [])),
        operations=operations,
        approvals=approvals,
        checkpoints=deepcopy(dict(normalized.get("checkpoints") or {})),
        evaluation_start_sequence=int(normalized.get("evaluation_start_sequence") or 0),
        created_at=str(normalized.get("created_at") or ""),
    )


class DynamoDBRunRepository:
    """DynamoDB-backed repository using the pre-provisioned state table."""

    def __init__(self, table_name: str, *, region: str | None = None, table: Any | None = None) -> None:
        self.table_name = table_name.strip()
        if not self.table_name:
            raise ValueError("DynamoDB table name is required")
        if table is not None:
            self.table = table
            return
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("boto3 is required for DynamoDB-backed simulator state") from exc
        self.table = boto3.resource("dynamodb", region_name=region).Table(self.table_name)

    @staticmethod
    def _key(run_id: str) -> dict[str, str]:
        return {"scenario_run_id": run_id, "record_key": "run"}

    def load(self, run_id: str) -> Any | None:
        response = self.table.get_item(Key=self._key(run_id), ConsistentRead=True)
        item = response.get("Item") or {}
        payload = item.get("payload")
        if not isinstance(payload, dict):
            return None
        run = deserialize_run(dict(payload))
        # The top-level version is deliberately separate from the payload so
        # the conditional write remains small and easy to inspect in DynamoDB.
        run.persistence_version = int(item.get("persistence_version", 0))
        return run

    def save(self, run: Any) -> None:
        expected_version = getattr(run, "persistence_version", None)
        next_version = 1 if expected_version is None else int(expected_version) + 1
        payload = serialize_run(run)
        payload["persistence_version"] = next_version
        item = {
            **self._key(str(run.run_id)),
            "schema_version": PERSISTENCE_SCHEMA,
            "persistence_version": next_version,
            "payload": payload,
        }
        if expected_version is None:
            condition = "attribute_not_exists(#version)"
            values: dict[str, Any] = {}
        else:
            condition = "attribute_not_exists(#version) OR #version = :expected"
            values = {":expected": int(expected_version)}
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression=condition,
                ExpressionAttributeNames={"#version": "persistence_version"},
                **({"ExpressionAttributeValues": values} if values else {}),
            )
        except Exception as exc:
            response = getattr(exc, "response", {})
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            if error.get("Code") == "ConditionalCheckFailedException":
                raise PersistenceConflictError(
                    f"scenario run {run.run_id} has a newer persisted version"
                ) from exc
            raise
        run.persistence_version = next_version

    def list_run_ids(self) -> list[str]:
        run_ids: list[str] = []
        request: dict[str, Any] = {"ProjectionExpression": "scenario_run_id, record_key"}
        while True:
            response = self.table.scan(**request)
            for item in response.get("Items") or []:
                if item.get("record_key") == "run" and item.get("scenario_run_id"):
                    run_ids.append(str(item["scenario_run_id"]))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            request["ExclusiveStartKey"] = last_key
        return sorted(set(run_ids))
