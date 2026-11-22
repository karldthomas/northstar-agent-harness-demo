#!/usr/bin/env bash

# The deployment manifest and lifecycle-step registry live in Python so the
# shell entry point does not need to duplicate stack order or parse YAML.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_PYTHON="${DEPLOY_PYTHON:-python3}"

if ! command -v "$DEPLOY_PYTHON" >/dev/null 2>&1; then
  printf 'ERROR: DEPLOY_PYTHON is not on PATH: %s\n' "$DEPLOY_PYTHON" >&2
  exit 1
fi

exec "$DEPLOY_PYTHON" "$ROOT/cloudformation/orchestrate.py" "$@"
