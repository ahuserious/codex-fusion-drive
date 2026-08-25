# Requirement matrix

| ID | Requirement | Implementation | Primary verification |
|---|---|---|---|
| CFG-01 | Canonical in-harness panel | `fusion-drive.default.json`, `validate_config` | `test_config_contract.py` |
| CFG-02 | GPT judge/fuser at xhigh | Seats and canonical engine invariants | `test_config_contract.py` |
| CFG-03 | Separate OpenRouter Fusion config | `engines.openrouter_fusion`, seat `openrouter_fusion` block | `test_config_contract.py` |
| CFG-04 | Unbounded reasoning/wall aggregate caps | Profile budgets use `null`; inherited schema accepts null | `test_config_contract.py` |
| CFG-05 | Requested/effective reasoning truth | `reasoning.py`, workflow report | `test_reasoning_presets.py` |
| CFG-07 | Separate OAuth subscription profile and topology | `engines.subscription_oauth`, `profiles.subscription-oauth`, OAuth gate set | `test_config_contract.py`, `test_engine_translation.py` |
| CFG-08 | Explicit xAI plus Claude OAuth hybrid without OpenRouter | `engines.xai_claude_oauth`, `profiles.xai-claude-oauth`, serialized direct-xAI gates | `test_config_contract.py`, `test_engine_translation.py`, `test_reporting.py` |
| PLAN-01 | Planning ends before execution | Main skill and lifecycle state machine | `test_lifecycle.py` |
| PLAN-02 | Mermaid and full settings returned | `report.py`, `fuse` result | `test_reporting.py` |
| PLAN-03 | Exact user confirmation | `confirm_plan` hash/CAS checks | `test_lifecycle.py` |
| GOAL-01 | Codex thread after execute request | Host `list_projects` / `create_thread` sequence and `goal_record` | `test_lifecycle.py` |
| GOAL-02 | Legacy lifecycle receipt compatibility | Per-lifecycle host-tool binding with `create_goal` fallback | `test_lifecycle.py` |
| GATE-01 | Grok approval gates | `approval-gates` invariant and engine translation | `test_config_contract.py`, `test_engine_translation.py` |
| GATE-02 | Complete gate lifecycle | Eight stages and receipts | `test_lifecycle.py` |
| SUB-01 | Per-subagent presets | `presets.py` | `test_reasoning_presets.py` |
| SUB-02 | GPT ultra drives Grok fusion | `grok-fusion-drive` preset | `test_reasoning_presets.py` |
| SUB-03 | Two-panel all-Grok shape | `engines.all_grok_4_5` invariant | `test_config_contract.py` |
| INT-01 | repo-merge and GitNexus | `capabilities.py`, main skill | `test_capabilities.py` |
| AUTH-01 | OAuth isolation | `oauth.py` | `test_oauth_batching.py` |
| AUTH-02 | No identity/token persistence | Config validation and OAuth redaction | `test_oauth_batching.py` |
| AUTH-03 | Protected Grok prompt-file invocation | `oauth.py` `--prompt-file`, `0600`, isolated stdin/tools/web/memory | `test_oauth_batching.py` |
| AUTH-04 | Claude envelope normalization and failure receipts | `oauth.py` result/structured/sequence parser and sanitized semantic-failure callbacks | `test_oauth_batching.py` |
| BAT-01 | Per-transport batch truth | `batching.py` | `test_oauth_batching.py` |
| BAT-02 | OpenAI/Anthropic bundles | `prepare_provider_batch` | `test_oauth_batching.py` |
| BAT-03 | Explicit billable submit approval | `submit_provider_batch` | `test_oauth_batching.py` |
| CFG-06 | Verbal proposal/final approval | `propose_config`, `approve_config`, config skill | `test_config_proposals.py` |
| RES-01 | Immutable rescue packet/checkpoints | `rescue.py` | `test_rescue_human_sim.py` |
| RES-02 | Bounded repeated-failure handoff | Rescue fingerprint threshold | `test_rescue_human_sim.py` |
| SIM-01 | Comprehensive user-test preferences | `human_sim_questions` and skill | `test_rescue_human_sim.py` |
| SIM-02 | Optional continuous goal loop | Campaign goal receipt and stop conditions | `test_rescue_human_sim.py` |
| EVAL-01 | Standalone HTML with embedded SVG | `auto_eval.py` | `test_auto_eval.py` |
| EVAL-02 | Deterministic bytes/hash | Canonical input and renderer | `test_auto_eval.py` |
| EVAL-03 | Complete requested sections | Evaluation schema and HTML sections | `test_auto_eval.py` |
| EVAL-04 | All selected Fusion engines understood | Profile-aware graph/settings and mixed-cost reporting | `test_auto_eval.py` |
| EVAL-05 | Contribution requires ablation | `_contribution_rows` | `test_auto_eval.py` |
| JOB-01 | Durable non-blocking fusion and gate jobs | `jobs.py`, MCP start/status/result/abort tools | `test_jobs.py` |
| JOB-02 | Exact idempotency and config binding | Request/config hashes and conflicting-key rejection | `test_jobs.py` |
| JOB-03 | Fail-closed worker/result recovery | Worker state, orphan handling, result hashes, recoverable abort | `test_jobs.py` |
| PKG-01 | Installable Codex plugin bundle | Manifest, MCP config, six skills | `test_plugin_surface.py` |
