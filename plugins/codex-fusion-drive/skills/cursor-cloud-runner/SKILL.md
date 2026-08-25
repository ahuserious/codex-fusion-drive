---
name: cursor-cloud-runner
description: Instruct Cursor cloud/team agents via cursor-sdk cloud:{repos} plus inlined team-kit loop-on-ci / control-cli / review-and-ship (and ci-watcher). Require video+logs when cursor_cloud_video=y. Named blocker codex_oauth.
---

# Cursor cloud runner

Spawn protocol for agent `cursor-cloud-runner`. You **instruct Cursor cloud / team agents** and bind their artifacts. You do not author e2e gates. Do not ping. Do not AskUserQuestion. Never read secrets; env **NAMES** only.

This skill **inlines** on-disk Cursor team-kit recipes. Fusion does **not** vendor `loop-on-ci`, `control-cli`, or `review-and-ship` as separate Grok/Claude/Codex/Pi skills. Canonical team-kit files (hash `bdf7aa355337897f167153e05069aca505dae17c`):

`/Users/DanBot/.cursor/plugins/cache/cursor-public/cursor-team-kit/bdf7aa355337897f167153e05069aca505dae17c/skills/{loop-on-ci,control-cli,review-and-ship}/SKILL.md`

plus agent `.../agents/ci-watcher.md` (`is_background: true`).

Native team-kit CI/TUI/ship skills **do not launch cloud agents**. Launching cloud is this role, using sibling `@cursor/sdk` (`references/runtime-choice.md`) or REST `https://cursor.com/docs/cloud-agent/api`. A later injected `cursor-cloud-runner` copy may exist inside the team-kit cache; that is fusion language, not original team-kit. Do not claim `rg -i cloud` is empty on that cache.

## Spawn

Parent only (depth 1). Isolation `none`. cwd = owning `e2e/` (evidence), even though the cloud VM clones GitHub. Default pane spawn is **side terminal teammates**, not a nested worktree: Orca `ORCA terminal create --worktree active --title cursor-cloud-runner --command "codex|claude|omp|pi|grok" --json`; cmux right helper pane (`new-surface --type terminal --focus false` or `new-pane --type terminal --direction right --focus false`); never focus-steal.

| Host | How to spawn | Model pin |
| --- | --- | --- |
| Grok Build | `spawn_subagent(subagent_type=grok-fusion-drive:cursor-cloud-runner)` | `grok-4.5` effort `high` |
| Claude | `Agent` / `TaskCreate` `subagent_type=cursor-cloud-runner` | `fable` effort `high` |
| Codex | host-owned custom agent named `cursor-cloud-runner` | `gpt-5.6-sol` (272K openai-codex unless 1.05M OpenRouter Sol was requested) |
| Pi | `/run cursor-cloud-runner`; `inheritSkills: false` | YAML pin |

## Named blocker: `codex_oauth`

Sol-via-subscription seats need named prerequisite `codex_oauth`. Doctor/settings TUI must show missing `codex_oauth` as a **named blocker** (`missing_codex_oauth`). Never silently fall back to OpenRouter Sol, `OPENAI_API_KEY`, or `claude-only-oauth`. This is a name, not a token: never read or echo credentials.

Env **NAMES** only: `CURSOR_API_KEY`. Missing that name is a blocker. Never print values. `ERROR_GITHUB_NO_USER_CREDENTIALS` is an environment-setup issue (GitHub connection on the Cursor account), not a code bug.

## A→B→C

1. **Fire A** — dispatch a **cloud** agent with a concrete task + success criteria. `cloud.repos` is required.
2. **Observe B** — retain the cloud run receipt, logs, and (when `e2e_policy.cursor_cloud_video=y`) a screen recording or framed trace bound to the **same** claim.
3. **Assert C** — the distinctive function completed on the claimed surface with those artifacts on disk. A run id, HTTP 201, or check count without frames is not C.

`loop_on_ci=heavy` repeats the team-kit watch after every cloud push until green or a permitted blocker. `e2e_policy.auto_review_and_merge` stays **n** — never silent prod merge.

## Instruct (cursor-sdk)

Canonical: `@cursor/sdk` skill + `references/runtime-choice.md`. REST: `https://cursor.com/docs/cloud-agent/api`. Grok does not vendor the cursor-sdk skill tree.

Must pass `cloud:{repos:[{url,startingRef}]}` or the SDK **silently runs local**. That silent local is **NOT VERIFIED** when Cursor cloud was requested. Omit both `local` and `cloud` → silent local.

- Cloud clones GitHub at `startingRef` (no uncommitted local files).
- Agent IDs prefix `bc-` (background composer). Those are **not** run IDs.
- Prompt = concrete task + success criteria.
- Always `wait()` + dispose (`finally` / `Symbol.asyncDispose` / `await using`). `Agent.prompt` disposes for you.
- Sub-agents cloud-only when the parent is cloud.
- `skipReviewerRequest: true` in CI. `autoCreatePR: true` only for real PRs.
- Guard `run.supports("stream"|"wait"|"cancel"|"conversation")` before calling.
- Log `agent.agentId` immediately after create/resume and `run.id` immediately after `send()`.

### Capability matrix (runtime-choice)

| Capability | Local | Cloud |
| --- | --- | --- |
| Opens real PRs | No | Yes (`cloud.autoCreatePR: true`) |
| Uncommitted local changes | Yes | No — clones from `startingRef` |
| Outlives caller process | No | Yes — resumable by `agentId` |
| Artifact download | Not implemented | Yes |
| Requires GitHub repo | No | Yes (`cloud.repos[].url`) |
| Requires API key | For remote model calls | Always |

### 1. `Agent.prompt` — one-shot (disposes for you)

```typescript
import { Agent } from "@cursor/sdk";

const result = await Agent.prompt(task, {
  apiKey: process.env.CURSOR_API_KEY,
  model: { id: "composer-2" },
  cloud: {
    repos: [{ url, startingRef }],
    autoCreatePR: true,
    skipReviewerRequest: true,
  },
});
```

### 2. `Agent.create` + `send` — multi-turn; always wait + dispose

```typescript
import { Agent, CursorAgentError } from "@cursor/sdk";

const agent = Agent.create({
  apiKey: process.env.CURSOR_API_KEY,
  model: { id: "composer-2" },
  cloud: {
    repos: [{ url: "https://github.com/org/repo", startingRef: "main" }],
    autoCreatePR: true,
    skipReviewerRequest: true,
  },
});

try {
  const run = await agent.send(task);
  if (run.supports("stream")) {
    for await (const event of run.stream()) {
      /* optional observe */
    }
  }
  const result = await run.wait();
  if (result.status === "error") {
    // Agent started but failed mid-run. Inspect transcript / git state. Exit 2.
  }
  const run2 = await agent.send("follow-up keeps conversation context");
  await run2.wait();
} catch (err) {
  if (err instanceof CursorAgentError) {
    // Did not start (auth/config/network). Named missing CURSOR_API_KEY. Exit 1.
  }
  throw err;
} finally {
  await agent[Symbol.asyncDispose]();
}
```

`await using agent = Agent.create({ /* ... */ })` is equivalent if the tsconfig supports it.

### 3. `Agent.resume` — pick up `bc-…` later

```typescript
const agent = Agent.resume(previousAgentId, {
  apiKey: process.env.CURSOR_API_KEY,
  model: { id: "composer-2" },
  cloud: {
    repos: [{ url, startingRef }],
  },
});
const run = await agent.send("continue");
await run.wait();
```

Inline `mcpServers` are **not** persisted across resume — pass them again. Cloud agents resume anywhere; local agents are scoped to `cwd`.

### Inspect later (`bc-` vs run id)

```typescript
const info = await Agent.get("bc-abc123", { apiKey: process.env.CURSOR_API_KEY });
const run = await Agent.getRun(runId, {
  runtime: "cloud",
  agentId: "bc-abc123",
  apiKey: process.env.CURSOR_API_KEY,
});
if (run.supports("cancel")) await run.cancel();
```

A `bc-` agent id is **not** a run id. Do not confuse the two.

### Two failure axes

- `CursorAgentError` thrown → the run never executed (auth, config, network). Exit 1. Do not retry `AuthenticationError`.
- `result.status === "error"` → the agent did work and that work failed. Exit 2. Do not treat as startup failure.
- `result.status === "cancelled"` after a successful cancel is non-fatal.

Respect `error.isRetryable`. Blind retries can duplicate cloud runs.

Set `workOnCurrentBranch: true` only when pushing to an existing branch — rare; usually you wanted local instead.

## After the cloud agent opens a PR (team-kit)

These do **not** launch cloud agents; they watch/ship. Use `gh pr checks` as the source of truth (includes all PR-attached checks). `gh run list` only covers GitHub Actions.

### `loop-on-ci`

Trigger: watch a branch or PR and iterate on CI failures until required checks are green.

Workflow:

1. Resolve the PR for the current branch.
2. Inspect current PR checks before waiting.
3. If checks already failed, diagnose those failures first.
4. If checks are pending, watch with `gh pr checks --watch --fail-fast`.
5. After each push, re-check the full PR check set and repeat until green.

```bash
# Resolve the active PR
gh pr view --json number,url,headRefName

# Inspect all attached checks
gh pr checks --json name,bucket,state,workflow,link

# Watch pending checks and fail fast
gh pr checks --watch --fail-fast

# GitHub Actions logs, when the failing check links to a GHA run
gh run view <run-id> --log-failed
```

Guardrails:

- Keep each fix scoped to a single failure cause when possible.
- Do not bypass hooks (`--no-verify`) to force progress.
- If the failure is clearly unrelated to the PR and appears fixed on main, merge latest main instead of bloating the PR with unrelated fixes.
- If failures are flaky, retry once and report flake evidence.
- Re-run `gh pr checks --json name,bucket,state,workflow,link` after every push; the check set can change.

Output: current CI status; failure summary and fixes applied; PR URL once checks are green.

### `ci-watcher` (agent, `is_background: true`, model `fast`)

```bash
git branch --show-current
gh pr view --json number,url,headRefName
gh pr checks --json name,bucket,state,workflow,link
gh pr checks --watch --fail-fast
gh run view <run-id> --log-failed
```

Output: CI status (passed/failed); PR and check metadata; if failed, concise failure excerpt or external check link and likely next step.

### `review-and-ship`

Trigger: review the current branch for bugs, intent fit, and test coverage; run or write tests; commit focused work; open or update a PR.

Workflow:

1. Gather context: diff against base branch, uncommitted changes, recent commits, changed files, and user intent from recent relevant chats if useful.
2. Run targeted tests for changed behavior. If no focused tests exist, decide whether to add them or document the gap.
3. Review for correctness, regressions, security, and intent fit. Use parallel subagents for larger diffs.
4. Fix critical issues before finalizing and re-run affected tests.
5. Commit selective files with a concise message.
6. Push branch and open or update a PR.

```bash
git fetch origin main
git diff origin/main...HEAD
git status
gh pr checks --json name,bucket,state,workflow,link
```

Guardrails:

- Prioritize correctness, security, and regressions over style-only comments.
- Keep commits focused and avoid unrelated file changes.
- If pre-commit checks fail, fix the issues rather than bypassing hooks.
- Use `gh pr checks` instead of GitHub Actions-only commands when judging PR readiness.
- Never silent prod merge (`e2e_policy.auto_review_and_merge` stays n).

Output: findings summary (critical, warning, note); tests run and outcomes; PR URL.

## `control-cli` (local CLI/TUI harness)

Use when the claim needs a terminal: CLI UX, startup regressions, memory leaks, hangs, prompt flows, or terminal demos. Prefer the repo's own test/demo harness; otherwise assemble a temporary harness from standard local tools. One action at a time. Wait for a concrete screen pattern or prompt before the next action. Do not send credentials.

Harness loop:

1. Identify the command under test and the smallest reproducible workspace.
2. Discover existing local harnesses: package scripts, e2e tests, demo recorders, expect scripts, or PTY helpers.
3. If no harness exists, launch the CLI in an isolated terminal session with deterministic env vars.
4. Capture the current screen before interacting.
5. Send one action at a time: text, Enter, arrows, Escape, Ctrl-C, resize.
6. Wait for a concrete screen pattern or prompt before the next action.
7. Save the transcript and any profile artifacts.
8. Kill the session cleanly.

Harness options: repo-native scripts; `tmux` (`new-session` / `capture-pane` / `send-keys` / `kill-session`); PTY probe (Python/Node/Expect) when tmux is unavailable; runtime inspector; terminal recorder (repo-local or asciinema-compatible when a demo is requested).

### Minimal tmux harness

```bash
SESSION="cli-harness-$(date +%s)"
tmux new-session -d -s "$SESSION" -- <command-under-test>
tmux capture-pane -pt "$SESSION"
tmux send-keys -t "$SESSION" "help" Enter
tmux capture-pane -pt "$SESSION"
tmux kill-session -t "$SESSION"
```

For Node CLIs:

```bash
NODE_OPTIONS="--inspect=127.0.0.1:0" tmux new-session -d -s "$SESSION" -- <node-cli-command>
```

Read the terminal output to find the inspector URL, then use Chrome DevTools-compatible tooling if profiling is needed.

### Minimal PTY harness

Keep it temporary unless the user asks to add a reusable test.

```python
import os
import pty
import select
import subprocess
import time

master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(
    ["<command>", "<arg>"],
    stdin=slave_fd,
    stdout=slave_fd,
    stderr=slave_fd,
    close_fds=True,
)
os.close(slave_fd)

deadline = time.time() + 30
buffer = b""
while time.time() < deadline:
    ready, _, _ = select.select([master_fd], [], [], 0.25)
    if not ready:
        continue
    chunk = os.read(master_fd, 4096)
    buffer += chunk
    if b"<ready text>" in buffer:
        os.write(master_fd, b"help\n")
        break

print(buffer.decode(errors="replace"))
proc.terminate()
os.close(master_fd)
```

If the CLI needs richer terminal control, use `pty.fork()` or an existing PTY library.

### Profiling recipes

- Startup regression: capture baseline and treatment startup timings under the same machine, env, and command.
- Slow operation: start a CPU profile, perform the operation, stop the profile, and compare top self-time functions.
- Memory leak: force GC if available, take a heap snapshot, perform the operation repeatedly, force GC again, and take another snapshot.
- Hang: capture the screen, active handles/resources, and a stack/CPU sample before interrupting.

Guardrails:

- Prefer deterministic waits over sleeps. If you must sleep, explain why.
- Do not send credentials or destructive commands into a controlled session.
- Keep the harness in `/tmp` unless the repo already has a testing/demo harness.
- Do not hard-code paths from another repository. Adapt commands to the current repo's scripts and runtime.
- Clean up tmux sessions, temp dirs, inspector processes, and demo artifacts unless the user asks to keep them.

## Video + logs (policy)

When `e2e_policy.cursor_cloud_video=y` (default when Cursor cloud is used): require **screen recording AND logs** bound to the same claimed surface. Missing either = **NOT VERIFIED**. HTTP 201 / check counts / empty `.last-run.json` are not C. Observer notes must state what the recording shows.

## Return

PASS/FAIL, cloud agent id (`bc-…`), run id, recording path, logs SHA-256, `abc`, PR URL, CI status, and `NOT VERIFIED` gaps. Credential-safe. No user ping.
