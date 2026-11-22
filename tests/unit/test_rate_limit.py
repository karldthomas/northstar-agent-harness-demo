from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.rate_limit import InMemoryRateLimiter


def test_fixed_window_rate_limiter_expires_and_reports_retry() -> None:
    limiter = InMemoryRateLimiter()

    assert limiter.check("actor", limit=1, window_seconds=10, now=0) == (True, 0)
    allowed, retry_after = limiter.check("actor", limit=1, window_seconds=10, now=1)
    assert allowed is False
    assert retry_after == 9
    assert limiter.check("actor", limit=1, window_seconds=10, now=10) == (True, 0)


def test_http_rate_limit_returns_429_without_exposing_bearer_material(monkeypatch) -> None:
    monkeypatch.setenv("NORTHSTAR_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("NORTHSTAR_RATE_LIMIT_REQUESTS", "1")
    monkeypatch.setenv("NORTHSTAR_RATE_LIMIT_WINDOW_SECONDS", "60")
    token = "rate-limit-test-" + uuid4().hex
    client = TestClient(app)

    first = client.get("/simulator/scenarios", headers={"Authorization": f"Bearer {token}"})
    second = client.get("/simulator/scenarios", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["x-ratelimit-limit"] == "1"
    assert "retry-after" in second.headers
    assert token not in second.text
