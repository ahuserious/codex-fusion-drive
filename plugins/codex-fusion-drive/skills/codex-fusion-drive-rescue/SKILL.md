---
name: codex-fusion-drive-rescue
description: Rescue stalled Fusion Drive work with immutable problem packets, fresh-context diagnosis, cross-perspective critique, bounded retries, and resumable evidence checkpoints.
---

# Codex Fusion Drive Rescue

This workflow preserves the durable parts of the earlier batch-create-eval,
gigaprompt, and exaflop patterns without relying on Claude terminal injection or
unbounded loops.

## Start

Call `rescue_create` with:

- The exact problem.
- Acceptance criteria.
- Small independently verifiable work units.
- Constraints and prohibited side effects.
- The evidence bar for each unit.

The returned problem packet is immutable and content-addressed.

## Attempt

For each ready unit:

1. Resume from its last proven checkpoint.
2. Use fresh context to diagnose the failure independently.
3. Compare that diagnosis against preserved attempts.
4. Seek one cross-perspective critique before changing strategy.
5. Make one bounded attempt.
6. Record outcome, evidence, diagnosis, checkpoint, and a stable failure
   fingerprint with `rescue_record`.

Do not erase failed attempts. Do not retry ambiguous external writes
automatically.

## Stop and hand off

- Complete only when every unit passes its evidence bar.
- Hand off after the configured identical-fingerprint threshold.
- Hand off when a unit or total-cycle bound is exhausted.
- Return the immutable packet, last proven checkpoint, all preserved failure
  evidence, and the smallest decision needed from the user.

Use `rescue_resume` after interruption. Never restart from an unproven
green-looking snapshot.

