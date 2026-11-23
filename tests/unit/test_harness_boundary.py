from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import api as harness_api
from harness.agentcore import HarnessEvent
from harness import smoke_test
from harness.provision import ProvisionedAgentCore
from harness.smoke_test import _cloud_suite_objective, _evaluation_trace, build_request, load_config


def test_phase_zero_harness_contract_requires_managed_orchestration() -> None:
    config = load_config()
    request = build_request(config)

    assert config["expected"]["managed_orchestration"] is True
    assert config["expected"]["application_model_tool_loop"] is False
    assert request["managed_orchestration_required"] is True
    assert request["application_model_tool_loop"] is False
    assert request["tools"] == ["check_service_health", "get_device_diagnostics"]


def test_cloud_suite_objective_requires_bounded_read_only_evidence() -> None:
    objective = _cloud_suite_objective("Investigate the seeded incident")

    assert objective.startswith("Investigate the seeded incident")
    assert "read-only evaluation run" in objective
    assert "scenario run ID" in objective
    assert "under 200 words" in objective


def test_cloud_suite_objective_can_bind_a_scenario_tool_contract() -> None:
    objective = _cloud_suite_objective(
        "Investigate the seeded incident",
        required_tools=("get_incident", "check_service_health"),
    )

    assert "Use each of these required read-only simulator tools at least once" in objective
    assert "get_incident, check_service_health" in objective


def test_direct_cloud_suite_requires_explicit_non_binding_mode() -> None:
    with pytest.raises(SystemExit, match="requires --skip-runtime-binding"):
        smoke_test.main(["--direct-scenario-suite"])


def test_full_cloud_catalog_requires_explicit_spend_acknowledgement() -> None:
    with pytest.raises(SystemExit, match="acknowledge-cloud-spend"):
        smoke_test.main(
            [
                "--direct-scenario-suite",
                "--skip-runtime-binding",
                "--all-scenarios",
            ]
        )


def test_agentcore_metrics_are_normalized_for_evaluation() -> None:
    assert _evaluation_trace(
        {
            "usage": {"inputTokens": 11, "outputTokens": 7},
            "metrics": {"latencyMs": 123, "costUsd": 0.004},
        }
    ) == {
        "latency_ms": 123,
        "input_tokens": 11,
        "output_tokens": 7,
        "cost_usd": 0.004,
        "policy_violations": [],
    }


def test_live_timeline_markers_preserve_tool_progress_without_model_content(monkeypatch) -> None:
    recorded: list[tuple[str, dict[str, object]]] = []

    def capture(run_id, event_type, *, context, payload=None):
        assert run_id == "run-monitor"
        recorded.append((event_type, dict(payload or {})))

    monkeypatch.setattr(harness_api, "_record_harness_lifecycle", capture)
    common = {
        "scenario_run_id": "run-monitor",
        "session_id": "s" * 33,
        "request_id": "request-monitor",
        "trace_id": "trace-monitor",
        "occurred_at": "2026-11-21T00:00:00+00:00",
    }
    events = (
        HarnessEvent(event_type="messageStart", sequence=1, payload={"role": "assistant"}, **common),
        HarnessEvent(
            event_type="contentBlockStart",
            sequence=2,
            payload={
                "start": {
                    "toolUse": {
                        "name": "northstar-simulator___check_service_health",
                        "toolUseId": "tool-monitor",
                        "input": {"run_id": "run-monitor", "secret": "do-not-store"},
                    }
                }
            },
            **common,
        ),
        HarnessEvent(
            event_type="contentBlockDelta",
            sequence=3,
            payload={"delta": {"text": "private model text"}},
            **common,
        ),
        HarnessEvent(
            event_type="metadata",
            sequence=4,
            payload={"usage": {"inputTokens": 3}, "metrics": {"latencyMs": 8}},
            **common,
        ),
        HarnessEvent(
            event_type="messageStop",
            sequence=5,
            payload={"stopReason": "end_turn"},
            **common,
        ),
    )

    for event in events:
        harness_api._record_harness_event(event)

    assert [event_type for event_type, _ in recorded] == [
        "agent_turn_started",
        "agent_tool_call_requested",
        "agent_metadata",
        "agent_turn_completed",
    ]
    tool_payload = recorded[1][1]
    assert tool_payload["tool_name"] == "northstar-simulator___check_service_health"
    assert tool_payload["tool_use_id"] == "tool-monitor"
    assert "input" not in tool_payload
    serialized = json.dumps(recorded)
    assert "private model text" not in serialized
    assert "do-not-store" not in serialized


def test_hook_decisions_and_tool_input_are_recorded_without_private_or_secret_content(monkeypatch) -> None:
    recorded: list[tuple[str, dict[str, object]]] = []

    def capture(run_id, event_type, *, context, payload=None):
        recorded.append((event_type, dict(payload or {})))

    monkeypatch.setattr(harness_api, "_record_harness_lifecycle", capture)
    common = {
        "scenario_run_id": "run-monitor",
        "session_id": "s" * 33,
        "request_id": "request-monitor",
        "trace_id": "trace-monitor",
        "occurred_at": "2026-11-21T00:00:00+00:00",
    }
    harness_api._record_harness_event(
        HarnessEvent(
            event_type="hookEvent",
            sequence=6,
            payload={"hookEventId": "hook-1", "name": "read-only-guard", "type": "beforeToolCall", "decision": "allow", "reason": "read-only tool"},
            **common,
        )
    )
    harness_api._record_harness_event(
        HarnessEvent(
            event_type="contentBlockDelta",
            sequence=7,
            payload={
                "contentBlockIndex": 2,
                "delta": {
                    "toolUse": {
                        "input": '{"run_id":"run-monitor","gateway_context_token":"secret"}',
                    }
                },
            },
            **common,
        )
    )

    assert recorded[0][0] == "agent_hook_decision"
    assert recorded[0][1]["decision"] == "allow"
    assert recorded[1][0] == "agent_tool_call_input"
    assert recorded[1][1]["content_block_index"] == 2
    assert recorded[1][1]["tool_input"]["gateway_context_token"] == "[redacted]"
    assert "secret" not in json.dumps(recorded)


def test_phase_zero_smoke_objective_bounds_tool_work_and_output() -> None:
    config = load_config()

    assert "at most two" in config["objective"]
    assert "scenario run ID" in config["objective"]
    assert "under 100 words" in config["objective"]
    assert "Do not remediate" in config["objective"]


def test_cloud_suite_creates_runs_through_the_direct_function_url(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_request(**kwargs):
        calls.update(kwargs)
        return {"state": {"scenario_id": "service-outage"}}

    monkeypatch.setattr(smoke_test, "_cognito_json_request", fake_request)
    run_id, result = smoke_test._create_cloud_suite_run(
        simulator_function_url="https://example.lambda-url.us-east-1.on.aws/",
        scenario_id="service-outage",
        seed=20261121,
    )

    assert run_id.startswith("cloud-service-outage-")
    assert calls["url"] == f"https://example.lambda-url.us-east-1.on.aws/simulator/runs"
    assert calls["method"] == "POST"
    assert calls["payload"]["scenario_id"] == "service-outage"
    assert result["state"]["scenario_id"] == "service-outage"


def test_cloud_suite_can_persist_combined_live_artifacts(monkeypatch, tmp_path: Path) -> None:
    context_path = tmp_path / "deployment-context.json"
    context_path.write_text(
        json.dumps({"CF_OUT_SimulatorFunctionUrl": "https://example.lambda-url.us-east-1.on.aws/"}),
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "artifacts"
    requests: list[dict[str, object]] = []

    class FakeResult:
        succeeded = True
        stop_reason = "end_turn"
        events = (object(),)
        tool_uses = ({"name": "northstar-simulator___check_service_health"},)
        errors: tuple[dict[str, object], ...] = ()
        text = "synthetic evidence"
        context = SimpleNamespace(
            request_id="hreq-test",
            session_id="session-test",
            trace_id="trace-test",
        )

        def to_dict(self) -> dict[str, object]:
            return {"succeeded": True, "stop_reason": self.stop_reason, "event_count": 1}

    def fake_request(**kwargs):
        requests.append(kwargs)
        return {"status": "passed", "score": 100}

    monkeypatch.setattr(smoke_test, "_cognito_json_request", fake_request)
    monkeypatch.setattr(
        smoke_test,
        "_create_cloud_suite_run",
        lambda **kwargs: ("cloud-service-outage-test", {"state": {"scenario_id": "service-outage"}}),
    )
    monkeypatch.setattr(smoke_test, "invoke_managed_result", lambda **kwargs: FakeResult())

    exit_code = smoke_test.run_cloud_scenario_suite(
        harness_config_path=tmp_path / "harness.yaml",
        context_path=context_path,
        scenario_ids=["service-outage"],
        profile=None,
        region="us-east-1",
        seed=20261121,
        artifact_dir=artifact_dir,
    )

    assert exit_code == 0
    assert requests == [
        {
            "url": "https://example.lambda-url.us-east-1.on.aws/evaluation/runs/cloud-service-outage-test",
            "method": "GET",
        }
    ]
    artifact = json.loads(
        (artifact_dir / "service-outage-cloud-service-outage-test.json").read_text(encoding="utf-8")
    )
    assert artifact["schema_version"] == "northstar.live-harness-artifact.v1"
    assert artifact["scorecard"]["status"] == "passed"
    assert json.loads((artifact_dir / "suite.json").read_text(encoding="utf-8"))["seed"] == 20261121


def test_provision_comparison_can_skip_browser_runtime_binding(monkeypatch, capsys) -> None:
    provisioned = ProvisionedAgentCore(
        environment="dev",
        region="us-east-1",
        gateway_id="gateway-id",
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/demo",
        gateway_url=None,
        gateway_target_id="target-id",
        harness_id="harness-id",
        harness_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/demo",
        harness_version="1",
        skills_bucket="skills-bucket",
        skills_prefix="skills/prefix",
        variant="baseline",
    )
    bound = False

    monkeypatch.setattr(smoke_test, "DEFAULT_CONTEXT", Path(__file__).resolve())

    def fake_provision(**kwargs):
        assert kwargs["variant"] == "baseline"
        return provisioned

    def should_not_bind(**kwargs):
        nonlocal bound
        bound = True
        raise AssertionError("comparison provisioning must not rebind the browser Lambda")

    monkeypatch.setattr("harness.provision.provision_agentcore", fake_provision)
    monkeypatch.setattr("harness.provision.bind_simulator_harness_runtime", should_not_bind)

    assert smoke_test.main(["--provision", "--skip-runtime-binding", "--variant", "baseline"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert bound is False
    assert output["runtime_binding"] == {
        "skipped": True,
        "reason": "explicitly requested non-binding comparison run",
    }
