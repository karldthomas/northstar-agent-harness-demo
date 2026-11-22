"""HTTP surface for the Northstar synthetic environment."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from .auth import require_http_auth
from .auth_routes import router as auth_router
from .rate_limit import rate_limiter
from evaluation.api import router as evaluation_router
from harness.api import router as harness_router
from harness.prompt_api import router as prompt_management_router
from simulator.api import router as simulator_router


app = FastAPI(
    title="Northstar IT Operations API",
    version="0.4.0",
    description="Synthetic IT operations API with authoritative, deterministic simulator state.",
)

cors_origins = [
    origin.strip()
    for origin in os.environ.get("NORTHSTAR_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def _rate_limit_enabled() -> bool:
    return os.environ.get("NORTHSTAR_RATE_LIMIT_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _rate_limit_value(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _rate_limit_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        # Keep bearer material out of the in-memory key and all diagnostics.
        return "token:" + sha256(authorization.encode("utf-8")).hexdigest()
    return "client:" + str(request.client.host if request.client else "anonymous")


def _rate_limit_policy(path: str) -> tuple[int, int] | None:
    # The browser shell, static bundle, and dependency-free health probe remain
    # available while an API client is throttled.
    if path == "/" or path == "/healthz" or path.startswith("/assets/"):
        return None
    window = _rate_limit_value("NORTHSTAR_RATE_LIMIT_WINDOW_SECONDS", 60)
    if path in {"/auth/login", "/auth/respond-to-challenge", "/admin/users"}:
        return _rate_limit_value("NORTHSTAR_AUTH_RATE_LIMIT_REQUESTS", 10), window
    return _rate_limit_value("NORTHSTAR_RATE_LIMIT_REQUESTS", 120), window


@app.middleware("http")
async def rate_limit_requests(request: Request, call_next) -> Response:
    if not _rate_limit_enabled() or request.method == "OPTIONS":
        return await call_next(request)
    policy = _rate_limit_policy(request.url.path)
    if policy is None:
        return await call_next(request)
    limit, window = policy
    allowed, retry_after = rate_limiter.check(
        _rate_limit_key(request),
        limit=limit,
        window_seconds=window,
    )
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "request rate limit exceeded"},
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(limit)},
        )
    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit)
    return response

app.include_router(auth_router)
app.include_router(evaluation_router, dependencies=[Depends(require_http_auth)])

# The local simulator remains convenient without credentials.  CloudFormation
# sets NORTHSTAR_AUTH_REQUIRED=true so every simulator/Harness HTTP route is
# protected by a Cognito bearer token.  Gateway Lambda target invocations do
# not pass through these HTTP routes and remain an internal tool boundary.
app.include_router(simulator_router, dependencies=[Depends(require_http_auth)])
app.include_router(harness_router, dependencies=[Depends(require_http_auth)])
app.include_router(prompt_management_router)

WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if (WEB_DIST / "assets").is_dir():
    # The browser bundle is optional for local/API-only development. In the
    # deployed Lambda package it is built before SAM packaging and served from
    # the same Function URL as the authenticated API.
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="web-assets")


def _web_console_enabled() -> bool:
    return (
        os.environ.get("NORTHSTAR_SERVE_WEB", "false").strip().lower() in {"1", "true", "yes", "on"}
        and (WEB_DIST / "index.html").is_file()
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Return a dependency-free process health response."""

    return {"status": "ok", "phase": "5-6"}


@app.get("/")
def root():
    """Expose the current implementation boundary."""

    if _web_console_enabled():
        return FileResponse(WEB_DIST / "index.html", media_type="text/html")

    return {
        "name": "northstar-it-operations",
        "status": "recovery-and-independent-evaluation-slice",
        "next": "comparative-harness-evaluation-and-hardening",
    }
