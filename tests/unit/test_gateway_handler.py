from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from apps.api.gateway_handler import handler
from simulator.api import store
import pytest


def test_gateway_lambda_dispatches_read_only_tool() -> None:
    token = store.get_gateway_context_token("demo-vpn", actor_id=None)
    result = handler(
        {"run_id": "demo-vpn", "gateway_context_token": token, "service_id": "svc-vpn-gateway"},
        SimpleNamespace(
            aws_request_id="lambda-request-1",
            client_context=SimpleNamespace(
                custom={
                    "bedrockAgentCoreToolName": "northstar-simulator___check_service_health",
                    "bedrockAgentCoreAwsRequestId": "gateway-request-1",
                }
            ),
        ),
    )

    assert result["tool_name"] == "check_service_health"
    assert result["scenario_run_id"] == "demo-vpn"
    assert result["status"] == "succeeded"
    assert result["read_only"] is True
    assert result["request_id"] == "gateway-request-1"


def test_gateway_lambda_rehydrates_known_cloud_suite_fixture() -> None:
    run_id = f"cloud-service-outage-unit-{uuid4().hex}"
    store.create_run("service-outage", run_id=run_id)
    token = store.get_gateway_context_token(run_id, actor_id=None)
    result = handler(
        {"run_id": run_id, "gateway_context_token": token, "service_id": "svc-payments"},
        SimpleNamespace(
            aws_request_id="lambda-request-2",
            client_context=SimpleNamespace(
                custom={
                    "bedrockAgentCoreToolName": "northstar-simulator___check_service_health",
                    "bedrockAgentCoreAwsRequestId": "gateway-request-2",
                }
            ),
        ),
    )

    assert result["status"] == "succeeded"
    assert result["scenario_run_id"] == run_id
    assert result["data"]["status"] == "failed"


def test_gateway_capability_cannot_be_reused_for_another_run() -> None:
    owner_run = f"gateway-owner-{uuid4().hex}"
    other_run = f"gateway-other-{uuid4().hex}"
    store.create_run("vpn-deployment-failure", run_id=owner_run)
    store.create_run("service-outage", run_id=other_run)
    token = store.get_gateway_context_token(owner_run, actor_id=None)

    with pytest.raises(ValueError, match="capability is invalid"):
        handler(
            {
                "run_id": other_run,
                "gateway_context_token": token,
                "incident_id": "INC-0003",
            },
            SimpleNamespace(
                aws_request_id="lambda-request-3",
                client_context=SimpleNamespace(
                    custom={"bedrockAgentCoreToolName": "northstar-simulator___get_incident"}
                ),
            ),
        )
