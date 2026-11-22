from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_toolchain_lock_captures_the_verified_demo_boundary() -> None:
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())

    assert lock["aws"] == {
        "profile": "NoTurningBack",
        "region": "us-east-1",
        "account": "832443555706",
        "model_id": "us.amazon.nova-2-lite-v1:0",
    }
    assert lock["python"] == "3.12.6"
    assert lock["aws_cli"] == "2.40.3"
    assert lock["sam_cli"] == "1.123.0"
    assert lock["harness"]["client"] == "boto3==1.44.9"
    assert lock["runtime"] == {
        "type": "sam-lambda",
        "container_image_digest": None,
    }


def test_lambda_requirements_are_exactly_pinned() -> None:
    requirements = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert requirements == {
        "boto3==1.44.9",
        "fastapi==0.165.0",
        "mangum==0.22.0",
        "PyJWT[crypto]==2.15.1",
        "PyYAML==6.0.4",
    }


def test_project_metadata_uses_the_same_exact_runtime_and_dev_pins() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert metadata["project"]["dependencies"] == [
        "boto3==1.44.9",
        "fastapi==0.165.0",
        "mangum==0.22.0",
        "PyJWT[crypto]==2.15.1",
        "PyYAML==6.0.4",
        "uvicorn[standard]==0.54.0",
    ]
    assert metadata["project"]["optional-dependencies"]["dev"] == [
        "pytest==8.3.3",
        "httpx==0.28.1",
    ]
