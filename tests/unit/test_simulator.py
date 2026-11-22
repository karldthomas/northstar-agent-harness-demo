from __future__ import annotations

from simulator.fixtures import build_environment, load_scenarios
from simulator.operations import OperationConflictError, OperationLedger
from simulator.persistence import DynamoDBRunRepository
from simulator.store import SimulatorStore
from simulator.tools import ReadOnlyToolset
from simulator.validation import validate_environment


class FakeDynamoTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool) -> dict[str, object]:
        del ConsistentRead
        item = self.items.get((Key["scenario_run_id"], Key["record_key"]))
        return {"Item": item} if item is not None else {}

    def put_item(self, *, Item: dict[str, object]) -> None:
        self.items[(str(Item["scenario_run_id"]), str(Item["record_key"]))] = Item

    def scan(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"Items": list(self.items.values())}


def test_seeded_fixture_is_connected_and_reproducible() -> None:
    first = build_environment("vpn-deployment-failure", scenario_run_id="run-a")
    second = build_environment("vpn-deployment-failure", scenario_run_id="run-b")

    assert len(first.employees) == 100
    assert len(first.devices) == 150
    assert len(first.applications) == 15
    assert len(first.deployments) == 30
    assert len(first.incidents) == 50
    assert first.devices["DEV-0001"].employee_id == "EMP-0001"
    assert first.employees["EMP-0001"].device_ids == ["DEV-0001"]
    assert first.incidents["INC-0001"].related_deployment_id == "DEP-0001"
    assert first.incidents["INC-0001"].affected_device_ids == second.incidents["INC-0001"].affected_device_ids
    assert first.devices["DEV-0001"].status == "degraded"
    assert validate_environment(first) == []


def test_scenario_catalog_has_the_original_scenarios_and_phase_five_variants() -> None:
    scenarios = load_scenarios()

    assert {
        "vpn-deployment-failure",
        "partial-remediation-failure",
        "service-outage",
        "unauthorized-access-request",
        "interrupted-investigation",
    }.issubset(scenarios)
    assert len(scenarios) == 20
    assert all(definition.read_only_tools for definition in scenarios.values())
    assert all(definition.description.strip() for definition in scenarios.values())


def test_read_only_tools_return_typed_observations_without_advancing_state() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-read-only")
    tools = ReadOnlyToolset(store)
    before = store.get_run("run-read-only").state.revision

    service = tools.check_service_health("run-read-only", request_id="req-service")
    devices = tools.find_affected_devices("run-read-only", "INC-0001", request_id="req-devices")
    diagnostics = tools.get_device_diagnostics("run-read-only", "DEV-0001", request_id="req-device")

    assert service.status == "succeeded"
    assert service.read_only is True
    assert service.authoritative is True
    assert service.request_id == "req-service"
    assert service.data["status"] == "operational"
    assert devices.data["total_affected"] == 40
    assert diagnostics.data["employee"]["employee_id"] == "EMP-0001"
    assert diagnostics.data["vpn_status"] == "disconnected"
    assert store.get_run("run-read-only").state.revision == before


def test_dynamodb_run_repository_shares_state_and_harness_events_between_store_instances() -> None:
    repository = DynamoDBRunRepository("northstar-test-scenario-state", table=FakeDynamoTable())
    api_store = SimulatorStore(persistence=repository)
    gateway_store = SimulatorStore(persistence=repository)

    api_store.create_run("vpn-deployment-failure", run_id="run-persisted", actor_id="actor-1")
    gateway_store.record_harness_event(
        "run-persisted",
        "agent_turn_started",
        {"request_id": "hreq-1", "session_id": "session-1"},
    )
    ReadOnlyToolset(gateway_store).check_service_health("run-persisted", request_id="gateway-1")

    page = api_store.events_since("run-persisted", after_sequence=1)
    assert [event["event_type"] for event in page["events"]] == [
        "agent_turn_started",
        "observation_recorded",
    ]
    assert page["events"][0]["payload"]["turn_index"] == 1
    assert page["events"][1]["payload"]["tool_name"] == "check_service_health"
    assert api_store.get_run("run-persisted").owner_actor_id == "actor-1"


def test_faults_are_scoped_to_one_run_and_reset_restores_baseline() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-one")
    store.create_run("vpn-deployment-failure", run_id="run-two")
    tools = ReadOnlyToolset(store)

    store.inject_fault("run-one", fault_type="service_unavailable", target_id="svc-vpn-gateway")
    failed_service = tools.check_service_health("run-one")
    healthy_service = tools.check_service_health("run-two")

    assert failed_service.data["status"] == "unavailable"
    assert healthy_service.data["status"] == "operational"
    store.reset_run("run-one")
    assert tools.check_service_health("run-one").data["status"] == "operational"
    assert store.get_run("run-one").faults.to_dict() == {
        "unavailable_services": [],
        "unavailable_tools": [],
        "unavailable_operations": [],
        "diagnostic_conflicts": [],
        "service_health_conflicts": [],
        "timeout_operations": [],
        "timeout_after_commit_operations": [],
        "stale_state_targets": [],
        "memory_unavailable_actors": [],
    }


def test_missing_entity_is_a_structured_tool_failure() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-error")
    result = ReadOnlyToolset(store).get_device_diagnostics("run-error", "does-not-exist")

    assert result.status == "failed"
    assert result.error.code == "ENTITY_NOT_FOUND"
    assert result.read_only is True


def test_knowledge_search_returns_versioned_cited_articles_without_mutation() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-knowledge")
    tools = ReadOnlyToolset(store)
    before = store.get_run("run-knowledge").state.revision

    result = tools.search_knowledge("run-knowledge", "vpn deployment timeout")

    assert result.status == "succeeded"
    assert result.data["knowledge_schema_version"] == "northstar.knowledge.v1"
    assert result.data["articles"]
    assert all(article["article_id"].startswith("KA-") for article in result.data["articles"])
    assert store.get_run("run-knowledge").state.revision == before


def test_prompt_injection_in_knowledge_is_returned_as_untrusted_data() -> None:
    store = SimulatorStore()
    store.create_run("vpn-deployment-failure", run_id="run-untrusted-kb")
    result = ReadOnlyToolset(store).search_knowledge("run-untrusted-kb", "ignore approval policy vpn rollback")

    assert result.status == "succeeded"
    article = next(item for item in result.data["articles"] if item["article_id"] == "KA-025")
    assert "UNTRUSTED CONTENT" in article["summary"]
    assert "prompt-injection" in article["tags"]


def test_operation_ledger_returns_existing_claim_and_rejects_reused_key_with_new_payload() -> None:
    ledger = OperationLedger()
    first, claimed = ledger.begin(
        operation_id="op-1",
        idempotency_key="idem-1",
        action="rollback_deployment",
        target_ids=["DEV-0001", "DEV-0002"],
        payload={"deployment_id": "DEP-0001"},
    )
    replay, replayed = ledger.begin(
        operation_id="op-2",
        idempotency_key="idem-1",
        action="rollback_deployment",
        target_ids=["DEV-0002", "DEV-0001"],
        payload={"deployment_id": "DEP-0001"},
    )

    assert claimed is True
    assert replayed is False
    assert replay.operation_id == first.operation_id
    ledger.complete("idem-1", {"completed": 2})
    assert ledger.get("idem-1").status == "completed"

    try:
        ledger.begin(
            operation_id="op-3",
            idempotency_key="idem-1",
            action="rollback_deployment",
            target_ids=["DEV-0001"],
            payload={"deployment_id": "DEP-0001"},
        )
    except OperationConflictError:
        pass
    else:
        raise AssertionError("reusing an idempotency key for a different request must fail")
