"""Load, validate, and resolve the declarative AgentCore Harness contract."""

from __future__ import annotations

import json
import os
import re
import copy
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "harness" / "config" / "northstar-harness.yaml"
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
READ_ONLY_TOOL_NAMES = (
    "northstar-simulator___check_service_health",
    "northstar-simulator___get_device_diagnostics",
    "northstar-simulator___get_incident",
    "northstar-simulator___find_affected_devices",
    "northstar-simulator___list_recent_deployments",
    "northstar-simulator___search_knowledge",
)
# AgentCore currently resolves the Gateway target selector reliably while its
# per-tool slash selectors do not resolve for this Lambda-backed Gateway. The
# target is deliberately bounded to the six read-only operations above.
READ_ONLY_GATEWAY_SELECTOR = "@northstar-simulator"
READ_ONLY_TOOLS = (READ_ONLY_GATEWAY_SELECTOR,)
HARNESS_VARIANTS = ("baseline", "skills-memory", "optimized")
DEFAULT_HARNESS_VARIANT = "skills-memory"


class HarnessConfigurationError(ValueError):
    """Raised when the Harness contract is unsafe or incomplete."""


def apply_harness_variant(config: dict[str, Any], variant: str = DEFAULT_HARNESS_VARIANT) -> dict[str, Any]:
    """Return an explicit comparison profile without mutating the source config.

    ``skills-memory`` preserves the deployed Northstar contract. ``baseline``
    removes optional skills and explicitly disables managed memory so
    comparisons have a clear control. ``optimized`` keeps the safety context
    but uses tighter execution limits and a concise planning instruction. All
    profiles retain the same read-only Gateway selector and model boundary.
    """

    resolved_variant = str(variant or DEFAULT_HARNESS_VARIANT).strip().lower()
    if resolved_variant not in HARNESS_VARIANTS:
        raise HarnessConfigurationError(
            f"unknown Harness variant {resolved_variant!r}; choose from {', '.join(HARNESS_VARIANTS)}"
        )
    result = copy.deepcopy(config)
    if resolved_variant == "baseline":
        result["skills"] = []
        # AgentCore models memory as a tagged union.  Omitting the field is not
        # a valid create/update request; the baseline must use its explicit
        # disabled member so it remains a real deployed comparison profile.
        result["memory"] = {"disabled": {}}
    elif resolved_variant == "optimized":
        result["maxIterations"] = min(int(result.get("maxIterations", 12)), 8)
        result["maxTokens"] = min(int(result.get("maxTokens", 4096)), 3072)
        result["timeoutSeconds"] = min(int(result.get("timeoutSeconds", 180)), 120)
        prompt = result.get("systemPrompt")
        if isinstance(prompt, list):
            result["systemPrompt"] = [*prompt, {"text": (
                "Prefer the smallest sufficient read-only evidence set, stop when "
                "the objective is answered, and keep the final report concise."
            )}]
    return result


def harness_name_for_variant(environment: str, variant: str = DEFAULT_HARNESS_VARIANT) -> str:
    """Return a service-safe, stable Harness name for one comparison profile."""

    resolved_variant = str(variant or DEFAULT_HARNESS_VARIANT).strip().lower()
    if resolved_variant not in HARNESS_VARIANTS:
        raise HarnessConfigurationError(
            f"unknown Harness variant {resolved_variant!r}; choose from {', '.join(HARNESS_VARIANTS)}"
        )
    environment_name = str(environment).strip().replace("-", "_")
    if not environment_name or not re.fullmatch(r"[A-Za-z0-9_]+", environment_name):
        raise HarnessConfigurationError("environment must contain only letters, digits, underscores, or hyphens")
    suffix = "" if resolved_variant == DEFAULT_HARNESS_VARIANT else f"_{resolved_variant.replace('-', '_')}"
    return f"northstar_{environment_name}{suffix}_harness"


def _load_document(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            import yaml
        except ImportError as exc:
            raise HarnessConfigurationError("PyYAML is required to load Harness YAML configuration") from exc
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HarnessConfigurationError(f"Harness configuration must be an object: {path}")
    return payload


def load_harness_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    return _load_document(Path(path).expanduser().resolve())


def _resolve(value: Any, values: Mapping[str, str], *, allow_unresolved: bool) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in values:
                return values[key]
            if key in os.environ:
                return os.environ[key]
            if allow_unresolved:
                return match.group(0)
            raise HarnessConfigurationError(f"Harness configuration value {key!r} is unresolved")

        return PLACEHOLDER.sub(replace, value)
    if isinstance(value, list):
        return [_resolve(item, values, allow_unresolved=allow_unresolved) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, values, allow_unresolved=allow_unresolved) for key, item in value.items()}
    return value


def resolve_harness_config(
    config: dict[str, Any],
    *,
    values: Mapping[str, str] | None = None,
    allow_unresolved: bool = False,
) -> dict[str, Any]:
    return _resolve(config, values or {}, allow_unresolved=allow_unresolved)


def validate_harness_config(config: dict[str, Any]) -> list[str]:
    """Validate safety-critical fields without requiring AWS credentials."""

    errors: list[str] = []
    model = config.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("bedrockModelConfig"), dict):
        errors.append("model.bedrockModelConfig is required")
    else:
        model_config = model["bedrockModelConfig"]
        if not model_config.get("modelId"):
            errors.append("model.bedrockModelConfig.modelId is required")
        if model_config.get("apiFormat") not in {"converse_stream", "responses", "chat_completions", None}:
            errors.append("model.bedrockModelConfig.apiFormat is invalid")

    prompt = config.get("systemPrompt")
    if not isinstance(prompt, list) or not prompt or any(not isinstance(item, dict) or not item.get("text") for item in prompt):
        errors.append("systemPrompt must contain at least one text block")

    tools = config.get("tools")
    if not isinstance(tools, list) or not tools:
        errors.append("tools must contain the simulator Gateway tool")
    else:
        gateway_tools = [item for item in tools if isinstance(item, dict) and item.get("type") == "agentcore_gateway"]
        if len(gateway_tools) != 1:
            errors.append("exactly one agentcore_gateway tool is required")
        elif not gateway_tools[0].get("config", {}).get("agentCoreGateway", {}).get("gatewayArn"):
            errors.append("the simulator Gateway ARN is required")

    allowed = config.get("allowedTools")
    if not isinstance(allowed, list) or not allowed:
        errors.append("allowedTools must be an explicit non-empty list")
    else:
        if any("*" in str(selector) for selector in allowed) or any(
            str(selector).startswith("@builtin") for selector in allowed
        ):
            errors.append("wildcard or builtin tools are forbidden for Northstar")
        unexpected = sorted(set(allowed) - set(READ_ONLY_TOOLS))
        if unexpected:
            errors.append("unexpected or state-changing tools are not allowed: " + ", ".join(unexpected))

    for field in ("maxIterations", "maxTokens", "timeoutSeconds"):
        value = config.get(field)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"{field} must be a positive integer")

    skills = config.get("skills", [])
    if not isinstance(skills, list):
        errors.append("skills must be a list")
    else:
        for index, skill in enumerate(skills):
            if not isinstance(skill, dict):
                errors.append(f"skills[{index}] must be an object")
                continue
            if isinstance(skill.get("path"), str):
                if skill["path"].startswith("/") or ".." in Path(skill["path"]).parts:
                    errors.append(f"skills[{index}] path must be relative and cannot traverse upward")
                continue
            s3 = skill.get("s3")
            if isinstance(s3, dict) and isinstance(s3.get("uri"), str):
                if not s3["uri"].startswith("s3://") or ".." in Path(s3["uri"][5:]).parts:
                    errors.append(f"skills[{index}] s3.uri must be a safe s3:// directory URI")
                continue
            git = skill.get("git")
            if isinstance(git, dict) and isinstance(git.get("url"), str):
                if not git["url"].startswith("https://"):
                    errors.append(f"skills[{index}] git.url must use https")
                continue
            aws_skills = skill.get("awsSkills")
            if isinstance(aws_skills, dict) and isinstance(aws_skills.get("paths"), list):
                if any(not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts for path in aws_skills["paths"]):
                    errors.append(f"skills[{index}] awsSkills.paths must be relative")
                continue
            errors.append(f"skills[{index}] must use a supported path, s3, git, or awsSkills source")
    return errors


def invoke_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Return only fields accepted by InvokeHarness for a controlled call."""

    errors = validate_harness_config(config)
    if errors:
        raise HarnessConfigurationError("; ".join(errors))
    return {
        key: config[key]
        for key in (
            "model",
            "systemPrompt",
            "tools",
            "allowedTools",
            "skills",
            "maxIterations",
            "maxTokens",
            "timeoutSeconds",
            "actorId",
        )
        if key in config
    }
