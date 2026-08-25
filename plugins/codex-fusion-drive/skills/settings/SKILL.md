---
name: settings
description: Open the Fusion Drive e2e_policy curses settings TUI (slash fusion-drive:settings / grok-fusion-drive:settings).
user-invocable: true
---

# Fusion Drive settings TUI

When the user runs `/fusion-drive:settings`, `/grok-fusion-drive:settings`,
`/settings` (this plugin), or asks to change fusion/e2e policy: **open the
curses TUI**. Do not interview in chat. Do not dump JSON as the primary UI.

## Launch

```bash
bash "$GROK_PLUGIN_ROOT/scripts/open_settings_tui.sh" interview
```

Fallback paths (first that exists):

- `~/.grok/installed-plugins/grok-fusion-drive-ef2029e8/scripts/open_settings_tui.sh`
- Claude cache `~/.claude/plugins/cache/fusion-drive/fusion-drive/0.2.2/scripts/open_settings_tui.sh`
- `python3 <plugin>/scripts/settings_tui.py interview`

Prefer a **side terminal** (Orca `terminal create --title fusion-drive-settings`,
cmux right pane). On macOS with no TTY, the opener pops Terminal.app.

Unattended e2e/GOAL/Stably: `doctor` only. Hash-bound apply. Overlay
`e2e_policy` only. Never flip attended `plan_stop_required`. Named blocker
`missing_codex_oauth`. Never read tokens.

Companion: `grok-fusion-drive-config` for MCP `config_propose` / `config_approve`.
