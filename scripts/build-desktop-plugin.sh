#!/usr/bin/env bash
# Build the claude.ai desktop-uploadable plugin zip.
#
# The Claude desktop app (Cowork / "local agent mode") installs plugins from a
# claude.ai marketplace, and a marketplace can only sync from git. claude-smart's
# git repo is deliberately non-installable (npm-only): the vendored Reflexio
# runtime (plugin/vendor/reflexio) and the marketplace manifest are gitignored and
# generated only at pack time. So the desktop app cannot track the npm release
# automatically — you update it by uploading a built plugin bundle via
# Settings -> Customize -> Plugins -> Add -> Upload plugin.
#
# This script produces that bundle from the npm tarball's vetted file set, so the
# zip always matches what npm ships (no .venv/node_modules/.next-cache leakage).
#
# Usage:
#   scripts/build-desktop-plugin.sh              # build a fresh tarball, then zip
#   scripts/build-desktop-plugin.sh --skip-build # reuse the newest existing tarball
#   scripts/build-desktop-plugin.sh --output PATH
#
# Prints the absolute path of the resulting zip on stdout; all logs go to stderr.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

log() { printf '[build-desktop-plugin] %s\n' "$*" >&2; }
die() { printf '[build-desktop-plugin] error: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '15,20p' "$0" >&2; }

SKIP_BUILD=0
OUTPUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1 ;;
    --output) OUTPUT="${2:-}"; shift ;;
    --output=*) OUTPUT="${1#--output=}" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

command -v zip >/dev/null 2>&1 || die "zip is required but not found on PATH"
command -v node >/dev/null 2>&1 || die "node is required but not found on PATH"

cd "$REPO_ROOT"

VERSION="$(node -p "require('./package.json').version")"
[ -n "$VERSION" ] || die "could not read version from package.json"

if [ "$SKIP_BUILD" -eq 0 ]; then
  log "building npm tarball (make package)..."
  make package >&2
fi

TARBALL="$(ls -t claude-smart-*.tgz 2>/dev/null | head -1 || true)"
[ -n "$TARBALL" ] || die "no claude-smart-*.tgz found; run without --skip-build to build one"
log "using tarball: $TARBALL"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/claude-smart-desktop.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
tar xf "$TARBALL" -C "$WORK"

PLUGIN_DIR="$WORK/package/plugin"
[ -f "$PLUGIN_DIR/.claude-plugin/plugin.json" ] \
  || die "tarball is missing plugin/.claude-plugin/plugin.json"
# Stray test artifact that occasionally rides along in the pack; not part of the plugin.
rm -f "$PLUGIN_DIR/.coverage"

OUT="${OUTPUT:-$REPO_ROOT/dist/claude-smart-desktop-$VERSION.zip}"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
# Zip the CONTENTS of plugin/ so .claude-plugin/plugin.json sits at the archive
# root, which is where Claude looks for the plugin manifest.
( cd "$PLUGIN_DIR" && zip -rq "$OUT" . )

# Guard against the exact failure this script exists to prevent: shipping the
# machine-local runtime caches instead of the vendored release bundle. Capture the
# listing first — piping unzip straight into `grep -q` trips `set -o pipefail`,
# because grep exits on the first match and SIGPIPEs unzip.
listing="$(unzip -l "$OUT")"
grep -q '\.claude-plugin/plugin\.json' <<<"$listing" \
  || die "built zip is missing the plugin manifest at its root"
if grep -Eq '(^|/)(\.venv|node_modules)/' <<<"$listing"; then
  die "built zip contains runtime caches (.venv/node_modules) — aborting"
fi

SIZE="$(du -h "$OUT" | cut -f1 | tr -d ' ')"
{
  printf '\n'
  printf '✓ built %s (%s)\n\n' "$OUT" "$SIZE"
  printf 'Upload to the Claude desktop app:\n'
  printf '  1. claude.ai -> Settings -> Customize -> Plugins -> Add -> Upload plugin\n'
  printf '  2. Drop %s -> Upload\n' "$(basename "$OUT")"
  printf '  3. Uninstall any older "Claude smart" plugin, then restart the app\n'
} >&2

echo "$OUT"
