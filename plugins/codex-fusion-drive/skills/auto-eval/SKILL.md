---
name: auto-eval
description: Evaluate a complete Fusion Drive workflow and generate a deterministic standalone HTML tearsheet with embedded SVG workflow and metric visuals.
---

# Auto Eval

Use after a planning run, execution, rescue cycle, human-sim campaign, or plugin
test suite.

## Evidence packet

Supply or collect:

- Run id and selected engine.
- Per-seat provider, model, role, requested/effective reasoning, token usage,
  latency, billed API cost, and subscription usage units when available.
- Every gate verdict, score, reviewer, and evidence reference.
- Tool calls and exact error categories.
- Verified, unknown, and disproven claims.
- Failure records.
- Configuration changes.
- Optional pinned ablations.

Never render missing subscription cost as zero. Never infer hallucinations from
style alone. A claim is unsupported only when supplied verification evidence
marks it false.

## Generate

- Call `auto_eval_run` for a persisted Fusion run.
- Call `auto_eval` for a composed evidence packet.
- If reproducibility matters, provide a fixed `report_timestamp`; otherwise the
  report displays `not supplied` rather than injecting current time.

The report must contain:

- Proposed workflow graph.
- API spend and subscription usage.
- Efficiency analysis.
- Workflow setting changes.
- Failure list.
- Gate grades.
- Tool-call errors.
- Unsupported claims/hallucinations.
- Model honesty.
- Over/under-reasoning indicators.
- Per-model intelligence contribution.

Intelligence contribution is `unknown` unless a pinned same-task,
same-configuration-except-model ablation supplies baseline and without-model
scores. Model self-report is never contribution evidence.

## Reproducibility contract

The renderer uses canonical sorted JSON, fixed ordering/rounding, inline CSS and
SVG, and no external assets, JavaScript packages, fonts, network requests, or
QuantStats. Equal evidence and configuration must produce equal HTML bytes and
hash.

