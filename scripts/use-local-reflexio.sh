#!/usr/bin/env bash
# Install a side-by-side Reflexio checkout into the claude-smart plugin venv.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_ROOT="$REPO_ROOT/plugin"
REFLEXIO_PATH="${REFLEXIO_PATH:-$REPO_ROOT/../reflexio}"

if [ ! -d "$REFLEXIO_PATH" ]; then
  echo "error: REFLEXIO_PATH does not exist: $REFLEXIO_PATH" >&2
  exit 1
fi
if [ ! -f "$REFLEXIO_PATH/pyproject.toml" ]; then
  echo "error: REFLEXIO_PATH does not contain pyproject.toml: $REFLEXIO_PATH" >&2
  exit 1
fi

REFLEXIO_PATH="$(cd "$REFLEXIO_PATH" && pwd)"
echo "Using Reflexio checkout: $REFLEXIO_PATH"

uv sync --project "$PLUGIN_ROOT"
PLUGIN_PYTHON="$PLUGIN_ROOT/.venv/bin/python"
if [ ! -x "$PLUGIN_PYTHON" ]; then
  echo "error: plugin Python was not created by uv sync: $PLUGIN_PYTHON" >&2
  exit 1
fi
uv pip install --project "$PLUGIN_ROOT" --python "$PLUGIN_PYTHON" -e "$REFLEXIO_PATH"
uv run --project "$PLUGIN_ROOT" --no-sync python -c 'import reflexio; print(reflexio.__file__)'
