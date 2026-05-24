#!/usr/bin/env bash
# Prepare a claude-smart release that depends on a newly published Reflexio.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REFLEXIO_PATH="${REFLEXIO_PATH:-$REPO_ROOT/../reflexio}"

cd "$REPO_ROOT"

echo "Using Reflexio checkout: $REFLEXIO_PATH"
python scripts/sync-reflexio-dep.py \
  --reflexio-path "$REFLEXIO_PATH" \
  --write \
  --check-pypi \
  --release-checks
uv lock --project plugin --upgrade-package reflexio-ai
uv sync --project plugin --locked
(cd plugin && uv run --project . pytest --rootdir .. -o addopts= ../tests -q)
npm pack --dry-run

cat <<'EOF'

Release checks passed.
Review and commit:
  plugin/pyproject.toml
  plugin/uv.lock
  reflexio.lock.json

Then publish claude-smart with the existing release flow, for example:
  make release VERSION=<new-claude-smart-version>
EOF
