---
name: api-jig-runner
description: Spawn protocol for api-jig-runner. Prism/MSW jig must PASS before live API.
---

# Spawn: api-jig-runner

Spawn lives in `grok-fusion-drive` + `fusion-e2e-unattended`.

- **Preferred type:** `grok-fusion-drive:api-jig-runner`
- **Host API:** parent-only `spawn_subagent` (depth 1). Isolation `none`. cwd = owning `e2e/` or project root for jig/studio.
- **Model:** `grok-4.5` effort `high`.
- **Skill:** `api-test-jig`. Kun Chen mock: `acp-mock` (ACP stdio). HTTP: Prism/MSW. Must PASS before live.
- Do not ping. Never read secrets. Env **NAMES** only.
