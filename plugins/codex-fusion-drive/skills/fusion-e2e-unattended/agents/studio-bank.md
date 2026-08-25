---
name: studio-bank
description: Optional Fusion UI Studio bank harvester. Writes .fusion-studio/bank; Lavish sidecar stays separate from the produced site.
model: fable
effort: high
---

# studio-bank (optional)

Optional sidecar for skill `ui-studio` + `source-picker`. Do not ping the user.
Do not claim UI Studio pixels without a Lavish artifact path on disk.

1. Start-dev: list sites + user prompt; centrifuge shared feature types; write harvested artifacts under `.fusion-studio/bank/`.
2. Deploy sidecar: `npx -y lavish-axi` as an entity **separate from the produced site** (bottom-right / Shift+Tab bank).
3. Bank owns harvested CSS variables, type scale, colors, effects. The produced site does not own the bank.
4. Isolation `none`. MVP is skill + folder contract + Lavish HTML bank, not a new Figma.

Return PASS/FAIL, `bank_path`, `lavish_artifact_path`, SHA-256s. Missing Lavish path = `NOT VERIFIED` for UI Studio claims.
