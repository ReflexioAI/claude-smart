#!/usr/bin/env bash
# Prepare a claude-smart release that needs a specific Reflexio checkout.
#
# Default mode vendors Reflexio into plugin/vendor/reflexio for the npm tarball,
# so user installs do not need GitHub or a freshly published reflexio-ai wheel.
# Set REFLEXIO_RELEASE_SOURCE=pypi to use the strict PyPI-published flow.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REFLEXIO_PATH="${REFLEXIO_PATH:-$REPO_ROOT/../reflexio}"
REFLEXIO_RELEASE_SOURCE="${REFLEXIO_RELEASE_SOURCE:-vendor}"
PYTHON_BIN="${PYTHON:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: Python interpreter not found: $PYTHON_BIN" >&2
  echo "Set PYTHON=/path/to/python3 or install python3." >&2
  exit 1
fi

verify_vendor_in_npm_pack() {
  pack_json="$(mktemp "${TMPDIR:-/tmp}/claude-smart-pack.XXXXXX.json")"
  npm pack --dry-run --json > "$pack_json"
  "$PYTHON_BIN" - "$pack_json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
files = {entry["path"] for package in payload for entry in package.get("files", [])}
required = {
    "plugin/vendor/reflexio/pyproject.toml",
    "plugin/vendor/reflexio/reflexio/__init__.py",
}
missing = sorted(required.difference(files))
if missing:
    raise SystemExit(
        "error: npm pack is missing vendored Reflexio files:\n  "
        + "\n  ".join(missing)
    )
print("OK: npm tarball includes vendored Reflexio files")
PY
  rm -f "$pack_json"
}

cd "$REPO_ROOT"

echo "Using Reflexio checkout: $REFLEXIO_PATH"

case "$REFLEXIO_RELEASE_SOURCE" in
  vendor)
    "$PYTHON_BIN" scripts/vendor-reflexio.py \
      --reflexio-path "$REFLEXIO_PATH" \
      --write
    uv sync --project plugin --locked
    PLUGIN_PYTHON="$REPO_ROOT/plugin/.venv/bin/python"
    if [ ! -x "$PLUGIN_PYTHON" ]; then
      echo "error: plugin Python was not created by uv sync: $PLUGIN_PYTHON" >&2
      exit 1
    fi
    uv pip install --project plugin --python "$PLUGIN_PYTHON" --reinstall --no-deps plugin/vendor/reflexio
    ;;
  pypi)
    "$PYTHON_BIN" scripts/sync-reflexio-dep.py \
      --reflexio-path "$REFLEXIO_PATH" \
      --write \
      --check-pypi \
      --release-checks
    uv lock --project plugin --upgrade-package reflexio-ai
    uv sync --project plugin --locked
    ;;
  *)
    echo "error: REFLEXIO_RELEASE_SOURCE must be 'vendor' or 'pypi'" >&2
    exit 1
    ;;
esac

(cd plugin && uv run --project . --no-sync pytest --rootdir .. -o addopts= ../tests -q)
if [ "$REFLEXIO_RELEASE_SOURCE" = "vendor" ]; then
  verify_vendor_in_npm_pack
else
  npm pack --dry-run
fi

if [ "$REFLEXIO_RELEASE_SOURCE" = "vendor" ]; then
  cat <<'EOF'

Release checks passed.
Review and commit:
  reflexio.lock.json

Keep generated plugin/vendor/reflexio in place until npm publish completes.
It is gitignored but included in the npm tarball.

Then publish the npm artifact only:
  make release-npm VERSION=<new-claude-smart-version>
EOF
else
  cat <<'EOF'

Release checks passed.
Review and commit:
  plugin/pyproject.toml
  plugin/uv.lock
  reflexio.lock.json

Then publish claude-smart with the existing release flow, for example:
  make release VERSION=<new-claude-smart-version>
EOF
fi
