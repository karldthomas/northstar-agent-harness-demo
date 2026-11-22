"""Thread-safe run isolation, reset, and controlled fault injection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections import Counter
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from .fixtures import DEFAULT_SEED, build_environment, load_scenarios
from .models import EnvironmentState, to_jsonable
from .operations import (
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalRecord,
    ApprovalRequiredError,
    OperationConflictError,
    OperationLedger,
    StaleStateError,
)
from .persistence import RunPersistence


class SimulatorError(RuntimeError):
    """Base error for invalid simulator operations."""

    code = "SIMULATOR_ERROR"


class RunNotFoundError(SimulatorError):
    code = "RUN_NOT_FOUND"


class EntityNotFoundError(SimulatorError):
    code = "ENTITY_NOT_FOUND"


class RunAccessDeniedError(SimulatorError):
    code = "RUN_ACCESS_DENIED"


class TransientMemoryError(SimulatorError):
    code = "MEMORY_UNAVAILABLE"


class TransientSimulatorError(SimulatorError):
    code = "SIMULATOR_UNAVAILABLE"


class InvalidFaultError(SimulatorError):
    code = "INVALID_FAULT"


def _status_counts(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def summarize_state(state: EnvironmentState) -> dict[str, Any]:
    """Return a compact read model suitable for the web console."""

    return {
        "scenario_run_id": state.scenario_run_id,
        "scenario_id": state.scenario_id,
        "seed": state.seed,
        "revision": state.revision,
        "entity_counts": {
            "employees": len(state.employees),
            "devices": len(state.devices),
            "applications": len(state.applications),
            "services": len(state.services),
            "deployments": len(state.deployments),
            "incidents": len(state.incidents),
            "access_requests": len(state.access_requests),
            "knowledge_articles": len(state.knowledge_articles),
        },
        "device_status_counts": _status_counts(device.status for device in state.devices.values()),
        "vpn_status_counts": _status_counts(device.vpn_status for device in state.devices.values()),
        "service_status_counts": _status_counts(service.status for service in state.services.values()),
        "incident_status_counts": _status_counts(incident.status for incident in state.incidents.values()),
        "deployment_status_counts": _status_counts(deployment.status for deployment in state.deployments.values()),
        "access_request_status_counts": _status_counts(
            request.status for request in state.access_requests.values()
        ),
        "open_incident_ids": sorted(
            incident.incident_id for incident in state.incidents.values() if incident.status == "open"
        ),
    }


def _changed_entities(
    before: dict[str, Any],
    after: dict[str, Any],
    entity_type: str,
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for entity_id in sorted(set(before) | set(after)):
        left = before.get(entity_id)
        right = after.get(entity_id)
        if left is None or right is None:
            changes.append({"entity_id": entity_id, "before": left, "after": right})
            continue
        changed = {
            field: {"before": left.get(field), "after": right.get(field)}
            for field in fields
            if left.get(field) != right.get(field)
        }
        if changed:
            changes.append({"entity_id": entity_id, "changes": changed})
    return changes


def compare_states(before: EnvironmentState, after: EnvironmentState) -> dict[str, Any]:
    """Compare authoritative state without relying on agent-reported text."""

    before_dict = before.to_dict()
    after_dict = after.to_dict()
    return {
        "run_id": after.scenario_run_id,
        "scenario_id": after.scenario_id,
        "before": summarize_state(before),
        "after": summarize_state(after),
        "changed": {
            "devices": _changed_entities(
                before_dict["devices"],
                after_dict["devices"],
                "device",
                ("status", "vpn_status", "installed_software", "service_ids"),
            ),
            "services": _changed_entities(
                before_dict["services"],
                after_dict["services"],
                "service",
                ("status", "version", "dependency_ids"),
            ),
            "incidents": _changed_entities(
                before_dict["incidents"],
                after_dict["incidents"],
                "incident",
                ("status", "severity", "affected_device_ids", "related_deployment_id"),
            ),
            "deployments": _changed_entities(
                before_dict["deployments"],
                after_dict["deployments"],
                "deployment",
                ("status", "version", "target_device_ids"),
            ),
            "access_requests": _changed_entities(
                before_dict["access_requests"],
                after_dict["access_requests"],
                "access_request",
                ("status", "requires_approval"),
            ),
        },
    }


@dataclass(slots=True)
class FaultProfile:
    unavailable_services: set[str] = field(default_factory=set)
    unavailable_tools: set[str] = field(default_factory=set)
    unavailable_operations: set[str] = field(default_factory=set)
    diagnostic_conflicts: set[str] = field(default_factory=set)
    service_health_conflicts: set[str] = field(default_factory=set)
    timeout_operations: set[str] = field(default_factory=set)
    timeout_after_commit_operations: set[str] = field(default_factory=set)
    stale_state_targets: set[str] = field(default_factory=set)
    memory_unavailable_actors: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "unavailable_services": sorted(self.unavailable_services),
            "unavailable_tools": sorted(self.unavailable_tools),
            "unavailable_operations": sorted(self.unavailable_operations),
            "diagnostic_conflicts": sorted(self.diagnostic_conflicts),
            "service_health_conflicts": sorted(self.service_health_conflicts),
            "timeout_operations": sorted(self.timeout_operations),
            "timeout_after_commit_operations": sorted(self.timeout_after_commit_operations),
            "stale_state_targets": sorted(self.stale_state_targets),
            "memory_unavailable_actors": sorted(self.memory_unavailable_actors),
        }


@dataclass(slots=True)
class ScenarioRun:
    run_id: str
    scenario_id: str
    initial_state: EnvironmentState
    state: EnvironmentState
    owner_actor_id: str | None = None
    tenant_id: str = "demo"
    faults: FaultProfile = field(default_factory=FaultProfile)
    events: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    operations: OperationLedger = field(default_factory=OperationLedger)
    approvals: dict[str, ApprovalRecord] = field(default_factory=dict)
    checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "owner_actor_id": self.owner_actor_id,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "summary": summarize_state(self.state),
            "baseline_summary": summarize_state(self.initial_state),
            "state": self.state.to_dict(),
            "faults": self.faults.to_dict(),
            "events": deepcopy(self.events),
            "observation_count": len(self.observations),
            "operations": [record.to_dict() for record in self.operations.list()],
            "approvals": [approval.to_dict() for approval in self.approvals.values()],
        }

    def summary(self) -> dict[str, Any]:
        return {
            **summarize_state(self.state),
            "owner_actor_id": self.owner_actor_id,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "faults": self.faults.to_dict(),
            "event_count": len(self.events),
            "observation_count": len(self.observations),
            "operation_status_counts": _status_counts(record.status for record in self.operations.list()),
            "approval_status_counts": _status_counts(approval.status for approval in self.approvals.values()),
        }

    def comparison(self) -> dict[str, Any]:
        return compare_states(self.initial_state, self.state)


class SimulatorStore:
    """Authoritative run store with an optional durable repository.

    Local development and unit tests use the in-memory default.  The deployed
    Lambda supplies a DynamoDB repository so Function URL and Gateway-target
    invocations can share browser-created runs and their event sequences.
    """

    VALID_FAULTS = {
        "service_unavailable",
        "diagnostic_conflict",
        "conflicting_evidence",
        "timeout",
        "timeout_after_commit",
        "api_unavailable",
        "stale_state",
        "memory_unavailable",
    }
    VALID_ACTIONS = {"rollback_vpn_deployment", "restart_service", "update_incident"}

    def __init__(self, *, persistence: RunPersistence | None = None) -> None:
        self._runs: dict[str, ScenarioRun] = {}
        self._lock = RLock()
        self._persistence = persistence

    def _load_persisted(self, run_id: str) -> ScenarioRun | None:
        if self._persistence is None:
            return None
        loaded = self._persistence.load(run_id)
        if loaded is not None:
            self._runs[run_id] = loaded
        return loaded

    def create_run(
        self,
        scenario_id: str,
        *,
        run_id: str | None = None,
        seed: int = DEFAULT_SEED,
        actor_id: str | None = None,
        tenant_id: str = "demo",
    ) -> ScenarioRun:
        load_scenarios()[scenario_id]
        resolved_run_id = run_id or f"run-{uuid4().hex}"
        with self._lock:
            if resolved_run_id in self._runs or self._load_persisted(resolved_run_id) is not None:
                raise SimulatorError(f"scenario run already exists: {resolved_run_id}")
            state = build_environment(scenario_id, scenario_run_id=resolved_run_id, seed=seed)
            run = ScenarioRun(
                run_id=resolved_run_id,
                scenario_id=scenario_id,
                initial_state=deepcopy(state),
                state=state,
                owner_actor_id=str(actor_id).strip() if actor_id else None,
                tenant_id=str(tenant_id or "demo").strip() or "demo",
            )
            self._runs[resolved_run_id] = run
            self._record_event(run, "scenario_created", {"seed": seed})
            return run

    def get_run(self, run_id: str) -> ScenarioRun:
        with self._lock:
            self._load_persisted(run_id)
            try:
                return self._runs[run_id]
            except KeyError as exc:
                raise RunNotFoundError(f"scenario run not found: {run_id}") from exc

    def list_runs(
        self,
        *,
        actor_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self._persistence is not None:
                for run_id in self._persistence.list_run_ids():
                    self._load_persisted(run_id)
            return [
                self._runs[run_id].summary()
                for run_id in sorted(self._runs)
                if self._can_access(self._runs[run_id], actor_id=actor_id, tenant_id=tenant_id)
            ]

    @staticmethod
    def _can_access(run: ScenarioRun, *, actor_id: str | None, tenant_id: str | None) -> bool:
        if tenant_id and run.tenant_id != tenant_id:
            return False
        # A run without an owner is the intentionally shared deterministic demo
        # fixture. New browser-created runs always carry an owner.
        return run.owner_actor_id is None or not actor_id or run.owner_actor_id == actor_id

    def get_run_for_actor(
        self,
        run_id: str,
        *,
        actor_id: str | None,
        tenant_id: str = "demo",
    ) -> ScenarioRun:
        with self._lock:
            run = self.get_run(run_id)
            if not self._can_access(run, actor_id=actor_id, tenant_id=tenant_id):
                raise RunAccessDeniedError(f"actor is not authorized to access scenario run {run_id}")
            return run

    def snapshot(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self.get_run(run_id).snapshot()

    def summary(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self.get_run(run_id).summary()

    def comparison(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self.get_run(run_id).comparison()

    def events_since(self, run_id: str, *, after_sequence: int = 0, limit: int = 100) -> dict[str, Any]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            run = self.get_run(run_id)
            events = [
                deepcopy(event)
                for event in run.events
                if int(event.get("sequence", 0)) > after_sequence
            ]
            page = events[:limit]
            next_sequence = int(page[-1]["sequence"]) if page else after_sequence
            return {
                "run_id": run_id,
                "events": page,
                "after_sequence": after_sequence,
                "next_sequence": next_sequence,
                "has_more": len(events) > len(page),
                "latest_sequence": len(run.events),
            }

    def entity(self, run_id: str, entity_type: str, entity_id: str) -> dict[str, Any]:
        collections = {
            "employee": "employees",
            "device": "devices",
            "application": "applications",
            "service": "services",
            "deployment": "deployments",
            "incident": "incidents",
            "access_request": "access_requests",
            "knowledge_article": "knowledge_articles",
        }
        collection_name = collections.get(entity_type)
        if collection_name is None:
            raise EntityNotFoundError(f"unsupported entity type: {entity_type}")
        with self._lock:
            run = self.get_run(run_id)
            collection = getattr(run.state, collection_name)
            try:
                entity = collection[entity_id]
            except KeyError as exc:
                raise EntityNotFoundError(f"{entity_type} not found: {entity_id}") from exc
            return {
                "run_id": run_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "state_revision": run.state.revision,
                "data": to_jsonable(entity),
            }

    def topology(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self.get_run(run_id)
            return {
                "run_id": run_id,
                "state_revision": run.state.revision,
                "services": [
                    {
                        "service_id": service.service_id,
                        "name": service.name,
                        "status": service.status,
                        "criticality": service.criticality,
                        "dependency_ids": list(service.dependency_ids),
                    }
                    for service in sorted(run.state.services.values(), key=lambda item: item.service_id)
                ],
            }

    def reset_run(self, run_id: str) -> ScenarioRun:
        with self._lock:
            run = self.get_run(run_id)
            run.state = deepcopy(run.initial_state)
            run.faults = FaultProfile()
            run.observations.clear()
            run.operations = OperationLedger()
            run.approvals.clear()
            run.checkpoints.clear()
            self._record_event(run, "scenario_reset", {"revision": run.state.revision})
            return run

    def inject_fault(self, run_id: str, *, fault_type: str, target_id: str) -> ScenarioRun:
        if fault_type not in self.VALID_FAULTS:
            raise InvalidFaultError(
                f"unsupported fault type {fault_type}; choose from {', '.join(sorted(self.VALID_FAULTS))}"
            )
        with self._lock:
            run = self.get_run(run_id)
            if fault_type == "service_unavailable":
                if target_id not in run.state.services:
                    raise EntityNotFoundError(f"service not found: {target_id}")
                run.faults.unavailable_services.add(target_id)
            elif fault_type == "diagnostic_conflict":
                if target_id not in run.state.devices:
                    raise EntityNotFoundError(f"device not found: {target_id}")
                run.faults.diagnostic_conflicts.add(target_id)
            elif fault_type == "conflicting_evidence":
                if target_id in run.state.devices:
                    run.faults.diagnostic_conflicts.add(target_id)
                elif target_id in run.state.services:
                    run.faults.service_health_conflicts.add(target_id)
                else:
                    raise EntityNotFoundError(f"device or service not found: {target_id}")
            elif fault_type == "timeout":
                run.faults.timeout_operations.add(target_id)
            elif fault_type == "timeout_after_commit":
                run.faults.timeout_after_commit_operations.add(target_id)
            elif fault_type == "api_unavailable":
                if target_id.startswith("tool:"):
                    run.faults.unavailable_tools.add(target_id.removeprefix("tool:"))
                else:
                    run.faults.unavailable_operations.add(target_id)
            elif fault_type == "stale_state":
                run.faults.stale_state_targets.add(target_id)
            elif fault_type == "memory_unavailable":
                run.faults.memory_unavailable_actors.add(target_id)
            self._record_event(run, "fault_injected", {"fault_type": fault_type, "target_id": target_id})
            return run

    def clear_fault(self, run_id: str, *, fault_type: str, target_id: str) -> ScenarioRun:
        """Clear one deterministic fault so a safe retry can be exercised."""

        if fault_type not in self.VALID_FAULTS:
            raise InvalidFaultError(f"unsupported fault type {fault_type}")
        with self._lock:
            run = self.get_run(run_id)
            values: set[str]
            if fault_type == "service_unavailable":
                values = run.faults.unavailable_services
            elif fault_type == "diagnostic_conflict":
                values = run.faults.diagnostic_conflicts
            elif fault_type == "conflicting_evidence":
                values = run.faults.diagnostic_conflicts | run.faults.service_health_conflicts
                run.faults.diagnostic_conflicts.discard(target_id)
                run.faults.service_health_conflicts.discard(target_id)
                values = set()
            elif fault_type == "timeout":
                values = run.faults.timeout_operations
            elif fault_type == "timeout_after_commit":
                values = run.faults.timeout_after_commit_operations
            elif fault_type == "api_unavailable":
                values = run.faults.unavailable_tools if target_id.startswith("tool:") else run.faults.unavailable_operations
                target_id = target_id.removeprefix("tool:")
            elif fault_type == "stale_state":
                values = run.faults.stale_state_targets
            else:
                values = run.faults.memory_unavailable_actors
            values.discard(target_id)
            self._record_event(run, "fault_cleared", {"fault_type": fault_type, "target_id": target_id})
            return run

    @staticmethod
    def _checkpoint_key(*, actor_id: str, tenant_id: str) -> str:
        return f"{tenant_id}:{actor_id}"

    def save_checkpoint(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "demo",
        objective: str,
        completed_steps: list[str] | None = None,
        pending_tasks: list[str] | None = None,
        unresolved_target_ids: list[str] | None = None,
        verified_outcomes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a compact, actor-scoped continuity checkpoint."""

        if not actor_id.strip():
            raise ValueError("actor_id is required")
        if not objective.strip():
            raise ValueError("objective is required")
        with self._lock:
            run = self.get_run_for_actor(run_id, actor_id=actor_id, tenant_id=tenant_id)
            key = self._checkpoint_key(actor_id=actor_id, tenant_id=tenant_id)
            checkpoint = {
                "schema_version": "northstar.checkpoint.v1",
                "run_id": run_id,
                "scenario_id": run.scenario_id,
                "actor_id": actor_id,
                "tenant_id": tenant_id,
                "state_revision": run.state.revision,
                "objective": objective.strip()[:2000],
                "constraints": {
                    "approval_required_for_mutation": True,
                    "managed_harness_tools_read_only": True,
                    "authoritative_state_source": "simulator",
                },
                "completed_steps": sorted({str(item).strip() for item in (completed_steps or []) if str(item).strip()}),
                "pending_tasks": sorted({str(item).strip() for item in (pending_tasks or []) if str(item).strip()}),
                "unresolved_target_ids": sorted({str(item).strip() for item in (unresolved_target_ids or []) if str(item).strip()}),
                "verified_outcomes": sorted({str(item).strip() for item in (verified_outcomes or []) if str(item).strip()}),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            run.checkpoints[key] = checkpoint
            self._record_event(
                run,
                "checkpoint_saved",
                {
                    "actor_id": actor_id,
                    "tenant_id": tenant_id,
                    "state_revision": run.state.revision,
                    "pending_task_count": len(checkpoint["pending_tasks"]),
                },
            )
            return deepcopy(checkpoint)

    def rehydrate_context(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "demo",
        allow_authoritative_fallback: bool = True,
    ) -> dict[str, Any]:
        """Rehydrate actor memory, or fall back to authoritative simulator state."""

        with self._lock:
            run = self.get_run_for_actor(run_id, actor_id=actor_id, tenant_id=tenant_id)
            if actor_id in run.faults.memory_unavailable_actors or "*" in run.faults.memory_unavailable_actors:
                self._record_event(run, "memory_unavailable", {"actor_id": actor_id, "tenant_id": tenant_id})
                if not allow_authoritative_fallback:
                    raise TransientMemoryError("synthetic AgentCore Memory is unavailable")
                return {
                    "memory_status": "unavailable",
                    "recovery": "authoritative_state_fallback",
                    "checkpoint": None,
                    "authoritative": {
                        "run_id": run_id,
                        "scenario_id": run.scenario_id,
                        "state_revision": run.state.revision,
                        "summary": summarize_state(run.state),
                    },
                }
            key = self._checkpoint_key(actor_id=actor_id, tenant_id=tenant_id)
            return {
                "memory_status": "available" if key in run.checkpoints else "empty",
                "recovery": "checkpoint_and_authoritative_state",
                "checkpoint": deepcopy(run.checkpoints.get(key)),
                "authoritative": {
                    "run_id": run_id,
                    "scenario_id": run.scenario_id,
                    "state_revision": run.state.revision,
                    "summary": summarize_state(run.state),
                },
            }

    def compact_checkpoint(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "demo",
    ) -> dict[str, Any]:
        """Simulate context compaction while retaining safety-critical fields."""

        with self._lock:
            run = self.get_run_for_actor(run_id, actor_id=actor_id, tenant_id=tenant_id)
            key = self._checkpoint_key(actor_id=actor_id, tenant_id=tenant_id)
            existing = run.checkpoints.get(key)
            if existing is None:
                raise SimulatorError("no checkpoint exists for this actor")
            compacted = {
                "schema_version": existing["schema_version"],
                "run_id": existing["run_id"],
                "scenario_id": existing["scenario_id"],
                "actor_id": existing["actor_id"],
                "tenant_id": existing["tenant_id"],
                "state_revision": existing["state_revision"],
                "objective": existing["objective"],
                "constraints": deepcopy(existing["constraints"]),
                "completed_steps": list(existing["completed_steps"]),
                "pending_tasks": list(existing["pending_tasks"]),
                "unresolved_target_ids": list(existing["unresolved_target_ids"]),
                "verified_outcomes": list(existing["verified_outcomes"]),
                "compacted": True,
                "compacted_at": datetime.now(timezone.utc).isoformat(),
            }
            run.checkpoints[key] = compacted
            self._record_event(
                run,
                "context_compacted",
                {
                    "actor_id": actor_id,
                    "tenant_id": tenant_id,
                    "preserved_fields": [
                        "objective",
                        "run_id",
                        "constraints",
                        "pending_tasks",
                        "unresolved_target_ids",
                        "verified_outcomes",
                    ],
                },
            )
            return deepcopy(compacted)

    def record_observation(self, run_id: str, observation: dict[str, Any]) -> None:
        with self._lock:
            run = self.get_run(run_id)
            recorded = deepcopy(observation)
            run.observations.append(recorded)
            self._record_event(
                run,
                "observation_recorded",
                {
                    "tool_name": recorded.get("tool_name"),
                    "request_id": recorded.get("request_id"),
                    "status": recorded.get("status"),
                    "read_only": recorded.get("read_only"),
                    "authoritative": recorded.get("authoritative"),
                    "state_revision": recorded.get("state_revision", run.state.revision),
                },
            )

    def record_harness_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Append a normalized managed-Harness lifecycle event to the run."""

        with self._lock:
            run = self.get_run(run_id)
            enriched = deepcopy(payload)
            if event_type == "agent_turn_started":
                enriched.setdefault(
                    "turn_index",
                    1
                    + sum(
                        1
                        for event in run.events
                        if event.get("event_type") == "agent_turn_started"
                    ),
                )
            self._record_event(run, event_type, enriched)

    def record_mutation(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            run = self.get_run(run_id)
            run.state.revision += 1
            self._record_event(run, event_type, payload)

    def _validate_operation_request(
        self,
        run: ScenarioRun,
        *,
        action: str,
        target_ids: list[str] | tuple[str, ...],
        payload: dict[str, Any],
    ) -> tuple[str, list[str], dict[str, Any]]:
        normalized_action = str(action or "").strip()
        if normalized_action not in self.VALID_ACTIONS:
            raise OperationConflictError(
                f"unsupported action {normalized_action}; choose from {', '.join(sorted(self.VALID_ACTIONS))}"
            )
        normalized_targets = [str(target_id).strip() for target_id in target_ids if str(target_id).strip()]
        if not normalized_targets:
            raise OperationConflictError("target_ids must contain at least one target")
        if len(normalized_targets) != len(set(normalized_targets)):
            raise OperationConflictError("target_ids must be unique")
        normalized_payload = deepcopy(payload)

        if normalized_action == "rollback_vpn_deployment":
            deployment_id = str(normalized_payload.get("deployment_id") or "DEP-0001").strip()
            deployment = run.state.deployments.get(deployment_id)
            if deployment is None:
                raise EntityNotFoundError(f"deployment not found: {deployment_id}")
            invalid_targets = sorted(set(normalized_targets) - set(deployment.target_device_ids))
            if invalid_targets:
                raise OperationConflictError(
                    f"rollback targets are outside deployment {deployment_id}: {', '.join(invalid_targets)}"
                )
            normalized_payload["deployment_id"] = deployment_id
        elif normalized_action == "restart_service":
            if len(normalized_targets) != 1:
                raise OperationConflictError("restart_service requires exactly one service target")
            if normalized_targets[0] not in run.state.services:
                raise EntityNotFoundError(f"service not found: {normalized_targets[0]}")
        elif normalized_action == "update_incident":
            if len(normalized_targets) != 1:
                raise OperationConflictError("update_incident requires exactly one incident target")
            if normalized_targets[0] not in run.state.incidents:
                raise EntityNotFoundError(f"incident not found: {normalized_targets[0]}")
            status = str(normalized_payload.get("status") or "").strip().lower()
            if status not in {"open", "closed"}:
                raise OperationConflictError("update_incident payload.status must be open or closed")
            normalized_payload["status"] = status

        # Capture the authoritative revision at proposal time. A first
        # execution must revalidate this precondition; reconciliation of an
        # already-executed operation deliberately skips it and verifies the
        # resulting state instead.
        normalized_payload.setdefault("expected_state_revision", run.state.revision)
        return normalized_action, sorted(normalized_targets), normalized_payload

    @staticmethod
    def _approval_expired(approval: ApprovalRecord) -> bool:
        try:
            expires_at = datetime.fromisoformat(approval.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires_at

    @staticmethod
    def _operation_result(run: ScenarioRun, approval: ApprovalRecord) -> dict[str, Any]:
        operation = run.operations.get(approval.idempotency_key)
        return operation.to_dict() if operation else {
            "operation_id": approval.operation_id,
            "idempotency_key": approval.idempotency_key,
            "status": "missing",
        }

    def _expire_approval(self, run: ScenarioRun, approval: ApprovalRecord) -> None:
        if approval.status in {"requested", "approved"}:
            approval.status = "expired"
            operation = run.operations.get(approval.idempotency_key)
            if operation is not None:
                run.operations.fail(approval.idempotency_key, "approval expired before execution")
            self._record_event(
                run,
                "approval_expired",
                {
                    "approval_id": approval.approval_id,
                    "operation_id": approval.operation_id,
                    "action": approval.action,
                },
            )

    def propose_operation(
        self,
        run_id: str,
        *,
        requested_by: str,
        action: str,
        target_ids: list[str] | tuple[str, ...],
        payload: dict[str, Any] | None = None,
        idempotency_key: str,
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        """Create an immutable, approval-required operation proposal."""

        if not str(idempotency_key or "").strip():
            raise OperationConflictError("idempotency_key is required")
        if ttl_seconds < 1 or ttl_seconds > 3600:
            raise OperationConflictError("ttl_seconds must be between 1 and 3600")
        with self._lock:
            run = self.get_run(run_id)
            normalized_action, normalized_targets, normalized_payload = self._validate_operation_request(
                run,
                action=action,
                target_ids=target_ids,
                payload=payload or {},
            )
            operation, claimed = run.operations.begin(
                operation_id=f"OP-{uuid4().hex}",
                idempotency_key=str(idempotency_key).strip(),
                action=normalized_action,
                target_ids=normalized_targets,
                payload=normalized_payload,
            )
            if not claimed:
                approval = next(
                    (item for item in run.approvals.values() if item.operation_id == operation.operation_id),
                    None,
                )
                if approval is None:
                    raise OperationConflictError(
                        f"operation {operation.operation_id} has no corresponding approval record"
                    )
                return {
                    "replayed": True,
                    "approval": approval.to_dict(),
                    "operation": operation.to_dict(),
                }

            approval = ApprovalRecord.create(
                operation_id=operation.operation_id,
                idempotency_key=operation.idempotency_key,
                action=operation.action,
                run_id=run_id,
                requested_by=str(requested_by or "local-operator"),
                target_ids=operation.target_ids,
                payload=operation.payload,
                parameters_hash=operation.request_fingerprint,
                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
            )
            run.approvals[approval.approval_id] = approval
            self._record_event(
                run,
                "approval_requested",
                {
                    "approval_id": approval.approval_id,
                    "operation_id": approval.operation_id,
                    "action": approval.action,
                    "target_ids": list(approval.target_ids),
                    "parameters_hash": approval.parameters_hash,
                    "requested_by": approval.requested_by,
                    "expires_at": approval.expires_at,
                },
            )
            return {
                "replayed": False,
                "approval": approval.to_dict(),
                "operation": operation.to_dict(),
            }

    def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            run = self.get_run(run_id)
            return [
                approval.to_dict()
                for approval in sorted(
                    run.approvals.values(),
                    key=lambda item: (item.created_at, item.approval_id),
                )
            ]

    def decide_approval(
        self,
        run_id: str,
        *,
        approval_id: str,
        decided_by: str,
        decision: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Approve or reject an exact proposal; repeat decisions are idempotent."""

        normalized_decision = str(decision or "").strip().lower()
        normalized_decision = {"approve": "approved", "reject": "rejected"}.get(
            normalized_decision,
            normalized_decision,
        )
        if normalized_decision not in {"approved", "rejected"}:
            raise OperationConflictError("decision must be approved or rejected")
        with self._lock:
            run = self.get_run(run_id)
            approval = run.approvals.get(approval_id)
            if approval is None:
                raise ApprovalNotFoundError(f"approval not found: {approval_id}")
            if self._approval_expired(approval) and approval.status in {"requested", "approved"}:
                self._expire_approval(run, approval)
                raise ApprovalExpiredError(f"approval expired: {approval_id}")
            if approval.status in {"approved", "rejected"}:
                if approval.status != normalized_decision:
                    raise OperationConflictError(
                        f"approval {approval_id} is already {approval.status}"
                    )
                return {
                    "replayed": True,
                    "approval": approval.to_dict(),
                    "operation": self._operation_result(run, approval),
                }
            if approval.status != "requested":
                raise OperationConflictError(
                    f"approval {approval_id} cannot be decided from status {approval.status}"
                )

            now = datetime.now(timezone.utc).isoformat()
            approval.status = normalized_decision
            approval.decided_by = str(decided_by or "local-admin")
            approval.decision_reason = str(reason).strip() if reason else None
            approval.decided_at = now
            if normalized_decision == "approved":
                operation = run.operations.get(approval.idempotency_key)
                if operation is not None:
                    operation.status = "approved"
                    operation.updated_at = now
            else:
                run.operations.fail(approval.idempotency_key, approval.decision_reason or "rejected")
            self._record_event(
                run,
                "approval_decided",
                {
                    "approval_id": approval.approval_id,
                    "operation_id": approval.operation_id,
                    "decision": approval.status,
                    "decided_by": approval.decided_by,
                    "reason": approval.decision_reason,
                },
            )
            return {
                "replayed": False,
                "approval": approval.to_dict(),
                "operation": self._operation_result(run, approval),
            }

    @staticmethod
    def _apply_operation(
        run: ScenarioRun,
        approval: ApprovalRecord,
    ) -> dict[str, Any]:
        """Apply one approved operation and return an auditable execution result."""

        action = approval.action
        payload = approval.payload
        changed_target_ids: list[str] = []
        failed_targets: list[dict[str, str]] = []
        ambiguous_targets: list[str] = []

        if action == "rollback_vpn_deployment":
            deployment_id = str(payload["deployment_id"])
            deployment = run.state.deployments[deployment_id]
            for target_id in approval.target_ids:
                if (
                    target_id in run.faults.unavailable_operations
                    or action in run.faults.unavailable_operations
                    or "*" in run.faults.unavailable_operations
                ):
                    failed_targets.append({"target_id": target_id, "reason": "synthetic API unavailable"})
                    continue
                if target_id in run.faults.timeout_operations:
                    failed_targets.append({"target_id": target_id, "reason": "synthetic timeout"})
                    continue
                device = run.state.devices[target_id]
                before = (device.status, device.vpn_status, device.installed_software.get("vpn-client"))
                device.status = "healthy"
                device.vpn_status = "connected"
                device.installed_software["vpn-client"] = "7.3.2"
                after = (device.status, device.vpn_status, device.installed_software.get("vpn-client"))
                if before != after:
                    changed_target_ids.append(target_id)
                if target_id in run.faults.timeout_after_commit_operations:
                    failed_targets.append({"target_id": target_id, "reason": "synthetic timeout after commit"})
                    ambiguous_targets.append(target_id)
            target_set = set(deployment.target_device_ids)
            if target_set.issubset(set(approval.target_ids)) and not failed_targets:
                deployment.status = "rolled_back"
            else:
                deployment.status = "partially_rolled_back"
            return {
                "action": action,
                "deployment_id": deployment_id,
                "changed_target_ids": changed_target_ids,
                "failed_targets": failed_targets,
                "ambiguous_targets": ambiguous_targets,
                "deployment_status": deployment.status,
            }

        if action == "restart_service":
            service_id = approval.target_ids[0]
            if (
                service_id in run.faults.unavailable_operations
                or action in run.faults.unavailable_operations
                or "*" in run.faults.unavailable_operations
            ):
                failed_targets.append({"target_id": service_id, "reason": "synthetic API unavailable"})
            elif service_id in run.faults.timeout_operations:
                failed_targets.append({"target_id": service_id, "reason": "synthetic timeout"})
            else:
                service = run.state.services[service_id]
                if service.status != "operational":
                    service.status = "operational"
                    changed_target_ids.append(service_id)
                if service_id in run.faults.timeout_after_commit_operations:
                    failed_targets.append({"target_id": service_id, "reason": "synthetic timeout after commit"})
                    ambiguous_targets.append(service_id)
            return {
                "action": action,
                "service_id": service_id,
                "changed_target_ids": changed_target_ids,
                "failed_targets": failed_targets,
                "ambiguous_targets": ambiguous_targets,
            }

        incident_id = approval.target_ids[0]
        if (
            incident_id in run.faults.unavailable_operations
            or action in run.faults.unavailable_operations
            or "*" in run.faults.unavailable_operations
        ):
            failed_targets.append({"target_id": incident_id, "reason": "synthetic API unavailable"})
        elif incident_id in run.faults.timeout_operations:
            failed_targets.append({"target_id": incident_id, "reason": "synthetic timeout"})
        else:
            incident = run.state.incidents[incident_id]
            desired_status = str(payload["status"])
            if incident.status != desired_status:
                incident.status = desired_status
                changed_target_ids.append(incident_id)
            if incident_id in run.faults.timeout_after_commit_operations:
                failed_targets.append({"target_id": incident_id, "reason": "synthetic timeout after commit"})
                ambiguous_targets.append(incident_id)
        return {
            "action": action,
            "incident_id": incident_id,
            "desired_status": str(payload["status"]),
            "changed_target_ids": changed_target_ids,
            "failed_targets": failed_targets,
            "ambiguous_targets": ambiguous_targets,
        }

    @staticmethod
    def _verify_operation(run: ScenarioRun, approval: ApprovalRecord) -> dict[str, Any]:
        failures: list[dict[str, str]] = []
        payload = approval.payload
        if approval.action == "rollback_vpn_deployment":
            for target_id in approval.target_ids:
                device = run.state.devices[target_id]
                if device.status != "healthy" or device.vpn_status != "connected" or device.installed_software.get("vpn-client") != "7.3.2":
                    failures.append({"target_id": target_id, "reason": "device did not reach the rollback state"})
        elif approval.action == "restart_service":
            service_id = approval.target_ids[0]
            if run.state.services[service_id].status != "operational":
                failures.append({"target_id": service_id, "reason": "service is not operational"})
        elif run.state.incidents[approval.target_ids[0]].status != str(payload["status"]):
            failures.append(
                {
                    "target_id": approval.target_ids[0],
                    "reason": "incident status does not match the requested status",
                }
            )
        return {
            "status": "verified" if not failures else "unverified",
            "failures": failures,
            "checked_target_ids": list(approval.target_ids),
        }

    def execute_operation(
        self,
        run_id: str,
        *,
        approval_id: str,
        executed_by: str,
    ) -> dict[str, Any]:
        """Execute an approved operation once, then verify authoritative state."""

        with self._lock:
            run = self.get_run(run_id)
            approval = run.approvals.get(approval_id)
            if approval is None:
                raise ApprovalNotFoundError(f"approval not found: {approval_id}")
            if approval.status in {"requested", "approved"} and self._approval_expired(approval):
                self._expire_approval(run, approval)
                raise ApprovalExpiredError(f"approval expired: {approval_id}")
            if approval.status == "verified":
                return {
                    "replayed": True,
                    "approval": approval.to_dict(),
                    "operation": self._operation_result(run, approval),
                }
            if approval.status not in {"approved", "executed"}:
                raise ApprovalRequiredError(
                    f"approval {approval_id} must be approved before execution (status={approval.status})"
                )

            operation = run.operations.get(approval.idempotency_key)
            if operation is None:
                raise OperationConflictError(f"operation not found for approval {approval_id}")

            if operation.status != "executed":
                expected_revision = approval.payload.get("expected_state_revision")
                stale_target = any(
                    target_id in run.faults.stale_state_targets for target_id in approval.target_ids
                )
                stale_action = approval.action in run.faults.stale_state_targets
                revision_changed = (
                    isinstance(expected_revision, int) and expected_revision != run.state.revision
                )
                if stale_target or stale_action or revision_changed:
                    now = datetime.now(timezone.utc).isoformat()
                    operation.status = "stale"
                    operation.error = "authoritative state changed after approval; re-proposal required"
                    operation.updated_at = now
                    approval.status = "stale"
                    self._record_event(
                        run,
                        "operation_stale",
                        {
                            "approval_id": approval.approval_id,
                            "operation_id": approval.operation_id,
                            "action": approval.action,
                            "expected_state_revision": expected_revision,
                            "actual_state_revision": run.state.revision,
                        },
                    )
                    raise StaleStateError(
                        f"operation {approval.operation_id} is stale; re-propose against current state"
                    )

            now = datetime.now(timezone.utc).isoformat()
            replayed = approval.status == "executed" and operation.status == "executed"
            if operation.status != "executed":
                self._record_event(
                    run,
                    "operation_started",
                    {
                        "approval_id": approval.approval_id,
                        "operation_id": approval.operation_id,
                        "action": approval.action,
                        "target_ids": list(approval.target_ids),
                        "executed_by": str(executed_by or "local-operator"),
                    },
                )
                execution = self._apply_operation(run, approval)
                if execution["changed_target_ids"]:
                    run.state.revision += 1
                operation.status = "executed"
                operation.result = {"execution": execution}
                operation.updated_at = now
                approval.status = "executed"
                approval.executed_at = now
                self._record_event(
                    run,
                    "operation_executed",
                    {
                        "approval_id": approval.approval_id,
                        "operation_id": approval.operation_id,
                        **execution,
                    },
                )
            else:
                # An executed/unverified operation is a reconciliation
                # candidate. Re-apply the same immutable operation against
                # authoritative state; each supported mutation is idempotent,
                # so healthy targets are not changed a second time while a
                # previously timed-out target can recover after the fault is
                # cleared.
                execution = self._apply_operation(run, approval)
                if execution["changed_target_ids"]:
                    run.state.revision += 1
                operation.result = {"execution": execution}
                operation.updated_at = now
                self._record_event(
                    run,
                    "operation_reconciled",
                    {
                        "approval_id": approval.approval_id,
                        "operation_id": approval.operation_id,
                        "action": approval.action,
                        **execution,
                    },
                )

            if execution.get("ambiguous_targets"):
                verification = {
                    "status": "unverified",
                    "failures": [
                        {
                            "target_id": target_id,
                            "reason": "timeout after commit; authoritative state must be re-read",
                        }
                        for target_id in execution["ambiguous_targets"]
                    ],
                    "checked_target_ids": list(approval.target_ids),
                }
            else:
                verification = self._verify_operation(run, approval)
            operation.result = {
                **(operation.result or {}),
                "verification": verification,
            }
            operation.updated_at = datetime.now(timezone.utc).isoformat()
            if verification["status"] == "verified":
                operation.status = "verified"
                approval.status = "verified"
                approval.verified_at = operation.updated_at
            else:
                operation.status = "executed"
                approval.status = "executed"
            self._record_event(
                run,
                "verification_completed",
                {
                    "approval_id": approval.approval_id,
                    "operation_id": approval.operation_id,
                    **verification,
                },
            )
            return {
                "replayed": replayed,
                "approval": approval.to_dict(),
                "operation": operation.to_dict(),
            }

    def _record_event(self, run: ScenarioRun, event_type: str, payload: dict[str, Any]) -> None:
        run.events.append(
            {
                "sequence": len(run.events) + 1,
                "event_type": event_type,
                "scenario_run_id": run.run_id,
                "state_revision": run.state.revision,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "payload": deepcopy(payload),
            }
        )
        if self._persistence is not None:
            self._persistence.save(run)

    def read(self, run_id: str, reader: Callable[[ScenarioRun], Any]) -> Any:
        """Run a read-only callback while holding the store lock."""

        with self._lock:
            run = self.get_run(run_id)
            return reader(run)
