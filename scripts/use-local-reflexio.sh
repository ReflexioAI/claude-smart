#!/usr/bin/env bash
# Install a side-by-side Reflexio checkout into the claude-smart plugin venv.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_ROOT="$REPO_ROOT/plugin"
REFLEXIO_PATH="${REFLEXIO_PATH:-$REPO_ROOT/../reflexio}"

expand_user_path() {
  case "$1" in
    "~")
      printf '%s\n' "$HOME"
      ;;
    "~/"*)
      printf '%s/%s\n' "$HOME" "${1#"~/"}"
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

resolve_venv_python() {
  venv_root="$1/.venv"
  if [ -x "$venv_root/bin/python" ]; then
    printf '%s\n' "$venv_root/bin/python"
    return 0
  fi
  if [ -x "$venv_root/Scripts/python.exe" ]; then
    printf '%s\n' "$venv_root/Scripts/python.exe"
    return 0
  fi
  return 1
}

REFLEXIO_PATH="$(expand_user_path "$REFLEXIO_PATH")"
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
if ! PLUGIN_PYTHON="$(resolve_venv_python "$PLUGIN_ROOT")"; then
  echo "error: plugin Python was not created by uv sync under $PLUGIN_ROOT/.venv" >&2
  exit 1
fi
uv pip install --project "$PLUGIN_ROOT" --python "$PLUGIN_PYTHON" -e "$REFLEXIO_PATH"
REFLEXIO_PATH="$REFLEXIO_PATH" python3 "$REPO_ROOT/scripts/doctor-reflexio.py"
