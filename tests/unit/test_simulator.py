from __future__ import annotations

import pytest

from simulator.fixtures import build_environment, load_scenarios
from simulator.operations import OperationConflictError, OperationLedger
from simulator.persistence import DynamoDBRunRepository, PersistenceConflictError as RepositoryPersistenceConflictError
from simulator.store import PersistenceConflictError, SimulatorStore
from simulator.tools import ReadOnlyToolset
from simulator.validation import validate_environment


class FakeDynamoTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool) -> dict[str, object]:
        del ConsistentRead
        item = self.items.get((Key["scenario_run_id"], Key["record_key"]))
        return {"Item": item} if item is not None else {}

    def put_item(
        self,
        *,
        Item: dict[str, object],
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, object] | None = None,
    ) -> None:
        del ExpressionAttributeNames
        key = (str(Item["scenario_run_id"]), str(Item["record_key"]))
        existing = self.items.get(key)
        if ConditionExpression and existing is not None:
            expected = (ExpressionAttributeValues or {}).get(":expected")
            version = existing.get("persistence_version")
            allows_missing = "attribute_not_exists" in ConditionExpression and version is None
            allows_expected = expected is not None and version == expected
            if not (allows_missing or allows_expected):
                error = RuntimeError("conditional check failed")
                error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
                raise error
        self.items[key] = Item

    def scan(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"Items": list(self.items.values())}


class ConflictOnceDynamoTable(FakeDynamoTable):
    def __init__(self) -> None:
        super().__init__()
        self.conflict_once = False

    def put_item(
        self,
        *,
        Item: dict[str, object],
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, object] | None = None,
    ) -> None:
        if self.conflict_once:
            self.conflict_once = False
            error = RuntimeError("conditional check failed")
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise error
        super().put_item(
            Item=Item,
            ConditionExpression=ConditionExpression,
            ExpressionAttributeNames=ExpressionAttributeNames,
            ExpressionAttributeValues=ExpressionAttributeValues,
        )


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
    assert scenarios["vpn-deployment-failure"].primary_incident_id == "INC-0001"
    assert scenarios["partial-remediation-failure"].primary_incident_id == "INC-0002"
    assert scenarios["service-outage"].primary_incident_id == "INC-0003"
    assert scenarios["unauthorized-access-request"].primary_incident_id == "INC-0004"
    assert scenarios["interrupted-investigation"].primary_incident_id == "INC-0005"


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


def test_read_only_observation_retries_after_dynamodb_conflict() -> None:
    table = ConflictOnceDynamoTable()
    repository = DynamoDBRunRepository("northstar-test-scenario-state", table=table)
    store = SimulatorStore(persistence=repository)
    store.create_run("vpn-deployment-failure", run_id="run-observation-retry")
    table.conflict_once = True

    result = ReadOnlyToolset(store).check_service_health("run-observation-retry")

    assert result.status == "succeeded"
    persisted = repository.load("run-observation-retry")
    assert persisted is not None
    assert len(persisted.observations) == 1
    assert [event["event_type"] for event in persisted.events][-1] == "observation_recorded"


def test_dynamodb_repository_rejects_stale_whole_snapshot_writes() -> None:
    table = FakeDynamoTable()
    repository = DynamoDBRunRepository("northstar-test-scenario-state", table=table)
    winner_store = SimulatorStore(persistence=repository)
    stale_store = SimulatorStore(persistence=repository)
    winner_store.create_run("vpn-deployment-failure", run_id="run-concurrent")
    stale_run = stale_store.get_run("run-concurrent")

    winner_store.record_harness_event("run-concurrent", "winner_event", {"request_id": "winner"})

    with pytest.raises(PersistenceConflictError):
        stale_store._record_event(stale_run, "stale_event", {"request_id": "stale"})

    persisted = repository.load("run-concurrent")
    assert persisted is not None
    event_types = [event["event_type"] for event in persisted.events]
    assert "winner_event" in event_types
    assert "stale_event" not in event_types


def test_repository_maps_dynamodb_conditional_failure_to_a_stable_error() -> None:
    table = FakeDynamoTable()
    repository = DynamoDBRunRepository("northstar-test-scenario-state", table=table)
    store = SimulatorStore(persistence=repository)
    store.create_run("vpn-deployment-failure", run_id="run-repository-error")
    stale = repository.load("run-repository-error")
    assert stale is not None
    store.record_harness_event("run-repository-error", "newer_event", {})

    with pytest.raises(RepositoryPersistenceConflictError):
        repository.save(stale)


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
