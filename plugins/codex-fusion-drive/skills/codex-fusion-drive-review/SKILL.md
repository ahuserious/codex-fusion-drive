---
name: codex-fusion-drive-review
description: Run fail-closed Grok approval gates for plans, execution, subagents, final evidence, and summaries.
---

# Codex Fusion Drive Review

Treat this as a release gate, not a request for a favorable opinion.

## Gate inventory

- `synthesis`: automatic engine gate over panel, judge, and fused artifact.
- `plan`: requirements trace, risk analysis, and workflow report.
- `pre_execution`: confirmed plan, Codex goal receipt, and scope boundaries.
- `subagent_pre_execution`: subagent scope and immutable preset hash.
- `subagent_post_execution`: subagent result, tool errors, and verification.
- `post_execution`: diff, tests, and requirement coverage.
- `final`: verdict, cost ledger, and provenance.
- `summarize`: decisions, open risks, and verification state.

All approval reviewers are Grok 4.5 with requested `xhigh` and effective direct
xAI effort `high`.

## Procedure

1. Hash the exact artifact under review.
2. Gather mechanical evidence. A model's statement that tests passed is not test
   evidence.
3. Call `approval_gate` with the exact artifact, stage, and lifecycle hash.
4. Require the configured number of independent passes.
5. Record `PASS`, `NEEDS_WORK`, or `FAIL`; do not soften a missing receipt into a
   pass.
6. On `NEEDS_WORK`, use one bounded correction cycle and review the new hash.
7. On repeated identical failure, open a rescue packet rather than looping.

Check unsupported claims, hidden scope expansion, unrelated changes, destructive
actions, credential exposure, tool errors, omitted tests, cost accounting, and
honest uncertainty.

