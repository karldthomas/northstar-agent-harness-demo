"""Cognito-admin-only HTTP routes for the Northstar Prompt Library."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.api.auth import Principal, require_admin

from .prompt_management import PromptManagementError, PromptManager


router = APIRouter(prefix="/admin/prompts", tags=["prompt-management"])


class PromptUpdateRequest(BaseModel):
    description: str = Field(default="", max_length=200)
    system: str = ""
    user: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    max_tokens: int = Field(default=1000, ge=1)
    temperature: float = Field(default=0, ge=0, le=2)


class PromptVersionRequest(BaseModel):
    description: str = Field(default="Created from the Bedrock draft.", max_length=200)


class PromptActivationRequest(BaseModel):
    version: str = Field(min_length=1)


class PromptPreviewRequest(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    version: str | None = None


def _manager() -> PromptManager:
    try:
        return PromptManager()
    except PromptManagementError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _run(operation: Any) -> Any:
    try:
        return operation()
    except PromptManagementError as exc:
        message = str(exc)
        status = 503 if message.startswith(("could not", "Bedrock", "boto3")) else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.get("")
def list_prompts(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    return _run(lambda: _manager().list_prompts())


@router.put("/{prompt_key}")
def update_prompt(
    prompt_key: str,
    payload: PromptUpdateRequest,
    _: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return _run(lambda: _manager().update_draft(prompt_key, payload.model_dump()))


@router.post("/{prompt_key}/versions", status_code=201)
def create_prompt_version(
    prompt_key: str,
    payload: PromptVersionRequest,
    _: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return _run(lambda: _manager().create_version(prompt_key, payload.description))


@router.post("/{prompt_key}/active")
def activate_prompt_version(
    prompt_key: str,
    payload: PromptActivationRequest,
    _: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return _run(lambda: _manager().activate(prompt_key, payload.version))


@router.post("/{prompt_key}/preview")
def preview_prompt(
    prompt_key: str,
    payload: PromptPreviewRequest,
    _: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return _run(lambda: _manager().preview(prompt_key, payload.variables, version=payload.version))
