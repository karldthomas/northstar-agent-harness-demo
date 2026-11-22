#!/usr/bin/env python3
"""Deploy one Northstar stack through AWS SAM.

Stacks remain independent so ordinary CloudFormation outputs can flow through
the deployment context without exports or nested stacks. This adapter owns
the SAM build/deploy boundary; target-specific source packaging belongs in the
Northstar stack template and its declared CodeUri paths.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

try:
    from .deploy import (
        _boto3_client,
        build_plan,
        hydrate_prior_outputs,
        load_context,
        output_context,
        parse_overrides,
        save_context,
    )
    from .manifest import DEFAULT_MANIFEST_PATH, load_manifest
except ImportError:  # Running as ``python cloudformation/sam_deploy.py``.
    from deploy import (
        _boto3_client,
        build_plan,
        hydrate_prior_outputs,
        load_context,
        output_context,
        parse_overrides,
        save_context,
    )
    from manifest import DEFAULT_MANIFEST_PATH, load_manifest


ROOT = Path(__file__).resolve().parent.parent
SAM_TMP_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "northstar-sam"


def _run(command: list[str]) -> None:
    print("+ " + " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def _uses_sam_transform(template_path: Path) -> bool:
    return "Transform: AWS::Serverless-2016-10-31" in template_path.read_text(encoding="utf-8")


def _build_dir(value: Path, stack_id: str) -> Path:
    directory = value if value.is_absolute() else ROOT / value
    return directory / stack_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT_NAME", "dev"))
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--s3-bucket", help="SAM staging bucket")
    parser.add_argument("--s3-prefix", default=os.environ.get("SAM_S3_PREFIX", "northstar-sam"))
    parser.add_argument(
        "--resolve-s3",
        action="store_true",
        help="ask SAM to create or resolve its managed staging bucket",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=SAM_TMP_ROOT / "build",
        help="parent directory for SAM build output",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=SAM_TMP_ROOT / "cache",
        help="directory for SAM dependency/build cache",
    )
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--sam-command", default=os.environ.get("SAM_COMMAND", "sam"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        manifest.definition(args.stack)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.s3_bucket and args.resolve_s3:
        raise SystemExit("--s3-bucket and --resolve-s3 cannot be used together")

    context_path = args.context_file or ROOT / "cloudformation" / "build" / f"deployment-context-{args.environment}.json"
    context = load_context(context_path)
    context.update({"EnvironmentName": args.environment, "ENVIRONMENT_NAME": args.environment})

    client = _boto3_client(args.region)
    context = hydrate_prior_outputs(
        client,
        environment=args.environment,
        first_stack=args.stack,
        context=context,
        manifest=manifest,
    )
    plan, _ = build_plan(
        args.stack,
        environment=args.environment,
        context=context,
        overrides=parse_overrides(args.overrides),
        allow_unresolved=False,
        manifest=manifest,
    )
    if plan.unresolved:
        raise SystemExit(f"unresolved values for {args.stack}: {', '.join(plan.unresolved)}")

    parameter_overrides = [
        f"{key}={value}"
        for key, value in sorted(plan.parameters.items())
        if value != ""
    ]
    if not _uses_sam_transform(Path(plan.template_path)):
        deploy_command = [
            "aws",
            "cloudformation",
            "deploy",
            "--template-file",
            plan.template_path,
            "--stack-name",
            plan.stack_name,
            "--region",
            args.region,
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "CAPABILITY_AUTO_EXPAND",
            "--no-fail-on-empty-changeset",
            "--parameter-overrides",
            *parameter_overrides,
        ]
        _run(deploy_command)
        context = output_context(client, plan.stack_name, context)
        save_context(context_path, context)
        print(
            json.dumps(
                {
                    "environment": args.environment,
                    "region": args.region,
                    "context_file": str(context_path),
                    "stack": plan.stack_id,
                    "stack_name": plan.stack_name,
                    "build_dir": None,
                    "deployment_mode": "cloudformation",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    build_dir = _build_dir(args.build_dir, args.stack)
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    built_template = build_dir / "template.yaml"
    _run(
        [
            args.sam_command,
            "build",
            "--template-file",
            plan.template_path,
            "--build-dir",
            str(build_dir),
            "--cache-dir",
            str(args.cache_dir),
            "--cached",
        ]
    )
    if not built_template.exists():
        raise RuntimeError(f"SAM did not produce the translated template: {built_template}")

    deploy_command = [
        args.sam_command,
        "deploy",
        "--template-file",
        str(built_template),
        "--stack-name",
        plan.stack_name,
        "--region",
        args.region,
    ]
    if args.s3_bucket:
        deploy_command.extend(
            [
                "--s3-bucket",
                args.s3_bucket,
                "--s3-prefix",
                f"{args.s3_prefix.rstrip('/')}/{args.environment}/{args.stack}",
            ]
        )
    elif args.resolve_s3:
        deploy_command.append("--resolve-s3")
    deploy_command.extend(
        [
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "CAPABILITY_AUTO_EXPAND",
            "--no-confirm-changeset",
            "--no-fail-on-empty-changeset",
            "--parameter-overrides",
            *parameter_overrides,
        ]
    )
    _run(deploy_command)

    context = output_context(client, plan.stack_name, context)
    save_context(context_path, context)
    print(
        json.dumps(
            {
                "environment": args.environment,
                "region": args.region,
                "context_file": str(context_path),
                "stack": plan.stack_id,
                "stack_name": plan.stack_name,
                "build_dir": str(build_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
