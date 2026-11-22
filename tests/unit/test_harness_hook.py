from __future__ import annotations

from apps.api.hook_handler import handler


def _event(tool_name: str, tool_input: object) -> dict[str, object]:
    return {
        "event": "before_tool_call",
        "context": {"toolName": tool_name, "toolInput": tool_input},
    }


def test_hook_allows_known_read_only_tool_with_run_id() -> None:
    result = handler(
        _event(
            "northstar-simulator___get_device_diagnostics",
            {"run_id": "run-hook", "device_id": "DEV-0001"},
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
    encoded = handler(
        _event("northstar-simulator___check_service_health", '{"run_id":"run-hook"}'),
        None,
    )
    other_event = handler({"event": "after_invocation", "context": {}}, None)

    assert encoded["decision"] == "allow"
    assert other_event["decision"] == "allow"
