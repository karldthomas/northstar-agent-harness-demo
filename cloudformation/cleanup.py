#!/usr/bin/env python3
"""Safely plan or execute cleanup of one Northstar environment.

The default mode is a non-destructive plan.  ``--execute`` is deliberately
paired with ``--confirm-environment`` so a cleanup cannot be triggered by an
ambiguous shell variable or an accidentally selected environment.  Versioned
S3 buckets are emptied only after they are discovered as resources of the
manifest-derived Northstar stacks.
"""

from __future__ import annotations

import argparse
import re
from typing import Any, Iterable

import boto3
from botocore.exceptions import ClientError

try:
    from .manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, load_manifest
except ImportError:  # Running as ``python cloudformation/cleanup.py``.
    from manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, load_manifest


ENVIRONMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")


def validate_environment(environment: str) -> str:
    """Validate the environment before using it to form stack names."""

    if not ENVIRONMENT_PATTERN.fullmatch(environment):
        raise ValueError("environment must be 1-32 letters, numbers, or hyphens")
    return environment


def planned_stack_names(manifest: DeploymentManifest, environment: str) -> tuple[str, ...]:
    """Return the manifest stack names in safe deletion order."""

    validate_environment(environment)
    return tuple(manifest.stack_name(environment, stack_id) for stack_id in reversed(manifest.stack_ids))


def _resource_pages(cloudformation: Any, stack_name: str) -> Iterable[dict[str, Any]]:
    paginator = cloudformation.get_paginator("describe_stack_resources")
    yield from paginator.paginate(StackName=stack_name)


def discover_buckets(cloudformation: Any, stack_names: Iterable[str]) -> tuple[str, ...]:
    """Find only S3 buckets owned by the exact manifest-derived stacks."""

    buckets: set[str] = set()
    for stack_name in stack_names:
        try:
            pages = _resource_pages(cloudformation, stack_name)
            for page in pages:
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("ResourceType") == "AWS::S3::Bucket":
                        physical_id = resource.get("PhysicalResourceId")
                        if physical_id:
                            buckets.add(physical_id)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") in {"ValidationError", "ResourceNotFoundException"}:
                continue
            raise
    return tuple(sorted(buckets))


def _chunks(items: list[dict[str, str]], size: int = 1000) -> Iterable[list[dict[str, str]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def empty_versioned_bucket(s3: Any, bucket: str) -> int:
    """Delete all object versions and delete markers from one exact bucket."""

    deleted = 0
    while True:
        response = s3.list_object_versions(Bucket=bucket, MaxKeys=1000)
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for field in ("Versions", "DeleteMarkers")
            for item in response.get(field, [])
        ]
        if not objects:
            return deleted
        for chunk in _chunks(objects):
            response = s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True}) or {}
            errors = response.get("Errors", [])
            if errors:
                raise RuntimeError(f"S3 cleanup failed for {bucket}: {errors!r}")
            deleted += len(chunk)


def delete_stacks(cloudformation: Any, stack_names: Iterable[str]) -> None:
    """Delete stacks in reverse dependency order and wait for completion."""

    for stack_name in stack_names:
        try:
            cloudformation.delete_stack(StackName=stack_name)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") in {"ValidationError", "ResourceNotFoundException"}:
                continue
            raise
        cloudformation.get_waiter("stack_delete_complete").wait(StackName=stack_name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--manifest", type=str, default=str(DEFAULT_MANIFEST_PATH))
    parser.add_argument("--execute", action="store_true", help="perform cleanup; otherwise print a plan")
    parser.add_argument(
        "--confirm-environment",
        help="required exact environment value when --execute is supplied",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        environment = validate_environment(args.environment)
        manifest = load_manifest(args.manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    stack_names = planned_stack_names(manifest, environment)
    if args.execute and args.confirm_environment != environment:
        raise SystemExit(
            "--execute requires --confirm-environment to exactly match "
            f"--environment {environment!r}"
        )

    print(f"Environment: {environment}")
    print(f"Region: {args.region}")
    print("Deletion order:")
    for stack_name in stack_names:
        print(f"  - {stack_name}")

    if not args.execute:
        print("Plan only: no AWS resources were changed.")
        return 0

    cloudformation = boto3.client("cloudformation", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    buckets = discover_buckets(cloudformation, stack_names)
    print("Versioned buckets to empty:")
    for bucket in buckets:
        print(f"  - {bucket}")
    for bucket in buckets:
        deleted = empty_versioned_bucket(s3, bucket)
        print(f"Emptied {bucket}: {deleted} object versions/markers")
    delete_stacks(cloudformation, stack_names)
    print("Cleanup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
