---
name: fusion-e2e-unattended
description: Use for unattended fusion/e2e/GOAL/Stably/sim-user/cloud-agent work that must ship with layered evidence and must not ping the user.
---

# Unattended fusion e2e

If the user asked for end-to-end work, fusion, a GOAL/PRD, Stably/cloud, simulated users, Cursor cloud, a swarm, or said not to stop: **do not interview, do not AskUserQuestion, do not wait for continue, do not plan-stop.** Record assumptions and keep working until terminal evidence or a permitted blocker (missing secret **name**, irreversible paid/prod write above an explicit cap). Explicit user-defined stop conditions are hard gates **before** the first mutating action.

Host-spawned named subagents ARE execution, not a ping and not a todo list.

## Live flags (do not pretend they are already off)

Shipped `lifecycle.require_explicit_user_confirmation` and
`profiles.*.execution.require_confirmed_plan` remain **true** in default JSON.
Do **not** run `config_propose` merely to flip those flags. For this class,
after the plan gate PASSes, hash the **original user message** into
`plan_confirm(confirmed=true, user_message_sha256=<sha256 of original request>)`,
create the Claude host goal with `TaskCreate`, call `goal_record`, then
`pre_execution`. Do not ping. Do not AskUserQuestion.

## Evidence layers (hard fail)

Do not call work shipped, done, green, or verified unless **every requested layer** exists on disk for the **exact claimed surface**. Name missing layers **NOT VERIFIED**. Never substitute static review, collection counts, HTTP status, or an empty green `.last-run.json` for a requested live layer. Another seat's PASS is not health.

1. **UI/UX** — fresh screenshots **and** screen recording (or a Stably/Playwright/Cursor-cloud trace that contains actual frames). Write observer notes of what the recording shows, including empty states, errors, and mobile/desktop.
2. **Simulated users** — run the existing human-sim manifest (agentic-app personas and/or raw user on web/mobile). Do not block on a new preference questionnaire when a manifest already exists. If none exists, mark simulated-users **NOT VERIFIED** rather than interview.
3. **Stably cloud** — when cloud verification was requested, run a full suite from the project that owns `e2e/`, not a disposable untracked copy. Drive `npx stably --browser cloud test` (never interactive `stably`). Surface `STABLY_API_KEY` / `STABLY_PROJECT_ID` as **names** only. `--browser cloud` that is still local is **NOT VERIFIED**.
4. **Cursor cloud** — spawn `cursor-cloud-runner`. When `e2e_policy.cursor_cloud_video=y`, require screen recording **and** logs bound to the same claim. Instruct via cursor-sdk (`cloud:{repos}`); watch/ship with team-kit `loop-on-ci` / `control-cli` / `review-and-ship`. Silent local SDK run is **NOT VERIFIED**.
5. **Backend / conceptual** — the distinctive function must traverse its real execution seam. Keep system logs, app logs, **and network logs**. HTTP 201-by-revision and test-item counts are not behavior.
6. **Verification author** — a separate agent authors the gates **and** Stably/sim-user campaigns into owning `e2e/`; the build loop keeps executing them until they pass. Do not ping the user to babysit.
7. **Unit tests** — on-disk receipts. Missing requested = **NOT VERIFIED**.
8. **API jig** — `api-jig-runner`. Kun Chen `acp-mock` for ACP/agent protocol; Prism/MSW for HTTP OpenAPI. Must pass before any live API. Depth `jig_video_logs` needs jig + video + logs.
9. **code_completion_judge** — optional LLM-as-judge **completion CI**, separate from fusion panel-judge. Missing requested = **NOT VERIFIED**.

A→B→C: fire A, observe B, assert C or FAIL. HTTP 201 / counts / empty `.last-run.json` are not C.

Schema: `schemas/e2e-layer-evidence.schema.json` (layers include `unit_tests`, `network_logs`, `code_completion_judge`). `e2e_policy` overlay (settings TUI) does not interview on this class.

## Fusion routing

- Run the **requested** fusion profile. Do not silently collapse to `claude-only-oauth`, `no-fable`, or Pi `duo`.
- When multi-model fusion or OpenRouter 1.05M Sol was requested, select `maximum-intelligence` (engine `in_harness`: grok45-panel + gpt56sol-panel via `openrouter_api` `openai/gpt-5.6-sol` + fable5-panel, gpt56sol judge/fuser). Do not keep `claude-only-oauth`. `xai-claude-oauth` has no OpenRouter Sol. `openrouter_fusion` is never a silent engine fallback.
- Record each seat's provider path, billing path (subscription vs OpenRouter), and context window. `openai-codex/gpt-5.6-sol` is **272K**. OpenRouter `openai/gpt-5.6-sol` is **1.05M**.
- Probe **that** named seat; another seat's success is not health.
- Treat the original unattended request as standing billable authorization for `fuse` / `fuse_start` external-usage confirmation unless a numeric cap would be exceeded.

## Named subagents (plugin `agents/`)

Keep existing `fusion-planner`, `fusion-gate-reviewer`, `fusion-rescue-agent`,
`human-sim-observer`. Add and spawn these exact YAML `name` values — never
`fable-5-sub`, `opus-5-sub`, unlabeled `agent()`, `general-purpose`, `explore`,
or `plan` as substitutes.

| YAML `name` | Host spawn | isolation | Notes |
| --- | --- | --- | --- |
| `verification-author` | `Agent` / `TaskCreate` `subagent_type=verification-author` | `none` | FIRST after execution_start. Authors gates; does not grade them. |
| `ui-observer` | parallel with backend-prober | `none` | Screenshots AND recording plus notes. |
| `backend-prober` | parallel with ui-observer | `none` | Real execution seam + system/app logs. |
| `human-sim-observer` | existing campaign only | `none` | No `human_sim_questions`. |
| `stably-runner` | after or parallel with observers | `none` | Author + run full suite from owning `e2e/`. Depth ladder: `ci_shots_logs` \| `jig_video_logs` \| `full_sim_ui`. `npx stably --browser cloud test` never bare `stably`. |
| `cursor-cloud-runner` | when Cursor cloud requested | `none` | cursor-sdk cloud + team-kit loop-on-ci. Video+logs when policy says so. |
| `api-jig-runner` | before live API | `none` | `acp-mock` (ACP) or Prism/MSW (HTTP); must pass before live. |
| `studio-bank` | optional UI Studio **WIP** | `none` | First-use interviews the user. Do not claim pixels without artifact path. |
| `e2e-evidence` | after collectors | `none` | Fail-closed ledger. |
| `fusion-planner` | plan artifact only | `none` | Do not execute. |
| `fusion-gate-reviewer` | ×2 per material stage | n/a | Exact-SHA receipts; does not author e2e gates. |
| `fusion-rescue-agent` | repeated fingerprints | n/a | Instead of pinging the user. |

### Claude spawn recipe

Parent session only (depth 1). Bracket every material batch:

1. `approval_gate_start` `stage=subagent_pre_execution` with artifact `{objective, tools, evidence paths, cost/egress, agent name}` and two independent `fusion-gate-reviewer` receipts.
2. Host `Agent` or `TaskCreate` with `subagent_type` equal to the YAML `name`, `isolation: none`, cwd = the project that owns `e2e/`. Driver stays host fable/opus. Fusion panel/judge/fuser stay MCP `fuse` / `seat_run` after `preset_resolve` — never Agent-tool seats.
3. `approval_gate_start` `stage=subagent_post_execution` on the result hash.

Order after `execution_start`: `verification-author` (gates + Stably/sim-user authoring into owning `e2e/`) → `api-jig-runner` (must pass before live API) → parallel `ui-observer` + `backend-prober` (+ `human-sim-observer` if a campaign exists, else at `full_sim_ui` author personas for all UI paths then run) + `stably-runner` + `cursor-cloud-runner` (when cloud used) → `e2e-evidence`. If verification-author gates fail, re-run stably-runner/observers/rescue until pass or a permitted blocker. Loop until every requested layer exists on disk.

### Pane spawn default (Orca / cmux)

Default spawn is **side terminal teammates**, isolation `none` for evidence. Not worktree isolation for screenshots/Stably.

- Orca: `ORCA terminal create --worktree active --title <name> --command "codex|claude|omp|pi|grok" --json`; `terminal wait --for tui-idle --timeout-ms 60000`; `terminal send --text "<brief>" --enter`. Split with `terminal split`. Do **not** `worktree create` unless a separate checkout is required.
- cmux: reuse one right helper pane (`new-surface --type terminal --focus false` or `new-pane --type terminal --direction right --focus false`); `cmux send --surface <id>`. Never focus-steal.

Codex OAuth is a **named prerequisite** for Sol-via-subscription seats. Doctor/settings TUI must show missing `codex_oauth` as a blocker. No silent fallback.

Graph: `/claude-fusion-drive:e2e-unattended` (`workflows/e2e-unattended.js`). Native agents own tools and writes. Stably/cloud via `stably-runner`, not `seat_run`. Do not hardcode model slugs.

### Other hosts (same names)

- **Grok Build**: `spawn_subagent(subagent_type=grok-fusion-drive:<name>)`. Native model exact `grok-4.5` / effort `high` (requested xhigh).
- **Codex**: host-owned `gpt-5.6-sol` custom agents with those names (272K openai-codex path unless 1.05M OpenRouter Sol was requested). Do not register the xAI tool-free reviewer TOML.
- **Pi**: `/run <name>` with YAML model pinned; `inheritSkills: false`. If fusion+1M was requested, do not leave `activeStyle=duo`; switch to `minimax-council` or `software-quad` (OpenRouter Sol 1.05M).

## Credential safety

Never `cat`, `head`, or `grep` `.env`, `secrets.env`, `~/.aws/`, `~/.ssh/`,
`~/.claude/settings.json`, auth.json, or token/password fields. Surface missing
env **NAMES** only: `XAI_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `STABLY_API_KEY`, `STABLY_PROJECT_ID`, `CURSOR_API_KEY`.
Accept sandbox denial. Never print secret values. Missing `codex_oauth` is a
named blocker (not an env value).

## Harness notes

- **Codex** `~/.codex/AGENTS.md` stop-and-ask is overridden for this class of work.
- **Claude** must not interview. Fusion Drive `active_profile` must match the requested multi-model profile.
- **Grok Fusion Drive** must not stop after the plan gate on unattended work.
- **Pi** `duo` is not full fusion when the user asked for fusion + 1M.
- **Orca / cmux** default spawn is **side terminal teammates** (`ORCA terminal create --worktree active` / `cmux` split), `isolation=none`. Not worktree isolation for screenshots/Stably.

## e2e_policy settings TUI

Slash **`/fusion-drive:settings`** (Claude) / **`/grok-fusion-drive:settings`**
opens the curses TUI (`scripts/open_settings_tui.sh`). Persist overlay
`e2e_policy` via hash-bound propose+approve. Unattended e2e/GOAL/Stably
**skips** the interview. `auto_review_and_merge=n` never silent prod merge.
Doctor/settings must show missing `codex_oauth` as a **named blocker** for
Sol-via-subscription, not a silent OpenRouter fallback.

## Per-workflow unit tests / contracts / A→B→C

These are **LLM instructions instantiated per workflow**, not a compiled plugin
CI. `verification-author` must author, for the claimed surface: unit tests
(on-disk receipts), contracts, A→B→C CI (fire A, observe B, assert C). Missing
requested layer = **NOT VERIFIED**.

## API jig before live calls

Kun Chen GitHub mock is `acp-mock` (ACP stdio). HTTP OpenAPI uses Prism/MSW.
Spawn `api-jig-runner` / skill `api-test-jig`. Local simulated endpoint
**MUST pass** before live API. Stably depth `jig_video_logs` uses this.

## Extra agents

Keep existing roster. Add: `cursor-cloud-runner` (Cursor cloud video+logs when `cursor_cloud_video=y`), `api-jig-runner` (before live HTTP), optional `studio-bank`. Host order: verification-author → api-jig-runner → parallel ui-observer+backend-prober → human-sim-observer if manifest → cursor-cloud-runner if Cursor cloud → stably-runner → e2e-evidence.

Schema `schemas/e2e-layer-evidence.schema.json` now requires `unit_tests`, `network_logs`, `code_completion_judge` in addition to the original six. Missing requested = **NOT VERIFIED**.

## Stably authoring + depth

Plugin **authors** Stably cloud runs and sim-user campaigns into owning `e2e/`. `npx stably --browser cloud test` never bare `stably`. Depth: `ci_shots_logs` | `jig_video_logs` | `full_sim_ui` (default on unattended e2e). At `full_sim_ui` author personas for all UI paths then run.

## UI Studio (WIP)

Skills `ui-studio` + `source-picker`. **WIP.** First-time use interviews the
user and iterates the bank with them; stamp `.fusion-studio/bank/.initialized`
only after accept. Unattended e2e skips studio interview. Lavish sidecar
(`npx -y lavish-axi`) is separate from the produced site. Do not claim pixels
without a Lavish artifact path.
