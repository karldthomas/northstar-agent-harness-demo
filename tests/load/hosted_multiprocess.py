"""Run a bounded, read-only multi-process probe against the hosted API.

This is intentionally a standalone opt-in probe rather than a default pytest
test. It exercises more than one client process while keeping the request
surface limited to the protected scenario catalog. Authenticated runs read an
ID token from ``NORTHSTAR_ID_TOKEN``; the token is never accepted as a CLI
argument or included in the JSON report.

Examples::

    NORTHSTAR_HOSTED_URL=https://... \\
      python3 tests/load/hosted_multiprocess.py

    NORTHSTAR_HOSTED_URL=https://... NORTHSTAR_ID_TOKEN="$ID_TOKEN" \\
      python3 tests/load/hosted_multiprocess.py --authenticated
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import ssl
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_REQUESTS = 128
MAX_WORKERS = 8
READ_ONLY_PATH = "/simulator/scenarios"


def _request_status(args: tuple[str, str | None]) -> int:
    """Fetch the fixed read-only route from one worker process."""

    url, token = args
    request = Request(
        f"{url.rstrip('/')}{READ_ONLY_PATH}",
        method="GET",
        headers={
            "Accept": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        try:
            with urlopen(request, timeout=30, context=context) as response:
                response.read()
                return response.status
        except HTTPError as error:
            error.read()
            return error.code
        except URLError as error:
            if not isinstance(error.reason, ssl.SSLCertVerificationError):
                return 599
            try:
                with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
                    response.read()
                    return response.status
            except HTTPError as fallback_error:
                fallback_error.read()
                return fallback_error.code
    except (OSError, URLError):
        return 599


def run_probe(*, url: str, token: str | None, request_count: int, worker_count: int) -> dict[str, object]:
    """Run the bounded probe and return non-sensitive aggregate evidence."""

    started = time.perf_counter()
    worker_args = [(url, token)] * request_count
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=worker_count, mp_context=context) as executor:
        statuses = list(executor.map(_request_status, worker_args))
    counts = Counter(str(status) for status in statuses)
    authenticated = token is not None
    expected = {200} if authenticated else {401, 429}
    unexpected = sorted(status for status in set(statuses) if status not in expected)
    return {
        "mode": "hosted-multiprocess-read-only",
        "authenticated": authenticated,
        "path": READ_ONLY_PATH,
        "request_count": request_count,
        "worker_count": worker_count,
        "status_counts": dict(sorted(counts.items())),
        "unexpected_statuses": unexpected,
        "transport_or_server_error_count": sum(status >= 500 for status in statuses),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("NORTHSTAR_HOSTED_URL", "").strip())
    parser.add_argument("--requests", type=int, default=64)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--authenticated",
        action="store_true",
        help="use NORTHSTAR_ID_TOKEN for the read-only requests",
    )
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("set NORTHSTAR_HOSTED_URL or pass --url")
    if not 1 <= args.requests <= MAX_REQUESTS:
        parser.error(f"--requests must be between 1 and {MAX_REQUESTS}")
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    token = os.environ.get("NORTHSTAR_ID_TOKEN", "").strip() or None
    if args.authenticated and not token:
        parser.error("--authenticated requires NORTHSTAR_ID_TOKEN set out-of-band")
    if not args.authenticated:
        token = None

    report = run_probe(
        url=args.url,
        token=token,
        request_count=args.requests,
        worker_count=args.workers,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report["unexpected_statuses"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
