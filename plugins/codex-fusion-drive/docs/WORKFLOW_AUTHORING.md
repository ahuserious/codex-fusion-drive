# Grok Fusion Drive Workflow Authoring

Grok Build workflows are deterministic Rhai scripts that orchestrate
subagents. The host runs them via the `workflow` tool. Graphs live under
user `~/.grok/workflows/*.rhai` or project `<repo>/.grok/workflows/*.rhai`.
This plugin does **not** ship a loadable `workflows/` directory — Grok does
not execute plugin-root graphs the way Claude loads `workflows/*.js`.

The complete Rhai dialect and host API (including pitfalls) is bundled skill
`create-workflow`. That skill is **not** a substitute for this document: it
has no Fusion e2e recipes and no `fork_context` for authored scripts.

Companion skill: `fusion-workflow-author` (YAML name exact).

## Per-workflow verification (LLM instructions, not a compiler)

Every authored workflow must tell `verification-author` to generate, for that
workflow's exact surface: **unit tests** (on-disk receipts), **contracts**,
and **A→B→C CI** (fire A, observe B, assert C). Optional
`code_completion_judge` is separate from fusion panel-judge. The plugin does
not compile a universal test runtime.

## API jig

Kun Chen GitHub mock is `acp-mock` (ACP stdio). HTTP OpenAPI uses Prism/MSW.
Jig MUST PASS before live API.

## Host split

| Host | Graphs | Invoke |
| --- | --- | --- |
| Grok Build | `~/.grok/workflows/*.rhai` | `/<meta.name>` or `/workflow <meta.name>` |
| Claude | plugin `workflows/*.js` | `/claude-fusion-drive:<meta.name>` |
| Codex / Pi | skills + named agents only | spawn YAML `name`; Pi may keep local JS under `~/.pi/agent/workflows/` |

## Shipped Grok graph

| Command | Graph | Writes |
| --- | --- | --- |
| `/fusion-e2e-author` | `~/.grok/workflows/fusion-e2e-author.rhai` | native `verification-author`, isolation `none` in owning `e2e/`; **does not grade** |

Claude counterparts (plugin graphs, not loaded by Grok):
`/claude-fusion-drive:e2e-author` and `/claude-fusion-drive:e2e-unattended`.
`e2e-unattended` runs collectors and grades evidence; `e2e-author` only writes
gates/suites/manifests.

## File contract

The first statement must be a pure-literal meta map — no variables, no
function calls:

```rhai
let meta = #{
    name: "fusion-e2e-author",
    description: "verification-author emits Stably cloud suites and sim-user manifests into the owning e2e tree; does not grade them",
    phases: [
        #{ title: "Author", detail: "Write Stably cloud suite, A to B to C gates, and sim-user campaign files" },
        #{ title: "Hash", detail: "SHA-256 of authored files; prove RED on untouched baseline" },
    ],
};
```

`meta.name` is lowercase letters, digits, and hyphens. `meta.phases` titles
must match later `phase()` calls so the `/workflows` rail lines up.

Supported host API for authored Fusion graphs:

- `agent(prompt)` / `agent(prompt, opts)` — opts: `label`, `phase`,
  `capability_mode` (`read-only` | `read-write` | `execute` | `all`),
  `output_schema`, `agent_type`, `model` (omit to inherit),
  `isolation_worktree` (bool). Evidence agents set
  `isolation_worktree: false` (isolation `none`).
- `parallel([#{ prompt, label, ... }, ...])` — array of option maps, **no
  closures**, barrier semantics.
- `phase(title)`, `log(message)`, `complete(value)`.
- `args` is the tool `args` value, or `()` if absent.
- `json_encode(value)` for quoting untrusted prompt data.
- `budget()`, `write_scratch_file` / `read_scratch_file` as needed.

**Do not** request `fork_context` (rejected on user/project/inline scripts).
**Functions do not close over outer vars** — they take arguments by value;
pass schemas, labels, and prompt text in. Maps are `#{ ... }`. Unit `()` is
null; `x != ()` is the existence check. Quote JSON-Schema keys because
`type` is a Rhai keyword. Reserved-but-unused identifiers fail at compile
(`default`, `match`, `spawn`, `async`, `await`, `null`, `shared`, …).

Build long prompts with `+=` statements. String mutators like `s.trim()`
change `s` in place and return `()`. Guard every agent output. Failed
`parallel()` slots are `()`. Agent-level failure is data (`success: false`);
infrastructure failure throws.

The graph body has no direct filesystem or shell access except scratch
helpers. Native `agent()` performs repository reads, tool calls, writes, and
commands.

Normalize task input without assuming a CLI encoding. Always provide a
useful no-argument fallback. Unattended e2e default `stably_depth` is
`full_sim_ui`. Clamp caller-controlled fan-out (shipped Fusion graphs cap
workers at 8).

Do not import Node modules. Workflows cannot launch other workflows — inline
the child's logic or split into separate named graphs.

## Isolation and integration

Evidence writers (`verification-author`, `api-jig-runner`, `ui-observer`,
`backend-prober`, `human-sim-observer`, `stably-runner`,
`cursor-cloud-runner`, `e2e-evidence`) use isolation `none` so screenshots,
recordings, logs, jig fixtures, and Stably traces land in the owning `e2e/`
tree rather than a discarded worktree. Set `isolation_worktree: false` (or
omit it).

Independent non-evidence writers that must not collide still use a private
worktree (`isolation_worktree: true`) plus an explicit select-and-apply step
if any edit should reach the parent. Workflow assets do not commit, push,
publish, or mutate remotes.

## Fusion e2e recipes

Skill `fusion-workflow-author` owns these four recipes. Copy them into any
new graph rather than paraphrasing away the command fences.

### 1. e2e-author — Stably + sim (does not grade)

Spawn **only** `verification-author` (`agent_type: "verification-author"`,
isolation `none`) to write into the project that already owns `e2e/`:

1. Stably cloud suite files executed later with exactly
   `npx stably --browser cloud test` (never bare `stably`).
2. Playwright/unit A→B→C gates. HTTP 201 is not C.
3. At `jig_video_logs` or `full_sim_ui`: `e2e/jig` OpenAPI/MSW fixtures. Jig
   **MUST PASS** before live API.
4. At `full_sim_ui`: `e2e/sim-users/manifest.json` personas for **all UI
   paths** with keys `platform_runtime`, `ui_ux`, `viewports`, `personas`,
   `accessibility`, `logs`, `performance`, `privacy_security`,
   `data_integrity`, `external_writes`. Else reuse an existing manifest.

Depth: `ci_shots_logs` | `jig_video_logs` | `full_sim_ui` (unattended default
`full_sim_ui`). Prove RED on the untouched baseline. Return `gates_path`,
`gates_sha256`, `baseline_red`, `authored`.

**Do not grade.** Do not run `stably-runner`, observers, or `e2e-evidence`
inside this graph. Host loops that execute the gates belong in
`e2e-unattended`. Shipped file:
`~/.grok/workflows/fusion-e2e-author.rhai`.

### 2. api-jig — must PASS before live API

No kunchenguid mock-API AXI exists. Spawn `api-jig-runner` / skill
`api-test-jig` (OpenAPI Prism or MSW). Isolation `none`.

```text
npx --yes @stoplight/prism-cli mock <spec> --port 4010
```

A→B→C: start jig (A), traffic hits the jig (B), assert recorded/replayed
contract (C). HTTP 201 against production is not C. Required at
`jig_video_logs` and `full_sim_ui`.

### 3. cursor-cloud — cursor-sdk + team-kit

Spawn `cursor-cloud-runner` via cursor-sdk
`cloud:{repos:[{url, startingRef}]}`. Silent local SDK (omit both `local`
and `cloud`) is **NOT VERIFIED**. When `e2e_policy.cursor_cloud_video=y`,
require video **and** logs bound to the same claim. Env **NAME**
`CURSOR_API_KEY`. Named prerequisite `codex_oauth`; no silent OpenRouter
fallback.

Team-kit on disk (cite; do not vendor):

`~/.cursor/plugins/cache/cursor-public/cursor-team-kit/*/skills/{loop-on-ci,control-cli,review-and-ship}/SKILL.md`

```text
gh pr view --json number,url,headRefName
gh pr checks --json name,bucket,state,workflow,link
gh pr checks --watch --fail-fast
gh run view <run-id> --log-failed
git fetch origin main
git diff origin/main...HEAD
git status
```

Source of truth is `gh pr checks`, not `gh run list`. No `--no-verify`.
`e2e_policy.auto_review_and_merge=n` — never silent prod merge. Control-cli:
one action at a time; tmux `new-session` / `capture-pane` / `send-keys` /
`kill-session`; cleanup; no credentials in the harness.

### 4. orca / cmux — side-terminal teammates

Default spawn is side terminals, isolation `none` for evidence. Do not
`worktree create` unless a separate checkout is required.

```text
ORCA terminal create --worktree active --title <name> --command "codex|claude|omp|pi|grok" --json
ORCA terminal wait --for tui-idle --timeout-ms 60000
ORCA terminal send --text "<brief>" --enter
```

cmux: right helper pane, never focus-steal:

```text
new-surface --type terminal --focus false
new-pane --type terminal --direction right --focus false
cmux send --surface <id>
```

## No mid-run human input (unattended)

Unattended e2e/GOAL/Stably graphs must not `pause` / `await_user` for
interview, plan confirmation, or preference questionnaires. Missing secret
**NAMES** (`STABLY_API_KEY`, `STABLY_PROJECT_ID`, `CURSOR_API_KEY`,
`codex_oauth`) are named blockers in the result, not prompts. Attended
graphs may `pause("verification", ...)` only when `args` itself is missing
and a resume cannot invent them.

Do not fake a human approval with another agent.

## Inspect, edit, and save

`/workflows` lists live and retained **runs**, not saved definitions. Saved
graphs are the `.rhai` files on disk. Each launch returns `script_path`;
edit that copy, smoke-check with `validate_only: true`, and launch as a new
run. Resume uses the original immutable script and args.

`validate_only` checks metadata, compiles the full script, and executes the
single path selected by the supplied args and canned host results. It does
not prove live tools or every branch.

## Review checklist

Before shipping a Grok graph, verify:

- `let meta` is the first pure-literal statement and phase titles match calls;
- task input handles `args.task`, raw strings, and missing args;
- every fan-out and loop is bounded;
- no `fork_context`; functions do not close over outer vars;
- evidence writers use `isolation_worktree: false` (owning `e2e/` tree);
- `e2e-author` spawns only `verification-author` and **does not grade**;
- jig PASS is required before live API in any graph that hits HTTP;
- cursor-cloud uses `cloud:{repos}` plus team-kit command fences;
- Orca/cmux default is side-terminal teammates, isolation `none`;
- collapse, missing evidence, and missing named secrets fail closed;
- no stage assumes arbitrary mid-run user input on the unattended class;
- no graph promises a merge, grade, or human approval it does not perform.

## Credential safety

Never `cat` / `head` / `grep` `.env`, `secrets.env`, `~/.aws/`, `~/.ssh/`,
or token/password fields. Surface missing env **NAMES** only. Accept sandbox
denial.
