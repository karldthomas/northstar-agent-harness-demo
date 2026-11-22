#!/usr/bin/env python3
"""Validate the independent Northstar CloudFormation deployment contract.

This is intentionally separate from ``deploy.py``. It performs repository
validation without creating or updating AWS resources: matching YAML/JSON
files, parameter contracts, output-context dependencies, forbidden coupling,
and (when installed) cfn-lint validation.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

try:
    from .manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, load_manifest
    from .scripts.validate_stack_contract import validate as validate_contract
except ImportError:  # Running as ``python cloudformation/validate.py``.
    from manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, load_manifest
    from scripts.validate_stack_contract import validate as validate_contract


PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
TOP_LEVEL = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s*$")
CHILD_KEY = re.compile(r"^  ([A-Za-z][A-Za-z0-9-]*):\s*$")
FORBIDDEN = ("Fn::ImportValue", "Export:", "AWS::CloudFormation::Stack")

# The installed cfn-lint catalog can lag newly released AWS resource types.
# These checks are deferred here because the templates are validated against
# the live CloudFormation service at deployment time as well.
NEW_RESOURCE_CATALOG_CHECKS = ("E3001", "E3002", "E1010", "E1019")


def output_names(template: str) -> set[str]:
    names: set[str] = set()
    in_outputs = False
    for line in template.splitlines():
        if line == "Outputs:":
            in_outputs = True
            continue
        if in_outputs and TOP_LEVEL.match(line):
            break
        if in_outputs:
            match = CHILD_KEY.match(line)
            if match:
                names.add(match.group(1))
    return names


def configured_placeholders(config_path: Path) -> set[str]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    placeholders: set[str] = set()
    for entry in payload.get("Configuration", []):
        if entry.get("CreateParameter", True) is False:
            continue
        placeholders.update(PLACEHOLDER.findall(str(entry.get("Value", ""))))
    return placeholders


def _stack_paths(stacks_dir: Path | None, manifest: DeploymentManifest, stack_id: str) -> tuple[Path, Path]:
    if stacks_dir is not None:
        template_path = stacks_dir / f"{stack_id}.yaml"
        return template_path, template_path.with_suffix(".json")
    definition = manifest.definition(stack_id)
    return definition.template_path, definition.config_path


def validate_repository(
    stacks_dir: Path | None = None,
    *,
    manifest: DeploymentManifest | None = None,
) -> list[str]:
    deployment_manifest = manifest or load_manifest(DEFAULT_MANIFEST_PATH)
    if stacks_dir is None:
        stacks_dir = deployment_manifest.path.parent / "stacks"
    errors: list[str] = []
    templates = {path.stem for path in stacks_dir.glob("*.yaml")}
    expected = set(deployment_manifest.stack_ids)
    if templates != expected:
        errors.append(f"expected stack templates {sorted(expected)}, found {sorted(templates)}")

    producer_outputs: dict[str, set[str]] = {}
    for stack_id in deployment_manifest.stack_ids:
        template_path, config_path = _stack_paths(stacks_dir, deployment_manifest, stack_id)
        if not template_path.exists():
            errors.append(f"missing template: {template_path}")
            continue
        if not config_path.exists():
            errors.append(f"missing matching configuration: {config_path}")
            continue
        errors.extend(f"{stack_id}: {error}" for error in validate_contract(template_path, config_path))
        template = template_path.read_text(encoding="utf-8")
        for marker in FORBIDDEN:
            if marker in template:
                errors.append(f"{stack_id}: forbidden CloudFormation coupling {marker}")
        producer_outputs[stack_id] = output_names(template)

        prior_outputs = set().union(
            *(producer_outputs.get(previous, set()) for previous in deployment_manifest.predecessors(stack_id))
        )
        for placeholder in sorted(configured_placeholders(config_path)):
            if not placeholder.startswith("CF_OUT_"):
                continue
            output_key = placeholder.removeprefix("CF_OUT_")
            if output_key not in prior_outputs:
                errors.append(
                    f"{stack_id}: {placeholder} is not produced by an earlier stack output"
                )
    return errors


def run_cfn_lint(
    stacks_dir: Path | None = None,
    *,
    manifest: DeploymentManifest | None = None,
) -> tuple[list[str], list[str]]:
    deployment_manifest = manifest or load_manifest(DEFAULT_MANIFEST_PATH)
    if stacks_dir is None:
        stacks_dir = deployment_manifest.path.parent / "stacks"
    executable = shutil.which("cfn-lint")
    if not executable:
        return [], ["cfn-lint is not installed; skipped template schema validation"]
    command = [
        executable,
        "--ignore-checks",
        *NEW_RESOURCE_CATALOG_CHECKS,
        "--non-zero-exit-code",
        "error",
        "--template",
        *[
            str(_stack_paths(stacks_dir, deployment_manifest, stack_id)[0])
            for stack_id in deployment_manifest.stack_ids
        ],
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    if result.returncode:
        return [f"cfn-lint failed (exit {result.returncode}):\n{output}"], []
    return [], ([f"cfn-lint: {output}"] if output else [])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--stacks-dir", type=Path)
    parser.add_argument("--skip-cfn-lint", action="store_true")
    parser.add_argument("--require-cfn-lint", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    errors = validate_repository(args.stacks_dir, manifest=manifest)
    warnings: list[str] = []
    if not args.skip_cfn_lint:
        if args.require_cfn_lint and shutil.which("cfn-lint") is None:
            errors.append("cfn-lint is required but is not installed")
        else:
            lint_errors, lint_warnings = run_cfn_lint(args.stacks_dir, manifest=manifest)
            errors.extend(lint_errors)
            warnings.extend(lint_warnings)
    for warning in warnings:
        print(f"WARNING: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"validated {len(manifest.stack_ids)} independent CloudFormation stack contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
