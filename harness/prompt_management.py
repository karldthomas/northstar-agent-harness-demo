"""Northstar's native Amazon Bedrock Prompt Management boundary.

Prompt bodies and immutable versions live in Bedrock Prompt Management when
the deployed service sets ``NORTHSTAR_PROMPT_SOURCE=bedrock``.  The checked-in
JSON seed is an offline contract for local development and deterministic unit
tests; it is never used by the hosted resolver when Bedrock is selected.

The current boto3 service model exposes prompt retrieval and versioning but not
a ``RenderPrompt`` operation.  Rendering therefore follows the Prompt
Management ``{{input}}`` contract after retrieving the selected immutable
version.  The renderer is intentionally small: it rejects missing variables,
never evaluates expressions, and never treats prompt content as executable
code.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED_PATH = ROOT / "harness" / "config" / "prompts.json"
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class PromptManagementError(RuntimeError):
    """Raised when a prompt cannot be safely resolved or managed."""


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    key: str
    label: str
    description: str
    name_suffix: str
    base_arn_env: str
    default_arn_env: str
    required_variables: tuple[str, ...] = ("input",)


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    key: str
    text: str
    version: str
    version_arn: str
    source: str

    def metadata(self) -> dict[str, str]:
        return {
            "prompt_key": self.key,
            "prompt_version": self.version,
            "prompt_version_arn": self.version_arn,
            "prompt_source": self.source,
        }


PROMPT_DEFINITIONS: tuple[PromptDefinition, ...] = (
    PromptDefinition(
        key="investigation",
        label="Northstar investigation",
        description="Evidence-first investigation and business-facing recommendation.",
        name_suffix="investigation",
        base_arn_env="NORTHSTAR_INVESTIGATION_PROMPT_ARN",
        default_arn_env="NORTHSTAR_INVESTIGATION_PROMPT_DEFAULT_ARN",
    ),
    PromptDefinition(
        key="approval_resumption",
        label="Northstar approval resumption",
        description="Read-only rehydration after an application-owned approved operation.",
        name_suffix="approval-resumption",
        base_arn_env="NORTHSTAR_APPROVAL_RESUMPTION_PROMPT_ARN",
        default_arn_env="NORTHSTAR_APPROVAL_RESUMPTION_PROMPT_DEFAULT_ARN",
    ),
    PromptDefinition(
        key="evaluation",
        label="Northstar evaluation",
        description="Bounded read-only evidence prompt used by smoke and evaluation runs.",
        name_suffix="evaluation",
        base_arn_env="NORTHSTAR_EVALUATION_PROMPT_ARN",
        default_arn_env="NORTHSTAR_EVALUATION_PROMPT_DEFAULT_ARN",
    ),
)


def _definition(key: str) -> PromptDefinition:
    for definition in PROMPT_DEFINITIONS:
        if definition.key == key:
            return definition
    raise PromptManagementError(f"unknown Northstar prompt: {key}")


def prompt_version_from_arn(arn: str) -> str:
    value = str(arn or "").strip()
    last = value.rsplit(":", 1)[-1] if value else ""
    return last if last.isdigit() else ""


def _prompt_source() -> str:
    configured = os.environ.get("NORTHSTAR_PROMPT_SOURCE", "").strip().lower()
    if configured in {"local", "bedrock"}:
        return configured
    if any(os.environ.get(item.base_arn_env, "").strip() for item in PROMPT_DEFINITIONS):
        return "bedrock"
    return "local"


@lru_cache(maxsize=1)
def load_seed_prompts(path: str = str(DEFAULT_SEED_PATH)) -> dict[str, dict[str, Any]]:
    seed_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptManagementError(f"cannot load local prompt seed: {seed_path}") from exc
    if not isinstance(payload, dict):
        raise PromptManagementError("local prompt seed must be a JSON object")
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def _text_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    return "\n\n".join(
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, Mapping) and block.get("text")
    ).strip()


def _fields_from_prompt(payload: Mapping[str, Any]) -> dict[str, Any]:
    variants = payload.get("variants") or []
    variant = variants[0] if isinstance(variants, list) and variants else {}
    if not isinstance(variant, Mapping):
        variant = {}
    configuration = (variant.get("templateConfiguration") or {}).get("chat") or {}
    if not isinstance(configuration, Mapping):
        configuration = {}
    messages = configuration.get("messages") or []
    user_messages = [item for item in messages if isinstance(item, Mapping) and item.get("role") == "user"]
    user_template = "\n\n".join(
        _text_blocks(message.get("content"))
        for message in user_messages
        if isinstance(message, Mapping)
    ).strip()
    inference = (variant.get("inferenceConfiguration") or {}).get("text") or {}
    if not isinstance(inference, Mapping):
        inference = {}
    return {
        "name": str(payload.get("name") or ""),
        "description": str(payload.get("description") or ""),
        "version": str(payload.get("version") or "DRAFT"),
        "updated_at": payload.get("updatedAt"),
        "system": _text_blocks(configuration.get("system")),
        "user": user_template,
        "model_id": str(variant.get("modelId") or "us.amazon.nova-2-lite-v1:0"),
        "max_tokens": inference.get("maxTokens", 1000),
        "temperature": inference.get("temperature", 0),
        "input_variables": [
            str(item.get("name"))
            for item in configuration.get("inputVariables", [])
            if isinstance(item, Mapping) and item.get("name")
        ],
    }


def _seed_payload(key: str) -> dict[str, Any]:
    definition = _definition(key)
    seed = load_seed_prompts().get(key)
    if not seed:
        raise PromptManagementError(f"local prompt seed is missing {key}")
    return {
        "name": f"northstar-local-{definition.name_suffix}",
        "description": str(seed.get("description") or definition.description),
        "version": "LOCAL",
        "updatedAt": None,
        "variants": [
            {
                "name": "default",
                "modelId": str(seed.get("model_id") or "us.amazon.nova-2-lite-v1:0"),
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": int(seed.get("max_tokens") or 1000),
                        "temperature": float(seed.get("temperature") or 0),
                    }
                },
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": str(name)} for name in seed.get("input_variables", ["input"])],
                        "system": ([{"text": str(seed["system"])}] if seed.get("system") else []),
                        "messages": [{"role": "user", "content": [{"text": str(seed.get("user") or "")}]}],
                    }
                },
            }
        ],
    }


def _variable_names(*values: str) -> list[str]:
    names: list[str] = []
    for value in values:
        for name in _VARIABLE.findall(value):
            if name not in names:
                names.append(name)
    return names


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    """Substitute simple Prompt Management variables and reject omissions."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise PromptManagementError(f"prompt variable {name!r} was not supplied")
        value = variables[name]
        if isinstance(value, str):
            return value
        return json.dumps(value, sort_keys=True)

    return _VARIABLE.sub(replace, template)


def _prompt_fields_from_payload(payload: Mapping[str, Any], *, definition: PromptDefinition, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    current = current or {}
    name = str(payload.get("name", current.get("name", ""))).strip()
    description = str(payload.get("description", current.get("description", definition.description))).strip()[:200]
    system = str(payload.get("system", current.get("system", ""))).strip()
    user = str(payload.get("user", current.get("user", "{{input}}"))).strip()
    model_id = str(payload.get("model_id", current.get("model_id", "us.amazon.nova-2-lite-v1:0"))).strip()
    if not name or not _NAME.fullmatch(name):
        raise PromptManagementError("prompt name must contain only letters, numbers, hyphens, and underscores")
    if not user:
        raise PromptManagementError("user message template is required")
    if not model_id:
        raise PromptManagementError("model ID is required")
    try:
        max_tokens = int(payload.get("max_tokens", current.get("max_tokens", 1000)))
    except (TypeError, ValueError) as exc:
        raise PromptManagementError("max tokens must be a positive integer") from exc
    if max_tokens < 1:
        raise PromptManagementError("max tokens must be a positive integer")
    try:
        temperature = float(payload.get("temperature", current.get("temperature", 0)))
    except (TypeError, ValueError) as exc:
        raise PromptManagementError("temperature must be a number between 0 and 2") from exc
    if not 0 <= temperature <= 2:
        raise PromptManagementError("temperature must be a number between 0 and 2")
    variables = _variable_names(system, user)
    missing = sorted(set(definition.required_variables) - set(variables))
    if missing:
        raise PromptManagementError("prompt must keep required variables: " + ", ".join(f"{{{{{item}}}}}" for item in missing))
    return {
        "name": name,
        "description": description,
        "system": system,
        "user": user,
        "model_id": model_id,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "input_variables": variables,
    }


def _variant(fields: Mapping[str, Any]) -> dict[str, Any]:
    chat: dict[str, Any] = {
        "inputVariables": [{"name": name} for name in fields["input_variables"]],
        "messages": [{"role": "user", "content": [{"text": fields["user"]}]}],
    }
    if fields["system"]:
        chat["system"] = [{"text": fields["system"]}]
    return {
        "name": "default",
        "modelId": fields["model_id"],
        "templateType": "CHAT",
        "templateConfiguration": {"chat": chat},
        "inferenceConfiguration": {
            "text": {
                "maxTokens": fields["max_tokens"],
                "temperature": fields["temperature"],
            }
        },
    }


class PromptVersionResolver:
    """Resolve the operator-selected immutable version for one prompt."""

    def __init__(self, definition: PromptDefinition, *, selection_table: Any | None = None) -> None:
        self.definition = definition
        self.selection_table = selection_table

    @property
    def base_arn(self) -> str:
        return os.environ.get(self.definition.base_arn_env, "").strip()

    @property
    def default_version_arn(self) -> str:
        return os.environ.get(self.definition.default_arn_env, "").strip()

    def resolve(self) -> str:
        if self.selection_table is not None:
            try:
                item = self.selection_table.get_item(Key={"PromptKey": self.definition.key}).get("Item", {})
            except Exception as exc:  # pragma: no cover - exercised by cloud permission/network failures
                print({"event": "prompt_selection_degraded", "prompt_key": self.definition.key, "error": str(exc)})
                item = {}
            selected = str(item.get("ActiveArn", "")).strip()
            if selected:
                return selected
        if self.default_version_arn:
            return self.default_version_arn
        if self.base_arn:
            raise PromptManagementError(f"no active version configured for prompt {self.definition.key}")
        raise PromptManagementError(f"prompt {self.definition.key} is not configured")


class PromptManager:
    """Read, render, and administer the fixed Northstar prompt catalog."""

    def __init__(self, *, client: Any | None = None, selection_table: Any | None = None, source: str | None = None) -> None:
        self.source = (source or _prompt_source()).strip().lower()
        if self.source not in {"local", "bedrock"}:
            raise PromptManagementError("NORTHSTAR_PROMPT_SOURCE must be local or bedrock")
        self._client = client
        self._selection_table = selection_table

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - deployment packaging guard
                raise PromptManagementError("boto3 is required for Bedrock Prompt Management") from exc
            self._client = boto3.client(
                "bedrock-agent",
                region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            )
        return self._client

    @property
    def selection_table(self) -> Any | None:
        if self._selection_table is not None:
            return self._selection_table
        table_name = os.environ.get("NORTHSTAR_PROMPT_SELECTIONS_TABLE_NAME", "").strip()
        if not table_name or self.source == "local":
            return None
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - deployment packaging guard
            raise PromptManagementError("boto3 is required for prompt selection storage") from exc
        self._selection_table = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        ).Table(table_name)
        return self._selection_table

    def definition(self, key: str) -> PromptDefinition:
        return _definition(key)

    def _require_bedrock(self, definition: PromptDefinition) -> str:
        if self.source != "bedrock":
            raise PromptManagementError("Bedrock Prompt Management is disabled for local mode")
        arn = os.environ.get(definition.base_arn_env, "").strip()
        if not arn:
            raise PromptManagementError(f"{definition.base_arn_env} is not configured")
        return arn

    def _get_prompt(self, key: str, *, version_arn: str | None = None, draft: bool = False) -> dict[str, Any]:
        definition = self.definition(key)
        if self.source == "local":
            return _seed_payload(key)
        base_arn = self._require_bedrock(definition)
        request: dict[str, Any] = {"promptIdentifier": version_arn or base_arn}
        if draft:
            request["promptVersion"] = "DRAFT"
            request["promptIdentifier"] = base_arn
        try:
            response = self.client.get_prompt(**request)
        except Exception as exc:  # pragma: no cover - AWS behavior is covered by integration smoke
            raise PromptManagementError(f"could not read Bedrock prompt {key}: {exc}") from exc
        if not isinstance(response, dict):
            raise PromptManagementError(f"Bedrock returned an invalid prompt for {key}")
        return response

    def _active(self, key: str) -> tuple[str, str, str]:
        definition = self.definition(key)
        if self.source == "local":
            return "LOCAL", "local://northstar/" + key + ":LOCAL", "local"
        resolver = PromptVersionResolver(definition, selection_table=self.selection_table)
        arn = resolver.resolve()
        return prompt_version_from_arn(arn) or "DRAFT", arn, "selected" if arn != resolver.default_version_arn else "default"

    def render(self, key: str, context: Mapping[str, Any], *, version_arn: str | None = None, draft: bool = False) -> RenderedPrompt:
        definition = self.definition(key)
        if self.source == "local":
            payload = self._get_prompt(key)
            selected_arn = "local://northstar/" + key + ":LOCAL"
        elif draft:
            payload = self._get_prompt(key, draft=True)
            selected_arn = os.environ.get(definition.base_arn_env, "").strip() + ":DRAFT"
        else:
            selected_arn = version_arn or self._active(key)[1]
            payload = self._get_prompt(key, version_arn=selected_arn)
        fields = _fields_from_prompt(payload)
        variables: dict[str, Any] = {str(name): value for name, value in context.items()}
        variables["input"] = json.dumps(dict(context), indent=2, sort_keys=True)
        user = render_template(fields["user"], variables)
        system = render_template(fields["system"], variables) if fields["system"] else ""
        text = "\n\n".join(value for value in (system, user) if value).strip()
        if not text:
            raise PromptManagementError(f"prompt {key} rendered to empty text")
        return RenderedPrompt(
            key=key,
            text=text,
            version=prompt_version_from_arn(selected_arn) or fields["version"],
            version_arn=selected_arn,
            source=self.source,
        )

    def _list_versions(self, definition: PromptDefinition) -> list[dict[str, Any]]:
        if self.source == "local":
            payload = _seed_payload(definition.key)
            return [{
                "version": "LOCAL",
                "arn": "local://northstar/" + definition.key + ":LOCAL",
                "description": "Offline seed prompt",
                "status": "ACTIVE",
                "created_at": None,
                "updated_at": None,
            }]
        base_arn = self._require_bedrock(definition)
        versions: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            request: dict[str, Any] = {"promptIdentifier": base_arn, "maxResults": 100}
            if next_token:
                request["nextToken"] = next_token
            try:
                response = self.client.list_prompts(**request)
            except Exception as exc:  # pragma: no cover - AWS behavior is covered by integration smoke
                raise PromptManagementError(f"could not list versions for prompt {definition.key}: {exc}") from exc
            for summary in response.get("promptSummaries", []):
                if not isinstance(summary, Mapping):
                    continue
                arn = str(summary.get("arn") or summary.get("promptArn") or "")
                version = str(summary.get("version") or prompt_version_from_arn(arn))
                if version and version.upper() != "DRAFT":
                    versions.append({
                        "version": version,
                        "arn": arn or f"{base_arn}:{version}",
                        "description": str(summary.get("description") or ""),
                        "status": str(summary.get("status") or ""),
                        "created_at": summary.get("createdAt"),
                        "updated_at": summary.get("updatedAt"),
                    })
            next_token = response.get("nextToken")
            if not next_token:
                break
        versions.sort(key=lambda item: int(item["version"]) if str(item["version"]).isdigit() else 0, reverse=True)
        return versions

    def list_prompts(self) -> dict[str, Any]:
        prompts: list[dict[str, Any]] = []
        for definition in PROMPT_DEFINITIONS:
            draft = self._get_prompt(definition.key, draft=self.source == "bedrock")
            fields = _fields_from_prompt(draft)
            versions = self._list_versions(definition)
            active_version, active_arn, active_source = self._active(definition.key)
            prompts.append({
                "key": definition.key,
                "label": definition.label,
                "description": definition.description,
                "arn": os.environ.get(definition.base_arn_env, "").strip() or None,
                "runtime": True,
                "active_version": active_version,
                "active_arn": active_arn,
                "active_source": active_source,
                "default_version": prompt_version_from_arn(os.environ.get(definition.default_arn_env, "")) or ("LOCAL" if self.source == "local" else None),
                "draft": fields,
                "versions": versions,
            })
        return {"prompts": prompts}

    def update_draft(self, key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        definition = self.definition(key)
        base_arn = self._require_bedrock(definition)
        current = _fields_from_prompt(self._get_prompt(key, draft=True))
        fields = _prompt_fields_from_payload({**dict(payload), "name": current["name"]}, definition=definition, current=current)
        request = {
            "promptIdentifier": base_arn,
            "name": fields["name"],
            "description": fields["description"],
            "defaultVariant": "default",
            "variants": [_variant(fields)],
        }
        try:
            updated = self.client.update_prompt(**request)
        except Exception as exc:  # pragma: no cover - AWS behavior is covered by integration smoke
            raise PromptManagementError(f"could not save draft for prompt {key}: {exc}") from exc
        return {
            "prompt_key": key,
            "arn": str(updated.get("arn") or base_arn),
            "version": str(updated.get("version") or "DRAFT"),
            "status": "draft_saved",
        }

    def create_version(self, key: str, description: str) -> dict[str, Any]:
        definition = self.definition(key)
        base_arn = self._require_bedrock(definition)
        description = str(description or "Created from the Bedrock draft.").strip()[:200]
        if not description:
            description = "Created from the Bedrock draft."
        try:
            created = self.client.create_prompt_version(promptIdentifier=base_arn, description=description)
        except Exception as exc:  # pragma: no cover - AWS behavior is covered by integration smoke
            raise PromptManagementError(f"could not create version for prompt {key}: {exc}") from exc
        arn = str(created.get("arn") or "")
        version = str(created.get("version") or prompt_version_from_arn(arn))
        return {"prompt_key": key, "version": version, "arn": arn or f"{base_arn}:{version}", "description": description, "status": "created"}

    def activate(self, key: str, version: str) -> dict[str, Any]:
        definition = self.definition(key)
        version = str(version or "").strip()
        if not version.isdigit() or int(version) < 1:
            raise PromptManagementError("version must be a positive integer")
        versions = self._list_versions(definition)
        selected = next((item for item in versions if str(item["version"]) == version), None)
        if selected is None:
            raise PromptManagementError("prompt version does not exist")
        table = self.selection_table
        if table is None:
            raise PromptManagementError("prompt selection storage is not configured")
        import time

        updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        table.put_item(Item={
            "PromptKey": key,
            "PromptArn": os.environ.get(definition.base_arn_env, "").strip(),
            "ActiveVersion": version,
            "ActiveArn": selected["arn"],
            "UpdatedAt": updated_at,
        })
        return {"prompt_key": key, "version": version, "arn": selected["arn"], "updated_at": updated_at, "status": "active"}

    def preview(self, key: str, variables: Mapping[str, Any], *, version: str | None = None) -> dict[str, Any]:
        definition = self.definition(key)
        version_arn: str | None = None
        draft = not version
        if version:
            if self.source == "local":
                version_arn = "local://northstar/" + key + ":LOCAL"
            else:
                base_arn = self._require_bedrock(definition)
                version_arn = f"{base_arn}:{version}"
        rendered = self.render(key, variables, version_arn=version_arn, draft=draft)
        return {"key": key, "version": rendered.version, "version_arn": rendered.version_arn, "source": rendered.source, "text": rendered.text}


def render_runtime_prompt(key: str, context: Mapping[str, Any]) -> RenderedPrompt:
    """Render one runtime prompt using the configured source."""

    return PromptManager().render(key, context)
