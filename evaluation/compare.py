"""Compare independent Northstar scorecard artifacts from fixed-seed runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


def _reports(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    reports = payload.get("reports")
    if not isinstance(reports, list) or not all(isinstance(report, dict) for report in reports):
        raise ValueError("scorecard payload must contain a reports array")
    return [dict(report) for report in reports]


def compare_scorecards(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    baseline_name: str = "baseline",
    candidate_name: str = "candidate",
) -> dict[str, Any]:
    """Compare reports while rejecting mismatched seeds or scenario catalogs."""

    if baseline.get("seed") != candidate.get("seed"):
        raise ValueError("scorecards must use the same deterministic seed")
    left = {str(report.get("scenario_id")): report for report in _reports(baseline)}
    right = {str(report.get("scenario_id")): report for report in _reports(candidate)}
    if set(left) != set(right):
        raise ValueError("scorecards must contain the same scenario IDs")

    rows: list[dict[str, Any]] = []
    for scenario_id in sorted(left):
        first = left[scenario_id]
        second = right[scenario_id]
        first_metrics = first.get("metrics") if isinstance(first.get("metrics"), dict) else {}
        second_metrics = second.get("metrics") if isinstance(second.get("metrics"), dict) else {}
        rows.append(
            {
                "scenario_id": scenario_id,
                "baseline_status": first.get("status"),
                "candidate_status": second.get("status"),
                "score_delta": round(float(second.get("score", 0)) - float(first.get("score", 0)), 2),
                "latency_ms_delta": _delta(first_metrics, second_metrics, "latency_ms"),
                "tool_call_count_delta": _delta(first_metrics, second_metrics, "tool_call_count"),
                "recovery_event_count_delta": _delta(first_metrics, second_metrics, "recovery_event_count"),
                "cost_usd_delta": _delta(first_metrics, second_metrics, "cost_usd"),
                "policy_violation_delta": _delta(first_metrics, second_metrics, "policy_violation_count"),
            }
        )
    return {
        "schema_version": "northstar.scorecard-comparison.v1",
        "seed": baseline.get("seed"),
        "baseline": baseline_name,
        "candidate": candidate_name,
        "scenario_count": len(rows),
        "rows": rows,
        "summary": {
            "candidate_pass_delta": sum(
                row["candidate_status"] == "passed" for row in rows
            ) - sum(row["baseline_status"] == "passed" for row in rows),
            "score_delta_average": round(
                sum(row["score_delta"] for row in rows) / len(rows), 2
            ) if rows else 0.0,
            "policy_violation_delta_total": sum(row["policy_violation_delta"] for row in rows),
        },
    }


def _delta(first: Mapping[str, Any], second: Mapping[str, Any], key: str) -> float | int | None:
    if first.get(key) is None or second.get(key) is None:
        return None
    value = float(second[key]) - float(first[key])
    return int(value) if value.is_integer() else round(value, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Northstar scorecard JSON artifacts")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline-name", default="baseline")
    parser.add_argument("--candidate-name", default="candidate")
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    print(json.dumps(compare_scorecards(baseline, candidate, baseline_name=args.baseline_name, candidate_name=args.candidate_name), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
