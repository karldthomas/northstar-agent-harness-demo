"""Managed Harness lifecycle-hook decision function.

This hook is intentionally narrower than the simulator's direct HTTP API. It
guards the currently deployed Harness selector: only the six read-only
Gateway operations may run, and every call must carry an isolated scenario
run ID plus its opaque run-bound capability. Approval-gated writes remain
behind the application API until the Harness resumption design is implemented.
"""

from __future__ import annotations

import json
from typing import Any

from harness.configuration import READ_ONLY_TOOL_NAMES
from simulator.api import store
from simulator.store import SimulatorError


def _tool_name(event: dict[str, Any]) -> str:
    context = event.get("context")
    if not isinstance(context, dict):
        return ""
    return str(context.get("toolName") or "").strip()


def _tool_input(event: dict[str, Any]) -> dict[str, Any] | None:
    context = event.get("context")
    if not isinstance(context, dict):
        return None
    value = context.get("toolInput")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def handler(event: dict[str, Any], _context: Any) -> dict[str, str]:
    """Return the AgentCore Harness lifecycle-hook decision."""

    if str(event.get("event") or "") != "before_tool_call":
        return {"decision": "allow", "reason": "non-tool-call lifecycle event"}

    tool_name = _tool_name(event)
    if tool_name not in READ_ONLY_TOOL_NAMES:
        return {"decision": "deny", "reason": "tool is outside the Northstar read-only Harness allow-list"}

    tool_input = _tool_input(event)
    run_id = str(tool_input.get("run_id") or "").strip() if tool_input else ""
    token = str(tool_input.get("gateway_context_token") or "").strip() if tool_input else ""
    if not run_id or not token:
        return {
            "decision": "deny",
            "reason": "every simulator tool call must include its run_id and Gateway context token",
        }
    try:
        store.verify_gateway_context_token(run_id, token)
    except SimulatorError:
        return {"decision": "deny", "reason": "Gateway context token is not valid for this scenario run"}

    return {"decision": "allow", "reason": "read-only simulator tool and run-bound Gateway context accepted"}
