from __future__ import annotations

from types import SimpleNamespace

from apps.api.gateway_handler import handler


def test_gateway_lambda_dispatches_read_only_tool() -> None:
    result = handler(
        {"run_id": "demo-vpn", "service_id": "svc-vpn-gateway"},
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
    run_id = "cloud-service-outage-unit"
    result = handler(
        {"run_id": run_id, "service_id": "svc-payments"},
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
