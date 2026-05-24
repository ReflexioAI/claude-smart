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

cd "$REPO_ROOT"

echo "Using Reflexio checkout: $REFLEXIO_PATH"

case "$REFLEXIO_RELEASE_SOURCE" in
  vendor)
    python scripts/vendor-reflexio.py \
      --reflexio-path "$REFLEXIO_PATH" \
      --write
    uv sync --project plugin --locked
    PLUGIN_PYTHON="$REPO_ROOT/plugin/.venv/bin/python"
    if [ ! -x "$PLUGIN_PYTHON" ]; then
      echo "error: plugin Python was not created by uv sync: $PLUGIN_PYTHON" >&2
      exit 1
    fi
    uv pip install --project plugin --python "$PLUGIN_PYTHON" -e plugin/vendor/reflexio
    ;;
  pypi)
    python scripts/sync-reflexio-dep.py \
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
npm pack --dry-run

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
