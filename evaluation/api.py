"""Authenticated read-only API for independent run scorecards."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from apps.api.auth import Principal, require_http_auth
from simulator.api import store
from simulator.store import RunAccessDeniedError, RunNotFoundError

from .runner import evaluate_run


router = APIRouter(prefix="/evaluation", tags=["evaluation"])


def _tenant_id(principal: Principal | None) -> str:
    if principal is None:
        return "demo"
    return str(principal.claims.get("custom:tenant_id") or "demo").strip() or "demo"


@router.get("/runs/{run_id}")
def evaluate_current_run(
    run_id: str,
    principal: Principal | None = Depends(require_http_auth),
) -> dict:
    try:
        run = store.get_run_for_actor(
            run_id,
            actor_id=principal.subject if principal else None,
            tenant_id=_tenant_id(principal),
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return evaluate_run(run, variant="live-run").to_dict()
