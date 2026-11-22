#!/usr/bin/env python3
"""Resolve independent Northstar CloudFormation stack plans with JSON context.

This deliberately does not use CloudFormation exports/imports. Each stack's
ordinary Outputs are read with ``describe-stacks`` and mapped to
``CF_OUT_<OutputKey>`` values for the next matching JSON configuration file.
Stack writes are owned by ``sam_deploy.py`` so every deployment receives the
same SAM progress and CloudFormation change-set behavior; this module provides
the repository-wide planner and context helpers used by that adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, load_manifest
    from .scripts.replace_environment_vars import (
        UnresolvedVariable,
        configuration_values,
        load_configuration,
        resolve_configuration,
    )
    from .scripts.validate_stack_contract import validate
except ImportError:  # Running as ``python cloudformation/deploy.py``.
    from manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, load_manifest
    from scripts.replace_environment_vars import UnresolvedVariable, configuration_values, load_configuration, resolve_configuration
    from scripts.validate_stack_contract import validate


# Compatibility for callers that used the old tuple.  The value is derived from
# the manifest rather than being a second source of stack truth.
STACK_ORDER = load_manifest(DEFAULT_MANIFEST_PATH).stack_ids
PLACEHOLDER = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


@dataclass(frozen=True)
class StackPlan:
    stack_id: str
    stack_name: str
    template_path: str
    config_path: str
    parameters: dict[str, str]
    unresolved: list[str]
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stack_id": self.stack_id,
            "stack_name": self.stack_name,
            "template_path": self.template_path,
            "config_path": self.config_path,
            "parameters": self.parameters,
            "unresolved": self.unresolved,
            "depends_on": list(self.depends_on),
        }


def parse_overrides(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--set requires KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"--set key cannot be empty: {item!r}")
        result[key] = value
    return result


def load_context(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"deployment context must be an object: {path}")
    return {str(key): str(value) for key, value in payload.items()}


def save_context(path: Path, context: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(context.items())), indent=2) + "\n", encoding="utf-8")


def stack_name(
    environment: str,
    stack_id: str,
    *,
    manifest: DeploymentManifest | None = None,
) -> str:
    return (manifest or load_manifest()).stack_name(environment, stack_id)


def stack_ids(
    selected: str | None,
    start: str | None,
    *,
    manifest: DeploymentManifest | None = None,
) -> list[str]:
    return list((manifest or load_manifest()).select_stack_ids(selected, start))


def build_plan(
    stack_id: str,
    *,
    environment: str,
    context: dict[str, str],
    overrides: dict[str, str],
    allow_unresolved: bool,
    stacks_dir: Path | None = None,
    manifest: DeploymentManifest | None = None,
) -> tuple[StackPlan, dict[str, str]]:
    if manifest is not None:
        definition = manifest.definition(stack_id)
        template_path = definition.template_path
        config_path = definition.config_path
        dependencies = definition.depends_on
        resolved_stack_name = manifest.stack_name(environment, stack_id)
    elif stacks_dir is not None:
        # Backwards-compatible helper behavior for callers that provide a
        # temporary stack directory.  Normal deployment entry points always
        # pass the validated manifest.
        template_path = stacks_dir / f"{stack_id}.yaml"
        config_path = template_path.with_suffix(".json")
        dependencies = ()
        resolved_stack_name = f"northstar-{environment}-{stack_id}"
    else:
        manifest = load_manifest()
        definition = manifest.definition(stack_id)
        template_path = definition.template_path
        config_path = definition.config_path
        dependencies = definition.depends_on
        resolved_stack_name = manifest.stack_name(environment, stack_id)
    if not template_path.exists() or not config_path.exists():
        raise FileNotFoundError(f"missing matching stack files for {stack_id}: {template_path}, {config_path}")
    errors = validate(template_path, config_path)
    if errors:
        raise ValueError("; ".join(errors))
    raw_config = load_configuration(config_path)
    merged_overrides = {"EnvironmentName": environment, "ENVIRONMENT_NAME": environment, **overrides}
    try:
        resolved = resolve_configuration(
            raw_config,
            context=context,
            overrides=merged_overrides,
            allow_unresolved=allow_unresolved,
        )
    except UnresolvedVariable:
        raise
    values = configuration_values(resolved)
    unresolved = sorted({match.group(0) for value in values.values() for match in PLACEHOLDER.finditer(value)})
    plan = StackPlan(
        stack_id=stack_id,
        stack_name=resolved_stack_name,
        template_path=str(template_path),
        config_path=str(config_path),
        parameters=values,
        unresolved=unresolved,
        depends_on=tuple(dependencies),
    )
    return plan, context


def _boto3_client(region: str):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for AWS context hydration and stack outputs; install the deployment dependency") from exc
    return boto3.client("cloudformation", region_name=region)


def output_context(client: Any, name: str, context: dict[str, str]) -> dict[str, str]:
    stack = client.describe_stacks(StackName=name)["Stacks"][0]
    for output in stack.get("Outputs", []):
        context[f"CF_OUT_{output['OutputKey']}"] = str(output.get("OutputValue", ""))
    return context


def hydrate_prior_outputs(
    client: Any,
    *,
    environment: str,
    first_stack: str,
    context: dict[str, str],
    manifest: DeploymentManifest | None = None,
) -> dict[str, str]:
    """Rebuild the prior-stack portion of context from CloudFormation.

    A deployment context file is a convenience cache, not the source of truth.
    When a caller resumes at a later stack and the cache is missing or stale,
    read the ordinary Outputs of the already-deployed predecessor stacks. This
    keeps stacks independent without introducing CloudFormation imports.
    """

    deployment_manifest = manifest or load_manifest()
    for predecessor in deployment_manifest.predecessors(first_stack):
        name = stack_name(environment, predecessor, manifest=deployment_manifest)
        try:
            context = output_context(client, name, context)
        except Exception as exc:
            raise RuntimeError(
                f"cannot hydrate outputs for predecessor stack {name}; "
                "deploy it first or provide a complete --context-file"
            ) from exc
    return context


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT_NAME", "dev"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--stack")
    parser.add_argument("--from", dest="start_stack")
    parser.add_argument("--plan", action="store_true", help="resolve and validate only; never call AWS")
    # Keep the old flags parseable long enough to provide a migration message
    # instead of an opaque argparse error. They are intentionally hidden: SAM
    # is the only supported stack write path now.
    parser.add_argument("--change-set", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--execute-change-set", help=argparse.SUPPRESS)
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--allow-unresolved", action="store_true", help="leave CF_OUT placeholders in a local plan")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute or args.change_set or args.execute_change_set:
        raise SystemExit(
            "deploy.py is a plan/context helper; use cloudformation/sam_deploy.py "
            "for every stack deployment"
        )
    if not args.plan:
        raise SystemExit("choose --plan; stack deployments use cloudformation/sam_deploy.py")
    try:
        manifest = load_manifest(args.manifest)
        selected_stack_ids = stack_ids(args.stack, args.start_stack, manifest=manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    overrides = parse_overrides(args.overrides)
    context_path = args.context_file or Path(__file__).parent / "build" / f"deployment-context-{args.environment}.json"
    context = load_context(context_path)
    context.update({"EnvironmentName": args.environment, "ENVIRONMENT_NAME": args.environment})
    plans: list[dict[str, Any]] = []
    for stack_id in selected_stack_ids:
        plan, context = build_plan(
            stack_id,
            environment=args.environment,
            context=context,
            overrides=overrides,
            allow_unresolved=args.allow_unresolved or args.plan,
            manifest=manifest,
        )
        item = plan.to_dict()
        plans.append(item)
    print(
        json.dumps(
            {
                "environment": args.environment,
                "region": args.region,
                "manifest": str(manifest.path),
                "context_file": str(context_path),
                "stacks": plans,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
