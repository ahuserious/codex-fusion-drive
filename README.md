# Codex Fusion Drive

Codex Fusion Drive is a Codex-first planning and verification plugin derived from Relentless Inception. It keeps the inherited multi-model provider/orchestration core, then adds a schema-v2 control plane for explicit reasoning normalization, separately configurable Fusion engines, confirmation-backed execution lifecycle, per-subagent presets, OAuth-safe CLI adapters, provider batching, rescue, simulated users, and deterministic auto-evaluation.

## Default maximum-intelligence workflow

| Role | Model | Requested effort | Effective transport effort |
|---|---|---:|---:|
| Panel | xAI Grok 4.5 | `xhigh` | `high` (`provider_ceiling`) |
| Panel | GPT 5.6 sol | `xhigh` | `xhigh` via OpenRouter |
| Panel | Claude Fable 5 | `xhigh` | `xhigh` via OpenRouter |
| Judge | GPT 5.6 sol | `xhigh` | `xhigh` via OpenRouter |
| Fuser | GPT 5.6 sol | `xhigh` | `xhigh` via OpenRouter |
| Approval reviewers | xAI Grok 4.5 | `xhigh` | `high` (`provider_ceiling`) |
| Grok Fusion Drive host | GPT 5.6 sol | `ultra` | host-owned `ultra` |

The direct xAI Grok 4.5 interface does not expose a literal `xhigh` value. The plugin preserves `xhigh` as the requested intelligence intent, sends `high`, and reports the normalization instead of overstating the wire behavior.

Aggregate `max_reasoning_tokens` and `max_wall_seconds` are `null`. Per-request timeouts, retries, call/token/cost ceilings, abort switches, rescue bounds, and approval gates remain active.

## Fusion engines

- `in_harness`: plugin-owned independent panel, anonymous GPT judge, and GPT generative fuser.
- `subscription_oauth`: explicitly selected subscription-only panel with two independent Grok 4.5 personas plus Claude Fable 5, a Grok 4.5 judge, and a Claude Fable 5 fuser.
- `xai_claude_oauth`: explicitly selected hybrid panel with two direct xAI Grok 4.5 personas plus Claude Fable 5 OAuth, a direct xAI Grok judge, a Claude OAuth fuser, and serialized direct xAI Grok plan reviewers. It has no OpenRouter route or fallback.
- `openrouter_fusion`: independent `openrouter/fusion` settings for analysis models, server judge, reasoning, and fallback. It does not inherit the in-harness panel block.
- `all_grok_4_5`: two Grok panel seats, one Grok judge, and one Grok fuser, with the selected approval gate set inherited.

`maximum-intelligence` remains the global API-backed default.
`subscription-oauth` and `xai-claude-oauth` are separate opt-in profiles and
are never used as silent fallbacks. The hybrid profile requires the
`XAI_API_KEY` environment reference while Claude remains on subscription OAuth.

## Gates

The configured stages are `synthesis`, `plan`, `pre_execution`, `subagent_pre_execution`, `subagent_post_execution`, `post_execution`, `final`, and `summarize`.

Planning stops after the plan gate. The host returns the plan, Mermaid graph, and full redacted effective configuration, then waits for explicit confirmation. When the user subsequently asks to execute, the Codex host lists projects, creates a Codex thread with `codex_app.create_thread`, and records its receipt before the pre-execution gate can pass. Legacy lifecycle files bound to `create_goal` remain readable.

## OAuth and batching

The plugin supports isolated tool-free Claude Code and Grok CLI subprocess adapters. Grok receives its protected `0600` prompt through `--prompt-file`; Claude receives the same isolated prompt through stdin. Every OAuth child removes both `ANTHROPIC_API_KEY` and `XAI_API_KEY` so API credentials cannot silently override the requested subscription OAuth path. Tokens, account emails, X handles, cookies, and keychain paths are never read or stored.

Claude result envelopes are normalized without exposing rejected content:
ordinary `result` text, canonical schema-backed `structured_output`, final
result envelopes in JSON sequences, and valid bare JSON are supported. Failed
or unusable calls retain an unknown-cost response receipt whose diagnostics
contain only output type, length, hash, exit status, and error category.

Batch behavior is transport-specific:

- OpenAI and Anthropic API configurations can prepare and explicitly submit their documented provider Batch API jobs.
- xAI Grok 4.5 cannot use the xAI Batch API.
- OpenRouter uses bounded microbatching and provider caching; no undocumented async discount is claimed.
- Claude/Grok subscription OAuth uses serialized isolated subprocesses and is not described as an API batch discount.

Subscription calls use `unknown_cost_policy: report_unknown`. Unknown
subscription cost remains explicitly unknown, increments the unknown-call
ledger, and does not become a fabricated `$0.00` charge or block the next call.

## Durable asynchronous fusion

`fuse_start` and `approval_gate_start` return durable job receipts immediately
after explicit external-usage confirmation. Callers provide a stable
idempotency key, poll with `job_status`, and retrieve a hash-verified result
with `job_result`. `job_abort` writes the recoverable inherited kill switch; it
does not force-kill an in-flight provider call.

Each private job directory persists the immutable request/configuration hashes,
worker state, sanitized failure receipt, and result hash. Replaying the same key
returns the original job. A conflicting request, configuration drift, or an
orphaned worker fails closed and is never automatically redispatched.

## Skills

- `/codex-fusion-drive`
- `/codex-fusion-drive-config`
- `/codex-fusion-drive-review`
- `/codex-fusion-drive-rescue`
- `/human-sim-users`
- `/auto-eval`

## Layout

- `plugins/codex-fusion-drive/config/fusion-drive.default.json`: schema-v2 defaults.
- `plugins/codex-fusion-drive/schemas/fusion-drive.schema.json`: documented schema.
- `plugins/codex-fusion-drive/codex_fusion_drive/`: control-plane runtime.
- `plugins/codex-fusion-drive/relentless_inception/`: vendored inherited engine.
- `tests/`: inherited regression coverage plus Fusion Drive requirement tests.
- `docs/REQUIREMENT_MATRIX.md`: objective-to-evidence map.

## Security boundary

Remote models never receive host credentials and never own workspace execution. API/provider calls can incur cost. Batch submission, provider tests, OAuth completion tests, external writes, repo merges, destructive actions, and Codex thread creation require explicit confirmation or host action.

## Attribution

This project is a derivative of `ahuserious/relentless-inception-codex` 0.1.4. The inherited license and notices are preserved. See `docs/UPSTREAM.md`.
