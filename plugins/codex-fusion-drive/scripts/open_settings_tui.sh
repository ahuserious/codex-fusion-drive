#!/usr/bin/env bash
# Open Fusion Drive curses settings TUI. Prefer this TTY; else a side/OS terminal.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/settings_tui.py"
ACTION="${1:-interview}"
shift || true

if [[ ! -f "$SCRIPT" ]]; then
  echo "missing $SCRIPT" >&2
  exit 1
fi

if [[ "${FUSION_E2E_UNATTENDED:-}" == "1" ]]; then
  exec python3 "$SCRIPT" doctor "$@"
fi

if [[ "$ACTION" != "interview" ]]; then
  exec python3 "$SCRIPT" "$ACTION" "$@"
fi

if [[ -t 0 && -t 1 ]]; then
  exec python3 "$SCRIPT" interview "$@"
fi

if command -v osascript >/dev/null 2>&1; then
  osascript -e "tell application \"Terminal\" to do script \"python3 $(printf %q "$SCRIPT") interview; exit\"" -e "tell application \"Terminal\" to activate" >/dev/null
  echo "opened Fusion Drive settings TUI in Terminal.app"
  exit 0
fi

exec python3 "$SCRIPT" interview "$@"
