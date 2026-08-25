---
description: Local multi-model ultraplan in this terminal, then /create-workflow with TUI settings and hard gates
argument-hint: "[task / light|medium|heavy|ultra / debate]"
---

Load and follow the plugin `ultraplan` skill in **this terminal**.

1. Read e2e_policy via `python3 scripts/settings_tui.py show` (no secrets). Apply those flags.
2. Phased deliberation here: Stage → Curate → Fuse (`grok_fusion_drive_deliberate`, engine from TUI, `in_harness` unless they asked otherwise).
3. On Grok, fire bundled `/create-workflow` / `fusion-workflow-author`: hard gates, test authoring, UI catalogue + WIP studio, api-jig, Stably, Cursor cloud as policy says. Smoke-check then run when unattended or they asked to execute.

$ARGUMENTS
