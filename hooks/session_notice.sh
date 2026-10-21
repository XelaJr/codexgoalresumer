#!/bin/sh
# SessionStart notice for unfinished Codex goals. Opt-in: exits immediately,
# without starting Python, unless the plugin option "notice" is enabled.
flag=$(printf '%s' "${CLAUDE_PLUGIN_OPTION_NOTICE:-$CODEX_GOALS_NOTICE}" | tr 'A-Z' 'a-z' | tr -d ' ')
case "$flag" in
  1|true|yes|on) ;;
  *) exit 0 ;;
esac
script="$(dirname -- "$0")/../scripts/codex_goals.py"
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    exec "$py" "$script" notify
  fi
done
exit 0
