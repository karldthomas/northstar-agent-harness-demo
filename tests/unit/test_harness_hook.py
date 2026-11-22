from __future__ import annotations

from apps.api.hook_handler import handler
from simulator.api import store


def _event(tool_name: str, tool_input: object) -> dict[str, object]:
    return {
        "event": "before_tool_call",
        "context": {"toolName": tool_name, "toolInput": tool_input},
    }


def test_hook_allows_known_read_only_tool_with_run_id() -> None:
    run_id = "demo-vpn"
    result = handler(
        _event(
            "northstar-simulator___get_device_diagnostics",
            {
                "run_id": run_id,
                "gateway_context_token": store.get_gateway_context_token(run_id, actor_id=None),
                "device_id": "DEV-0001",
            },
        ),
        None,
    )

    assert result["decision"] == "allow"


def test_hook_denies_unknown_tool_and_missing_run_id() -> None:
    unknown = handler(_event("northstar-simulator___execute_remediation", {"run_id": "run-hook"}), None)
    missing_run = handler(_event("northstar-simulator___get_incident", {"incident_id": "INC-0001"}), None)

    assert unknown["decision"] == "deny"
    assert missing_run["decision"] == "deny"


def test_hook_accepts_json_tool_input_and_allows_non_tool_events() -> None:
    run_id = "demo-vpn"
    token = store.get_gateway_context_token(run_id, actor_id=None)
    encoded = handler(
        _event(
            "northstar-simulator___check_service_health",
            f'{{"run_id":"{run_id}","gateway_context_token":"{token}"}}',
        ),
        None,
    )
    other_event = handler({"event": "after_invocation", "context": {}}, None)

    assert encoded["decision"] == "allow"
    assert other_event["decision"] == "allow"


def test_hook_denies_a_capability_for_a_different_run() -> None:
    token = store.get_gateway_context_token("demo-vpn", actor_id=None)
    result = handler(
        _event(
            "northstar-simulator___get_incident",
            {"run_id": "run-does-not-exist", "gateway_context_token": token, "incident_id": "INC-0001"},
        ),
        None,
    )

    assert result["decision"] == "deny"
