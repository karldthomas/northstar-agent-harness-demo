from __future__ import annotations

import pytest

from apps.api.auth import (
    Principal,
    _principal_from_claims,
    get_current_principal,
)
from apps.api.main import app
from fastapi.testclient import TestClient


def test_cognito_groups_are_the_only_source_of_admin_authority() -> None:
    admin = _principal_from_claims(
        {"sub": "sub-1", "cognito:username": "admin@example.com", "cognito:groups": '["northstar-admin"]'},
        admin_group="northstar-admin",
    )
    operator = _principal_from_claims(
        {"sub": "sub-2", "cognito:username": "operator@example.com", "groups": ["operator"]},
        admin_group="northstar-admin",
    )

    assert admin.is_admin is True
    assert operator.is_admin is False
    assert admin.to_dict()["groups"] == ["northstar-admin"]


def test_missing_bearer_token_is_rejected() -> None:
    client = TestClient(app)
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert "Bearer" in response.headers["www-authenticate"]


def test_cloud_auth_mode_protects_simulator_routes_but_keeps_health_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NORTHSTAR_AUTH_REQUIRED", "true")
    client = TestClient(app)

    health = client.get("/healthz")
    scenarios = client.get("/simulator/scenarios")

    assert health.status_code == 200
    assert scenarios.status_code == 401


def test_only_admin_principal_can_create_users(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NORTHSTAR_COGNITO_USER_POOL_ID", "us-east-1_pool")
    monkeypatch.setenv("NORTHSTAR_COGNITO_CLIENT_ID", "client")
    monkeypatch.setenv("NORTHSTAR_COGNITO_ISSUER", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool")

    class FakeCognito:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def admin_create_user(self, **kwargs: object) -> dict[str, object]:
            self.requests.append(kwargs)
            return {"User": {"Username": kwargs["Username"]}}

    fake = FakeCognito()
    monkeypatch.setattr("apps.api.auth_routes._cognito_client", lambda: fake)
    admin = _principal_from_claims(
        {"sub": "sub-1", "cognito:username": "admin@example.com", "cognito:groups": ["northstar-admin"]},
        admin_group="northstar-admin",
    )
    operator = Principal("sub-2", "operator@example.com", "operator@example.com", frozenset(), {})
    client = TestClient(app)

    app.dependency_overrides[get_current_principal] = lambda: operator
    denied = client.post("/admin/users", json={"email": "new@example.com"})
    assert denied.status_code == 403
    assert fake.requests == []

    app.dependency_overrides[get_current_principal] = lambda: admin
    created = client.post("/admin/users", json={"email": "New@example.com"})
    assert created.status_code == 201
    assert created.json() == {
        "status": "INVITED",
        "username": "new@example.com",
        "email": "new@example.com",
        "created_by": "admin@example.com",
    }
    assert fake.requests[0]["UserPoolId"] == "us-east-1_pool"
    assert fake.requests[0]["DesiredDeliveryMediums"] == ["EMAIL"]
    app.dependency_overrides.clear()
