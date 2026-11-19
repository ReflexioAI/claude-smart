set -eu
owned=$(mktemp -d)
trap 'rm -rf -- "$owned"' EXIT
export HOME="$owned/account"
runtime="$owned/runtime canonical"
mkdir -p "$runtime/scripts" "$HOME/.reflexio"
printf '%s\n' 'printf "%s\n" "scope-slash-fallback-ok"' >"$runtime/scripts/cli.sh"
cygpath -w "$runtime" >"$HOME/.reflexio/plugin-root.txt"
root="$HOME/.reflexio/plugin-root"; if [ ! -d "$root" ]; then IFS= read -r root <"$root.txt" || exit 1; if command -v cygpath >/dev/null 2>&1; then root="$(cygpath -u "$root")"; fi; fi; bash "$root/scripts/cli.sh" show
