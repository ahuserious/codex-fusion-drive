---
name: codex-fusion-drive-config
description: Configure Codex Fusion Drive conversationally through validated proposals, complete updated workflow reports, and exact-hash final approval.
---

# Codex Fusion Drive Configuration

Use this skill whenever the user asks what is configured, asks to change a
model/provider/gate/preset/budget, or reaches a verbal configuration conclusion.

## Settings interview TUI

`python3 scripts/settings_tui.py` or `python3 fusion_ctl.py settings`
(show|doctor|interview|apply --hash \<sha256\> --confirmed). Stdlib curses
list TUI: ↑↓ select, ←→ or y/n cycle, `s` propose, `q` quit without write.
Non-TTY / `FUSION_E2E_UNATTENDED=1` dumps JSON + hash. Persist overlay
`e2e_policy` only — never write `default.json`, never flip
`profiles.*.plan_stop_required`. Unattended e2e overrides interview. Missing
`codex_oauth` for Sol-via-subscription is a named blocker, never a silent
`OPENAI_API_KEY` fallback.

## Read

1. Call `config_show` for the complete merged configuration.
2. Call `workflow_report` for the current Mermaid graph, gates, presets, batch
   policy, lifecycle, and requested/effective reasoning.
3. Use `config_get` for focused follow-up questions.

Always distinguish:

- In-harness Fusion, where this plugin owns panel, judge, and fuser seats.
- Subscription OAuth Fusion, where local Claude/Grok CLI OAuth seats are
  selected explicitly and unknown subscription cost is reported honestly.
- OpenRouter Fusion, where `openrouter/fusion` owns server-side fan-out and has a
  separate analysis-model/judge/reasoning block.
- Requested reasoning intent from the effective provider wire value.
- API billing from subscription usage.

## Propose

1. Translate the verbal conclusion into the smallest merge-style `changes`
   object.
2. Call `config_propose`.
3. Return the exact proposal hash, rationale, changes, validation result, updated
   Mermaid graph, complete redacted candidate configuration, reasoning
   normalization, gate list, batch implications, and cost/egress implications.
4. Ask for final approval of that exact proposal hash.

Never include a token, API-key value, password, cookie, email identity, X handle,
or keychain path in configuration. Only environment-variable names and CLI OAuth
modes are allowed.

## Approve

Call `config_approve` only after the user explicitly approves the exact proposal
hash. Pass `confirmed: true`.

If the base configuration changed after the proposal, do not force it through.
Create a fresh proposal and return its complete report.

`config_set` is a compatibility alias that creates a proposal. It never bypasses
final approval.

## Invariants

- `maximum-intelligence` remains the canonical three-model API-backed default.
- `subscription-oauth`, `xai-claude-oauth`, and `all-grok-4.5` are separately
  named opt-in profiles; none is a silent fallback.
- GPT 5.6 sol remains canonical judge and fuser.
- Planning seats request `xhigh`.
- Aggregate `max_reasoning_tokens` and `max_wall_seconds` remain `null`.
- Approval gates use Grok 4.5 with requested `xhigh`, effective `high`.
- A configuration proposal cannot create a Codex goal or authorize execution.
