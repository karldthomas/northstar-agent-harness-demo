from __future__ import annotations

import pytest

from cloudformation.cleanup import build_parser, empty_versioned_bucket, planned_stack_names, validate_environment
from cloudformation.manifest import load_manifest


def test_cleanup_names_follow_manifest_in_reverse_order() -> None:
    manifest = load_manifest()

    assert planned_stack_names(manifest, "dev") == tuple(
        f"northstar-dev-{stack_id}" for stack_id in reversed(manifest.stack_ids)
    )


def test_cleanup_requires_safe_environment_names() -> None:
    assert validate_environment("demo-01") == "demo-01"

    with pytest.raises(ValueError):
        validate_environment("../prod")


def test_empty_versioned_bucket_deletes_versions_and_markers() -> None:
    class FakeS3:
        def __init__(self) -> None:
            self.calls = 0
            self.deleted: list[dict[str, str]] = []

        def list_object_versions(self, **_: object) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                return {
                    "Versions": [{"Key": "a", "VersionId": "v1"}],
                    "DeleteMarkers": [{"Key": "b", "VersionId": "m1"}],
                }
            return {}

        def delete_objects(self, *, Delete: dict[str, object], **_: object) -> dict[str, object]:
            self.deleted.extend(Delete["Objects"])  # type: ignore[arg-type]
            return {}

    s3 = FakeS3()

    assert empty_versioned_bucket(s3, "northstar-dev-example") == 2
    assert s3.deleted == [
        {"Key": "a", "VersionId": "v1"},
        {"Key": "b", "VersionId": "m1"},
    ]


def test_execute_requires_exact_environment_confirmation() -> None:
    args = build_parser().parse_args(["--environment", "dev", "--execute"])

    assert args.execute is True
    assert args.confirm_environment is None
