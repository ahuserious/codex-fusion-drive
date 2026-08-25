---
name: api-test-jig
description: Simulate the live API before live calls. Kun Chen acp-mock for ACP/agent protocol; Prism/MSW for HTTP OpenAPI. Must PASS before any live API.
---

# API test jig

Inventory of Kun Chen / `kunchenguid` GitHub (71 public repos, 2026-08-25):
the **only** mock repo is [`kunchenguid/acp-mock`](https://github.com/kunchenguid/acp-mock)
(npm `acp-mock`). It is a **deterministic ACP stdio agent**, not an HTTP OpenAPI
server. AXI catalog still has no `http-mock-axi`. Do not invent one.

## Pick the jig

| Surface | Tool | Command |
| --- | --- | --- |
| App talks **ACP / agent protocol** | Kun Chen `acp-mock` | `npx --yes acp-mock --agent-message-json '{"success":true}'` (stdio). Logs: `--event-log <path>`. |
| App talks **HTTP / OpenAPI / REST** | Prism (or MSW) | `npx --yes @stoplight/prism-cli mock <spec> --port 4010` |

A→B→C: start jig (A), traffic hits the jig (B), assert recorded/replayed
contract (C) or FAIL. HTTP 201 against production is not C.

1. Prefer an OpenAPI (HTTP) or ACP client config in the owning repo. Else
   harvest fixtures under `e2e/jig/` (author if missing at
   `stably_depth=jig_video_logs` or `full_sim_ui`).
2. Point the app or Stably at the jig (ACP command, or HTTP base URL). Keep
   jig **and** video/logs at `jig_video_logs`.
3. Only after jig PASS may `backend-prober` hit the real seam / live agent.

Never `cat` `.env` / `secrets.env`. Env **NAMES** only.

Spawn: `api-jig-runner` (isolation `none`, owning tree).

| `stably_depth` | Jig |
| --- | --- |
| `ci_shots_logs` | optional |
| `jig_video_logs` | required + recording |
| `full_sim_ui` | required + recording + sim-users |
