from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import EventStreamError

from harness.agentcore import (
    AgentCoreHarnessClient,
    HarnessInvocationError,
    InvocationContext,
    build_approval_resumption_prompt,
    build_investigation_prompt,
    extract_recommendation,
)
from harness.configuration import load_harness_config, resolve_harness_config
from harness.provision import ProvisionedAgentCore, bind_simulator_harness_runtime


class FakeHarnessClient:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    def invoke_harness(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"stream": self.events}


class FailingStreamHarnessClient:
    def invoke_harness(self, **kwargs: Any) -> dict[str, Any]:
        def stream():
            yield {"metadata": {"usage": {"inputTokens": 10}}}
            raise EventStreamError(
                {
                    "Error": {
                        "Code": "runtimeClientError",
                        "Message": "Model stopped generating due to maximum token limit",
                    }
                },
                "InvokeHarness",
            )

        return {"stream": stream()}


def phase_two_config() -> dict[str, Any]:
    config = load_harness_config()
    return resolve_harness_config(
        config,
        values={
            "HARNESS_MODEL_ID": "us.amazon.nova-lite-v1:0",
            "NORTHSTAR_GATEWAY_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/northstar",
        },
    )


def test_invocation_adapter_forwards_managed_request_and_correlates_events() -> None:
    fake = FakeHarnessClient(
        [
            {"messageStart": {"role": "assistant"}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {
                        "toolUse": {
                            "name": "northstar-simulator___check_service_health",
                            "toolUseId": "tool-1",
                            "type": "tool_use",
                        }
                    },
                }
            },
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": "Investigating"}}},
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {
                        "toolUse": {
                            "name": "northstar-simulator___check_service_health",
                            "toolUseId": "tool-1",
                            "input": {"run_id": "run-1", "service_id": "svc-vpn-gateway"},
                        }
                    },
                }
            },
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}, "metrics": {"latencyMs": 12}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )
    client = AgentCoreHarnessClient(
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/northstar",
        client=fake,
        config=phase_two_config(),
    )
    context = InvocationContext.create(
        "run-1",
        session_id="s" * 33,
        user_id="demo-user",
        actor_id="demo-actor",
    )
    prompt = build_investigation_prompt(
        "Investigate the VPN failure",
        scenario_run_id="run-1",
        incident_id="INC-0001",
    )

    observed_events = []
    result = client.invoke(context, prompt, on_event=observed_events.append)

    request = fake.calls[0]
    assert request["harnessArn"].endswith("harness/northstar")
    assert request["runtimeSessionId"] == "s" * 33
    assert request["runtimeUserId"] == "demo-user"
    assert request["actorId"] == "demo-actor"
    assert request["messages"][0]["content"][0]["text"] == prompt
    assert request["allowedTools"] == phase_two_config()["allowedTools"]
    assert result.succeeded is True
    assert result.text == "Investigating"
    assert result.stop_reason == "end_turn"
    assert result.usage == {"inputTokens": 10, "outputTokens": 5}
    assert result.metrics == {"latencyMs": 12}
    assert result.context.scenario_run_id == "run-1"
    assert all(event.session_id == context.session_id for event in result.events)
    assert all(event.request_id == context.request_id for event in result.events)
    assert any(tool["name"] == "northstar-simulator___check_service_health" for tool in result.tool_uses)
    assert observed_events == list(result.events)
    assert result.turn_count == 1


def test_invocation_context_rejects_short_runtime_session_id() -> None:
    with pytest.raises(HarnessInvocationError, match="33 characters"):
        InvocationContext.create("run-1", session_id="too-short")


def test_event_stream_runtime_error_is_normalized_without_escaping() -> None:
    client = AgentCoreHarnessClient(
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/northstar",
        client=FailingStreamHarnessClient(),
    )

    result = client.invoke(
        InvocationContext.create("run-1", session_id="s" * 33),
        "Inspect the synthetic service health.",
    )

    assert result.succeeded is False
    assert len(result.events) == 2
    assert result.errors == (
        {
            "event_type": "runtimeClientError",
            "errorCode": "runtimeClientError",
            "message": "Model stopped generating due to maximum token limit",
            "stopReason": "runtime_client_error",
        },
    )


def test_investigation_prompt_binds_the_authoritative_run_context() -> None:
    prompt = build_investigation_prompt(
        "Find the affected devices",
        scenario_run_id="run-42",
        incident_id="INC-0007",
    )

    assert "Scenario run ID: run-42" in prompt
    assert "Incident ID: INC-0007" in prompt
    assert "read-only" in prompt
    assert "Proposed change:" in prompt
    assert "Approval required:" in prompt
    assert "Execution status: Not executed" in prompt


def test_extract_recommendation_exposes_the_approval_decision() -> None:
    recommendation = extract_recommendation(
        """### Recommendation
        - Proposed change: Restart the Payments Platform service.
        - Scope: svc-payments for incident INC-0003.
        - Approval required: Yes — this changes service state.
        - Evidence: Service health is failed while dependencies are operational.
        - Execution status: Not executed
        """
    )

    assert recommendation["proposed_change"] == "Restart the Payments Platform service."
    assert recommendation["scope"] == "svc-payments for incident INC-0003."
    assert recommendation["approval_required_yes"] is True
    assert recommendation["actionable"] is True
    assert recommendation["execution_status"] == "Not executed"


def test_extract_recommendation_does_not_invent_a_change() -> None:
    recommendation = extract_recommendation(
        "The next safe step is to collect more read-only evidence. No remediation has been proposed or executed."
    )

    assert recommendation["proposed_change"] == "No change recommended by the agent."
    assert recommendation["actionable"] is False
    assert recommendation["approval_required_yes"] is False


def test_approval_resumption_prompt_preserves_run_and_forbids_second_mutation() -> None:
    prompt = build_approval_resumption_prompt(
        "Investigate and remediate the VPN incident",
        scenario_run_id="run-42",
        approval_id="APR-42",
        operation_id="OP-42",
        action="rollback_vpn_deployment",
    )

    assert "Scenario run ID: run-42" in prompt
    assert "Approval ID: APR-42" in prompt
    assert "Operation ID: OP-42" in prompt
    assert "Do not execute or propose a mutation" in prompt
    assert "read-only simulator tools" in prompt


def test_runtime_binding_preserves_existing_lambda_environment(tmp_path) -> None:
    context_path = tmp_path / "deployment-context.json"
    context_path.write_text(
        '{"CF_OUT_SimulatorFunctionArn":"arn:aws:lambda:us-east-1:123:function:northstar"}\n',
        encoding="utf-8",
    )

    class FakeLambdaClient:
        def __init__(self) -> None:
            self.updated = None

        def get_function_configuration(self, **kwargs):
            assert kwargs["FunctionName"].endswith(":northstar")
            return {"Environment": {"Variables": {"NORTHSTAR_ENVIRONMENT": "dev"}}}

        def update_function_configuration(self, **kwargs):
            self.updated = kwargs
            return kwargs

    fake = FakeLambdaClient()
    provisioned = ProvisionedAgentCore(
        environment="dev",
        region="us-east-1",
        gateway_id="gateway-id",
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:123:gateway/gateway-id",
        gateway_url=None,
        gateway_target_id="target-id",
        harness_id="harness-id",
        harness_arn="arn:aws:bedrock-agentcore:us-east-1:123:harness/harness-id",
        harness_version="5",
        skills_bucket="skills-bucket",
        skills_prefix="northstar/dev/skills/v1",
    )

    binding = bind_simulator_harness_runtime(
        provisioned,
        context_path=context_path,
        model_id="us.amazon.nova-2-lite-v1:0",
        region="us-east-1",
        lambda_client=fake,
    )

    assert binding["harness_arn"] == provisioned.harness_arn
    variables = fake.updated["Environment"]["Variables"]
    assert variables["NORTHSTAR_ENVIRONMENT"] == "dev"
    assert variables["NORTHSTAR_HARNESS_ARN"] == provisioned.harness_arn
    assert variables["NORTHSTAR_GATEWAY_ARN"] == provisioned.gateway_arn
    saved_context = json.loads(context_path.read_text(encoding="utf-8"))
    assert saved_context["CF_OUT_HarnessArn"] == provisioned.harness_arn
