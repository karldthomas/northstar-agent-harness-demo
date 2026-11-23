from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from apps.api import main as main_api
from apps.api.auth import Principal, require_http_auth
from apps.api.main import app
from harness.agentcore import HarnessEvent
import harness.api as harness_api
from simulator.api import store


def test_simulator_api_exposes_health_and_typed_read_only_tool_routes() -> None:
    client = TestClient(app)

    health = client.get("/healthz")
    service = client.get("/simulator/runs/demo-vpn/tools/check-service-health")
    devices = client.get(
        "/simulator/runs/demo-vpn/tools/find-affected-devices",
        params={"incident_id": "INC-0001"},
    )

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "phase": "5-6"}
    assert service.status_code == 200
    assert service.json()["read_only"] is True
    assert service.json()["data"]["status"] == "operational"
    assert devices.status_code == 200
    assert devices.json()["data"]["total_affected"] == 40


def test_deployed_function_url_can_serve_the_bundled_react_console(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (main_api.WEB_DIST / "index.html").is_file():
        pytest.skip("the frontend bundle has not been built in this checkout")
    monkeypatch.setenv("NORTHSTAR_SERVE_WEB", "true")
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in response.text


def test_independent_evaluation_route_returns_state_based_scorecard() -> None:
    client = TestClient(app)
    client.get("/simulator/runs/demo-vpn/tools/get-incident", params={"incident_id": "INC-0001"})
    response = client.get("/evaluation/runs/demo-vpn")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scenario_id"] == "vpn-deployment-failure"
    assert "hard_gates_passed" in payload
    assert payload["metrics"]["tool_call_count"] >= 1


def test_run_snapshot_returns_authoritative_tool_observations_for_the_trace() -> None:
    run_id = f"snapshot-observations-{uuid4().hex}"
    store.create_run("vpn-deployment-failure", run_id=run_id)
    client = TestClient(app)

    tool_response = client.get(
        f"/simulator/runs/{run_id}/tools/get-incident",
        params={"incident_id": "INC-0001"},
    )
    snapshot_response = client.get(f"/simulator/runs/{run_id}")

    assert tool_response.status_code == 200
    assert snapshot_response.status_code == 200
    observations = snapshot_response.json()["observations"]
    assert observations[0]["tool_name"] == "get_incident"
    assert observations[0]["authoritative"] is True
    assert observations[0]["data"]["incident_id"] == "INC-0001"


def test_harness_api_requires_an_explicit_managed_harness_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NORTHSTAR_HARNESS_ARN", raising=False)
    client = TestClient(app)

    response = client.post(
        "/harness/invocations",
        json={"scenario_run_id": "demo-vpn", "objective": "Investigate the VPN incident"},
    )

    assert response.status_code == 503
    assert "NORTHSTAR_HARNESS_ARN" in response.json()["detail"]


def test_harness_api_streams_correlated_events_without_owning_orchestration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def stream(self, context, prompt):
            assert context.scenario_run_id == "demo-vpn"
            assert "Scenario run ID: demo-vpn" in prompt
            assert "Incident ID: INC-0001" in prompt
            assert '"primary_incident_id": "INC-0001"' in prompt
            assert '"service_id": "svc-vpn-gateway"' in prompt
            assert '"related_deployment_id": "DEP-0001"' in prompt
            yield HarnessEvent(
                event_type="contentBlockDelta",
                sequence=1,
                scenario_run_id=context.scenario_run_id,
                session_id=context.session_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                payload={"delta": {"text": "done"}},
                occurred_at="2026-11-20T00:00:00+00:00",
            )

    monkeypatch.setattr(harness_api, "_client", lambda: StubClient())
    client = TestClient(app)

    response = client.post(
        "/harness/invocations/stream",
        json={"scenario_run_id": "demo-vpn", "objective": "Investigate the VPN incident"},
    )

    assert response.status_code == 200
    assert "event: contentBlockDelta" in response.text
    assert '"scenario_run_id": "demo-vpn"' in response.text
    assert "event: complete" in response.text


def test_harness_stream_records_turn_and_tool_markers_in_the_run_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = f"harness-observability-{uuid4().hex}"
    store.create_run("vpn-deployment-failure", run_id=run_id)

    class StubClient:
        def stream(self, context, prompt):
            del prompt
            yield HarnessEvent(
                event_type="messageStart",
                sequence=1,
                scenario_run_id=context.scenario_run_id,
                session_id=context.session_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                payload={"role": "assistant"},
                occurred_at="2026-11-20T00:00:00+00:00",
            )
            yield HarnessEvent(
                event_type="contentBlockStart",
                sequence=2,
                scenario_run_id=context.scenario_run_id,
                session_id=context.session_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                payload={"start": {"toolUse": {"name": "northstar-simulator___get_incident", "toolUseId": "tool-1"}}},
                occurred_at="2026-11-20T00:00:01+00:00",
            )
            yield HarnessEvent(
                event_type="contentBlockDelta",
                sequence=3,
                scenario_run_id=context.scenario_run_id,
                session_id=context.session_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                payload={"delta": {"toolUse": {"name": "northstar-simulator___get_incident", "toolUseId": "tool-1", "input": {"run_id": run_id, "incident_id": "INC-0001", "gateway_context_token": "secret"}}}},
                occurred_at="2026-11-20T00:00:01+00:00",
            )
            yield HarnessEvent(
                event_type="messageStop",
                sequence=4,
                scenario_run_id=context.scenario_run_id,
                session_id=context.session_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                payload={"stopReason": "end_turn"},
                occurred_at="2026-11-20T00:00:02+00:00",
            )

    monkeypatch.setattr(harness_api, "_client", lambda: StubClient())
    response = TestClient(app).post(
        "/harness/invocations/stream",
        json={"scenario_run_id": run_id, "objective": "Investigate the VPN incident"},
    )

    assert response.status_code == 200
    event_types = [event["event_type"] for event in store.events_since(run_id)["events"]]
    assert event_types == [
        "scenario_created",
        "skills_selected",
        "agent_invocation_started",
        "agent_turn_started",
        "agent_tool_call_requested",
                "agent_tool_call_input",
        "agent_turn_completed",
        "agent_invocation_completed",
    ]
    selection = store.events_since(run_id)["events"][1]["payload"]
    assert selection["selected_skill_ids"] == ["vpn-diagnostics", "incident-triage", "safe-remediation"]
    assert "# VPN Diagnostics" in next(
        skill["text"]
        for skill in selection["selected_skills"]
        if skill["id"] == "vpn-diagnostics"
    )
    tool_input = store.events_since(run_id)["events"][5]["payload"]["tool_input"]
    assert tool_input["incident_id"] == "INC-0001"
    assert tool_input["gateway_context_token"] == "[redacted]"


def test_harness_invocation_enforces_actor_run_ownership() -> None:
    run_id = f"harness-owner-{uuid4().hex}"
    store.create_run("vpn-deployment-failure", run_id=run_id, actor_id="owner-sub")
    operator = Principal("other-sub", "operator@example.com", "operator@example.com", frozenset(), {})
    app.dependency_overrides[require_http_auth] = lambda: operator
    try:
        response = TestClient(app).post(
            "/harness/invocations/stream",
            json={"scenario_run_id": run_id, "objective": "Inspect the incident"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_harness_api_resumes_the_same_session_after_an_approved_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def invoke(self, context, prompt):
            assert context.scenario_run_id == "demo-vpn"
            assert context.session_id.startswith("northstar-v1-")
            assert len(context.session_id) >= 33
            assert "Approval ID: APR-42" in prompt
            assert "Do not execute or propose a mutation" in prompt
            return SimpleNamespace(to_dict=lambda: {"session_id": context.session_id, "succeeded": True})

    monkeypatch.setattr(harness_api, "_client", lambda: StubClient())
    client = TestClient(app)

    response = client.post(
        "/harness/invocations",
        json={
            "scenario_run_id": "demo-vpn",
            "objective": "Investigate and remediate the VPN incident",
            "session_id": "s" * 33,
            "approval_id": "APR-42",
            "operation_id": "OP-42",
            "action": "rollback_vpn_deployment",
        },
    )

    assert response.status_code == 200
    assert response.json()["session_id"].startswith("northstar-v1-")
    assert response.json()["succeeded"] is True


def test_harness_invocation_does_not_retry_an_internal_type_error() -> None:
    calls = 0

    class StubClient:
        def invoke(self, context, prompt, on_event=None):
            nonlocal calls
            del context, prompt, on_event
            calls += 1
            raise TypeError("internal invocation failure")

    with pytest.raises(TypeError, match="internal invocation failure"):
        harness_api._invoke_with_observability(StubClient(), SimpleNamespace(), "prompt")

    assert calls == 1
