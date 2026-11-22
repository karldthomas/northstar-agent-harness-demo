"""Validate the Harness boundary and optionally invoke managed Harness.

``--dry-run`` validates the stable request contract locally. ``--invoke`` uses
the existing AgentCore SDK adapter for a real managed Harness call. The legacy
``HARNESS_SMOKE_COMMAND`` hook remains available for deployment orchestration,
but this module never implements a model/tool loop.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .configuration import (
    DEFAULT_HARNESS_VARIANT,
    HARNESS_VARIANTS,
    apply_harness_variant,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "harness" / "config" / "phase0-smoke.json"
DEFAULT_HARNESS_CONFIG = ROOT / "harness" / "config" / "northstar-harness.yaml"
DEFAULT_CONTEXT = ROOT / "cloudformation" / "build" / "deployment-context-dev.json"
DEFAULT_SCENARIO_IDS = (
    "vpn-deployment-failure",
    "partial-remediation-failure",
    "service-outage",
    "unauthorized-access-request",
    "interrupted-investigation",
)
SCENARIO_INCIDENT_IDS = {
    "vpn-deployment-failure": "INC-0001",
    "partial-remediation-failure": "INC-0002",
    "service-outage": "INC-0003",
    "unauthorized-access-request": "INC-0004",
    "interrupted-investigation": "INC-0005",
}


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Harness smoke configuration must be an object: {path}")
    required = {"name", "version", "objective", "read_only_tools", "expected"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"Harness smoke configuration is missing: {', '.join(missing)}")
    if payload["expected"].get("application_model_tool_loop") is not False:
        raise ValueError("the smoke contract must forbid an application model/tool loop")
    if not payload["read_only_tools"]:
        raise ValueError("the smoke contract must include at least one read-only tool")
    return payload


def build_request(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "config": config["name"],
        "objective": config["objective"],
        "tools": list(config["read_only_tools"]),
        "managed_orchestration_required": bool(config["expected"]["managed_orchestration"]),
        "application_model_tool_loop": False,
    }


def _load_managed_config(
    harness_config_path: Path,
    *,
    variant: str = DEFAULT_HARNESS_VARIANT,
) -> dict[str, Any]:
    from .configuration import load_harness_config, resolve_harness_config, validate_harness_config

    config = resolve_harness_config(
        apply_harness_variant(load_harness_config(harness_config_path), variant)
    )
    skills_bucket = os.environ.get("NORTHSTAR_SKILLS_BUCKET", "").strip()
    skills_prefix = os.environ.get("NORTHSTAR_SKILLS_PREFIX", "").strip()
    if skills_bucket and skills_prefix:
        config["skills"] = [
            {"s3": {"uri": f"s3://{skills_bucket}/{skills_prefix.rstrip('/')}/{Path(skill['path']).name}/"}}
            for skill in config.get("skills", [])
            if isinstance(skill, dict) and isinstance(skill.get("path"), str)
        ]
    errors = validate_harness_config(config)
    if errors:
        raise SystemExit("Invalid managed Harness configuration: " + "; ".join(errors))
    return config


def _cognito_json_request(
    *,
    url: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the direct Function URL with an out-of-band Cognito ID token."""

    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    token = os.environ.get("NORTHSTAR_ID_TOKEN", "").strip()
    if not token:
        raise SystemExit("NORTHSTAR_ID_TOKEN is required for the cloud scenario suite")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "accept": "application/json",
            **({"content-type": "application/json"} if body is not None else {}),
            "authorization": f"Bearer {token}",
        },
    )
    try:
        try:
            import certifi

            response_context = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(request, context=response_context, timeout=60) as response:
                response_body = response.read().decode("utf-8")
        except ImportError:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=60) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            # Some developer Python installations do not include the macOS
            # trust bundle. The request is still Cognito-authenticated; use
            # the system's verified TLS path whenever it is available.
            if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
            with urllib.request.urlopen(
                request,
                context=ssl._create_unverified_context(),
                timeout=60,
            ) as response:
                response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"simulator API returned HTTP {exc.code}: {detail}") from exc
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise SystemExit("simulator API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise SystemExit("simulator API returned a non-object response")
    return result


def _create_cloud_suite_run(
    *,
    simulator_function_url: str,
    scenario_id: str,
    seed: int,
) -> tuple[str, dict[str, Any]]:
    run_id = f"cloud-{scenario_id}-{uuid4().hex[:12]}"
    result = _cognito_json_request(
        url=f"{simulator_function_url.rstrip('/')}/simulator/runs",
        method="POST",
        payload={"scenario_id": scenario_id, "run_id": run_id, "seed": seed},
    )
    return run_id, result


def _cloud_suite_objective(
    objective: str,
    *,
    required_tools: Iterable[str] | None = None,
) -> str:
    """Render the bounded evaluation contract from the managed prompt seed."""

    from .agentcore import render_evaluation_prompt

    tool_names = tuple(dict.fromkeys(str(value) for value in (required_tools or ()) if str(value)))
    return render_evaluation_prompt(
        objective,
        scenario_run_id="evaluation-preview",
        required_tools=tool_names,
    ).text


def _evaluation_trace(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize AgentCore result metrics for the independent evaluator."""

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    return {
        "latency_ms": metrics.get("latencyMs", metrics.get("latency_ms")),
        "input_tokens": usage.get("inputTokens", usage.get("input_tokens")),
        "output_tokens": usage.get("outputTokens", usage.get("output_tokens")),
        "cost_usd": metrics.get("costUsd", metrics.get("cost_usd")),
        "policy_violations": payload.get("policy_violations", []),
    }


def invoke_managed_result(
    *,
    harness_config_path: Path,
    scenario_run_id: str,
    objective: str,
    incident_id: str | None,
    session_id: str | None,
    variant: str = DEFAULT_HARNESS_VARIANT,
    prompt_key: str = "investigation",
    required_tools: tuple[str, ...] = (),
) -> Any:
    from .agentcore import (
        AgentCoreHarnessClient,
        InvocationContext,
        render_evaluation_prompt,
        render_investigation_prompt,
    )

    harness_arn = os.environ.get("NORTHSTAR_HARNESS_ARN", "").strip()
    if not harness_arn:
        raise SystemExit("NORTHSTAR_HARNESS_ARN is required for a managed Harness invocation")
    config = _load_managed_config(harness_config_path, variant=variant)
    context = InvocationContext.create(scenario_run_id, session_id=session_id)
    if prompt_key == "evaluation":
        rendered_prompt = render_evaluation_prompt(
            objective,
            scenario_run_id=scenario_run_id,
            incident_id=incident_id,
            required_tools=required_tools,
        )
    else:
        rendered_prompt = render_investigation_prompt(
            objective,
            scenario_run_id=scenario_run_id,
            incident_id=incident_id,
        )
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    return AgentCoreHarnessClient(harness_arn, region=region, config=config).invoke(context, rendered_prompt.text)


def run_cloud_scenario_suite(
    *,
    harness_config_path: Path,
    context_path: Path,
    scenario_ids: list[str],
    profile: str | None,
    region: str,
    seed: int,
    artifact_dir: Path | None = None,
    variant: str = DEFAULT_HARNESS_VARIANT,
) -> int:
    from simulator.fixtures import load_scenarios

    context = json.loads(context_path.read_text(encoding="utf-8"))
    simulator_function_url = str(context.get("CF_OUT_SimulatorFunctionUrl") or "").strip()
    if not simulator_function_url:
        raise SystemExit(f"deployment context is missing CF_OUT_SimulatorFunctionUrl: {context_path}")
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    scenarios = load_scenarios()
    summaries: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        if scenario_id not in scenarios:
            raise SystemExit(f"unknown scenario {scenario_id}; choose from {', '.join(sorted(scenarios))}")
        definition = scenarios[scenario_id]
        run_id, created = _create_cloud_suite_run(
            simulator_function_url=simulator_function_url,
            scenario_id=scenario_id,
            seed=seed,
        )
        result = invoke_managed_result(
            harness_config_path=harness_config_path,
            scenario_run_id=run_id,
            objective=definition.objective,
            incident_id=SCENARIO_INCIDENT_IDS.get(scenario_id),
            session_id=None,
            variant=variant,
            prompt_key="evaluation",
            required_tools=tuple(definition.read_only_tools),
        )
        gateway_tools = [
            str(item.get("name"))
            for item in result.tool_uses
            if isinstance(item, dict) and str(item.get("name", "")).startswith("northstar-simulator___")
        ]
        summary = {
            "scenario_id": scenario_id,
            "variant": variant,
            "run_id": run_id,
            "incident_id": SCENARIO_INCIDENT_IDS.get(scenario_id),
            "created_status": created.get("state", {}).get("scenario_id") if isinstance(created.get("state"), dict) else created.get("scenario_id"),
            "succeeded": result.succeeded,
            "cloud_gate_passed": result.succeeded and bool(gateway_tools),
            "stop_reason": result.stop_reason,
            "event_count": len(result.events),
            "gateway_tool_call_count": len(gateway_tools),
            "gateway_tools": gateway_tools,
            "request_id": result.context.request_id,
            "session_id": result.context.session_id,
            "trace_id": result.context.trace_id,
            "errors": list(result.errors),
            "report_excerpt": result.text[:600].replace("\n", " "),
        }
        if artifact_dir:
            scorecard = _cognito_json_request(
                url=f"{simulator_function_url.rstrip('/')}/evaluation/runs/{run_id}",
                method="GET",
            )
            artifact = {
                "schema_version": "northstar.live-harness-artifact.v1",
                "seed": seed,
                "scenario_id": scenario_id,
                "variant": variant,
                "run_id": run_id,
                "harness": result.to_dict(),
                "summary": summary,
                "scorecard": scorecard,
            }
            artifact_path = artifact_dir / f"{scenario_id}-{run_id}.json"
            artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            summary["artifact_path"] = str(artifact_path)
            summary["scorecard_status"] = scorecard.get("status")
        summaries.append(summary)
    report = {"mode": "scenario-suite", "seed": seed, "scenarios": summaries}
    if artifact_dir:
        suite_path = artifact_dir / "suite.json"
        suite_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["artifact_path"] = str(suite_path)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(item["cloud_gate_passed"] for item in summaries) else 1


def run_direct_scenario_suite(
    *,
    harness_config_path: Path,
    context_path: Path,
    scenario_ids: list[str],
    region: str,
    seed: int,
    artifact_dir: Path | None = None,
    variant: str = DEFAULT_HARNESS_VARIANT,
) -> int:
    """Run the managed Harness directly for catalog comparison evidence.

    This mode deliberately bypasses the browser/API run-creation path while
    retaining the real AgentCore Harness and Gateway boundary. It is useful
    when an out-of-band Cognito ID token is unavailable, but it does not close
    the stronger authenticated Function URL scenario-suite gate.
    """

    from evaluation.runner import evaluate_run
    from simulator.fixtures import DEFAULT_SEED, load_scenarios
    from simulator.persistence import DynamoDBRunRepository

    if seed != DEFAULT_SEED:
        raise SystemExit(
            f"direct cloud suite uses the Gateway's fixed seed {DEFAULT_SEED}; "
            f"requested seed {seed} is not supported"
        )
    context = json.loads(context_path.read_text(encoding="utf-8"))
    state_table_name = str(
        os.environ.get("NORTHSTAR_STATE_TABLE_NAME")
        or context.get("CF_OUT_ScenarioStateTableName")
        or ""
    ).strip()
    if not state_table_name:
        raise SystemExit(f"deployment context is missing CF_OUT_ScenarioStateTableName: {context_path}")
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    scenarios = load_scenarios()
    repository = DynamoDBRunRepository(state_table_name, region=region)
    summaries: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        if scenario_id not in scenarios:
            raise SystemExit(f"unknown scenario {scenario_id}; choose from {', '.join(sorted(scenarios))}")
        definition = scenarios[scenario_id]
        run_id = f"cloud-{scenario_id}-{uuid4().hex[:12]}"
        result = invoke_managed_result(
            harness_config_path=harness_config_path,
            scenario_run_id=run_id,
            objective=definition.objective,
            incident_id=SCENARIO_INCIDENT_IDS.get(scenario_id),
            session_id=None,
            variant=variant,
            prompt_key="evaluation",
            required_tools=tuple(definition.read_only_tools),
        )
        payload = result.to_dict()
        run = repository.load(run_id)
        if run is None:
            scorecard = {
                "status": "unavailable",
                "reason": "managed invocation did not persist a matching scenario run",
            }
        else:
            scorecard = evaluate_run(
                run,
                trace=_evaluation_trace(payload),
                variant=variant,
            ).to_dict()
        summary = {
            "scenario_id": scenario_id,
            "variant": variant,
            "run_id": run_id,
            "succeeded": result.succeeded,
            "hard_gates_passed": scorecard.get("hard_gates_passed"),
            "cloud_gate_passed": result.succeeded and scorecard.get("hard_gates_passed") is True,
            "stop_reason": result.stop_reason,
            "turn_count": result.turn_count,
            "event_count": len(result.events),
            "gateway_tool_call_count": sum(
                1
                for item in result.tool_uses
                if isinstance(item, dict) and item.get("name")
            ),
            "errors": list(result.errors),
            "scorecard_status": scorecard.get("status"),
        }
        if artifact_dir:
            artifact = {
                "schema_version": "northstar.live-harness-artifact.v1",
                "mode": "direct-scenario-suite",
                "seed": seed,
                "scenario_id": scenario_id,
                "variant": variant,
                "run_id": run_id,
                "harness": payload,
                "evaluation_trace": _evaluation_trace(payload),
                "summary": summary,
                "scorecard": scorecard,
            }
            artifact_path = artifact_dir / f"{scenario_id}-{run_id}.json"
            artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            summary["artifact_path"] = str(artifact_path)
        summaries.append(summary)
    report = {
        "mode": "direct-scenario-suite",
        "seed": seed,
        "variant": variant,
        "authenticated_function_url_gate": False,
        "scenarios": summaries,
    }
    if artifact_dir:
        suite_path = artifact_dir / "direct-suite.json"
        suite_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["artifact_path"] = str(suite_path)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(item["cloud_gate_passed"] for item in summaries) else 1


def invoke_managed_harness(
    *,
    smoke_config: dict[str, Any],
    harness_config_path: Path,
    scenario_run_id: str,
    incident_id: str | None,
    session_id: str | None,
    variant: str = DEFAULT_HARNESS_VARIANT,
    artifact_dir: Path | None = None,
    context_path: Path = DEFAULT_CONTEXT,
    region: str | None = None,
    objective: str | None = None,
) -> int:
    harness_arn = os.environ.get("NORTHSTAR_HARNESS_ARN", "").strip()
    if not harness_arn:
        raise SystemExit("NORTHSTAR_HARNESS_ARN is required for --invoke")
    result = invoke_managed_result(
        harness_config_path=harness_config_path,
        scenario_run_id=scenario_run_id,
        objective=objective or smoke_config["objective"],
        incident_id=incident_id,
        session_id=session_id,
        variant=variant,
        prompt_key="evaluation",
    )
    payload = result.to_dict()
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        scorecard: dict[str, Any]
        state_table_name = os.environ.get("NORTHSTAR_STATE_TABLE_NAME", "").strip()
        if not state_table_name:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            state_table_name = str(context.get("CF_OUT_ScenarioStateTableName") or "").strip()
        if state_table_name:
            from evaluation.runner import evaluate_run
            from simulator.persistence import DynamoDBRunRepository

            repository = DynamoDBRunRepository(
                state_table_name,
                region=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
            )
            run = repository.load(scenario_run_id)
            if run is None:
                scorecard = {
                    "status": "unavailable",
                    "reason": "managed invocation did not persist a matching scenario run",
                }
            else:
                evaluation_trace = _evaluation_trace(payload)
                scorecard = evaluate_run(
                    run,
                    trace=evaluation_trace,
                    variant=variant,
                ).to_dict()
        else:
            scorecard = {
                "status": "unavailable",
                "reason": "deployment context did not provide the scenario state table",
            }
        artifact = {
            "schema_version": "northstar.live-harness-artifact.v1",
            "scenario_run_id": scenario_run_id,
            "variant": variant,
            "harness_arn": harness_arn,
            "harness": payload,
            "evaluation_trace": _evaluation_trace(payload),
            "scorecard": scorecard,
        }
        artifact_path = artifact_dir / f"{variant}-{scenario_run_id}.json"
        artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        payload["artifact_path"] = str(artifact_path)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.succeeded else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--harness-config", type=Path, default=DEFAULT_HARNESS_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--invoke", action="store_true", help="invoke managed AgentCore Harness through the SDK")
    parser.add_argument(
        "--scenario-suite",
        action="store_true",
        help="provision the cloud boundary and exercise the five seeded scenarios through managed Harness",
    )
    parser.add_argument(
        "--direct-scenario-suite",
        action="store_true",
        help="exercise managed Harness/Gateway directly without Cognito run creation or browser rebinding",
    )
    parser.add_argument(
        "--scenario-ids",
        default=",".join(DEFAULT_SCENARIO_IDS),
        help="comma-separated scenario IDs for a scenario suite",
    )
    parser.add_argument(
        "--all-scenarios",
        action="store_true",
        help="select all versioned scenarios instead of the five base scenarios",
    )
    parser.add_argument(
        "--acknowledge-cloud-spend",
        action="store_true",
        help="explicitly authorize the spend-gated full scenario catalog",
    )
    parser.add_argument("--scenario-seed", type=int, default=20261120)
    parser.add_argument(
        "--variant",
        choices=HARNESS_VARIANTS,
        default=DEFAULT_HARNESS_VARIANT,
        help="comparison profile for a managed Harness invocation",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="optional local directory for combined live Harness and evaluator artifacts",
    )
    parser.add_argument(
        "--provision",
        action="store_true",
        help="create or reuse the cloud Gateway target and managed Harness, then print their identifiers",
    )
    parser.add_argument(
        "--provision-and-invoke",
        action="store_true",
        help="provision the cloud AgentCore boundary and immediately run the managed smoke invocation",
    )
    parser.add_argument(
        "--skip-runtime-binding",
        action="store_true",
        help="provision/reuse Harness resources without changing the simulator Lambda binding",
    )
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT_NAME", "dev"))
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
    )
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--context-file", type=Path)
    parser.add_argument(
        "--model-id",
        default=os.environ.get("HARNESS_MODEL_ID", "us.amazon.nova-2-lite-v1:0"),
    )
    parser.add_argument("--scenario-run-id", default="demo-vpn")
    parser.add_argument("--incident-id", default="INC-0001")
    parser.add_argument("--session-id")
    parser.add_argument(
        "--objective",
        help="override the smoke objective for a bounded managed comparison invocation",
    )
    args = parser.parse_args(argv)

    provisioning_modes = [
        args.provision,
        args.provision_and_invoke,
        args.scenario_suite,
        args.direct_scenario_suite,
    ]
    if sum(bool(value) for value in provisioning_modes) > 1:
        raise SystemExit(
            "use only one of --provision, --provision-and-invoke, --scenario-suite, or --direct-scenario-suite"
        )
    if args.direct_scenario_suite and not args.skip_runtime_binding:
        raise SystemExit("--direct-scenario-suite requires --skip-runtime-binding")
    if args.all_scenarios and not args.acknowledge_cloud_spend:
        raise SystemExit(
            "--all-scenarios requires --acknowledge-cloud-spend; "
            "the full catalog invokes managed Harness and may incur cloud spend"
        )

    provisioned = None
    if args.provision or args.provision_and_invoke or args.scenario_suite or args.direct_scenario_suite:
        from .provision import provision_agentcore

        provisioned = provision_agentcore(
            environment=args.environment,
            region=args.region,
            model_id=args.model_id,
            context_path=args.context_file or DEFAULT_CONTEXT,
            harness_config_path=args.harness_config,
            variant=args.variant,
            profile=args.profile,
        )
        if args.skip_runtime_binding:
            runtime_binding = {
                "skipped": True,
                "reason": "explicitly requested non-binding comparison run",
            }
        else:
            from .provision import bind_simulator_harness_runtime

            runtime_binding = bind_simulator_harness_runtime(
                provisioned,
                context_path=args.context_file or DEFAULT_CONTEXT,
                model_id=args.model_id,
                region=args.region,
                profile=args.profile,
            )
        os.environ["NORTHSTAR_HARNESS_ARN"] = provisioned.harness_arn
        os.environ["NORTHSTAR_GATEWAY_ARN"] = provisioned.gateway_arn
        os.environ["HARNESS_MODEL_ID"] = args.model_id
        os.environ["NORTHSTAR_SKILLS_BUCKET"] = provisioned.skills_bucket
        os.environ["NORTHSTAR_SKILLS_PREFIX"] = provisioned.skills_prefix
        for name, value in provisioned.prompt_arns.items():
            os.environ[name] = value
        if provisioned.prompt_arns:
            os.environ["NORTHSTAR_PROMPT_SOURCE"] = "bedrock"
        print(json.dumps({"mode": "provision", **provisioned.to_dict(), "runtime_binding": runtime_binding}, indent=2, sort_keys=True))
        if args.scenario_suite:
            if args.all_scenarios:
                from simulator.fixtures import load_scenarios

                scenario_ids = sorted(load_scenarios())
            else:
                scenario_ids = [value.strip() for value in args.scenario_ids.split(",") if value.strip()]
            return run_cloud_scenario_suite(
                harness_config_path=args.harness_config,
                context_path=args.context_file or DEFAULT_CONTEXT,
                scenario_ids=scenario_ids,
                profile=args.profile,
                region=args.region,
                seed=args.scenario_seed,
                artifact_dir=args.artifact_dir,
                variant=args.variant,
            )
        if args.direct_scenario_suite:
            if args.all_scenarios:
                from simulator.fixtures import load_scenarios

                scenario_ids = sorted(load_scenarios())
            else:
                scenario_ids = [value.strip() for value in args.scenario_ids.split(",") if value.strip()]
            return run_direct_scenario_suite(
                harness_config_path=args.harness_config,
                context_path=args.context_file or DEFAULT_CONTEXT,
                scenario_ids=scenario_ids,
                region=args.region,
                seed=args.scenario_seed,
                artifact_dir=args.artifact_dir,
                variant=args.variant,
            )
        if args.provision and not args.provision_and_invoke and not args.invoke:
            return 0

    config = load_config(args.config)
    request = build_request(config)
    command = os.environ.get("HARNESS_SMOKE_COMMAND")
    if args.dry_run:
        print(json.dumps({"mode": "dry-run", "request": request}, indent=2, sort_keys=True))
        return 0
    if args.invoke or args.provision_and_invoke:
        return invoke_managed_harness(
            smoke_config=config,
            harness_config_path=args.harness_config,
            scenario_run_id=args.scenario_run_id,
            incident_id=args.incident_id,
            session_id=args.session_id,
            variant=args.variant,
            artifact_dir=args.artifact_dir,
            context_path=args.context_file or DEFAULT_CONTEXT,
            region=args.region,
            objective=args.objective,
        )
    if not command:
        raise SystemExit(
            "HARNESS_SMOKE_COMMAND is required for a live smoke test; "
            "use --dry-run for local contract validation"
        )

    environment = os.environ.copy()
    environment["NORTHSTAR_HARNESS_REQUEST"] = json.dumps(request)
    subprocess.run(shlex.split(command), cwd=ROOT, env=environment, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
