from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.configuration import (
    DEFAULT_HARNESS_VARIANT,
    HARNESS_VARIANTS,
    READ_ONLY_TOOLS,
    READ_ONLY_TOOL_NAMES,
    HarnessConfigurationError,
    apply_harness_variant,
    harness_name_for_variant,
    invoke_overrides,
    load_harness_config,
    resolve_harness_config,
    validate_harness_config,
)


ROOT = Path(__file__).resolve().parents[2]


def resolved_config() -> dict[str, object]:
    config = load_harness_config(ROOT / "harness" / "config" / "northstar-harness.yaml")
    return resolve_harness_config(
        config,
        values={
            "HARNESS_MODEL_ID": "us.amazon.nova-lite-v1:0",
            "NORTHSTAR_GATEWAY_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/northstar",
        },
    )


def test_managed_harness_config_is_explicitly_read_only() -> None:
    config = resolved_config()

    assert validate_harness_config(config) == []
    assert config["allowedTools"] == list(READ_ONLY_TOOLS)
    assert "*" not in config["allowedTools"]
    assert config["maxIterations"] == 12
    assert config["maxTokens"] == 4096
    assert config["timeoutSeconds"] == 180
    assert invoke_overrides(config)["allowedTools"] == list(READ_ONLY_TOOLS)


def test_comparison_variants_preserve_the_read_only_boundary() -> None:
    config = resolved_config()

    assert DEFAULT_HARNESS_VARIANT == "skills-memory"
    assert set(HARNESS_VARIANTS) == {"baseline", "skills-memory", "optimized"}
    assert harness_name_for_variant("dev", "baseline") == "northstar_dev_baseline_harness"
    assert harness_name_for_variant("dev", "skills-memory") == "northstar_dev_harness"
    assert harness_name_for_variant("demo-env", "optimized") == "northstar_demo_env_optimized_harness"

    baseline = apply_harness_variant(config, "baseline")
    optimized = apply_harness_variant(config, "optimized")

    assert validate_harness_config(baseline) == []
    assert baseline["skills"] == []
    assert baseline["memory"] == {"disabled": {}}
    assert baseline["allowedTools"] == list(READ_ONLY_TOOLS)
    assert validate_harness_config(optimized) == []
    assert optimized["maxIterations"] == 8
    assert optimized["maxTokens"] == 3072
    assert optimized["timeoutSeconds"] == 120
    assert optimized["allowedTools"] == list(READ_ONLY_TOOLS)

    with pytest.raises(HarnessConfigurationError):
        apply_harness_variant(config, "write-enabled")


def test_harness_config_rejects_wildcards_and_path_traversal() -> None:
    config = resolved_config()
    config["allowedTools"] = ["*"]
    config["skills"] = [{"path": "../outside-skill"}]

    errors = validate_harness_config(config)

    assert any("wildcard" in error for error in errors)
    assert any("traverse" in error for error in errors)
    with pytest.raises(HarnessConfigurationError):
        invoke_overrides(config)


def test_gateway_openapi_contract_has_only_read_only_named_operations() -> None:
    document = json.loads(
        (ROOT / "harness" / "gateway" / "northstar-tools.openapi.json").read_text(encoding="utf-8")
    )
    operations = [
        operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    ]

    assert {operation["operationId"] for operation in operations} == {
        tool.split("___", 1)[1] for tool in READ_ONLY_TOOL_NAMES
    }
    assert all(
        method == "get"
        for path_item in document["paths"].values()
        for method in path_item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    )
