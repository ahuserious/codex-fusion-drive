# Fusion Drive architecture

```mermaid
flowchart TD
  U["User task"] --> H["Codex planning host"]
  H --> S{"Selected Fusion engine"}
  S -->|"in_harness"| P["Grok 4.5 + GPT 5.6 sol + Fable 5"]
  P --> J["GPT 5.6 sol judge"]
  J --> F["GPT 5.6 sol fuser"]
  S -->|"subscription_oauth"| OP["2 Grok 4.5 personas + Claude Fable 5"]
  OP --> OJ["Grok 4.5 judge"]
  OJ --> OF["Claude Fable 5 fuser"]
  OF --> F
  S -->|"xai_claude_oauth"| HP["2 direct xAI Grok 4.5 personas + Claude Fable 5 OAuth"]
  HP --> HJ["Direct xAI Grok 4.5 judge"]
  HJ --> HF["Claude Fable 5 OAuth fuser"]
  HF --> F
  S -->|"openrouter_fusion"| OR["openrouter/fusion separate config"]
  OR --> F
  H -. "grok-fusion-drive preset" .-> D["GPT 5.6 sol ultra host driver"]
  D --> AG["2 Grok panels + Grok judge + Grok fuser"]
  AG --> F
  F --> SG["Synthesis gate"]
  SG --> PG["Plan gate"]
  PG --> C{"Explicit plan confirmation"}
  C -->|"revise"| H
  C -->|"confirm and execute"| G["Codex host codex_app.create_thread"]
  G --> PRE["Pre-execution gate"]
  PRE --> X["Host-owned execution"]
  X --> SUB["Subagent post gates"]
  SUB --> POST["Post-execution gate"]
  POST --> FINAL["Final gate"]
  FINAL --> SUM["Summary gate"]
  SUM --> AE["Deterministic HTML/SVG auto-eval"]
```

## Trust boundary

The MCP server owns configuration, external model calls, persisted artifacts,
gate receipts, lifecycle validation, and reports. The Codex host owns user
interaction, project selection and thread creation, native subagent/model selection, filesystem tools,
and execution.

This split is deliberate. An MCP process cannot truthfully prove a user clicked
or typed a confirmation and cannot invoke host-only goal state. It records
content-addressed host receipts and rejects illegal transitions.

## Configuration boundary

Schema-v2 keeps five independent engine objects:

- `engines.in_harness`
- `engines.subscription_oauth`
- `engines.xai_claude_oauth`
- `engines.openrouter_fusion`
- `engines.all_grok_4_5`

`profiles.subscription-oauth` selects only OAuth CLI seats and its serialized
two-reviewer OAuth gate set. The global `maximum-intelligence` default remains
the canonical API-backed profile, with no silent fallback between profiles.

`profiles.xai-claude-oauth` selects direct xAI Grok panel/judge/gate seats and
Claude OAuth panel/fuser seats. Its gate reviewers are serialized, its xAI cost
is metered, its Claude subscription use remains unknown, and it does not route
through OpenRouter.

Seat-level OpenRouter server settings use `openrouter_fusion`, avoiding the
upstream ambiguity where profile engine selection and seat plugin parameters
both used the key `fusion`.

## Persistence

- Inherited engine runs: `~/.codex/codex-fusion-drive/engine/runs`
- Durable asynchronous jobs: `~/.codex/codex-fusion-drive/jobs`
- Host lifecycle: `~/.codex/codex-fusion-drive/workflows`
- Configuration proposals: `~/.codex/codex-fusion-drive/proposals`
- Provider batch bundles: `~/.codex/codex-fusion-drive/batches`
- Rescue packets: `~/.codex/codex-fusion-drive/rescue`
- Human-sim campaigns: `~/.codex/codex-fusion-drive/human-sim-users`
- Auto-eval reports: `~/.codex/codex-fusion-drive/reports`

Lifecycle and campaign mutations are atomic and compare-and-swap protected.
Lifecycle events additionally form a SHA-256 chain.

Job manifests bind an idempotency key to exact request and configuration hashes.
Workers persist their state and a hash of any completed result. Orphaned workers
are marked failed without redispatch. Abort requests write the inherited
recoverable `KILL` switch and never force-kill a potentially billable provider
call.
