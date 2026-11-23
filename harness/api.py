"""Backend routes for managed AgentCore Harness invocations.

The application owns request validation and correlation only. AgentCore
Harness owns model reasoning, tool selection, and orchestration.
"""

from __future__ import annotations

import json
import inspect
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from apps.api.auth import Principal, require_http_auth
from .agentcore import (
    AgentCoreHarnessClient,
    HarnessEvent,
    HarnessInvocationError,
    InvocationContext,
    render_approval_resumption_prompt,
    render_investigation_prompt,
)
from .prompt_management import PromptManagementError, RenderedPrompt
from .configuration import (
    DEFAULT_CONFIG_PATH,
    HarnessConfigurationError,
    load_harness_config,
    resolve_harness_config,
    skill_guidance_trace,
    validate_harness_config,
)
from simulator.api import store
from simulator.fixtures import scenario_definition
from simulator.store import SimulatorError


router = APIRouter(prefix="/harness", tags=["harness"])


class HarnessInvocationRequest(BaseModel):
    scenario_run_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    incident_id: str | None = None
    session_id: str | None = Field(default=None, min_length=33)
    user_id: str | None = None
    actor_id: str | None = None
    approval_id: str | None = Field(default=None, min_length=1)
    operation_id: str | None = Field(default=None, min_length=1)
    action: str | None = Field(default=None, min_length=1)


def _configuration() -> dict[str, Any]:
    config_path = Path(os.environ.get("NORTHSTAR_HARNESS_CONFIG", str(DEFAULT_CONFIG_PATH)))
    try:
        config = load_harness_config(config_path)
        resolved = resolve_harness_config(config)
    except (OSError, HarnessConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=f"Harness configuration is unavailable: {exc}") from exc
    # Cloud provisioning uploads the versioned skill packages to S3.  The
    # source YAML keeps local paths for development, while the Function URL
    # invocation bridge must send service-readable S3 skill sources.
    skills_bucket = os.environ.get("NORTHSTAR_SKILLS_BUCKET", "").strip()
    skills_prefix = os.environ.get("NORTHSTAR_SKILLS_PREFIX", "").strip().rstrip("/")
    if skills_bucket and skills_prefix:
        resolved["skills"] = [
            {
                "s3": {
                    "uri": f"s3://{skills_bucket}/{skills_prefix}/{Path(skill['path']).name}/"
                }
            }
            for skill in resolved.get("skills", [])
            if isinstance(skill, dict) and isinstance(skill.get("path"), str)
        ]
    errors = validate_harness_config(resolved)
    if errors:
        raise HTTPException(status_code=503, detail="Invalid Harness configuration: " + "; ".join(errors))
    return resolved


def _client() -> AgentCoreHarnessClient:
    harness_arn = os.environ.get("NORTHSTAR_HARNESS_ARN", "").strip()
    if not harness_arn:
        raise HTTPException(status_code=503, detail="NORTHSTAR_HARNESS_ARN is not configured")
    config = _configuration()
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    try:
        return AgentCoreHarnessClient(harness_arn, region=region, config=config)
    except HarnessInvocationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _context(request: HarnessInvocationRequest, principal: Principal | None = None) -> InvocationContext:
    try:
        tenant_id = (
            str(principal.claims.get("custom:tenant_id") or "demo").strip() or "demo"
            if principal
            else "demo"
        )
        gateway_context_token = store.get_gateway_context_token(
            request.scenario_run_id,
            actor_id=principal.subject if principal else request.actor_id,
            tenant_id=tenant_id,
        )
        return InvocationContext.create(
            request.scenario_run_id,
            session_id=request.session_id,
            user_id=principal.username if principal else request.user_id,
            actor_id=principal.subject if principal else request.actor_id,
            tenant_id=tenant_id,
            gateway_context_token=gateway_context_token,
        )
    except (HarnessInvocationError, SimulatorError) as exc:
        if isinstance(exc, SimulatorError):
            status_code = 403 if getattr(exc, "code", "") == "RUN_ACCESS_DENIED" else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _investigation_context(
    request: HarnessInvocationRequest,
) -> tuple[str | None, dict[str, Any]]:
    """Build compact, authoritative anchors for the managed investigation prompt."""

    run = store.get_run(request.scenario_run_id)
    definition = scenario_definition(run.scenario_id)
    requested_incident_id = str(request.incident_id or "").strip()
    incident_id = requested_incident_id if requested_incident_id in run.state.incidents else None
    if incident_id is None and definition.primary_incident_id in run.state.incidents:
        incident_id = definition.primary_incident_id
    if incident_id is None:
        open_incidents = sorted(
            incident.incident_id
            for incident in run.state.incidents.values()
            if incident.status == "open"
        )
        incident_id = open_incidents[0] if len(open_incidents) == 1 else None

    incident = run.state.incidents.get(incident_id or "")
    service_id = incident.service_id if incident else None
    deployment_id = incident.related_deployment_id if incident else None
    affected_device_ids = list(incident.affected_device_ids) if incident else []
    return incident_id, {
        "scenario_id": run.scenario_id,
        "scenario_name": definition.name,
        "required_read_only_tools": list(definition.read_only_tools),
        "available_service_ids": sorted(run.state.services),
        "open_incident_ids": sorted(
            item.incident_id for item in run.state.incidents.values() if item.status == "open"
        ),
        "investigation_anchors": {
            "primary_incident_id": incident_id,
            "service_id": service_id,
            "related_deployment_id": deployment_id,
            "affected_device_count": len(affected_device_ids),
            "sample_device_id": affected_device_ids[0] if affected_device_ids else None,
        },
    }


def _prompt(request: HarnessInvocationRequest, context: InvocationContext) -> RenderedPrompt:
    resume_fields = (request.approval_id, request.operation_id, request.action)
    if any(value is not None for value in resume_fields):
        if not all(value is not None for value in resume_fields):
            raise HTTPException(
                status_code=422,
                detail="approval_id, operation_id, and action are required together when resuming",
            )
        return render_approval_resumption_prompt(
            request.objective,
            scenario_run_id=request.scenario_run_id,
            approval_id=request.approval_id or "",
            operation_id=request.operation_id or "",
            action=request.action or "",
            gateway_context_token=context.gateway_context_token,
        )
    incident_id, runtime_context = _investigation_context(request)
    return render_investigation_prompt(
        request.objective,
        scenario_run_id=request.scenario_run_id,
        incident_id=incident_id,
        gateway_context_token=context.gateway_context_token,
        runtime_context=runtime_context,
    )


def _render_prompt(request: HarnessInvocationRequest, context: InvocationContext) -> RenderedPrompt:
    try:
        return _prompt(request, context)
    except PromptManagementError as exc:
        raise HTTPException(status_code=503, detail=f"Managed prompt is unavailable: {exc}") from exc


def _skill_selection_payload(
    request: HarnessInvocationRequest,
    rendered_prompt: RenderedPrompt,
) -> dict[str, Any]:
    """Build observability metadata without changing the Harness request."""

    try:
        run = store.get_run(request.scenario_run_id)
        definition = scenario_definition(run.scenario_id)
        config_path = Path(os.environ.get("NORTHSTAR_HARNESS_CONFIG", str(DEFAULT_CONFIG_PATH)))
        config = load_harness_config(config_path)
        bucket = os.environ.get("NORTHSTAR_SKILLS_BUCKET", "").strip()
        prefix = os.environ.get("NORTHSTAR_SKILLS_PREFIX", "").strip().rstrip("/")
        runtime_source_prefix = f"s3://{bucket}/{prefix}" if bucket and prefix else None
        payload = skill_guidance_trace(
            config,
            scenario_profile=definition.profile,
            restricted_actions=definition.restricted_actions,
            resuming=bool(request.approval_id),
            runtime_source_prefix=runtime_source_prefix,
        )
        payload.update(rendered_prompt.metadata())
        payload["invocation_kind"] = "approval_resumption" if request.approval_id else "investigation"
        return payload
    except Exception as exc:
        # Teaching metadata is best effort. A missing local skill document or
        # custom config must never change the managed invocation boundary.
        return {
            **rendered_prompt.metadata(),
            "selection_mode": "unavailable",
            "selected_skill_ids": [],
            "selected_skills": [],
            "available_skill_ids": [],
            "skill_count": 0,
            "invocation_kind": "approval_resumption" if request.approval_id else "investigation",
            "selection_error": type(exc).__name__,
            "note": "Teaching metadata was unavailable; AgentCore Harness invocation was not changed.",
        }


def _authorize_run(request: HarnessInvocationRequest, principal: Principal | None) -> None:
    """Apply the same actor/tenant run boundary to Harness as to simulator APIs."""

    try:
        if principal is None:
            store.get_run(request.scenario_run_id)
            return
        tenant_id = str(principal.claims.get("custom:tenant_id") or "demo").strip() or "demo"
        store.get_run_for_actor(
            request.scenario_run_id,
            actor_id=principal.subject,
            tenant_id=tenant_id,
        )
    except SimulatorError as exc:
        status_code = 403 if getattr(exc, "code", "") == "RUN_ACCESS_DENIED" else 404
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


def _record_harness_lifecycle(
    run_id: str,
    event_type: str,
    *,
    context: InvocationContext,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort correlation of Harness lifecycle state with a simulator run."""

    try:
        store.record_harness_event(
            run_id,
            event_type,
            {
                "session_id": context.session_id,
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                **(payload or {}),
            },
        )
    except Exception:
        # The invocation boundary must still return the managed result if an
        # observability write cannot be completed.
        pass


_SENSITIVE_TOOL_INPUT_KEYS = {
    "password",
    "token",
    "access_token",
    "authorization",
    "gateway_context_token",
    "session_token",
    "id_token",
}


def _redact_tool_input(value: Any) -> Any:
    """Keep display-safe tool arguments without persisting capability tokens."""

    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if str(key).lower() in _SENSITIVE_TOOL_INPUT_KEYS
            else _redact_tool_input(item) if isinstance(item, (dict, list)) else item
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_tool_input(item) if isinstance(item, (dict, list)) else item for item in value]
    if isinstance(value, str):
        try:
            return _redact_tool_input(json.loads(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            # Converse streaming sends partial JSON fragments. They cannot be
            # safely inspected for sensitive fields until complete, so do not
            # persist the fragment itself.
            return "[redacted input fragment]"
    return value


def _record_harness_event(event: HarnessEvent) -> None:
    """Persist only useful lifecycle/tool markers, never model text deltas."""

    tool_use = event.tool_use
    if event.event_type == "messageStart":
        normalized_type = "agent_turn_started"
        payload: dict[str, Any] = {"harness_event_type": event.event_type}
    elif event.event_type == "contentBlockStart" and tool_use:
        normalized_type = "agent_tool_call_requested"
        payload = {
            "harness_event_type": event.event_type,
            "tool_name": tool_use.get("name"),
            "tool_use_id": tool_use.get("toolUseId"),
            "content_block_index": event.payload.get("contentBlockIndex"),
        }
    elif event.event_type == "contentBlockDelta" and tool_use and "input" in tool_use:
        normalized_type = "agent_tool_call_input"
        payload = {
            "harness_event_type": event.event_type,
            "tool_name": tool_use.get("name"),
            "tool_use_id": tool_use.get("toolUseId"),
            "content_block_index": event.payload.get("contentBlockIndex"),
            "tool_input": _redact_tool_input(tool_use.get("input")),
        }
    elif event.event_type == "hookEvent":
        normalized_type = "agent_hook_decision"
        payload = {
            "harness_event_type": event.event_type,
            "hook_event_id": event.payload.get("hookEventId"),
            "hook_name": event.payload.get("name"),
            "hook_type": event.payload.get("type"),
            "decision": event.payload.get("decision"),
            "reason": event.payload.get("reason"),
        }
    elif event.event_type == "messageStop":
        normalized_type = "agent_turn_completed"
        payload = {
            "harness_event_type": event.event_type,
            "stop_reason": event.payload.get("stopReason"),
        }
    elif event.event_type == "metadata":
        normalized_type = "agent_metadata"
        payload = {
            "harness_event_type": event.event_type,
            "usage": event.payload.get("usage", {}),
            "metrics": event.payload.get("metrics", {}),
        }
    elif event.event_type in {"runtimeClientError", "internalServerException", "validationException"}:
        normalized_type = "agent_error"
        payload = {
            "harness_event_type": event.event_type,
            "error_code": event.payload.get("errorCode"),
            "message": event.payload.get("message"),
            "stop_reason": event.payload.get("stopReason"),
        }
    else:
        return

    _record_harness_lifecycle(
        event.scenario_run_id,
        normalized_type,
        context=InvocationContext(
            scenario_run_id=event.scenario_run_id,
            session_id=event.session_id,
            request_id=event.request_id,
            trace_id=event.trace_id,
        ),
        payload={
            "harness_sequence": event.sequence,
            "occurred_at": event.occurred_at,
            **payload,
        },
    )


def _invoke_with_observability(client: Any, context: InvocationContext, prompt: str) -> Any:
    """Invoke a client while remaining compatible with small test doubles."""

    # Inspect the callable before invoking it. Catching TypeError around the
    # call can accidentally execute a real Harness request twice when the
    # first request raises TypeError internally.
    parameters = inspect.signature(client.invoke).parameters
    accepts_callback = "on_event" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_callback:
        return client.invoke(context, prompt, on_event=_record_harness_event)
    return client.invoke(context, prompt)


@router.post("/invocations")
def invoke_harness(
    request: HarnessInvocationRequest,
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    """Invoke managed Harness and return the normalized completed result."""

    _authorize_run(request, principal)
    client = _client()
    context = _context(request, principal)
    rendered_prompt = _render_prompt(request, context)
    _record_harness_lifecycle(
        request.scenario_run_id,
        "skills_selected",
        context=context,
        payload=_skill_selection_payload(request, rendered_prompt),
    )
    _record_harness_lifecycle(
        request.scenario_run_id,
        "agent_invocation_started",
        context=context,
        payload=rendered_prompt.metadata(),
    )
    try:
        result = _invoke_with_observability(client, context, rendered_prompt.text)
    except HarnessInvocationError as exc:
        _record_harness_lifecycle(
            request.scenario_run_id,
            "agent_invocation_failed",
            context=context,
            payload={"error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _record_harness_lifecycle(
        request.scenario_run_id,
        "agent_invocation_completed",
        context=context,
        payload={
            "succeeded": getattr(result, "succeeded", None),
            "stop_reason": getattr(result, "stop_reason", None),
            "tool_call_count": len(getattr(result, "tool_uses", ()) or ()),
            "turn_count": getattr(result, "turn_count", None),
            **rendered_prompt.metadata(),
        },
    )
    return result.to_dict()


def _event_sse(event: HarnessEvent) -> str:
    payload = {
        "event_type": event.event_type,
        "sequence": event.sequence,
        "scenario_run_id": event.scenario_run_id,
        "session_id": event.session_id,
        "request_id": event.request_id,
        "trace_id": event.trace_id,
        "occurred_at": event.occurred_at,
        "payload": event.payload,
    }
    return f"event: {event.event_type}\ndata: {json.dumps(payload, sort_keys=True)}\n\n"


def _stream_events(
    client: AgentCoreHarnessClient,
    context: InvocationContext,
    prompt: str,
    prompt_metadata: dict[str, str] | None = None,
) -> Iterator[str]:
    try:
        turn_count = 0
        tool_call_count = 0
        stop_reason: str | None = None
        for event in client.stream(context, prompt):
            if event.event_type == "messageStart":
                turn_count += 1
            if event.tool_use:
                tool_call_count += 1
            if event.event_type == "messageStop":
                stop_reason = event.payload.get("stopReason")
            _record_harness_event(event)
            yield _event_sse(event)
        _record_harness_lifecycle(
            context.scenario_run_id,
            "agent_invocation_completed",
            context=context,
            payload={
                "succeeded": True,
                "stop_reason": stop_reason,
                "tool_call_count": tool_call_count,
                "turn_count": turn_count,
                **(prompt_metadata or {}),
            },
        )
        yield "event: complete\ndata: {\"status\":\"complete\"}\n\n"
    except HarnessInvocationError as exc:
        _record_harness_lifecycle(
            context.scenario_run_id,
            "agent_invocation_failed",
            context=context,
            payload={"error": str(exc), **(prompt_metadata or {})},
        )
        payload = json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True)
        yield f"event: error\ndata: {payload}\n\n"


@router.post("/invocations/stream")
def stream_harness(
    request: HarnessInvocationRequest,
    principal: Principal | None = Depends(require_http_auth),
) -> StreamingResponse:
    """Stream normalized Harness events as server-sent events."""

    _authorize_run(request, principal)
    client = _client()
    context = _context(request, principal)
    rendered_prompt = _render_prompt(request, context)
    skill_selection = _skill_selection_payload(request, rendered_prompt)
    _record_harness_lifecycle(
        request.scenario_run_id,
        "skills_selected",
        context=context,
        payload=skill_selection,
    )
    _record_harness_lifecycle(
        request.scenario_run_id,
        "agent_invocation_started",
        context=context,
        payload=rendered_prompt.metadata(),
    )
    return StreamingResponse(
        _stream_events(client, context, rendered_prompt.text, rendered_prompt.metadata()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
