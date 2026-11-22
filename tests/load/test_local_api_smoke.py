from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from apps.api.main import app


def test_local_read_only_api_handles_bounded_concurrent_smoke(monkeypatch) -> None:
    """Exercise the read-only catalog with a small deterministic local burst."""

    # This is an in-process load smoke, not a claim about Lambda capacity. Keep
    # it independent of deployment-only authentication and throttling settings.
    monkeypatch.setenv("NORTHSTAR_AUTH_REQUIRED", "false")
    monkeypatch.setenv("NORTHSTAR_RATE_LIMIT_ENABLED", "false")

    def request_catalog(_: int) -> int:
        with TestClient(app) as client:
            return client.get("/simulator/scenarios").status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(request_catalog, range(32)))

    assert statuses == [200] * 32
