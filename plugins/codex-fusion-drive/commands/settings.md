---
description: Open the Fusion Drive e2e_policy settings TUI
argument-hint: "[show|doctor|interview|apply]"
---

Open the Fusion Drive **settings TUI box** now. Do not dump JSON as the primary UI.

1. Resolve `scripts/open_settings_tui.sh` next to this plugin (Grok: `$GROK_PLUGIN_ROOT/scripts/open_settings_tui.sh` or `~/.grok/installed-plugins/grok-fusion-drive-ef2029e8/scripts/open_settings_tui.sh`; Claude: plugin cache `scripts/open_settings_tui.sh`).
2. Launch it so a curses list TUI appears:
   - Prefer a **side terminal** (Orca `ORCA terminal create --worktree active --title fusion-drive-settings --command "bash <script> interview" --json`; cmux right helper pane). Isolation `none`.
   - Else run `bash <script> interview` (opens Terminal.app on macOS when this session has no TTY).
3. Unattended e2e/GOAL/Stably: `bash <script> doctor` only — skip interview.
4. Persist overlay `e2e_policy` via printed SHA-256 (`apply --hash <sha> --confirmed`). Never flip attended `plan_stop_required`. Never read tokens. `missing_codex_oauth` is a named blocker.

Args: $ARGUMENTS
