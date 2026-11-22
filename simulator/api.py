"""HTTP routes for the local synthetic environment and guarded operations."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from apps.api.auth import Principal, require_admin, require_http_auth
from .fixtures import load_scenarios
from .operations import OperationError
from .persistence import DynamoDBRunRepository
from .store import SimulatorError, SimulatorStore
from .tools import ReadOnlyToolset


def _build_store() -> SimulatorStore:
    table_name = os.environ.get("NORTHSTAR_STATE_TABLE_NAME", "").strip()
    if not table_name:
        return SimulatorStore()
    return SimulatorStore(
        persistence=DynamoDBRunRepository(
            table_name,
            region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        )
    )


store = _build_store()
tools = ReadOnlyToolset(store)
try:
    store.get_run("demo-vpn")
except SimulatorError:
    store.create_run("vpn-deployment-failure", run_id="demo-vpn")

router = APIRouter(prefix="/simulator", tags=["simulator"])


def _raise_http(exc: Exception) -> None:
    code = getattr(exc, "code", "")
    if code in {"RUN_NOT_FOUND", "ENTITY_NOT_FOUND", "APPROVAL_NOT_FOUND"}:
        status_code = 404
    elif code == "RUN_ACCESS_DENIED":
        status_code = 403
    elif code == "APPROVAL_EXPIRED":
        status_code = 410
    elif isinstance(exc, OperationError):
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(status_code=status_code, detail=str(exc)) from exc


def _tenant_id(principal: Principal | None) -> str:
    if principal is None:
        return "demo"
    return str(principal.claims.get("custom:tenant_id") or "demo").strip() or "demo"


def _actor_id(principal: Principal | None) -> str | None:
    return principal.subject if principal else None


def _authorized_run(run_id: str, principal: Principal | None) -> None:
    try:
        store.get_run_for_actor(run_id, actor_id=_actor_id(principal), tenant_id=_tenant_id(principal))
    except SimulatorError as exc:
        _raise_http(exc)


@router.get("/scenarios")
def list_scenarios() -> list[dict[str, Any]]:
    return [asdict(definition) for definition in load_scenarios().values()]


@router.get("/runs")
def list_runs(principal: Principal | None = Depends(require_http_auth)) -> list[dict[str, Any]]:
    return store.list_runs(actor_id=_actor_id(principal), tenant_id=_tenant_id(principal))


@router.post("/runs")
def create_run(
    payload: dict[str, Any],
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    scenario_id = str(payload.get("scenario_id") or "").strip()
    if not scenario_id:
        raise HTTPException(status_code=422, detail="scenario_id is required")
    try:
        run = store.create_run(
            scenario_id,
            run_id=str(payload["run_id"]) if payload.get("run_id") else None,
            seed=int(payload.get("seed", 20261120)),
            actor_id=_actor_id(principal),
            tenant_id=_tenant_id(principal),
        )
    except (KeyError, ValueError, SimulatorError) as exc:
        _raise_http(exc)
    return run.snapshot()


@router.get("/runs/{run_id}/summary")
def get_run_summary(
    run_id: str,
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    try:
        _authorized_run(run_id, principal)
        return store.summary(run_id)
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.get("/runs/{run_id}/compare")
def compare_run(run_id: str, principal: Principal | None = Depends(require_http_auth)) -> dict[str, Any]:
    try:
        _authorized_run(run_id, principal)
        return store.comparison(run_id)
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.get("/runs/{run_id}/topology")
def get_topology(run_id: str, principal: Principal | None = Depends(require_http_auth)) -> dict[str, Any]:
    try:
        _authorized_run(run_id, principal)
        return store.topology(run_id)
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.get("/runs/{run_id}/entities/{entity_type}/{entity_id}")
def get_entity(
    run_id: str,
    entity_type: str,
    entity_id: str,
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    try:
        _authorized_run(run_id, principal)
        return store.entity(run_id, entity_type, entity_id)
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.get("/runs/{run_id}/events")
def list_events(
    run_id: str,
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    try:
        _authorized_run(run_id, principal)
        return store.events_since(run_id, after_sequence=after_sequence, limit=limit)
    except (SimulatorError, ValueError) as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.post("/runs/{run_id}/operations/proposals", status_code=201)
def propose_operation(
    run_id: str,
    payload: dict[str, Any],
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip()
    target_ids = payload.get("target_ids")
    operation_payload = payload.get("payload") or {}
    if not action or not isinstance(target_ids, list) or not isinstance(operation_payload, dict):
        raise HTTPException(
            status_code=422,
            detail="action, target_ids (array), and payload (object) are required",
        )
    try:
        ttl_seconds = int(payload.get("ttl_seconds", 900))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="ttl_seconds must be an integer") from exc
    requested_by = principal.username if principal else str(payload.get("requested_by") or "local-operator")
    try:
        _authorized_run(run_id, principal)
        return store.propose_operation(
            run_id,
            requested_by=requested_by,
            action=action,
            target_ids=[str(item) for item in target_ids],
            payload=operation_payload,
            idempotency_key=str(payload.get("idempotency_key") or ""),
            ttl_seconds=ttl_seconds,
        )
    except (SimulatorError, OperationError) as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.get("/runs/{run_id}/approvals")
def list_approvals(
    run_id: str,
    principal: Principal | None = Depends(require_http_auth),
) -> list[dict[str, Any]]:
    try:
        _authorized_run(run_id, principal)
        return store.list_approvals(run_id)
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.post("/runs/{run_id}/approvals/{approval_id}/decision")
def decide_approval(
    run_id: str,
    approval_id: str,
    payload: dict[str, Any],
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    decision = str(payload.get("decision") or "").strip()
    if not decision:
        raise HTTPException(status_code=422, detail="decision is required")
    try:
        _authorized_run(run_id, principal)
        return store.decide_approval(
            run_id,
            approval_id=approval_id,
            decided_by=principal.username,
            decision=decision,
            reason=str(payload["reason"]).strip() if payload.get("reason") else None,
        )
    except (SimulatorError, OperationError) as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.post("/runs/{run_id}/approvals/{approval_id}/execute")
def execute_approval(
    run_id: str,
    approval_id: str,
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    executed_by = principal.username if principal else "local-operator"
    try:
        _authorized_run(run_id, principal)
        return store.execute_operation(run_id, approval_id=approval_id, executed_by=executed_by)
    except (SimulatorError, OperationError) as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


def _event_sse(event: dict[str, Any]) -> str:
    return f"event: simulator_event\ndata: {json.dumps(event, sort_keys=True)}\n\n"


def _stream_run_events(
    run_id: str,
    *,
    after_sequence: int,
    poll_seconds: float,
    max_wait_seconds: float,
) -> Any:
    cursor = after_sequence
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        page = store.events_since(run_id, after_sequence=cursor, limit=500)
        events = page["events"]
        if events:
            for event in events:
                cursor = int(event["sequence"])
                yield _event_sse(event)
            continue
        yield ": heartbeat\n\n"
        time.sleep(poll_seconds)
    yield f"event: complete\ndata: {json.dumps({'run_id': run_id, 'after_sequence': cursor})}\n\n"


@router.get("/runs/{run_id}/events/stream")
def stream_run_events(
    run_id: str,
    after_sequence: int = Query(0, ge=0),
    poll_seconds: float = Query(0.25, gt=0, le=2),
    max_wait_seconds: float = Query(20, gt=0, le=60),
    principal: Principal | None = Depends(require_http_auth),
) -> StreamingResponse:
    try:
        _authorized_run(run_id, principal)
    except SimulatorError as exc:
        _raise_http(exc)
    return StreamingResponse(
        _stream_run_events(
            run_id,
            after_sequence=after_sequence,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}")
def get_run(run_id: str, principal: Principal | None = Depends(require_http_auth)) -> dict[str, Any]:
    try:
        _authorized_run(run_id, principal)
        return store.get_run(run_id).snapshot()
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.post("/runs/{run_id}/reset")
def reset_run(run_id: str, principal: Principal | None = Depends(require_http_auth)) -> dict[str, Any]:
    try:
        _authorized_run(run_id, principal)
        return store.reset_run(run_id).snapshot()
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.post("/runs/{run_id}/faults")
def inject_fault(
    run_id: str,
    payload: dict[str, Any],
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    fault_type = str(payload.get("fault_type") or "").strip()
    target_id = str(payload.get("target_id") or "").strip()
    if not fault_type or not target_id:
        raise HTTPException(status_code=422, detail="fault_type and target_id are required")
    try:
        _authorized_run(run_id, principal)
        return store.inject_fault(run_id, fault_type=fault_type, target_id=target_id).snapshot()
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.delete("/runs/{run_id}/faults")
def clear_fault(
    run_id: str,
    payload: dict[str, Any],
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    fault_type = str(payload.get("fault_type") or "").strip()
    target_id = str(payload.get("target_id") or "").strip()
    if not fault_type or not target_id:
        raise HTTPException(status_code=422, detail="fault_type and target_id are required")
    try:
        _authorized_run(run_id, principal)
        return store.clear_fault(run_id, fault_type=fault_type, target_id=target_id).snapshot()
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.post("/runs/{run_id}/checkpoint")
def save_checkpoint(
    run_id: str,
    payload: dict[str, Any],
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    actor_id = _actor_id(principal) or "local-operator"
    try:
        return store.save_checkpoint(
            run_id,
            actor_id=actor_id,
            tenant_id=_tenant_id(principal),
            objective=str(payload.get("objective") or ""),
            completed_steps=[str(item) for item in payload.get("completed_steps", [])],
            pending_tasks=[str(item) for item in payload.get("pending_tasks", [])],
            unresolved_target_ids=[str(item) for item in payload.get("unresolved_target_ids", [])],
            verified_outcomes=[str(item) for item in payload.get("verified_outcomes", [])],
        )
    except (SimulatorError, ValueError, TypeError) as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.get("/runs/{run_id}/checkpoint")
def rehydrate_checkpoint(
    run_id: str,
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    try:
        return store.rehydrate_context(
            run_id,
            actor_id=_actor_id(principal) or "local-operator",
            tenant_id=_tenant_id(principal),
        )
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.post("/runs/{run_id}/checkpoint/compact")
def compact_checkpoint(
    run_id: str,
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    try:
        return store.compact_checkpoint(
            run_id,
            actor_id=_actor_id(principal) or "local-operator",
            tenant_id=_tenant_id(principal),
        )
    except SimulatorError as exc:
        _raise_http(exc)
    raise AssertionError("unreachable")


@router.get("/runs/{run_id}/tools/check-service-health")
def check_service_health(
    run_id: str,
    service_id: str = "svc-vpn-gateway",
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    _authorized_run(run_id, principal)
    return tools.check_service_health(run_id, service_id=service_id).to_dict()


@router.get("/runs/{run_id}/tools/get-device-diagnostics")
def get_device_diagnostics(
    run_id: str,
    device_id: str = Query(...),
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    _authorized_run(run_id, principal)
    return tools.get_device_diagnostics(run_id, device_id=device_id).to_dict()


@router.get("/runs/{run_id}/tools/list-recent-deployments")
def list_recent_deployments(
    run_id: str,
    application_id: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    _authorized_run(run_id, principal)
    return tools.list_recent_deployments(run_id, application_id=application_id, limit=limit).to_dict()


@router.get("/runs/{run_id}/tools/find-affected-devices")
def find_affected_devices(
    run_id: str,
    incident_id: str = Query(...),
    limit: int = Query(100, ge=1, le=150),
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    _authorized_run(run_id, principal)
    return tools.find_affected_devices(run_id, incident_id=incident_id, limit=limit).to_dict()


@router.get("/runs/{run_id}/tools/get-incident")
def get_incident(
    run_id: str,
    incident_id: str = Query(...),
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    _authorized_run(run_id, principal)
    return tools.get_incident(run_id, incident_id=incident_id).to_dict()


@router.get("/runs/{run_id}/tools/search-knowledge")
def search_knowledge(
    run_id: str,
    query: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=10),
    principal: Principal | None = Depends(require_http_auth),
) -> dict[str, Any]:
    _authorized_run(run_id, principal)
    return tools.search_knowledge(run_id, query=query, limit=limit).to_dict()
