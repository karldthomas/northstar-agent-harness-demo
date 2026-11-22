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
    validate_harness_config,
)
from simulator.api import store
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
    return render_investigation_prompt(
        request.objective,
        scenario_run_id=request.scenario_run_id,
        incident_id=request.incident_id,
        gateway_context_token=context.gateway_context_token,
    )


def _render_prompt(request: HarnessInvocationRequest, context: InvocationContext) -> RenderedPrompt:
    try:
        return _prompt(request, context)
    except PromptManagementError as exc:
        raise HTTPException(status_code=503, detail=f"Managed prompt is unavailable: {exc}") from exc


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
