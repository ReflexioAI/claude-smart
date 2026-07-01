#!/usr/bin/env bash
# Generate / verify plugin/uv.lock as a STANDALONE lockfile.
#
# Why this exists: plugin/ is a member of the enterprise uv workspace
# (../../../pyproject.toml [tool.uv.workspace]). Inside that workspace `uv lock
# --project plugin` writes the PARENT lockfile and leaves plugin/uv.lock's
# dependency graph untouched (only `make bump` awk-patches its version). End
# users `npm install` the tarball and run `uv sync --locked` with NO workspace
# and NO ../../reflexio on disk, so a workspace-resolved plugin/uv.lock makes
# the bootstrap fail (`The lockfile needs to be updated, but --locked was
# provided`). check-locked-project-version only compares the version field, so
# this drift is silent until an end user hits it.
#
# Fix: always resolve plugin/uv.lock OUTSIDE the workspace, against PyPI
# reflexio-ai. This script does that by copying plugin/ into a temp dir (no
# parent workspace reachable) and running uv there.
#
#   standalone-lock.sh --write   regenerate plugin/uv.lock from plugin/pyproject.toml
#   standalone-lock.sh --check   fail if the committed plugin/uv.lock is stale
#
# Mirrors the --write / --check convention of check-reflexio-lock.py.
set -euo pipefail

mode="${1:-}"
case "$mode" in
  --write | --check) ;;
  *)
    echo "usage: $0 --write|--check" >&2
    exit 2
    ;;
esac

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
plugin_dir="$repo_root/plugin"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found on PATH" >&2
  exit 1
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/cs-standalone-lock.XXXXXX")"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

# Copy the plugin project out of the workspace tree. Exclude heavy/generated
# dirs that uv does not need to resolve the lock.
rsync -a \
  --exclude '.venv' \
  --exclude 'node_modules' \
  --exclude '.next' \
  --exclude 'dist' \
  --exclude 'vendor' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.ruff_cache' \
  --exclude '.mypy_cache' \
  "$plugin_dir"/ "$tmp"/

# VIRTUAL_ENV (e.g. an activated enterprise .venv) confuses uv's project
# resolution; clear it for the standalone run.
unset VIRTUAL_ENV

if [ "$mode" = "--write" ]; then
  (cd "$tmp" && uv lock --python 3.12 --refresh-package reflexio-ai >&2)
  cp "$tmp/uv.lock" "$plugin_dir/uv.lock"
  echo "→ wrote standalone plugin/uv.lock"
else
  # --check: verify the committed lock would not change when resolved
  # standalone. uv lock --check (alias --locked) is non-mutating.
  if (cd "$tmp" && uv lock --check --python 3.12 >&2); then
    echo "→ ok: plugin/uv.lock is standalone-consistent"
  else
    echo "error: plugin/uv.lock is STALE when resolved standalone (outside the" >&2
    echo "       enterprise uv workspace). End-user 'uv sync --locked' will fail." >&2
    echo "       Regenerate with: make relock   (or scripts/standalone-lock.sh --write)" >&2
    exit 1
  fi
fi
