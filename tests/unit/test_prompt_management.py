from __future__ import annotations

import pytest

from apps.api.auth import Principal, _principal_from_claims, get_current_principal
from apps.api.main import app
from fastapi.testclient import TestClient
from harness.prompt_management import (
    PROMPT_DEFINITIONS,
    PromptManagementError,
    PromptManager,
    PromptVersionResolver,
    render_template,
)
import harness.prompt_api as prompt_api


def test_local_prompt_seed_renders_explicit_run_context_without_python_prose() -> None:
    rendered = PromptManager(source="local").render(
        "investigation",
        {
            "objective": "Find the affected devices",
            "scenario_run_id": "run-42",
            "incident_id": "INC-0007",
        },
    )

    assert rendered.source == "local"
    assert rendered.version == "LOCAL"
    assert "Find the affected devices" in rendered.text
    assert "Scenario run ID: run-42" in rendered.text
    assert "Incident ID: INC-0007" in rendered.text
    assert "never pass `all`" in rendered.text
    assert "Proposed change:" in rendered.text


def test_local_prompt_catalog_is_safe_for_admin_preview() -> None:
    catalog = PromptManager(source="local").list_prompts()

    assert [prompt["key"] for prompt in catalog["prompts"]] == [
        "investigation",
        "approval_resumption",
        "evaluation",
    ]
    assert all(prompt["active_version"] == "LOCAL" for prompt in catalog["prompts"])


def test_renderer_rejects_missing_variables_and_does_not_evaluate_content() -> None:
    with pytest.raises(PromptManagementError, match="required"):
        render_template("{{required}}", {})

    assert render_template("{{input}}", {"input": "{{not-an-expression}}"}) == "{{not-an-expression}}"


def test_active_prompt_selection_prefers_operator_choice_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = PROMPT_DEFINITIONS[0]
    monkeypatch.setenv(definition.base_arn_env, "arn:aws:bedrock:us-east-1:123:prompt/abc")
    monkeypatch.setenv(definition.default_arn_env, "arn:aws:bedrock:us-east-1:123:prompt/abc:1")

    class SelectionTable:
        def __init__(self, item):
            self.item = item

        def get_item(self, **kwargs):
            assert kwargs == {"Key": {"PromptKey": "investigation"}}
            return {"Item": self.item}

    selected = PromptVersionResolver(
        definition,
        selection_table=SelectionTable({"ActiveArn": "arn:aws:bedrock:us-east-1:123:prompt/abc:2"}),
    )
    fallback = PromptVersionResolver(definition, selection_table=SelectionTable({}))

    assert selected.resolve().endswith(":2")
    assert fallback.resolve().endswith(":1")


def test_prompt_management_routes_are_admin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeManager:
        def list_prompts(self):
            return {"prompts": []}

    monkeypatch.setattr(prompt_api, "_manager", lambda: FakeManager())
    operator = Principal("sub-operator", "operator@example.com", "operator@example.com", frozenset(), {})
    admin = _principal_from_claims(
        {"sub": "sub-admin", "cognito:username": "admin@example.com", "cognito:groups": ["northstar-admin"]},
        admin_group="northstar-admin",
    )
    client = TestClient(app)

    app.dependency_overrides[get_current_principal] = lambda: operator
    try:
        assert client.get("/admin/prompts").status_code == 403

        app.dependency_overrides[get_current_principal] = lambda: admin
        response = client.get("/admin/prompts")
        assert response.status_code == 200
        assert response.json() == {"prompts": []}
    finally:
        app.dependency_overrides.clear()
