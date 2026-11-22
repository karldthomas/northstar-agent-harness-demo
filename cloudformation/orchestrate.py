#!/usr/bin/env python3
"""Run the manifest-defined Northstar deployment workflow.

``deploy-all.sh`` is only a shell entry point. This module parses the
deployment manifest, executes its allow-listed lifecycle handlers at declared
stack boundaries, and delegates each CloudFormation stack to ``sam_deploy.py``.
AWS credentials are deliberately never selected here; boto3, the AWS CLI, and
SAM use the ambient environment.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .deploy import load_context, save_context
    from .manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, LifecycleStep, load_manifest
except ImportError:  # Running as ``python cloudformation/orchestrate.py``.
    from deploy import load_context, save_context
    from manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, LifecycleStep, load_manifest


ROOT = Path(__file__).resolve().parent.parent
SAM_DEPLOY = Path(__file__).with_name("sam_deploy.py")
VALIDATE = Path(__file__).with_name("validate.py")

HANDLERS = frozenset(
    {
        "run_tests",
        "validate_contracts",
        "check_aws_identity",
        "build_web",
        "publish_web",
        "build_runtime_image",
        "run_harness_smoke",
        "run_web_smoke",
    }
)


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    capture_output: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command))
    return subprocess.run(
        command,
        cwd=cwd or ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _boto3_client(service: str, region: str) -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise SystemExit(
            "boto3 is required for deployment orchestration; install it locally with: "
            "python3 -m pip install --user boto3"
        ) from exc
    return boto3.client(service, region_name=region)


def update_release_context(
    context: dict[str, Any],
    *,
    release_id: str,
    web_release_published: bool,
) -> dict[str, Any]:
    """Keep web artifact identity stable across stack-only retries."""

    updated = dict(context)
    updated["NORTHSTAR_DEPLOYMENT_RELEASE_ID"] = release_id
    if web_release_published or not str(updated.get("NORTHSTAR_RELEASE_ID") or "").strip():
        updated["NORTHSTAR_RELEASE_ID"] = release_id
    return updated


class DeploymentRunner:
    """Execute lifecycle handlers and stack deployments for one release."""

    def __init__(self, args: argparse.Namespace, manifest: DeploymentManifest) -> None:
        self.args = args
        self.manifest = manifest
        self.environment = args.environment
        self.region = args.region
        self.release_id = args.release_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        self.runtime_image_tag = args.runtime_image_tag or f"{self.environment}-{self.release_id}"
        self.sam_s3_prefix = args.sam_s3_prefix or f"northstar-sam-{self.release_id}"
        self.stack_ids = manifest.select_stack_ids(args.stack, args.start_stack)
        self.context_path = args.context_file or ROOT / "cloudformation" / "build" / f"deployment-context-{self.environment}.json"
        self.context_path = self.context_path if self.context_path.is_absolute() else ROOT / self.context_path
        self.image_platform = args.image_platform
        self.child_env = os.environ.copy()
        self.child_env.update(
            {
                "ENVIRONMENT_NAME": self.environment,
                "AWS_REGION": self.region,
                "AWS_DEFAULT_REGION": self.region,
                "RELEASE_ID": self.release_id,
                "SAM_S3_PREFIX": self.sam_s3_prefix,
            }
        )
        self.artifact_bucket: str | None = None
        self.runtime_image_uri: str | None = None
        self.deployed: set[str] = set()
        self.completed_steps: set[str] = set()
        # Delay SDK client construction until after preflight steps so local
        # validation remains useful without credentials or a metadata endpoint.
        self.cloudformation: Any | None = None

    def validate_handlers(self) -> None:
        unknown = sorted({step.handler for step in self.manifest.steps} - HANDLERS)
        if unknown:
            raise SystemExit(
                "deployment manifest contains unsupported lifecycle handler(s): "
                + ", ".join(unknown)
            )

    def _stack_name(self, stack_id: str) -> str:
        return self.manifest.stack_name(self.environment, stack_id)

    def _cloudformation_client(self) -> Any:
        if self.cloudformation is None:
            self.cloudformation = _boto3_client("cloudformation", self.region)
        return self.cloudformation

    def _stack_output(self, stack_id: str, key: str) -> str:
        client = self._cloudformation_client()
        try:
            response = client.describe_stacks(StackName=self._stack_name(stack_id))
        except Exception as exc:
            raise RuntimeError(f"could not read outputs for stack {stack_id}") from exc
        stacks = response.get("Stacks", [])
        if not stacks:
            raise RuntimeError(f"stack {stack_id} returned no stack record")
        for output in stacks[0].get("Outputs", []):
            if output.get("OutputKey") == key and output.get("OutputValue"):
                return str(output["OutputValue"])
        raise RuntimeError(f"missing {key} output from {stack_id}")

    def run_tests(self) -> None:
        _run([sys.executable, "-m", "pytest"], env=self.child_env)

    def validate_contracts(self) -> None:
        _run(
            [sys.executable, str(VALIDATE), "--manifest", str(self.manifest.path)],
            env=self.child_env,
        )

    def check_aws_identity(self) -> None:
        _run(["aws", "--region", self.region, "sts", "get-caller-identity"], env=self.child_env)

    def build_web(self) -> None:
        """Build the self-contained React bundle before SAM packages Lambda."""

        web_root = ROOT / "apps" / "web"
        web_env = {
            **self.child_env,
            # The deployed console is same-origin with the Lambda Function URL.
            # This keeps the browser/API boundary direct and avoids baking a
            # stale environment URL into a release artifact.
            "VITE_NORTHSTAR_LAMBDA_URL": "",
            "VITE_NORTHSTAR_DEV_MODE": "false",
        }
        _run(["npm", "ci", "--ignore-scripts"], env=web_env, cwd=web_root)
        _run(["npm", "run", "build"], env=web_env, cwd=web_root)
        index = web_root / "dist" / "index.html"
        if not index.is_file():
            raise RuntimeError(f"web build did not produce {index}")

    def publish_web(self) -> None:
        """Publish an auditable private release artifact after the web stack exists."""

        bucket = self._stack_output("05-web", "WebArtifactBucketName")
        dist = ROOT / "apps" / "web" / "dist"
        if not dist.is_dir() or not (dist / "index.html").is_file():
            raise RuntimeError(f"web distribution is missing; run the build lifecycle step first: {dist}")
        s3 = _boto3_client("s3", self.region)
        for path in sorted(item for item in dist.rglob("*") if item.is_file()):
            key = path.relative_to(dist).as_posix()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            cache_control = "no-cache" if path.name == "index.html" else "public,max-age=31536000,immutable"
            s3.upload_file(
                str(path),
                bucket,
                key,
                ExtraArgs={"ContentType": content_type, "CacheControl": cache_control},
            )
        release = {
            "release_id": self.release_id,
            "environment": self.environment,
            "function_url": self._stack_output("02-simulator", "SimulatorFunctionUrl"),
            "artifact_prefix": "",
        }
        s3.put_object(
            Bucket=bucket,
            Key="release.json",
            Body=(json.dumps(release, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-cache",
        )
        context = load_context(self.context_path)
        context.update(
            {
                "NORTHSTAR_RELEASE_ID": self.release_id,
                "NORTHSTAR_WEB_ARTIFACT_BUCKET": bucket,
                "NORTHSTAR_WEB_RELEASE_KEY": "release.json",
            }
        )
        save_context(self.context_path, context)
        print(f"Published web release {self.release_id} to s3://{bucket}/")

    def _configure_docker_auth(
        self,
        repository_uri: str,
        *,
        docker_config: Path,
        env: dict[str, str],
    ) -> None:
        print("+ aws --region " + self.region + " ecr get-login-password > temporary Docker auth config")
        password = subprocess.run(
            ["aws", "--region", self.region, "ecr", "get-login-password"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            check=True,
            text=True,
        )
        token = password.stdout.strip()
        if not token:
            raise RuntimeError("ECR returned an empty authorization token")
        encoded = base64.b64encode(f"AWS:{token}".encode("utf-8")).decode("ascii")
        (docker_config / "config.json").write_text(
            json.dumps({"auths": {repository_uri: {"auth": encoded}}}) + "\n",
            encoding="utf-8",
        )

    def build_runtime_image(self) -> None:
        repository_uri = self._stack_output(self.args.artifact_stack, self.args.repository_output_key)
        repository_name = repository_uri.rsplit("/", 1)[-1]
        with tempfile.TemporaryDirectory(prefix="northstar-docker-config-") as docker_config:
            docker_config_path = Path(docker_config)
            docker_env = {**self.child_env, "DOCKER_CONFIG": str(docker_config_path)}
            configured_docker_dir = Path(self.child_env.get("DOCKER_CONFIG", Path.home() / ".docker"))
            plugin_dir = configured_docker_dir / "cli-plugins"
            if plugin_dir.is_dir():
                (docker_config_path / "cli-plugins").symlink_to(plugin_dir, target_is_directory=True)
            self._configure_docker_auth(repository_uri, docker_config=docker_config_path, env=docker_env)
            _run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    self.image_platform,
                    "--provenance=false",
                    "--tag",
                    f"{repository_uri}:{self.runtime_image_tag}",
                    "--push",
                    str(ROOT),
                ],
                env=docker_env,
            )
        ecr = _boto3_client("ecr", self.region)
        response = ecr.describe_images(
            repositoryName=repository_name,
            imageIds=[{"imageTag": self.runtime_image_tag}],
        )
        details = response.get("imageDetails", [])
        digest = details[0].get("imageDigest") if details else None
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise RuntimeError(
                f"could not resolve the pushed image digest for {repository_uri}:{self.runtime_image_tag}"
            )
        self.runtime_image_uri = f"{repository_uri}@{digest}"
        print(f"Runtime image: {self.runtime_image_uri}")

    def _run_configured_command(self, command: str | None, *, label: str, required: bool) -> None:
        if not command:
            message = f"{label} command is not configured"
            if required:
                raise RuntimeError(message + "; set the corresponding environment variable")
            print(f"Skipping {label}: set the corresponding environment variable to enable it")
            return
        _run(shlex.split(command), env=self.child_env)

    def run_harness_smoke(self) -> None:
        self._run_configured_command(
            self.args.harness_smoke_command,
            label="Harness smoke test",
            required=self.args.require_harness_smoke,
        )

    def run_web_smoke(self) -> None:
        if self.args.web_smoke_command:
            self._run_configured_command(
                self.args.web_smoke_command,
                label="web smoke test",
                required=self.args.require_web_smoke,
            )
            return
        if not self.args.require_web_smoke:
            print("Skipping web smoke test: set --require-web-smoke or --web-smoke-command to enable it")
            return

        url = self._stack_output("05-web", "WebUrl").rstrip("/") + "/"
        try:
            import certifi

            tls = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            tls = ssl.create_default_context()
        request = urllib.request.Request(url, headers={"Accept": "text/html"})
        with urllib.request.urlopen(request, context=tls, timeout=60) as response:
            if response.status != 200 or not response.headers.get("content-type", "").startswith("text/html"):
                raise RuntimeError(f"web smoke root check failed for {url}: HTTP {response.status}")
            html = response.read().decode("utf-8")
        if '<div id="root"></div>' not in html:
            raise RuntimeError("web smoke root check did not return the React root element")
        match = re.search(r'src="([^\"]+\.js)"', html)
        if not match:
            raise RuntimeError("web smoke root check did not expose a bundled JavaScript asset")
        asset_url = urllib.request.urljoin(url, match.group(1))
        with urllib.request.urlopen(
            urllib.request.Request(asset_url, headers={"Accept": "application/javascript"}),
            context=tls,
            timeout=60,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"web smoke asset check failed for {asset_url}: HTTP {response.status}")
            response.read(1)
        print(f"Web smoke passed: {url} and {asset_url}")

    def _overrides(self) -> list[str]:
        if not self.runtime_image_uri:
            return []
        return ["--set", f"{self.args.runtime_image_parameter}={self.runtime_image_uri}"]

    def deploy_stack(self, stack_id: str) -> None:
        command = [
            sys.executable,
            str(SAM_DEPLOY),
            "--manifest",
            str(self.manifest.path),
            "--environment",
            self.environment,
            "--region",
            self.region,
            "--context-file",
            str(self.context_path),
            "--stack",
            stack_id,
        ]
        if self.artifact_bucket:
            command.extend(["--s3-bucket", self.artifact_bucket, "--s3-prefix", self.sam_s3_prefix])
        elif self.args.resolve_s3:
            command.append("--resolve-s3")
        if self.args.sam_command:
            command.extend(["--sam-command", self.args.sam_command])
        command.extend(self._overrides())
        _run(command, env=self.child_env)
        if stack_id == self.args.artifact_stack and self.args.artifact_bucket_output_key:
            try:
                self.artifact_bucket = self._stack_output(stack_id, self.args.artifact_bucket_output_key)
            except RuntimeError:
                if self.args.require_artifact_bucket:
                    raise
                print(
                    f"No {self.args.artifact_bucket_output_key} output found on {stack_id}; "
                    "continuing without a shared SAM artifact bucket"
                )

    def _skip_step(self, step: LifecycleStep) -> bool:
        return (
            (step.handler == "run_tests" and self.args.skip_tests)
            or (step.handler == "validate_contracts" and self.args.skip_validation)
            or (step.handler == "run_harness_smoke" and self.args.skip_harness_smoke)
            or (step.handler == "run_web_smoke" and self.args.skip_web_smoke)
        )

    def execute_step(self, step: LifecycleStep) -> None:
        if step.step_id in self.completed_steps:
            return
        self.completed_steps.add(step.step_id)
        if self._skip_step(step):
            print(f"Skipping lifecycle step: {step.step_id}")
            return
        print(f"\n==> Lifecycle step: {step.step_id}")
        getattr(self, step.handler)()

    def run(self) -> None:
        self.validate_handlers()
        for stack_id in self.stack_ids:
            for step in self.manifest.steps_before(stack_id):
                self.execute_step(step)
            print(f"\n==> Deploying stack: {stack_id}")
            self.deploy_stack(stack_id)
            self.deployed.add(stack_id)
            for step in self.manifest.steps_after(stack_id, self.deployed):
                self.execute_step(step)

        print("\n==> Deployment complete")
        print(f"Environment: {self.environment}")
        print(f"Region: {self.region}")
        if self.args.final_output_key:
            try:
                final_output = self._stack_output(self.stack_ids[-1], self.args.final_output_key)
            except RuntimeError:
                final_output = None
            if final_output:
                print(f"Final output ({self.args.final_output_key}): {final_output}")
        context = update_release_context(
            load_context(self.context_path),
            release_id=self.release_id,
            web_release_published="publish-web" in self.completed_steps,
        )
        context.update(
            {
                "NORTHSTAR_ENVIRONMENT": self.environment,
                "NORTHSTAR_REGION": self.region,
                "NORTHSTAR_DEPLOYED_STACKS": ",".join(self.stack_ids),
            }
        )
        save_context(self.context_path, context)
        print(f"Deployment context: {self.context_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT_NAME", "dev"))
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
    )
    parser.add_argument("--release-id", default=os.environ.get("RELEASE_ID"))
    parser.add_argument("--runtime-image-tag", default=os.environ.get("RUNTIME_IMAGE_TAG"))
    parser.add_argument("--image-platform", default=os.environ.get("IMAGE_PLATFORM", "linux/arm64"))
    parser.add_argument("--stack", help="deploy only this stack; predecessor outputs are hydrated from AWS")
    parser.add_argument(
        "--from",
        dest="start_stack",
        help="deploy this stack and every downstream stack; predecessor outputs are hydrated from AWS",
    )
    parser.add_argument("--sam-s3-prefix", default=os.environ.get("SAM_S3_PREFIX"))
    parser.add_argument("--sam-command", default=os.environ.get("SAM_COMMAND", "sam"))
    parser.add_argument("--resolve-s3", action="store_true", default=os.environ.get("RESOLVE_S3") == "1")
    parser.add_argument("--artifact-stack", default=os.environ.get("ARTIFACT_STACK", "00-foundation"))
    parser.add_argument(
        "--repository-output-key",
        default=os.environ.get("REPOSITORY_OUTPUT_KEY", "RuntimeRepositoryUri"),
    )
    parser.add_argument(
        "--artifact-bucket-output-key",
        default=os.environ.get("ARTIFACT_BUCKET_OUTPUT_KEY", "ArtifactBucketName"),
    )
    parser.add_argument("--require-artifact-bucket", action="store_true")
    parser.add_argument(
        "--runtime-image-parameter",
        default=os.environ.get("RUNTIME_IMAGE_PARAMETER", "RuntimeImageUri"),
    )
    parser.add_argument("--final-output-key", default=os.environ.get("FINAL_OUTPUT_KEY", "WebUrl"))
    parser.add_argument("--harness-smoke-command", default=os.environ.get("HARNESS_SMOKE_COMMAND"))
    parser.add_argument("--web-smoke-command", default=os.environ.get("WEB_SMOKE_COMMAND"))
    parser.add_argument("--require-harness-smoke", action="store_true")
    parser.add_argument("--require-web-smoke", action="store_true")
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--skip-tests", action="store_true", default=os.environ.get("SKIP_TESTS") == "1")
    parser.add_argument("--skip-validation", action="store_true", default=os.environ.get("SKIP_VALIDATION") == "1")
    parser.add_argument("--skip-harness-smoke", action="store_true", default=os.environ.get("SKIP_HARNESS_SMOKE") == "1")
    parser.add_argument("--skip-web-smoke", action="store_true", default=os.environ.get("SKIP_WEB_SMOKE") == "1")
    return parser


def _require_tools(args: argparse.Namespace, manifest: DeploymentManifest) -> None:
    required = {"aws", args.sam_command}
    selected_stack_ids = manifest.select_stack_ids(args.stack, args.start_stack)
    if any(
        step.handler == "build_web" and set(step.before).intersection(selected_stack_ids)
        for step in manifest.steps
    ):
        required.add("npm")
    if any(
        step.handler == "build_runtime_image" and set(step.before).intersection(selected_stack_ids)
        for step in manifest.steps
    ):
        required.add("docker")
    for command in sorted(required):
        if shutil.which(command) is None:
            raise SystemExit(f"{command} is required")
    if "docker" in required:
        result = subprocess.run(
            ["docker", "buildx", "version"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise SystemExit("docker buildx is required")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        _require_tools(args, manifest)
        DeploymentRunner(args, manifest).run()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
