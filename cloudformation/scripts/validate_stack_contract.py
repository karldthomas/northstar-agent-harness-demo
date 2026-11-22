#!/usr/bin/env python3
"""Validate matching YAML/JSON stack contracts without requiring PyYAML."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PARAMETER_HEADER = re.compile(r"^Parameters:\s*$")
TOP_LEVEL = re.compile(r"^[A-Za-z][A-Za-z0-9]*:\s*$")
PARAMETER_NAME = re.compile(r"^  ([A-Za-z][A-Za-z0-9]*):\s*$")


def parameter_names(template: str) -> set[str]:
    names: set[str] = set()
    in_parameters = False
    for line in template.splitlines():
        if PARAMETER_HEADER.match(line):
            in_parameters = True
            continue
        if in_parameters and TOP_LEVEL.match(line):
            break
        if in_parameters:
            match = PARAMETER_NAME.match(line)
            if match:
                names.add(match.group(1))
    return names


def required_parameter_names(template: str) -> set[str]:
    """Return parameter names without a template-level Default."""

    names: set[str] = set()
    in_parameters = False
    current: str | None = None
    has_default = False
    for line in template.splitlines():
        if PARAMETER_HEADER.match(line):
            in_parameters = True
            continue
        if in_parameters and TOP_LEVEL.match(line):
            break
        if not in_parameters:
            continue
        match = PARAMETER_NAME.match(line)
        if match:
            if current and not has_default:
                names.add(current)
            current = match.group(1)
            has_default = False
        elif current and line.startswith("    Default:"):
            has_default = True
    if current and not has_default:
        names.add(current)
    return names


def validate(template_path: str | Path, config_path: str | Path) -> list[str]:
    template = Path(template_path).read_text(encoding="utf-8")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    declared = parameter_names(template)
    configured = {str(entry["Key"]) for entry in config.get("Configuration", []) if entry.get("CreateParameter", True) is not False}
    errors = [f"configuration parameter {name} is not declared in the template" for name in sorted(configured - declared)]
    errors.extend(
        f"required template parameter {name} is missing from configuration"
        for name in sorted(required_parameter_names(template) - configured)
    )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template")
    parser.add_argument("config")
    args = parser.parse_args(argv)
    errors = validate(args.template, args.config)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
