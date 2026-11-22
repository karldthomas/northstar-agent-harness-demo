from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.operations import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    OperationConflictError,
)
from simulator.store import SimulatorStore
from apps.api.auth import _principal_from_claims, get_current_principal
from apps.api.main import app
from fastapi.testclient import TestClient


def _propose(store: SimulatorStore, run_id: str, *, key: str = "approval-1", targets: list[str] | None = None) -> dict:
    return store.propose_operation(
        run_id,
        requested_by="operator@example.com",
        action="rollback_vpn_deployment",
        target_ids=targets or ["DEV-0001", "DEV-0002"],
        payload={"deployment_id": "DEP-0001"},
        idempotency_key=key,
    )


def test_proposal_is_immutable_and_execution_requires_approval() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-approval")
    before_revision = store.get_run("run-approval").state.revision
    proposal = _propose(store, "run-approval")
    approval_id = proposal["approval"]["approval_id"]

    with pytest.raises(ApprovalRequiredError):
        store.execute_operation("run-approval", approval_id=approval_id, executed_by="operator@example.com")

    assert store.get_run("run-approval").state.revision == before_revision
    assert store.entity("run-approval", "device", "DEV-0001")["data"]["status"] == "degraded"

    store.decide_approval(
        "run-approval",
        approval_id=approval_id,
        decided_by="admin@example.com",
        decision="approved",
        reason="validated rollback scope",
    )
    result = store.execute_operation("run-approval", approval_id=approval_id, executed_by="operator@example.com")

    assert result["approval"]["status"] == "verified"
    assert result["operation"]["status"] == "verified"
    assert result["operation"]["result"]["verification"]["status"] == "verified"
    assert store.get_run("run-approval").state.revision == before_revision + 1
    assert store.entity("run-approval", "device", "DEV-0001")["data"]["vpn_status"] == "connected"
    assert [event["event_type"] for event in store.get_run("run-approval").events][-4:] == [
        "approval_decided",
        "operation_started",
        "operation_executed",
        "verification_completed",
    ]

    replay = store.execute_operation("run-approval", approval_id=approval_id, executed_by="operator@example.com")
    assert replay["replayed"] is True
    assert store.get_run("run-approval").state.revision == before_revision + 1


def test_idempotency_returns_the_same_approval_without_reapplying_state() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-idempotent")

    first = _propose(store, "run-idempotent", key="same-request", targets=["DEV-0001"])
    replay = _propose(store, "run-idempotent", key="same-request", targets=["DEV-0001"])

    assert replay["replayed"] is True
    assert replay["approval"]["approval_id"] == first["approval"]["approval_id"]
    assert len(store.list_approvals("run-idempotent")) == 1

    with pytest.raises(OperationConflictError):
        _propose(store, "run-idempotent", key="same-request", targets=["DEV-0003"])


def test_rejected_and_expired_approvals_do_not_mutate_state() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-denied")
    rejected = _propose(store, "run-denied", key="rejected")
    rejected_id = rejected["approval"]["approval_id"]
    store.decide_approval(
        "run-denied",
        approval_id=rejected_id,
        decided_by="admin@example.com",
        decision="rejected",
        reason="scope is too broad",
    )
    with pytest.raises(ApprovalRequiredError):
        store.execute_operation("run-denied", approval_id=rejected_id, executed_by="operator@example.com")
    assert store.entity("run-denied", "device", "DEV-0001")["data"]["status"] == "degraded"

    expired = _propose(store, "run-denied", key="expired", targets=["DEV-0003"])
    expired_id = expired["approval"]["approval_id"]
    store.get_run("run-denied").approvals[expired_id].expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ApprovalExpiredError):
        store.decide_approval(
            "run-denied",
            approval_id=expired_id,
            decided_by="admin@example.com",
            decision="approved",
        )
    assert store.get_run("run-denied").approvals[expired_id].status == "expired"
    assert store.entity("run-denied", "device", "DEV-0003")["data"]["status"] == "degraded"


def test_partial_timeout_is_executed_but_not_verified() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-partial")
    store.inject_fault("run-partial", fault_type="timeout", target_id="DEV-0002")
    proposal = _propose(store, "run-partial", targets=["DEV-0001", "DEV-0002"])
    approval_id = proposal["approval"]["approval_id"]
    store.decide_approval(
        "run-partial",
        approval_id=approval_id,
        decided_by="admin@example.com",
        decision="approved",
    )
    result = store.execute_operation("run-partial", approval_id=approval_id, executed_by="operator@example.com")

    assert result["approval"]["status"] == "executed"
    assert result["operation"]["status"] == "executed"
    assert result["operation"]["result"]["verification"]["status"] == "unverified"
    assert store.entity("run-partial", "device", "DEV-0001")["data"]["status"] == "healthy"
    assert store.entity("run-partial", "device", "DEV-0002")["data"]["status"] == "degraded"

    # Clearing the fault lets the same approved operation reconcile from
    # authoritative state. The already-healthy target is not duplicated and
    # the previously timed-out target becomes verified.
    store.get_run("run-partial").faults.timeout_operations.remove("DEV-0002")
    retry = store.execute_operation("run-partial", approval_id=approval_id, executed_by="operator@example.com")

    assert retry["approval"]["status"] == "verified"
    assert retry["operation"]["result"]["verification"]["status"] == "verified"
    assert retry["operation"]["result"]["execution"]["changed_target_ids"] == ["DEV-0002"]
    assert store.entity("run-partial", "device", "DEV-0002")["data"]["status"] == "healthy"


@pytest.mark.parametrize(
    ("action", "target_id", "payload", "entity_type", "expected_field", "expected_value"),
    [
        ("restart_service", "svc-payments", {}, "service", "status", "operational"),
        ("update_incident", "INC-0003", {"status": "closed"}, "incident", "status", "closed"),
    ],
)
def test_non_rollback_operations_are_approval_gated_and_verified(
    action: str,
    target_id: str,
    payload: dict[str, str],
    entity_type: str,
    expected_field: str,
    expected_value: str,
) -> None:
    store = SimulatorStore()
    store.create_run("service-outage", run_id=f"run-{action}")
    proposal = store.propose_operation(
        f"run-{action}",
        requested_by="operator@example.com",
        action=action,
        target_ids=[target_id],
        payload=payload,
        idempotency_key=f"key-{action}",
    )
    approval_id = proposal["approval"]["approval_id"]
    store.decide_approval(
        f"run-{action}",
        approval_id=approval_id,
        decided_by="admin@example.com",
        decision="approved",
    )
    result = store.execute_operation(f"run-{action}", approval_id=approval_id, executed_by="operator@example.com")

    assert result["approval"]["status"] == "verified"
    assert store.entity(f"run-{action}", entity_type, target_id)["data"][expected_field] == expected_value


def test_approval_decision_route_is_admin_only() -> None:
    client = TestClient(app)
    proposal = client.post(
        "/simulator/runs/demo-vpn/operations/proposals",
        json={
            "action": "rollback_vpn_deployment",
            "target_ids": ["DEV-0004"],
            "payload": {"deployment_id": "DEP-0001"},
            "idempotency_key": "api-approval-route-test",
        },
    )
    assert proposal.status_code == 201
    approval_id = proposal.json()["approval"]["approval_id"]
    assert client.post(
        f"/simulator/runs/demo-vpn/approvals/{approval_id}/decision",
        json={"decision": "approved"},
    ).status_code == 401

    admin = _principal_from_claims(
        {"sub": "sub-api-admin", "cognito:username": "api-admin@example.com", "cognito:groups": ["northstar-admin"]},
        admin_group="northstar-admin",
    )
    app.dependency_overrides[get_current_principal] = lambda: admin
    try:
        decided = client.post(
            f"/simulator/runs/demo-vpn/approvals/{approval_id}/decision",
            json={"decision": "approved"},
        )
        assert decided.status_code == 200
        assert decided.json()["approval"]["status"] == "approved"
    finally:
        app.dependency_overrides.clear()
