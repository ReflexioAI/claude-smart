#!/usr/bin/env bash
# Run the backend command and stream its stdout/stderr into a capped log file.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"

if [ "$#" -lt 4 ] || [ "$3" != "--" ]; then
  exit 2
fi

LOG_FILE="$1"
MAX_BYTES="$2"
shift 3

mkdir -p "$(dirname "$LOG_FILE")"
claude_smart_trim_log_file "$LOG_FILE" "$MAX_BYTES"

STATUS_FILE="${TMPDIR:-/tmp}/claude-smart-backend-log-runner.$$.status"
rm -f "$STATUS_FILE"

(
  set +e
  "$@" 2>&1
  printf '%s\n' "$?" > "$STATUS_FILE"
) | while IFS= read -r line || [ -n "$line" ]; do
  claude_smart_append_capped_log "$LOG_FILE" "$MAX_BYTES" "$line"
done

status=$(cat "$STATUS_FILE" 2>/dev/null || printf '1')
rm -f "$STATUS_FILE"
exit "$status"
