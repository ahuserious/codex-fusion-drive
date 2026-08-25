# Codex Fusion Drive plugin bundle

The Codex plugin entrypoint is `.codex-plugin/plugin.json`; MCP configuration is `.mcp.json`. The schema-v2 runtime is in `codex_fusion_drive`, while `relentless_inception` is the preserved provider/fusion engine inherited under its original license.

## Workflow surface

The upstream [fusion-harness](https://github.com/disler/fusion-harness) is a Pi
extension with three shipped commands (`/opinion`, `/fusion`, and
`/auto-validate`) and its own split-column widget/footer. Its `/debate`,
`/parallel`, and `/coordinate` examples were presented as **build-it patterns**,
not shipped upstream commands.

Codex Fusion Drive ships a Codex-native MCP control plane, not Pi commands or a
Claude Dynamic Workflow bundle. `seat_run`, `fuse_start`, approval gates,
durable jobs, and lifecycle tools are the composable primitives. Opinion,
debate, parallel, coordinate, auto-validation, and best-of-n remain workflow
patterns a host or graph compiler can build from those primitives; this release
does not claim a visual graph editor or a plugin-defined workflow TUI.

## Rendering and evidence boundary

MCP tools return a concise human summary in `content` and retain the bounded
machine receipt in `structuredContent`. When a full receipt exceeds the inline
budget, the response identifies its private artifact path, size, and sections;
the receipt is not silently truncated into invalid JSON.

Each host-authored external graph must reuse one `graph_run_id` so its nodes
share a durable, profile/config-bound budget ledger rather than resetting call,
token, cost, or approval thresholds. An external seat remains tool-free and
cannot use Codex host MCP, shell, or workspace writes. Its receipt records
configured and actual route evidence, reasoning, cost, and artifacts. Complete
model text remains available to downstream graph nodes; duplicate evidence
stays artifact-backed. Host-owned agents—not the external model—own repository
inspection, writes, integration, and verification. `seat_run` rejects a native
`codex_host` seat so this provenance boundary cannot be blurred.

Codex has no plugin-defined statusline API. This bundle therefore
installs no statusline or settings file. Use durable job receipts and
`job_status`, `job_wait`, and `job_result` for live progress and diagnostics.

## Manual-first and session lifetime

Installing or enabling the plugin does not dispatch an external seat, modify a
repository, or start a workflow. Resolve the selected profile and effective
configuration first, then explicitly confirm any operation that can consume
provider usage. Host-owned shell, web, and MCP actions remain governed by Codex
permissions; external seats never write.

Codex tasks snapshot plugin skills and MCP definitions. After an install or
upgrade, start a new Codex task (or restart the app if the server was retained)
before checking the new surface. A still-open task may use older tool
definitions while the files on disk show the new version. Durable Fusion Drive
jobs and artifacts remain available after restart.

Use the `codex-fusion-drive` skill for the full
plan-confirm-thread-execute lifecycle.
The direct-xAI plus Claude OAuth `xai-claude-oauth` profile is the shipped
default and has no OpenRouter route or fallback. The API-backed
`maximum-intelligence` profile remains explicit opt-in. The subscription-only
Grok/Claude workflow is selected explicitly as `subscription-oauth`.
Prefer durable `fuse_start` and `approval_gate_start` jobs, waiting with
`job_wait`; `job_status` and `job_result` remain available for manual
inspection.

`fuse_continue_start` is a narrow recovery path for a terminal failed fusion
whose panel is already complete. It requires caller-audited source job, source
tree, engine manifest, ledger, failed-judge response, and per-panel response
hashes. It creates a distinct child run, imports source-bound panels without
relabeling their receipts, carries corrected source usage into residual budget
enforcement, and may dispatch only the judge retry, synthesizer, and gates. A
source written by the 0.2.1 Grok adapter retains an explicit legacy tool
isolation taint. Any missing or drifting evidence fails closed; it never falls
back to redispatching panels. Each source owns one deterministic child claim,
regardless of caller retry keys. Rejected children receive no host lifecycle.
If paid reasoning passes but lifecycle persistence fails, use
`fuse_continue_reconcile` with the immutable base result hash reported by the
job. After reconciliation, `job_result` labels that base hash separately from
the effective result hash; idempotent retries may supply either advertised
hash. The append-only reconciliation receipt leaves the job manifest and base
result unchanged, revalidates source, config, child, and lifecycle evidence,
and never invokes providers.

Use the `codex-fusion-drive-config` skill for exact-hash configuration proposals and
approval. New lifecycles bind to `codex_app.create_thread`; legacy
`create_goal` receipts remain compatible.
