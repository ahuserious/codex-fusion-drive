---
name: human-sim-users
description: Configure and run bounded simulated-user testing across UI/UX, accessibility, logs, performance, security, data integrity, and external-write scenarios.
---

# Human Sim Users

## Discover preferences

Call `human_sim_questions` and ask the user for all unanswered dimensions:

- Platforms, browsers, devices, and runtime versions.
- UI/UX goals and reference states.
- Desktop, tablet, mobile, zoom, and orientation viewports.
- Personas, expertise, locales, and assistive technology.
- Accessibility standard and keyboard/focus/screen-reader expectations.
- Forbidden console, network, server, and telemetry warnings.
- Latency, responsiveness, memory, CPU, bundle, and throughput budgets.
- Privacy, authentication, authorization, injection, and egress tests.
- Empty, malformed, boundary, concurrent, offline, and partial-failure data.
- Permission to create accounts, send messages, change remote data, or incur
  charges.

Do not silently invent pass/fail budgets the user may disagree with.

## Create and run

Call `human_sim_create` with preferences, acceptance criteria, and explicit
scenarios.

An extra continuous Codex goal is optional:

1. Ask whether the user wants it.
2. Pass both `request_extra_goal` and `confirmed_extra_goal` only after explicit
   confirmation.
3. Confirm the campaign's repository scope, then call the configured host lifecycle tool
   (`codex_app.create_thread` since 0.1.1) for the selected project.
4. Record the returned thread id with `human_sim_goal_record`.

The loop is driven by the campaign manifest, not a forever-running shell.

For each iteration, test observable behavior and call `human_sim_record` with
evidence, errors, performance state, criteria state, and stalled subagents.

## Completion

Stop only when:

- Open errors are zero.
- Every acceptance criterion has evidence.
- No subagent is stalled.
- Performance budgets pass.
- Every scenario passes.

Repeated identical failures trigger a preserved-evidence human handoff instead
of infinite retries.
