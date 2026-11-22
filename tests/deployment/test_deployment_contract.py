from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cloudformation.deploy import build_plan, hydrate_prior_outputs
from cloudformation.manifest import load_manifest
from cloudformation.orchestrate import build_parser, update_release_context
from cloudformation.scripts.replace_environment_vars import resolve_configuration
from cloudformation.validate import validate_repository


ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / "cloudformation" / "deployment.yaml"
STACKS = ROOT / "cloudformation" / "stacks"


def test_northstar_manifest_has_stable_order_and_downstream_dependencies() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    assert manifest.name == "northstar"
    assert manifest.stack_ids == (
        "00-foundation",
        "01-state",
        "02-simulator",
        "03-harness",
        "04-application",
        "05-web",
        "06-observability",
    )
    assert manifest.predecessors("06-observability") == (
        "00-foundation",
        "01-state",
        "02-simulator",
        "03-harness",
        "04-application",
    )
    assert manifest.predecessors("05-web") == (
        "00-foundation",
        "01-state",
        "02-simulator",
        "03-harness",
        "04-application",
    )
    assert manifest.stack_name("dev", "03-harness") == "northstar-dev-03-harness"


def test_all_northstar_stack_contracts_validate_without_forbidden_coupling() -> None:
    assert validate_repository() == []
    for template in STACKS.glob("*.yaml"):
        text = template.read_text(encoding="utf-8")
        assert "ledgerflow" not in text.lower()
        assert "Fn::ImportValue" not in text
        assert "AWS::CloudFormation::Stack" not in text
        assert template.with_suffix(".json").exists()


def test_web_stack_consumes_the_direct_simulator_function_url() -> None:
    template = (STACKS / "05-web.yaml").read_text(encoding="utf-8")
    config = json.loads((STACKS / "05-web.json").read_text(encoding="utf-8"))
    configured = {entry["Key"]: entry["Value"] for entry in config["Configuration"]}

    assert "SimulatorFunctionUrl:" in template
    assert configured["SimulatorFunctionUrl"] == "${CF_OUT_SimulatorFunctionUrl}"
    assert "WebArtifactBucketName:" in template
    assert "BlockPublicAcls: true" in template
    assert "RestrictPublicBuckets: true" in template


def test_simulator_deployment_declares_runtime_guardrails() -> None:
    simulator = (STACKS / "02-simulator.yaml").read_text(encoding="utf-8")
    state = (STACKS / "01-state.yaml").read_text(encoding="utf-8")

    assert "NORTHSTAR_RATE_LIMIT_ENABLED: 'true'" in simulator
    assert "ReservedConcurrentExecutions: 10" in simulator
    assert "SimulatorInvocationBudgetAlarm:" in simulator
    assert "Threshold: 5000" in simulator
    assert "NORTHSTAR_STATE_TABLE_NAME: !Sub northstar-${EnvironmentName}-scenario-state" in simulator
    assert "PersistScenarioRunStateAndEvents" in simulator
    assert "dynamodb:GetItem" in simulator
    assert "BlockPublicAcls: true" in state


def test_prompt_management_deployment_contract_is_native_and_admin_scoped() -> None:
    state = (STACKS / "01-state.yaml").read_text(encoding="utf-8")
    simulator = (STACKS / "02-simulator.yaml").read_text(encoding="utf-8")
    harness = (STACKS / "03-harness.yaml").read_text(encoding="utf-8")
    harness_config = json.loads((STACKS / "03-harness.json").read_text(encoding="utf-8"))
    configured = {entry["Key"]: entry["Value"] for entry in harness_config["Configuration"]}

    assert "PromptSelectionsTable:" in state
    assert "TableName: !Sub northstar-${EnvironmentName}-prompt-selections" in state
    assert "DeletionPolicy: Retain" in state
    assert "NORTHSTAR_PROMPT_SOURCE: bedrock" in simulator
    assert "NORTHSTAR_PROMPT_SELECTIONS_TABLE_NAME" in simulator
    assert "PromptSelectionStorage" in simulator
    assert "bedrock:GetPrompt" in simulator
    assert "bedrock:UpdatePrompt" in simulator
    assert "bedrock:CreatePromptVersion" in simulator
    assert "AWS::Bedrock::Prompt" in harness
    assert "AWS::Bedrock::PromptVersion" in harness
    for output in (
        "InvestigationPromptArn",
        "InvestigationPromptVersionArn",
        "ApprovalResumptionPromptArn",
        "ApprovalResumptionPromptVersionArn",
        "EvaluationPromptArn",
        "EvaluationPromptVersionArn",
    ):
        assert f"  {output}:" in harness
    assert configured["PromptModelId"] == "${PROMPT_MODEL_ID}"


def test_gateway_role_is_scoped_to_the_lambda_target() -> None:
    foundation = (STACKS / "00-foundation.yaml").read_text(encoding="utf-8")

    assert "execute-api:Invoke" not in foundation
    assert "function:northstar-${EnvironmentName}-simulator" in foundation
    assert "gateway/northstar-${EnvironmentName}-*" in foundation
    assert ":gateway/*" not in foundation
    assert "foundation-model/amazon.nova-2-lite-v1:0" in foundation
    assert "inference-profile/us.amazon.nova-2-lite-v1:0" in foundation
    assert "memory/northstar_${EnvironmentName}*_harness-*" in foundation
    assert "memory/harness_*" not in foundation
    assert "memory/northstar_*" not in foundation
    assert "log-group:/aws/bedrock-agentcore/runtimes/harness_northstar_${EnvironmentName}_harness-*" in foundation
    assert "log-group:/aws/bedrock-agentcore/*\n" not in foundation
    assert "northstar-${EnvironmentName}-01-state-fixturebucket-*" in foundation
    assert "s3:::northstar-${EnvironmentName}-*\n" not in foundation


def test_stack_only_retry_preserves_published_web_release_id() -> None:
    existing = {
        "NORTHSTAR_RELEASE_ID": "20261121-iam",
        "NORTHSTAR_WEB_ARTIFACT_BUCKET": "northstar-dev-web",
    }

    retry = update_release_context(existing, release_id="retry-timestamp", web_release_published=False)
    full = update_release_context(existing, release_id="20261122-web", web_release_published=True)

    assert retry["NORTHSTAR_RELEASE_ID"] == "20261121-iam"
    assert retry["NORTHSTAR_DEPLOYMENT_RELEASE_ID"] == "retry-timestamp"
    assert full["NORTHSTAR_RELEASE_ID"] == "20261122-web"


def test_orchestrator_exposes_single_stack_and_downstream_resume_selection() -> None:
    parser = build_parser()
    manifest = load_manifest(MANIFEST_PATH)

    one_stack = parser.parse_args(["--stack", "05-web"])
    assert manifest.select_stack_ids(one_stack.stack, one_stack.start_stack) == ("05-web",)
    assert [step.step_id for step in manifest.steps_before("05-web")] == ["build-web"]

    resume = parser.parse_args(["--from", "04-application"])
    assert manifest.select_stack_ids(resume.stack, resume.start_stack) == (
        "04-application",
        "05-web",
        "06-observability",
    )


def test_later_stack_retry_rehydrates_predecessor_outputs() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    class FakeCloudFormation:
        def describe_stacks(self, *, StackName: str) -> dict[str, object]:
            stack_id = StackName.removeprefix("northstar-dev-")
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": "EnvironmentName", "OutputValue": "dev"},
                            {"OutputKey": "DeploymentContractVersion", "OutputValue": "northstar-phase0"},
                            {"OutputKey": "StackMarker", "OutputValue": stack_id},
                        ]
                    }
                ]
            }

    context = hydrate_prior_outputs(
        FakeCloudFormation(),
        environment="dev",
        first_stack="05-web",
        context={},
        manifest=manifest,
    )

    assert context["CF_OUT_StackMarker"] == "04-application"
    assert context["CF_OUT_EnvironmentName"] == "dev"


def test_first_stack_plan_resolves_without_aws_and_later_stack_uses_context_outputs() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    first, _ = build_plan(
        "00-foundation",
        environment="test",
        context={},
        overrides={},
        allow_unresolved=False,
        manifest=manifest,
    )
    assert first.stack_name == "northstar-test-00-foundation"
    assert first.parameters == {
        "EnvironmentName": "test",
        "DeploymentContractVersion": "northstar-phase0",
    }

    later, _ = build_plan(
        "01-state",
        environment="test",
        context={
            "CF_OUT_EnvironmentName": "test",
            "CF_OUT_DeploymentContractVersion": "northstar-phase0",
        },
        overrides={},
        allow_unresolved=False,
        manifest=manifest,
    )
    assert later.unresolved == []
    assert later.parameters == first.parameters


def test_full_plan_is_non_mutating_and_reports_symbolic_prior_outputs() -> None:
    result = subprocess.run(
        [sys.executable, "cloudformation/deploy.py", "--environment", "test", "--plan"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert [stack["stack_id"] for stack in payload["stacks"]] == list(load_manifest().stack_ids)
    assert payload["stacks"][0]["unresolved"] == []
    assert payload["stacks"][1]["unresolved"] == [
        "${CF_OUT_DeploymentContractVersion}",
        "${CF_OUT_EnvironmentName}",
    ]


def test_configuration_precedence_is_override_then_context_then_environment(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT_NAME", "from-environment")
    resolved = resolve_configuration(
        {
            "Configuration": [
                {"Key": "EnvironmentName", "Value": "${ENVIRONMENT_NAME}"},
                {"Key": "Artifact", "Value": "${CF_OUT_Artifact}"},
            ]
        },
        context={"CF_OUT_Artifact": "from-context"},
        overrides={"ENVIRONMENT_NAME": "from-override"},
    )
    assert {entry["Key"]: entry["Value"] for entry in resolved["Configuration"]} == {
        "EnvironmentName": "from-override",
        "Artifact": "from-context",
    }
