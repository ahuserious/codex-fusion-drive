---
name: ultraplan
description: >
  Local multi-model fusion + phased in-terminal planning, then author a host
  workflow. Grok fires /create-workflow with TUI e2e_policy, hard gates, test
  authoring, UI catalogue/studio, and connections. Use when the user says
  ultraplan or /ultraplan.
user-invocable: true
---

# ultraplan

**Stay in this terminal** for planning. Do not spawn a side pane for the
planner itself. Evidence workers later use Orca/cmux **side terminals**.

Load `fusion-e2e-unattended` when the request is unattended e2e/GOAL/Stably —
do not interview, do not plan-stop.

## 0. Apply settings TUI first

Read overlay `e2e_policy` (no secrets):

```bash
python3 "$GROK_PLUGIN_ROOT/scripts/settings_tui.py" show
```

Fallbacks: `~/.grok/installed-plugins/grok-fusion-drive-ef2029e8/scripts/settings_tui.py`
or Claude cache `scripts/settings_tui.py`. If the user has not set policy and
the run is **attended**, offer `/fusion-drive:settings` once; unattended skips
interview and uses the overlay/defaults.

Bake every flag into the fused plan **and** the authored workflow: `unattended`,
screenshots, video, `cursor_cloud_video`, `stably_authoring`, `stably_depth`,
`loop_on_ci`, `auto_review_and_merge` (never silent prod merge), `fusion_preset`
/`engine` (`in_harness` unless they asked `openrouter_fusion`),
`validation_gating`, `test_flow_author_style=verification-author-first`,
`api_jig_required_before_live`. Named blocker `missing_codex_oauth`.

## 1. Parse weight / topology

Same map as Claude ultraplan: `light|medium|heavy|ultra|single-stream`,
`debate`/`rounds`, `opinion`, `best-of-n`, `no gates` → `-nogate` twin,
multi-repo → stage fan-out. Default unstated small task → `single-stream`;
context-heavy → `medium`. Task text is the user goal **verbatim**.

## 2. Phased deliberation (this terminal)

| Phase | Who | What |
| --- | --- | --- |
| **Stage** | Host explorers (tools on) | Sweep scopes. Real reads (grep/read_file). ≥2 repos or ≳50 files or "grab context" → parallel explorers. |
| **Curate** | Host | Context Bundle v1 only: `{id,title,scope,interfaces,invariants,constraints,risks,open_questions,evidence(file:line)}`. Target ~25k tok/bundle, cap ~60k. Main window keeps fused plan + bundle **index** only. |
| **Fuse** | Local multi-model fusion | Grok: `grok_fusion_drive_settings` → `provider_status` → `deliberate` with `engine` from TUI (`in_harness`: grok45-panel + gpt56sol-panel 1.05M + fable5-panel, gpt56sol judge/fuser). Claude: existing `ultraplan`/`plan-debate`/`draco-fusion` graphs + `seat_run`. Never collapse to `claude-only-oauth` / `no-fable` / Pi `duo`. |
| **Plan the workflow** | This session | Executable plan that **names** hard gates, test authoring, UI catalogue/studio, connections. |

Record seat provider/billing/context. `openai-codex/gpt-5.6-sol` is 272K;
OpenRouter `openai/gpt-5.6-sol` is 1.05M.

Attended: after plan-gate PASS, stop unless they already said execute.
Unattended: do not stop.

## 3. Grok: fire `/create-workflow`

Follow bundled skill `create-workflow` (Rhai host API, no `fork_context`).
Also load `fusion-workflow-author`. Author, smoke-check (`validate_only: true`),
save (`~/.grok/workflows/<name>.rhai` or project `.grok/workflows/`), then
**run** when unattended or they asked to execute.

The graph MUST include these phases (skip a phase only when TUI turns it off,
and say so in the plan):

1. **verification-author** — unit tests, contracts, A→B→C CI (fire A, observe B,
   assert C). HTTP 201 / counts / empty `.last-run.json` are not C. Does not grade.
2. **api-jig-runner** — before live. ACP: `npx --yes acp-mock`. HTTP: Prism/MSW.
3. **UI catalogue + studio (WIP)** — `source-picker` harvest (nav/form/table/empty/error)
   into `.fusion-studio/bank/harvest/`. `ui-studio` / `studio-bank`: first-use
   interviews the user unless unattended (then leave studio WIP). Lavish sidecar
   separate from the produced site.
4. Parallel **ui-observer** + **backend-prober** (+ **human-sim-observer** if
   a manifest exists or `stably_depth=full_sim_ui`).
5. **stably-runner** if `stably_authoring=y` — `npx stably --browser cloud test`
   from owning `e2e/`. Depth from TUI.
6. **cursor-cloud-runner** if Cursor cloud is in the plan — team-kit
   `loop-on-ci` / `control-cli` / `review-and-ship`. Video+logs when
   `cursor_cloud_video=y`.
7. **e2e-evidence** fail-closed vs `schemas/e2e-layer-evidence.schema.json`.
8. Independent skeptics (read-only) on each claimed layer.

Isolation `none` for evidence. Side-terminal teammates for workers, not the
planner. `loop_on_ci` n|y|heavy. Missing requested layer = **NOT VERIFIED**.

## 4. Claude / Codex / Pi

- **Claude:** keep routing to plugin graphs (`ultraplan`, `plan-debate`,
  `draco-fusion`, `opinion`, `best-of-n`) with `args.e2e_policy` from the TUI.
  Execution handoff: `/claude-fusion-drive:e2e-author` then `e2e-unattended`.
- **Codex / Pi:** no create-workflow. Spawn named agents from
  `fusion-workflow-author` with the same phase list.

## 5. Connections

Orca/cmux side terminals for evidence workers. Env **NAMES** only:
`XAI_API_KEY`, `OPENROUTER_API_KEY`, `STABLY_API_KEY`, `STABLY_PROJECT_ID`,
`CURSOR_API_KEY`. Never echo secrets.
