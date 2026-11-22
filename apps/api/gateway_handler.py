"""Read-only Lambda target adapter for the AgentCore Gateway.

The HTTP API and the Gateway target share the same authoritative simulator
toolset. The Gateway remains intentionally read-only while the HTTP API owns
approval-gated synthetic operations. This adapter only translates the Gateway
Lambda-target event shape; it does not select tools or implement a model loop.
"""

from __future__ import annotations

import json
from typing import Any

from simulator.api import store, tools
from simulator.store import SimulatorError


TOOL_NAMES = {
    "check_service_health",
    "get_device_diagnostics",
    "get_incident",
    "find_affected_devices",
    "list_recent_deployments",
    "search_knowledge",
}


def _agentcore_context(context: Any) -> dict[str, Any]:
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if isinstance(custom, dict):
        return custom
    return {}


def _tool_name(event: dict[str, Any], context: Any) -> str:
    value = (
        event.get("tool_name")
        or event.get("name")
        or event.get("operation")
        or _agentcore_context(context).get("bedrockAgentCoreToolName")
    )
    if not value:
        raise ValueError("Gateway event is missing tool_name")
    name = str(value)
    if "___" in name:
        name = name.rsplit("___", 1)[1]
    if name not in TOOL_NAMES:
        raise ValueError(f"unregistered read-only tool: {name}")
    return name


def _arguments(event: dict[str, Any]) -> dict[str, Any]:
    arguments: Any = event.get("arguments") or event.get("parameters") or event.get("input")
    if arguments is None:
        arguments = event
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("Gateway tool arguments must be an object")
    return dict(arguments)


def _authorize_gateway_run(arguments: dict[str, Any]) -> str:
    """Require the run-bound capability issued by the authenticated API."""

    run_id = str(arguments.get("run_id") or "").strip()
    token = str(arguments.get("gateway_context_token") or "").strip()
    if not run_id:
        raise ValueError("Gateway tool arguments must include run_id")
    if not token:
        raise ValueError("Gateway tool arguments must include gateway_context_token")
    try:
        store.verify_gateway_context_token(run_id, token)
    except SimulatorError as exc:
        raise ValueError("Gateway capability is invalid for the requested scenario run") from exc
    return run_id


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dispatch one Gateway-selected read-only operation."""

    agentcore_context = _agentcore_context(context)
    name = _tool_name(event, context)
    arguments = _arguments(event)
    run_id = _authorize_gateway_run(arguments)
    request_id = str(
        event.get("request_id")
        or agentcore_context.get("bedrockAgentCoreAwsRequestId")
        or getattr(context, "aws_request_id", "")
        or ""
    ) or None
    if name == "check_service_health":
        return tools.check_service_health(
            run_id,
            service_id=str(arguments.get("service_id") or "svc-vpn-gateway"),
            request_id=request_id,
        ).to_dict()
    if name == "get_device_diagnostics":
        return tools.get_device_diagnostics(
            run_id,
            device_id=str(arguments["device_id"]),
            request_id=request_id,
        ).to_dict()
    if name == "get_incident":
        return tools.get_incident(
            run_id,
            incident_id=str(arguments["incident_id"]),
            request_id=request_id,
        ).to_dict()
    if name == "find_affected_devices":
        return tools.find_affected_devices(
            run_id,
            incident_id=str(arguments["incident_id"]),
            limit=int(arguments.get("limit", 100)),
            request_id=request_id,
        ).to_dict()
    if name == "list_recent_deployments":
        application_id = arguments.get("application_id")
        return tools.list_recent_deployments(
            run_id,
            application_id=str(application_id) if application_id else None,
            limit=int(arguments.get("limit", 10)),
            request_id=request_id,
        ).to_dict()
    return tools.search_knowledge(
        run_id,
        query=str(arguments["query"]),
        limit=int(arguments.get("limit", 5)),
        request_id=request_id,
    ).to_dict()
