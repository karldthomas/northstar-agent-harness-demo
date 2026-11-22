"""Programmatic AgentCore Harness adapter with stream/event correlation.

This module calls the managed ``InvokeHarness`` API and normalizes its event
stream. It intentionally contains no model selection loop, tool dispatcher, or
retrying model orchestration; those responsibilities remain in AgentCore.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import re
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4

from botocore.exceptions import EventStreamError

from .configuration import invoke_overrides
from .prompt_management import RenderedPrompt, render_runtime_prompt


class HarnessInvocationError(RuntimeError):
    """Raised when the Harness data-plane call cannot be started."""


@dataclass(frozen=True, slots=True)
class InvocationContext:
    scenario_run_id: str
    session_id: str
    request_id: str
    trace_id: str
    user_id: str | None = None
    actor_id: str | None = None
    tenant_id: str = "demo"
    gateway_context_token: str | None = None

    @classmethod
    def create(
        cls,
        scenario_run_id: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        actor_id: str | None = None,
        tenant_id: str = "demo",
        gateway_context_token: str | None = None,
    ) -> "InvocationContext":
        normalized_tenant = str(tenant_id or "demo").strip() or "demo"
        normalized_actor = str(actor_id or user_id or "anonymous").strip() or "anonymous"
        binding = hashlib.sha256(
            f"{normalized_tenant}:{normalized_actor}:{scenario_run_id}".encode("utf-8")
        ).hexdigest()[:24]
        supplied_session = str(session_id or "").strip()
        if supplied_session and len(supplied_session) < 33:
            raise HarnessInvocationError("runtimeSessionId must contain at least 33 characters")
        managed_match = re.fullmatch(r"northstar-v1-([0-9a-f]{24})-([0-9a-f]{32})", supplied_session)
        if managed_match:
            if managed_match.group(1) != binding:
                raise HarnessInvocationError("runtimeSessionId is bound to a different scenario run or actor")
            resolved_session = supplied_session
        elif supplied_session:
            # Preserve caller continuity while deriving an AgentCore session
            # that cannot be reused across runs or tenants.
            suffix = hashlib.sha256(supplied_session.encode("utf-8")).hexdigest()[:32]
            resolved_session = f"northstar-v1-{binding}-{suffix}"
        else:
            resolved_session = f"northstar-v1-{binding}-{uuid4().hex}"
        if len(resolved_session) < 33:
            raise HarnessInvocationError("runtimeSessionId must contain at least 33 characters")
        return cls(
            scenario_run_id=scenario_run_id,
            session_id=resolved_session,
            request_id=f"hreq-{uuid4().hex}",
            trace_id=uuid4().hex,
            user_id=user_id,
            actor_id=actor_id,
            tenant_id=normalized_tenant,
            gateway_context_token=gateway_context_token,
        )


@dataclass(frozen=True, slots=True)
class HarnessEvent:
    event_type: str
    sequence: int
    scenario_run_id: str
    session_id: str
    request_id: str
    trace_id: str
    payload: dict[str, Any]
    occurred_at: str

    @property
    def tool_use(self) -> dict[str, Any] | None:
        if self.event_type == "contentBlockStart":
            return self.payload.get("start", {}).get("toolUse")
        if self.event_type == "contentBlockDelta":
            return self.payload.get("delta", {}).get("toolUse")
        return None

    @property
    def text_delta(self) -> str:
        if self.event_type != "contentBlockDelta":
            return ""
        return str(self.payload.get("delta", {}).get("text") or "")


@dataclass(frozen=True, slots=True)
class HarnessInvocationResult:
    context: InvocationContext
    text: str
    stop_reason: str | None
    events: tuple[HarnessEvent, ...]
    tool_uses: tuple[dict[str, Any], ...]
    usage: dict[str, Any]
    metrics: dict[str, Any]
    errors: tuple[dict[str, Any], ...]

    @property
    def succeeded(self) -> bool:
        return not self.errors and self.stop_reason not in {"runtime_client_error", "internal_server_exception", "validation_exception"}

    @property
    def turn_count(self) -> int:
        """Count managed assistant turns without implementing an agent loop."""

        return sum(event.event_type == "messageStart" for event in self.events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_run_id": self.context.scenario_run_id,
            "session_id": self.context.session_id,
            "request_id": self.context.request_id,
            "trace_id": self.context.trace_id,
            "text": self.text,
            "recommendation": extract_recommendation(self.text),
            "stop_reason": self.stop_reason,
            "succeeded": self.succeeded,
            "tool_uses": list(self.tool_uses),
            "turn_count": self.turn_count,
            "usage": self.usage,
            "metrics": self.metrics,
            "errors": list(self.errors),
            "event_count": len(self.events),
        }


_RECOMMENDATION_LABELS = (
    "Proposed change",
    "Scope",
    "Approval required",
    "Evidence",
    "Execution status",
)


def _clean_recommendation_value(value: str) -> str:
    """Remove presentation markdown while preserving the model's meaning."""

    return re.sub(r"\s+", " ", value.replace("**", "").replace("`", "")).strip()


def _is_no_change(value: str) -> bool:
    normalized = value.casefold()
    return (
        not normalized
        or normalized in {"none", "n/a", "not applicable"}
        or bool(
            re.search(
                r"\b(?:none|no (?:safe )?change|no remediation|not proposed|not recommended|"
                r"further investigation required before remediation)\b",
                normalized,
            )
        )
    )


def extract_recommendation(text: str) -> dict[str, Any]:
    """Normalize report prose for the human approval surface.

    Harness returns model prose, so the application keeps the raw report but
    also exposes the exact recommendation fields when the model follows the
    requested format. Older or incomplete reports remain safe: they are shown
    as having no actionable recommendation rather than being converted into a
    mutation request from scenario metadata.
    """

    fields: dict[str, str] = {}
    labels = "|".join(re.escape(label) for label in _RECOMMENDATION_LABELS)
    pattern = re.compile(
        rf"^\s*(?:[-*]\s+)?(?:#{{1,6}}\s*)?(?:\*\*)?({labels})(?:\*\*)?\s*:\s*(.*?)\s*$",
        re.IGNORECASE,
    )
    current: str | None = None
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            current = match.group(1).casefold()
            fields[current] = _clean_recommendation_value(match.group(2))
        elif current and line.strip():
            continuation = _clean_recommendation_value(line)
            fields[current] = f"{fields[current]} {continuation}".strip()

    proposed = fields.get("proposed change", "").strip()
    structured = bool(fields)
    if not proposed and re.search(
        r"\b(?:no remediation (?:(?:has been )?(?:proposed|recommended)|attempted)|no change is recommended|"
        r"no remediation proposed|no state-changing action)\b",
        text,
        re.IGNORECASE,
    ):
        proposed = "No change recommended by the agent."

    actionable = bool(proposed) and not _is_no_change(proposed)
    if not proposed:
        proposed = "No explicit change recommendation was provided."

    scope = fields.get("scope") or "Not specified by the agent."
    approval_required = fields.get("approval required")
    if not approval_required:
        approval_required = (
            "Not explicitly stated by the agent."
            if actionable
            else "No — the agent did not recommend a state-changing action."
        )
    evidence = fields.get("evidence") or "See the full investigation report for the supporting observations."
    execution_status = fields.get("execution status") or "Not executed (read-only investigation)."
    approval_required_yes = bool(re.match(r"^(?:yes|true)\b", approval_required, re.IGNORECASE))

    return {
        "proposed_change": proposed,
        "scope": scope,
        "approval_required": approval_required,
        "approval_required_yes": approval_required_yes,
        "evidence": evidence,
        "execution_status": execution_status,
        "actionable": actionable,
        "structured": structured,
    }


def render_investigation_prompt(
    objective: str,
    *,
    scenario_run_id: str,
    incident_id: str | None = None,
    gateway_context_token: str | None = None,
) -> RenderedPrompt:
    """Render the managed investigation prompt for one isolated simulator run."""

    return render_runtime_prompt(
        "investigation",
        {
            "objective": objective,
            "scenario_run_id": scenario_run_id,
            "incident_id": incident_id,
            "gateway_context_token": gateway_context_token or "",
        },
    )


def build_investigation_prompt(
    objective: str,
    *,
    scenario_run_id: str,
    incident_id: str | None = None,
    gateway_context_token: str | None = None,
) -> str:
    """Compatibility wrapper returning the rendered managed prompt text."""

    return render_investigation_prompt(
        objective,
        scenario_run_id=scenario_run_id,
        incident_id=incident_id,
        gateway_context_token=gateway_context_token,
    ).text


def render_approval_resumption_prompt(
    objective: str,
    *,
    scenario_run_id: str,
    approval_id: str,
    operation_id: str,
    action: str,
    gateway_context_token: str | None = None,
) -> RenderedPrompt:
    """Render the managed read-only resumption prompt after an API-owned mutation."""

    return render_runtime_prompt(
        "approval_resumption",
        {
            "objective": objective,
            "scenario_run_id": scenario_run_id,
            "approval_id": approval_id,
            "operation_id": operation_id,
            "action": action,
            "gateway_context_token": gateway_context_token or "",
        },
    )


def build_approval_resumption_prompt(
    objective: str,
    *,
    scenario_run_id: str,
    approval_id: str,
    operation_id: str,
    action: str,
    gateway_context_token: str | None = None,
) -> str:
    """Compatibility wrapper returning the rendered managed prompt text."""

    return render_approval_resumption_prompt(
        objective,
        scenario_run_id=scenario_run_id,
        approval_id=approval_id,
        operation_id=operation_id,
        action=action,
        gateway_context_token=gateway_context_token,
    ).text


def render_evaluation_prompt(
    objective: str,
    *,
    scenario_run_id: str,
    incident_id: str | None = None,
    required_tools: tuple[str, ...] = (),
    gateway_context_token: str | None = None,
) -> RenderedPrompt:
    """Render the managed bounded read-only evaluation prompt."""

    return render_runtime_prompt(
        "evaluation",
        {
            "objective": objective,
            "scenario_run_id": scenario_run_id,
            "incident_id": incident_id,
            "required_tools": list(required_tools),
            "required_tools_text": ", ".join(required_tools),
            "gateway_context_token": gateway_context_token or "",
        },
    )


class AgentCoreHarnessClient:
    """Thin wrapper around the AWS SDK's managed Harness invocation API."""

    def __init__(
        self,
        harness_arn: str,
        *,
        region: str | None = None,
        client: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        if not harness_arn.strip():
            raise HarnessInvocationError("a Harness ARN is required")
        self.harness_arn = harness_arn.strip()
        self.region = region
        self.config = config
        if client is not None:
            self.client = client
        else:
            try:
                import boto3
            except ImportError as exc:
                raise HarnessInvocationError("boto3 is required for AgentCore Harness invocation") from exc
            self.client = boto3.client("bedrock-agentcore", region_name=region)

    def _request_kwargs(
        self,
        context: InvocationContext,
        prompt: str,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(context.session_id) < 33:
            raise HarnessInvocationError("runtimeSessionId must contain at least 33 characters")
        kwargs: dict[str, Any] = {
            "harnessArn": self.harness_arn,
            "runtimeSessionId": context.session_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "traceId": context.trace_id,
        }
        if context.user_id:
            kwargs["runtimeUserId"] = context.user_id
        if context.actor_id:
            kwargs["actorId"] = context.actor_id
        if self.config:
            kwargs.update(invoke_overrides(self.config))
        if overrides:
            kwargs.update(dict(overrides))
        return kwargs

    def stream(
        self,
        context: InvocationContext,
        prompt: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        on_event: Callable[[HarnessEvent], None] | None = None,
    ) -> Iterator[HarnessEvent]:
        try:
            response = self.client.invoke_harness(**self._request_kwargs(context, prompt, overrides=overrides))
        except Exception as exc:
            raise HarnessInvocationError(f"InvokeHarness could not start: {exc}") from exc
        stream = response.get("stream") if isinstance(response, dict) else None
        if stream is None:
            raise HarnessInvocationError("InvokeHarness response did not contain a stream")
        sequence = 0
        try:
            for raw_event in stream:
                sequence += 1
                if not isinstance(raw_event, dict) or not raw_event:
                    continue
                event_type, payload = next(iter(raw_event.items()))
                if not isinstance(payload, dict):
                    payload = {"value": payload}
                event = HarnessEvent(
                    event_type=str(event_type),
                    sequence=sequence,
                    scenario_run_id=context.scenario_run_id,
                    session_id=context.session_id,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    payload=payload,
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                )
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        # Observability is deliberately best effort. A
                        # persistence or serialization failure must not turn a
                        # managed Harness invocation into an agent failure.
                        pass
                yield event
        except EventStreamError as exc:
            error_response = getattr(exc, "response", {})
            error = error_response.get("Error", {}) if isinstance(error_response, Mapping) else {}
            error_code = str(error.get("Code") or "runtimeClientError")
            message = str(error.get("Message") or str(exc))
            sequence += 1
            event = HarnessEvent(
                event_type=(
                    error_code
                    if error_code in {"runtimeClientError", "internalServerException", "validationException"}
                    else "runtimeClientError"
                ),
                sequence=sequence,
                scenario_run_id=context.scenario_run_id,
                session_id=context.session_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                payload={
                    "errorCode": error_code,
                    "message": message,
                    "stopReason": "runtime_client_error",
                },
                occurred_at=datetime.now(timezone.utc).isoformat(),
            )
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    pass
            yield event

    def invoke(
        self,
        context: InvocationContext,
        prompt: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        on_event: Callable[[HarnessEvent], None] | None = None,
    ) -> HarnessInvocationResult:
        events = tuple(self.stream(context, prompt, overrides=overrides, on_event=on_event))
        text = "".join(event.text_delta for event in events)
        stop_reason = next(
            (
                str(event.payload.get("stopReason"))
                for event in reversed(events)
                if event.event_type == "messageStop" and event.payload.get("stopReason")
            ),
            None,
        )
        tool_uses: list[dict[str, Any]] = []
        for event in events:
            use = event.tool_use
            if use:
                tool_uses.append(dict(use))
        usage = next(
            (dict(event.payload.get("usage", {})) for event in reversed(events) if event.event_type == "metadata"),
            {},
        )
        metrics = next(
            (dict(event.payload.get("metrics", {})) for event in reversed(events) if event.event_type == "metadata"),
            {},
        )
        errors = tuple(
            {"event_type": event.event_type, **event.payload}
            for event in events
            if event.event_type in {"runtimeClientError", "internalServerException", "validationException"}
        )
        return HarnessInvocationResult(
            context=context,
            text=text,
            stop_reason=stop_reason,
            events=events,
            tool_uses=tuple(tool_uses),
            usage=usage,
            metrics=metrics,
            errors=errors,
        )
