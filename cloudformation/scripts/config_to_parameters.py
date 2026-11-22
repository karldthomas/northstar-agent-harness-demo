#!/usr/bin/env python3
"""Convert resolved matching-stack JSON configuration into CFN parameters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def to_parameters(configuration: dict[str, Any]) -> list[dict[str, str]]:
    parameters: list[dict[str, str]] = []
    for entry in configuration.get("Configuration", []):
        if entry.get("CreateParameter", True) is False:
            continue
        parameters.append({"ParameterKey": str(entry["Key"]), "ParameterValue": str(entry["Value"])})
    return parameters


def to_parameter_map(configuration: dict[str, Any]) -> dict[str, str]:
    return {item["ParameterKey"]: item["ParameterValue"] for item in to_parameters(configuration)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    args = parser.parse_args(argv)
    with Path(args.config).open(encoding="utf-8") as handle:
        configuration = json.load(handle)
    print(json.dumps(to_parameters(configuration), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
