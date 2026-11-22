"""Provision the Northstar Gateway target and managed Harness.

This is the cloud-only handoff behind the existing Harness smoke flow. It does
not create a second deployment entry point and it never implements a model or
tool loop. CloudFormation owns the API, roles, and storage primitives; this
module owns the AgentCore control-plane resources that are not represented by
the stack graph.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .configuration import (
    DEFAULT_HARNESS_VARIANT,
    apply_harness_variant,
    harness_name_for_variant,
    load_harness_config,
    resolve_harness_config,
    validate_harness_config,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = ROOT / "cloudformation" / "build" / "deployment-context-dev.json"
DEFAULT_GATEWAY_SPEC = ROOT / "harness" / "gateway" / "northstar-tools.openapi.json"
DEFAULT_SKILL_PREFIX = "northstar/{environment}/skills/v1"
READY_STATUSES = {"READY", "ACTIVE"}
FAILED_STATUSES = {"FAILED", "CREATE_FAILED", "UPDATE_FAILED"}


class AgentCoreProvisioningError(RuntimeError):
    """Raised when the managed AgentCore boundary cannot be provisioned safely."""


@dataclass(frozen=True, slots=True)
class ProvisionedAgentCore:
    environment: str
    region: str
    gateway_id: str
    gateway_arn: str
    gateway_url: str | None
    gateway_target_id: str
    harness_id: str
    harness_arn: str
    harness_version: str | None
    skills_bucket: str
    skills_prefix: str
    variant: str = DEFAULT_HARNESS_VARIANT
    prompt_arns: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pages(client: Any, method_name: str, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Read every page from a control-plane list operation."""

    items: list[dict[str, Any]] = []
    request = dict(kwargs)
    while True:
        response = getattr(client, method_name)(**request)
        page = response.get(result_key, [])
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
        token = response.get("nextToken")
        if not token:
            return items
        request["nextToken"] = token


def _status_reason(resource: Mapping[str, Any]) -> str:
    reasons = resource.get("statusReasons") or []
    if not isinstance(reasons, list):
        return str(reasons)
    return "; ".join(str(item) for item in reasons)


def _wait_for(
    fetch: Callable[[], dict[str, Any]],
    *,
    label: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = fetch()
        status = str(last.get("status") or "").upper()
        if status in READY_STATUSES:
            return last
        if status in FAILED_STATUSES:
            reason = _status_reason(last)
            raise AgentCoreProvisioningError(f"{label} entered {status}: {reason or 'no reason returned'}")
        time.sleep(poll_seconds)
    status = last.get("status") or "unknown"
    reason = _status_reason(last)
    raise AgentCoreProvisioningError(
        f"Timed out waiting for {label}; last status={status} {reason}".strip()
    )


def _stable_token(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"northstar-{digest}"


def _context_value(context: Mapping[str, Any], key: str) -> str:
    value = str(context.get(key, "")).strip()
    if not value:
        raise AgentCoreProvisioningError(f"deployment context is missing {key}")
    return value


def _prompt_environment_values(context: Mapping[str, Any]) -> dict[str, str]:
    """Translate 03-harness prompt outputs into Lambda/runtime env names."""

    pairs = (
        (
            "NORTHSTAR_INVESTIGATION_PROMPT_ARN",
            "NORTHSTAR_INVESTIGATION_PROMPT_DEFAULT_ARN",
            "CF_OUT_InvestigationPromptArn",
            "CF_OUT_InvestigationPromptVersionArn",
        ),
        (
            "NORTHSTAR_APPROVAL_RESUMPTION_PROMPT_ARN",
            "NORTHSTAR_APPROVAL_RESUMPTION_PROMPT_DEFAULT_ARN",
            "CF_OUT_ApprovalResumptionPromptArn",
            "CF_OUT_ApprovalResumptionPromptVersionArn",
        ),
        (
            "NORTHSTAR_EVALUATION_PROMPT_ARN",
            "NORTHSTAR_EVALUATION_PROMPT_DEFAULT_ARN",
            "CF_OUT_EvaluationPromptArn",
            "CF_OUT_EvaluationPromptVersionArn",
        ),
    )
    result: dict[str, str] = {}
    for base_env, default_env, base_output, default_output in pairs:
        base_arn = str(context.get(base_output) or "").strip()
        default_arn = str(context.get(default_output) or "").strip()
        if base_arn and default_arn:
            result[base_env] = base_arn
            result[default_env] = default_arn
    return result


def _load_context(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentCoreProvisioningError(f"deployment context does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentCoreProvisioningError(f"deployment context is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AgentCoreProvisioningError("deployment context must be a JSON object")
    return payload


def _gateway_spec(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise AgentCoreProvisioningError(f"cannot load Gateway OpenAPI schema: {path}") from exc
    if not isinstance(document, dict):
        raise AgentCoreProvisioningError("Gateway OpenAPI schema must be a JSON object")
    return copy.deepcopy(document)


def _lambda_tool_definitions(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate the checked-in OpenAPI contract into Gateway Lambda schemas."""

    def supported_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
        # The AgentCore Lambda inline schema is intentionally narrower than a
        # full OpenAPI/JSON-Schema document. Keep the contract's callable
        # shape, but leave range/default enforcement to the simulator tool.
        result: dict[str, Any] = {}
        for key in ("type", "required", "description"):
            if key in schema:
                result[key] = copy.deepcopy(schema[key])
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            result["properties"] = {
                str(name): supported_schema(value)
                for name, value in properties.items()
                if isinstance(value, Mapping)
            }
        items = schema.get("items")
        if isinstance(items, Mapping):
            result["items"] = supported_schema(items)
        return result

    components = document.get("components") or {}
    parameters = components.get("parameters") or {}
    definitions: list[dict[str, Any]] = []
    paths = document.get("paths") or {}
    if not isinstance(paths, Mapping):
        raise AgentCoreProvisioningError("Gateway schema paths must be an object")

    for path, path_item in paths.items():
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if str(method).lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(operation, Mapping):
                raise AgentCoreProvisioningError(f"Gateway operation for {path} is not an object")
            operation_id = str(operation.get("operationId") or "").strip()
            if not operation_id:
                raise AgentCoreProvisioningError(f"Gateway operation for {path} is missing operationId")
            properties: dict[str, Any] = {}
            required: list[str] = []
            operation_parameters = operation.get("parameters") or []
            if not isinstance(operation_parameters, list):
                raise AgentCoreProvisioningError(f"Gateway parameters for {operation_id} are not a list")
            for raw_parameter in operation_parameters:
                parameter = raw_parameter
                if isinstance(raw_parameter, Mapping) and raw_parameter.get("$ref"):
                    reference = str(raw_parameter["$ref"])
                    prefix = "#/components/parameters/"
                    if not reference.startswith(prefix):
                        raise AgentCoreProvisioningError(
                            f"unsupported Gateway parameter reference for {operation_id}: {reference}"
                        )
                    parameter = parameters.get(reference[len(prefix):])
                if not isinstance(parameter, Mapping):
                    raise AgentCoreProvisioningError(f"invalid Gateway parameter for {operation_id}")
                name = str(parameter.get("name") or "").strip()
                schema = parameter.get("schema")
                if not name or not isinstance(schema, Mapping):
                    raise AgentCoreProvisioningError(f"Gateway parameter for {operation_id} is incomplete")
                parameter_schema = supported_schema(schema)
                if parameter.get("description") and "description" not in parameter_schema:
                    parameter_schema["description"] = str(parameter["description"])
                properties[name] = parameter_schema
                if parameter.get("required") is True:
                    required.append(name)
            input_schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                input_schema["required"] = required
            definitions.append(
                {
                    "name": operation_id,
                    "description": str(
                        operation.get("summary") or operation.get("description") or operation_id
                    ),
                    "inputSchema": input_schema,
                }
            )

    if not definitions:
        raise AgentCoreProvisioningError("Gateway schema contains no operations")
    return definitions


def _target_configuration(document: Mapping[str, Any], lambda_arn: str) -> dict[str, Any]:
    return {
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {
                    "inlinePayload": _lambda_tool_definitions(document),
                },
            }
        }
    }


def _ensure_gateway(
    client: Any,
    *,
    name: str,
    role_arn: str,
    environment: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    matches = [item for item in _pages(client, "list_gateways", "items") if item.get("name") == name]
    if matches:
        gateway_id = str(matches[0].get("gatewayId") or "")
        if not gateway_id:
            raise AgentCoreProvisioningError(f"Gateway {name} has no gatewayId")
        gateway = _wait_for(
            lambda: client.get_gateway(gatewayIdentifier=gateway_id),
            label=f"Gateway {name}",
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        if gateway.get("authorizerType") not in {None, "AWS_IAM"}:
            raise AgentCoreProvisioningError(
                f"Gateway {name} exists with authorizerType={gateway.get('authorizerType')}; refusing to reuse it"
            )
        if gateway.get("roleArn") not in {None, role_arn}:
            raise AgentCoreProvisioningError(
                f"Gateway {name} exists with an unexpected execution role; refusing to reuse it"
            )
        return gateway

    response = client.create_gateway(
        name=name,
        description=f"Northstar {environment} read-only simulator tools.",
        clientToken=_stable_token("gateway", environment),
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="AWS_IAM",
    )
    gateway_id = str(response.get("gatewayId") or "")
    if not gateway_id:
        raise AgentCoreProvisioningError(f"Gateway {name} creation returned no gatewayId")
    return _wait_for(
        lambda: client.get_gateway(gatewayIdentifier=gateway_id),
        label=f"Gateway {name}",
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )


def _ensure_gateway_target(
    client: Any,
    *,
    gateway_id: str,
    target_name: str,
    target_configuration: dict[str, Any],
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    credentials = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    matches = [
        item
        for item in _pages(client, "list_gateway_targets", "items", gatewayIdentifier=gateway_id)
        if item.get("name") == target_name
    ]
    if matches:
        target_id = str(matches[0].get("targetId") or "")
        if not target_id:
            raise AgentCoreProvisioningError(f"Gateway target {target_name} has no targetId")
        target_type = str(matches[0].get("targetType") or "").upper()
        if target_type and target_type != "LAMBDA":
            # The previous experiment used an OpenAPI/API Gateway target. The
            # control plane does not support changing that target's kind in
            # place, so replace only this exact Northstar target.
            client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                try:
                    client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
                except Exception as exc:  # boto3 is optional at import time.
                    response = getattr(exc, "response", {}) or {}
                    code = str((response.get("Error") or {}).get("Code") or "")
                    if code in {"ResourceNotFoundException", "NotFoundException"}:
                        break
                    raise
                time.sleep(poll_seconds)
            else:
                raise AgentCoreProvisioningError(
                    f"Timed out deleting incompatible Gateway target {target_name}"
                )
            response = client.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=target_name,
                description="Northstar simulator Lambda read-only target.",
                clientToken=_stable_token("gateway-target-lambda", gateway_id, target_name),
                targetConfiguration=target_configuration,
                credentialProviderConfigurations=credentials,
            )
            target_id = str(response.get("targetId") or "")
            if not target_id:
                raise AgentCoreProvisioningError(f"Gateway target {target_name} recreation returned no targetId")
        else:
            client.update_gateway_target(
                gatewayIdentifier=gateway_id,
                targetId=target_id,
                name=target_name,
                description="Northstar simulator Lambda read-only target.",
                targetConfiguration=target_configuration,
                credentialProviderConfigurations=credentials,
            )
    else:
        response = client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=target_name,
            description="Northstar simulator Lambda read-only target.",
            clientToken=_stable_token("gateway-target-lambda", gateway_id, target_name),
            targetConfiguration=target_configuration,
            credentialProviderConfigurations=credentials,
        )
        target_id = str(response.get("targetId") or "")
        if not target_id:
            raise AgentCoreProvisioningError(f"Gateway target {target_name} creation returned no targetId")

    # Inline Lambda targets are ready when the target status is READY. The
    # explicit synchronization API is not needed for this target kind.
    return _wait_for(
        lambda: client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id),
        label=f"Gateway target {target_name}",
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )


def _skill_sources(
    s3_client: Any,
    *,
    config: Mapping[str, Any],
    bucket: str,
    prefix: str,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for skill in config.get("skills", []):
        if not isinstance(skill, dict) or not isinstance(skill.get("path"), str):
            raise AgentCoreProvisioningError("cloud provisioning requires local path-based source skills")
        relative_path = Path(skill["path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise AgentCoreProvisioningError(f"unsafe skill path: {skill['path']}")
        skill_dir = ROOT / relative_path
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            raise AgentCoreProvisioningError(f"skill package is missing SKILL.md: {skill_file}")
        skill_name = skill_dir.name
        key_prefix = f"{prefix.rstrip('/')}/{skill_name}"
        s3_client.put_object(
            Bucket=bucket,
            Key=f"{key_prefix}/SKILL.md",
            Body=skill_file.read_bytes(),
            ContentType="text/markdown; charset=utf-8",
        )
        sources.append({"s3": {"uri": f"s3://{bucket}/{key_prefix}/"}})
    return sources


def _harness_is_compatible(harness: Mapping[str, Any], *, role_arn: str, gateway_arn: str) -> bool:
    if harness.get("executionRoleArn") not in {None, role_arn}:
        return False
    tools = harness.get("tools") or []
    gateway_tools = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "agentcore_gateway":
            continue
        gateway = (tool.get("config") or {}).get("agentCoreGateway") or {}
        gateway_tools.append(gateway.get("gatewayArn"))
    return bool(gateway_tools) and all(value in {None, gateway_arn} for value in gateway_tools)


def _harness_needs_update(harness: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """Compare mutable Harness contract fields while ignoring service metadata."""

    for field in (
        "model",
        "systemPrompt",
        "tools",
        "skills",
        "allowedTools",
        "maxIterations",
        "maxTokens",
        "timeoutSeconds",
    ):
        if harness.get(field) != desired.get(field):
            return True
    if "hooks" in desired and harness.get("hooks") != desired.get("hooks"):
        return True

    desired_memory = desired.get("memory") or {}
    actual_memory = harness.get("memory") or {}
    desired_managed = desired_memory.get("managedMemoryConfiguration") if isinstance(desired_memory, dict) else None
    actual_managed = actual_memory.get("managedMemoryConfiguration") if isinstance(actual_memory, dict) else None
    if isinstance(desired_managed, Mapping):
        if not isinstance(actual_managed, Mapping):
            return True
        for key, value in desired_managed.items():
            if actual_managed.get(key) != value:
                return True
    return False


def _ensure_harness(
    client: Any,
    *,
    harness_name: str,
    execution_role_arn: str,
    gateway_arn: str,
    model_id: str,
    harness_config: Mapping[str, Any],
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    matches = [
        item for item in _pages(client, "list_harnesses", "harnesses") if item.get("harnessName") == harness_name
    ]
    if matches:
        harness_id = str(matches[0].get("harnessId") or "")
        if not harness_id:
            raise AgentCoreProvisioningError(f"Harness {harness_name} has no harnessId")
        request: dict[str, Any] = {"harnessId": harness_id}
        if matches[0].get("harnessVersion"):
            request["harnessVersion"] = matches[0]["harnessVersion"]
        harness = client.get_harness(**request).get("harness") or matches[0]
        if not _harness_is_compatible(
            harness,
            role_arn=execution_role_arn,
            gateway_arn=gateway_arn,
        ):
            raise AgentCoreProvisioningError(
                f"Harness {harness_name} exists with an incompatible role or Gateway; refusing to reuse it"
            )
        desired = {
            "model": harness_config["model"],
            "systemPrompt": harness_config["systemPrompt"],
            "tools": harness_config["tools"],
            "skills": harness_config.get("skills", []),
            "allowedTools": harness_config["allowedTools"],
            "memory": harness_config.get("memory", {}),
            "maxIterations": harness_config["maxIterations"],
            "maxTokens": harness_config["maxTokens"],
            "timeoutSeconds": harness_config["timeoutSeconds"],
        }
        if "hooks" in harness_config:
            desired["hooks"] = harness_config["hooks"]
        if _harness_needs_update(harness, desired):
            update_fields = dict(desired)
            update_fields["memory"] = {"optionalValue": desired["memory"]}
            update_token = _stable_token(
                "harness-update",
                harness_name,
                model_id,
                json.dumps(update_fields, sort_keys=True, separators=(",", ":"), default=str),
            )
            client.update_harness(
                harnessId=harness_id,
                clientToken=update_token,
                executionRoleArn=execution_role_arn,
                **update_fields,
            )
        return _wait_for(
            lambda: client.get_harness(harnessId=harness_id).get("harness") or {},
            label=f"Harness {harness_name}",
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )

    request = {
        "harnessName": harness_name,
        "clientToken": _stable_token("harness", harness_name),
        "executionRoleArn": execution_role_arn,
        "model": harness_config["model"],
        "systemPrompt": harness_config["systemPrompt"],
        "tools": harness_config["tools"],
        "skills": harness_config.get("skills", []),
        "allowedTools": harness_config["allowedTools"],
        "memory": harness_config.get("memory", {}),
        "maxIterations": harness_config["maxIterations"],
        "maxTokens": harness_config["maxTokens"],
        "timeoutSeconds": harness_config["timeoutSeconds"],
    }
    if "hooks" in harness_config:
        request["hooks"] = harness_config["hooks"]
    response = client.create_harness(**request)
    harness = response.get("harness") or {}
    harness_id = str(harness.get("harnessId") or "")
    if not harness_id:
        raise AgentCoreProvisioningError(f"Harness {harness_name} creation returned no harnessId")
    return _wait_for(
        lambda: client.get_harness(harnessId=harness_id).get("harness") or {},
        label=f"Harness {harness_name}",
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )


def provision_agentcore(
    *,
    environment: str,
    region: str,
    model_id: str,
    context_path: Path = DEFAULT_CONTEXT_PATH,
    harness_config_path: Path | None = None,
    gateway_spec_path: Path = DEFAULT_GATEWAY_SPEC,
    variant: str = DEFAULT_HARNESS_VARIANT,
    skill_prefix: str | None = None,
    profile: str | None = None,
    control_client: Any | None = None,
    s3_client: Any | None = None,
    timeout_seconds: float = 600,
    poll_seconds: float = 5,
) -> ProvisionedAgentCore:
    """Create or reuse the cloud AgentCore resources for one environment."""

    context = _load_context(context_path)
    simulator_function_arn = _context_value(context, "CF_OUT_SimulatorFunctionArn")
    gateway_role_arn = _context_value(context, "CF_OUT_GatewayExecutionRoleArn")
    harness_role_arn = _context_value(context, "CF_OUT_HarnessExecutionRoleArn")
    skills_bucket = _context_value(context, "CF_OUT_FixtureBucketName")
    hook_arn = context.get("CF_OUT_HarnessHookFunctionArn", "").strip()
    config_path = harness_config_path or ROOT / "harness" / "config" / "northstar-harness.yaml"
    variant_name = str(variant or DEFAULT_HARNESS_VARIANT).strip().lower()
    raw_config = apply_harness_variant(load_harness_config(config_path), variant_name)

    if control_client is None or s3_client is None:
        try:
            import boto3
        except ImportError as exc:
            raise AgentCoreProvisioningError("boto3 is required for cloud AgentCore provisioning") from exc
        session = boto3.Session(profile_name=profile, region_name=region)
        control_client = control_client or session.client("bedrock-agentcore-control")
        s3_client = s3_client or session.client("s3")

    gateway_name = f"northstar-{environment}-gateway"
    gateway = _ensure_gateway(
        control_client,
        name=gateway_name,
        role_arn=gateway_role_arn,
        environment=environment,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    gateway_id = _context_value(gateway, "gatewayId")
    gateway_arn = _context_value(gateway, "gatewayArn")
    schema = _gateway_spec(gateway_spec_path)
    target = _ensure_gateway_target(
        control_client,
        gateway_id=gateway_id,
        target_name="northstar-simulator",
        target_configuration=_target_configuration(schema, simulator_function_arn),
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )

    resolved_config = resolve_harness_config(
        raw_config,
        values={"HARNESS_MODEL_ID": model_id, "NORTHSTAR_GATEWAY_ARN": gateway_arn},
    )
    skill_prefix = skill_prefix or DEFAULT_SKILL_PREFIX.format(environment=environment)
    cloud_config = copy.deepcopy(resolved_config)
    cloud_config["skills"] = _skill_sources(
        s3_client,
        config=resolved_config,
        bucket=skills_bucket,
        prefix=skill_prefix,
    )
    if hook_arn:
        cloud_config["hooks"] = [
            {
                "beforeToolCall": {
                    "name": "northstar-read-only-guard",
                    "target": {
                        "lambda": {
                            "arn": hook_arn,
                            "timeoutSeconds": 5,
                            "failureMode": "deny",
                        }
                    },
                }
            }
        ]
    errors = validate_harness_config(cloud_config)
    if errors:
        raise AgentCoreProvisioningError("invalid cloud Harness configuration: " + "; ".join(errors))

    # AgentCore Harness names use the service's identifier pattern (letters,
    # digits, and underscores); CloudFormation and Gateway names may keep the
    # hyphenated environment convention.
    harness_name = harness_name_for_variant(environment, variant_name)
    harness = _ensure_harness(
        control_client,
        harness_name=harness_name,
        execution_role_arn=harness_role_arn,
        gateway_arn=gateway_arn,
        model_id=model_id,
        harness_config=cloud_config,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    harness_id = _context_value(harness, "harnessId")
    harness_arn = _context_value(harness, "arn")
    return ProvisionedAgentCore(
        environment=environment,
        variant=variant_name,
        region=region,
        gateway_id=gateway_id,
        gateway_arn=gateway_arn,
        gateway_url=gateway.get("gatewayUrl"),
        gateway_target_id=_context_value(target, "targetId"),
        harness_id=harness_id,
        harness_arn=harness_arn,
        harness_version=harness.get("harnessVersion"),
        skills_bucket=skills_bucket,
        skills_prefix=skill_prefix,
        prompt_arns=_prompt_environment_values(context),
    )


def bind_simulator_harness_runtime(
    provisioned: ProvisionedAgentCore,
    *,
    context_path: Path = DEFAULT_CONTEXT_PATH,
    model_id: str,
    region: str,
    profile: str | None = None,
    lambda_client: Any | None = None,
) -> dict[str, str]:
    """Bind the provisioned managed Harness to the browser API Lambda.

    The simulator stack deploys before the control-plane Harness exists.  The
    existing deployment flow therefore performs this narrow post-provision
    environment update after the Harness/Gateway are ready.  The browser
    still calls only the Function URL; the Lambda uses its execution role to
    invoke the exact account/region Harness and keeps the Gateway selector
    read-only.
    """

    context = _load_context(context_path)
    function_arn = _context_value(context, "CF_OUT_SimulatorFunctionArn")
    if lambda_client is None:
        try:
            import boto3
        except ImportError as exc:
            raise AgentCoreProvisioningError("boto3 is required to bind the simulator Harness runtime") from exc
        lambda_client = boto3.Session(profile_name=profile, region_name=region).client("lambda")

    current = lambda_client.get_function_configuration(FunctionName=function_arn)
    existing = ((current.get("Environment") or {}).get("Variables") or {}) if isinstance(current, dict) else {}
    variables = {str(key): str(value) for key, value in existing.items()}
    variables.update(
        {
            "HARNESS_MODEL_ID": model_id,
            "NORTHSTAR_GATEWAY_ARN": provisioned.gateway_arn,
            "NORTHSTAR_HARNESS_ARN": provisioned.harness_arn,
            "NORTHSTAR_SKILLS_BUCKET": provisioned.skills_bucket,
            "NORTHSTAR_SKILLS_PREFIX": provisioned.skills_prefix,
            **provisioned.prompt_arns,
        }
    )
    if provisioned.prompt_arns:
        variables["NORTHSTAR_PROMPT_SOURCE"] = "bedrock"
    lambda_client.update_function_configuration(
        FunctionName=function_arn,
        Environment={"Variables": variables},
    )
    context.update(
        {
            "CF_OUT_GatewayArn": provisioned.gateway_arn,
            "CF_OUT_HarnessArn": provisioned.harness_arn,
            "CF_OUT_HarnessVersion": provisioned.harness_version or "",
            "CF_OUT_HarnessSkillsBucket": provisioned.skills_bucket,
            "CF_OUT_HarnessSkillsPrefix": provisioned.skills_prefix,
        }
    )
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(dict(sorted(context.items())), indent=2) + "\n", encoding="utf-8")
    return {
        "function_arn": function_arn,
        "harness_arn": provisioned.harness_arn,
        "gateway_arn": provisioned.gateway_arn,
    }
