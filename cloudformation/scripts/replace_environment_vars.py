#!/usr/bin/env python3
"""Resolve the reference deployment pattern's ``${NAME}`` configuration values."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class UnresolvedVariable(ValueError):
    pass


def load_configuration(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("Configuration"), list):
        raise ValueError(f"{path} must contain a Configuration array")
    return payload


def _resolve_string(
    value: str,
    *,
    values: Mapping[str, Any],
    default: Any = None,
    has_default: bool = False,
    allow_unresolved: bool = False,
) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values and values[key] is not None:
            return str(values[key])
        if key in os.environ:
            return os.environ[key]
        if has_default:
            return str(default)
        if allow_unresolved:
            return match.group(0)
        raise UnresolvedVariable(f"configuration value {key!r} is unresolved")

    return PLACEHOLDER.sub(replace, value)


def resolve_configuration(
    configuration: dict[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    allow_unresolved: bool = False,
) -> dict[str, Any]:
    """Return a resolved configuration while retaining creation flags.

    Precedence is explicit overrides, deployment context, process environment,
    then entry-level ``Default``—the same contract used by ``deploy.py``.
    """

    merged: dict[str, Any] = {}
    merged.update(context or {})
    merged.update(overrides or {})
    resolved_entries: list[dict[str, Any]] = []
    for raw in configuration["Configuration"]:
        if not isinstance(raw, dict) or "Key" not in raw or "Value" not in raw:
            raise ValueError("each Configuration entry requires Key and Value")
        entry = dict(raw)
        entry["Value"] = _resolve_string(
            str(raw["Value"]),
            values=merged,
            default=raw.get("Default"),
            has_default="Default" in raw,
            allow_unresolved=allow_unresolved,
        )
        resolved_entries.append(entry)
        merged[str(raw["Key"])] = entry["Value"]
    return {**configuration, "Configuration": resolved_entries}


def configuration_values(resolved: dict[str, Any]) -> dict[str, str]:
    return {str(entry["Key"]): str(entry["Value"]) for entry in resolved["Configuration"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--allow-unresolved", action="store_true")
    args = parser.parse_args(argv)
    resolved = resolve_configuration(load_configuration(args.config), allow_unresolved=args.allow_unresolved)
    print(json.dumps(resolved, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
