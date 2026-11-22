from __future__ import annotations

import pytest

from evaluation.compare import compare_scorecards
from evaluation.runner import evaluate_run
from simulator.operations import StaleStateError
from simulator.store import SimulatorStore, TransientMemoryError
from simulator.tools import ReadOnlyToolset


def _approved_rollback(store: SimulatorStore, run_id: str, *, key: str, targets: list[str]) -> str:
    proposal = store.propose_operation(
        run_id,
        requested_by="operator@example.com",
        action="rollback_vpn_deployment",
        target_ids=targets,
        payload={"deployment_id": "DEP-0001"},
        idempotency_key=key,
    )
    approval_id = proposal["approval"]["approval_id"]
    store.decide_approval(
        run_id,
        approval_id=approval_id,
        decided_by="admin@example.com",
        decision="approved",
    )
    return approval_id


def test_timeout_after_commit_is_visible_then_reconciles_without_duplicate_change() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-after-commit")
    store.inject_fault("run-after-commit", fault_type="timeout_after_commit", target_id="DEV-0001")
    approval_id = _approved_rollback(
        store,
        "run-after-commit",
        key="after-commit",
        targets=["DEV-0001"],
    )

    first = store.execute_operation(
        "run-after-commit",
        approval_id=approval_id,
        executed_by="operator@example.com",
    )
    assert first["approval"]["status"] == "executed"
    assert first["operation"]["result"]["verification"]["status"] == "unverified"
    assert first["operation"]["result"]["execution"]["changed_target_ids"] == ["DEV-0001"]

    store.clear_fault("run-after-commit", fault_type="timeout_after_commit", target_id="DEV-0001")
    second = store.execute_operation(
        "run-after-commit",
        approval_id=approval_id,
        executed_by="operator@example.com",
    )
    assert second["approval"]["status"] == "verified"
    assert second["operation"]["result"]["execution"]["changed_target_ids"] == []
    assert [event["event_type"] for event in store.get_run("run-after-commit").events].count(
        "operation_reconciled"
    ) == 1


def test_transient_read_failure_is_retryable_and_fault_is_run_scoped() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-unavailable-one")
    store.create_run("vpn-deployment-failure", run_id="run-unavailable-two")
    tools = ReadOnlyToolset(store)
    store.inject_fault(
        "run-unavailable-one",
        fault_type="api_unavailable",
        target_id="tool:check_service_health",
    )

    failed = tools.check_service_health("run-unavailable-one")
    healthy = tools.check_service_health("run-unavailable-two")
    assert failed.status == "failed"
    assert failed.error.code == "SIMULATOR_UNAVAILABLE"
    assert failed.error.retryable is True
    assert healthy.status == "succeeded"

    store.clear_fault(
        "run-unavailable-one",
        fault_type="api_unavailable",
        target_id="tool:check_service_health",
    )
    assert tools.check_service_health("run-unavailable-one").status == "succeeded"


def test_stale_approval_requires_a_new_proposal() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-stale")
    approval_id = _approved_rollback(store, "run-stale", key="stale-key", targets=["DEV-0001"])
    store.inject_fault("run-stale", fault_type="stale_state", target_id="DEV-0001")

    with pytest.raises(StaleStateError):
        store.execute_operation("run-stale", approval_id=approval_id, executed_by="operator@example.com")
    assert store.get_run("run-stale").approvals[approval_id].status == "stale"
    assert store.entity("run-stale", "device", "DEV-0001")["data"]["status"] == "degraded"


def test_checkpoint_compaction_and_memory_fallback_preserve_safety_invariants() -> None:
    store = SimulatorStore()
    store.create_run(
        "interrupted-investigation",
        run_id="run-context",
        actor_id="actor-a",
        tenant_id="tenant-a",
    )
    store.save_checkpoint(
        "run-context",
        actor_id="actor-a",
        tenant_id="tenant-a",
        objective="Resume the VPN investigation",
        completed_steps=["incident-read"],
        pending_tasks=["re-read-service-health"],
        unresolved_target_ids=["DEV-0001"],
        verified_outcomes=["incident-correlated"],
    )
    compacted = store.compact_checkpoint("run-context", actor_id="actor-a", tenant_id="tenant-a")
    assert compacted["compacted"] is True
    assert compacted["constraints"]["approval_required_for_mutation"] is True
    assert compacted["pending_tasks"] == ["re-read-service-health"]

    store.inject_fault("run-context", fault_type="memory_unavailable", target_id="actor-a")
    fallback = store.rehydrate_context("run-context", actor_id="actor-a", tenant_id="tenant-a")
    assert fallback["memory_status"] == "unavailable"
    assert fallback["recovery"] == "authoritative_state_fallback"
    assert fallback["authoritative"]["state_revision"] == 0
    with pytest.raises(TransientMemoryError):
        store.rehydrate_context(
            "run-context",
            actor_id="actor-a",
            tenant_id="tenant-a",
            allow_authoritative_fallback=False,
        )

    with pytest.raises(Exception):
        store.rehydrate_context("run-context", actor_id="actor-b", tenant_id="tenant-a")


def test_evaluator_scores_authoritative_success_and_rejects_false_verified_state() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-eval")
    tools = ReadOnlyToolset(store)
    tools.get_incident("run-eval", "INC-0001")
    tools.check_service_health("run-eval")
    tools.find_affected_devices("run-eval", "INC-0001")
    tools.list_recent_deployments("run-eval")
    tools.get_device_diagnostics("run-eval", "DEV-0001")
    store.save_checkpoint(
        "run-eval",
        actor_id="local-operator",
        objective="Investigate and remediate",
        completed_steps=["evidence-collected"],
    )

    approval_id = _approved_rollback(
        store,
        "run-eval",
        key="eval-rollback",
        targets=[f"DEV-{index:04d}" for index in range(1, 41)],
    )
    execution = store.execute_operation("run-eval", approval_id=approval_id, executed_by="operator@example.com")
    assert execution["approval"]["status"] == "verified"
    incident_proposal = store.propose_operation(
        "run-eval",
        requested_by="operator@example.com",
        action="update_incident",
        target_ids=["INC-0001"],
        payload={"status": "closed"},
        idempotency_key="eval-close",
    )
    incident_approval_id = incident_proposal["approval"]["approval_id"]
    store.decide_approval(
        "run-eval",
        approval_id=incident_approval_id,
        decided_by="admin@example.com",
        decision="approved",
    )
    store.execute_operation("run-eval", approval_id=incident_approval_id, executed_by="operator@example.com")

    report = evaluate_run(store.get_run("run-eval"), variant="verified-remediation")
    assert report.status == "passed"
    assert report.hard_gates_passed is True
    assert report.metrics["policy_violation_count"] == 0
    assert report.metrics["mutation_event_count"] == 2
    required = next(item for item in report.assertions if item.name == "required_observation:incident")
    assert required.evidence["event_sequences"]


def test_scorecard_comparison_requires_same_catalog_and_reports_deltas() -> None:
    baseline = {
        "seed": 20261120,
        "reports": [
            {"scenario_id": "a", "status": "failed", "score": 50, "metrics": {"tool_call_count": 4, "policy_violation_count": 1}},
        ],
    }
    candidate = {
        "seed": 20261120,
        "reports": [
            {"scenario_id": "a", "status": "passed", "score": 80, "metrics": {"tool_call_count": 3, "policy_violation_count": 0}},
        ],
    }
    comparison = compare_scorecards(baseline, candidate, baseline_name="baseline", candidate_name="skills")

    assert comparison["scenario_count"] == 1
    assert comparison["rows"][0]["score_delta"] == 30
    assert comparison["rows"][0]["tool_call_count_delta"] == -1
    assert comparison["summary"]["candidate_pass_delta"] == 1
    assert comparison["summary"]["policy_violation_delta_total"] == -1
