---
name: fusion-workflow-author
description: Author Fusion Drive orchestration graphs and e2e recipes (Grok Rhai, Claude Dynamic Workflows, Codex/Pi named agents) for Stably+sim, api-jig-before-live, cursor-cloud team-kit, and Orca side terminals. Use when writing or adapting fusion workflows. e2e-author does not grade.
---

# Fusion workflow author

Author orchestration graphs that spawn named evidence agents. This skill is
**not** a fusion engine and **not** a substitute for bundled `create-workflow`
(generic Rhai host API only; no `fork_context`; no e2e recipes) or for
`verification-author` (that agent writes into owning `e2e/` and does not grade).

## Host surfaces

| Host | Where graphs live | How to invoke |
| --- | --- | --- |
| **Grok Build** | `~/.grok/workflows/*.rhai` (user) or `<repo>/.grok/workflows/*.rhai` (project) | `/fusion-e2e-author` or `/workflow fusion-e2e-author` |
| **Claude** | plugin `workflows/*.js` | `/claude-fusion-drive:<meta.name>` |
| **Codex / Pi** | skills + named agents only (no plugin graph runtime) | spawn YAML `name` values; Pi may also have local `~/.pi/agent/workflows/*.js` |

Grok plugin packs do **not** ship a loadable `workflows/` directory. The Grok
host discovers user/project `.rhai` files. Docs: Grok/Claude plugin
`docs/WORKFLOW_AUTHORING.md`. Rhai host API: bundled skill `create-workflow`.

Shipped Grok graph: `~/.grok/workflows/fusion-e2e-author.rhai` (verification-author
only; **does not grade**). Claude: `workflows/e2e-author.js` (same contract).

## Grok Rhai contract

First statement is a pure-literal `let meta = #{ name, description, phases }`.
`meta.name` is lowercase letters, digits, and hyphens. Phase titles must match
`phase()` calls.

Host API: `agent`, `parallel`, `phase`, `log`, `complete`. Optional
`await_user` / `pause` only for attended missing-`args` cases — **not** on
unattended e2e. No `fork_context` (rejected on authored scripts). Functions
take arguments by value and **must not close over outer vars**; pass what they
need. Maps are `#{ ... }`. Quote JSON-Schema keys (`"type"` is reserved).
Reserved identifiers include `default`, `match`, `spawn`, `async`, `await`,
`null`. Build long prompts with `+=`. Guard every agent result:
`r != () && r.success && r.output != ()`. Failed `parallel()` slots are `()`.
`isolation_worktree: false` for evidence (isolation `none` in the owning tree).
Do not import Node, touch the filesystem from the graph, or launch nested
workflows.

Unattended defaults: `stably_depth=full_sim_ui`; no interview; no
`AskUserQuestion`. Smoke-check with the host `workflow` tool
`validate_only: true` and representative `args` before offering a real run.

## Claude JS contract

`export const meta = { ... }` is the first pure-literal statement. Body uses
`agent`, `parallel`, `pipeline`, `phase`, `log`, `args`. Graphs have no direct
I/O; native `agent()` does reads/writes/commands. Evidence writers use
`isolation: 'none'` and `agentType` equal to the YAML agent name. External
seats are `seat_run` proxies only; Stably/cloud go through `stably-runner`,
not `seat_run`. Do not hardcode model slugs.

## Codex / Pi

No plugin `workflows/*.js` / `*.rhai` runtime. Author by spawning named agents
from this skill's recipes (`verification-author`, `api-jig-runner`,
`cursor-cloud-runner`, …). Isolation `none`. Do not register xAI tool-free
reviewer TOML. Pi: `/run <name>`, `inheritSkills: false`.

## Isolation and panes

Evidence (screenshots, recordings, Stably traces, jig fixtures, sim manifests)
lands in the **owning** `e2e/` tree. Isolation `none`. Do not use discarded
worktrees for those artifacts. Independent non-evidence writers still use
worktree isolation on Claude coordinate/parallel graphs.

Default pane spawn is **side-terminal teammates**, isolation `none`. See
recipe 4.

## Recipes

### 1. e2e-author (Stably + sim) — does not grade

Spawn **only** `verification-author` (isolation `none`) to write into the
project that already owns `e2e/` (never a disposable copy):

1. Stably cloud suite files that `stably-runner` will later execute with
   **exactly** `npx stably --browser cloud test` (never bare `stably`).
2. Playwright/unit A→B→C gates (fire A, observe B, assert C). HTTP 201 is not C.
3. If `stably_depth` is `jig_video_logs` or `full_sim_ui`: `e2e/jig` OpenAPI/MSW
   fixtures. Jig **MUST PASS** before live API.
4. If `stably_depth` is `full_sim_ui`: `e2e/sim-users/manifest.json` personas
   for **all UI paths** with keys `platform_runtime`, `ui_ux`, `viewports`,
   `personas`, `accessibility`, `logs`, `performance`, `privacy_security`,
   `data_integrity`, `external_writes`. Else reuse an existing manifest.

Depth: `ci_shots_logs` | `jig_video_logs` | `full_sim_ui` (unattended default
`full_sim_ui`). Prove RED on the untouched baseline. Return `gates_path`,
`gates_sha256`, `baseline_red`, `authored`, `sim_manifest_path`,
`stably_suite_path`.

**Do not grade.** Do not run `stably-runner`, observers, or `e2e-evidence` inside
`e2e-author`. Do not call `fusion-gate-reviewer` as a substitute author. Host
loops that **run** the gates belong in `e2e-unattended`, not this graph.

Grok: `~/.grok/workflows/fusion-e2e-author.rhai`. Claude:
`/claude-fusion-drive:e2e-author`. Missing `STABLY_API_KEY` /
`STABLY_PROJECT_ID` are named blockers (names only).

### 2. api-jig — must PASS before live API

Kun Chen GitHub mock is **`acp-mock`** (ACP stdio agent), not HTTP OpenAPI.
Spawn `api-jig-runner` / skill `api-test-jig`. Isolation `none`.

- ACP / agent-protocol apps: `npx --yes acp-mock --agent-message-json '{"success":true}'`
- HTTP / OpenAPI apps: `npx --yes @stoplight/prism-cli mock <spec> --port 4010`

Or MSW with `e2e/jig/` fixtures. A→B→C: start jig (A), traffic hits the jig
(B), assert recorded/replayed contract (C). HTTP 201 against production is not
C. Only after jig PASS may `backend-prober` hit the real seam. Required at
`jig_video_logs` and `full_sim_ui`.

### 3. cursor-cloud — cursor-sdk + team-kit

Spawn `cursor-cloud-runner` (isolation `none`) via cursor-sdk
`cloud:{repos:[{url, startingRef}]}`. Omit both `local` and `cloud` ⇒ silent
local, which is **NOT VERIFIED**. When `e2e_policy.cursor_cloud_video=y`,
require screen recording **and** logs bound to the same claim. Env **NAME**
`CURSOR_API_KEY`. Named prerequisite `codex_oauth` (not an env value); no
silent OpenRouter fallback.

Watch/ship with on-disk team-kit (do not vendor those skills; cite them):

`~/.cursor/plugins/cache/cursor-public/cursor-team-kit/*/skills/{loop-on-ci,control-cli,review-and-ship}/SKILL.md`

```text
gh pr view --json number,url,headRefName
gh pr checks --json name,bucket,state,workflow,link
gh pr checks --watch --fail-fast
gh run view <run-id> --log-failed
```

Source of truth is `gh pr checks`, not `gh run list`. Re-read the full set after
every push. No `--no-verify`.

```text
git fetch origin main
git diff origin/main...HEAD
git status
```

`e2e_policy.auto_review_and_merge=n` — never silent prod merge. Control-cli:
one action at a time; wait for screen patterns; prefer repo-native harness;
tmux `new-session` / `capture-pane` / `send-keys` / `kill-session`; cleanup
sessions; no credentials in the harness.

### 4. orca / cmux — side-terminal teammates

Default spawn is side terminals, isolation `none` for evidence. Do **not**
`worktree create` unless a separate checkout is required.

```text
ORCA terminal create --worktree active --title <name> --command "codex|claude|omp|pi|grok" --json
ORCA terminal wait --for tui-idle --timeout-ms 60000
ORCA terminal send --text "<brief>" --enter
```

Split with `terminal split`. Resolve the `ORCA` executable per skill `orca-cli`
(`ORCA_CLI_COMMAND`, else `orca-dev` / `orca-ide` / `orca`). Prefer `--json`.

cmux: reuse one **right** helper pane; never focus-steal:

```text
new-surface --type terminal --focus false
new-pane --type terminal --direction right --focus false
cmux send --surface <id>
```

## Spawn names (parent only, depth 1)

`verification-author`, `api-jig-runner`, `cursor-cloud-runner`, `ui-observer`,
`backend-prober`, `human-sim-observer`, `stably-runner`, `e2e-evidence`. Grok
type `grok-fusion-drive:<name>`, model `grok-4.5`, effort `high`. Never
substitute `general-purpose`, `explore`, `plan`, or `fusion-planner` for
evidence types.

## Per-workflow verification authoring (LLM instructions)

The plugin does **not** compile a universal unit-test / A→B→C CI runtime.
Every authored workflow MUST include a `verification-author` stage whose
prompt tells the LLM, for **this** workflow's exact surface:

1. **Unit tests** — generate and write tests next to the code / in owning
   `e2e/`; on-disk receipts required or the layer is **NOT VERIFIED**.
2. **Contracts** — OpenAPI or ACP fixtures; jig PASS (`acp-mock` or Prism/MSW)
   before live calls.
3. **A→B→C CI** — fire A, observe B, assert C or FAIL. Wire as a CI job or
   local script the host re-runs. HTTP 201 / counts / empty `.last-run.json`
   are not C.
4. **Optional `code_completion_judge`** — LLM-as-judge of completions,
   separate from fusion panel-judge.

`verification-author` instantiates these instructions per project/workflow.
`e2e-author` does not grade. Host loops that run the gates belong in
`e2e-unattended`.

## Credential safety

Never `cat` / `head` / `grep` `.env`, `secrets.env`, `~/.aws/`, `~/.ssh/`,
token/password fields. Surface missing env **NAMES** only: `XAI_API_KEY`,
`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `STABLY_API_KEY`,
`STABLY_PROJECT_ID`, `CURSOR_API_KEY`. Named blocker: `codex_oauth`. Accept
sandbox denial.
