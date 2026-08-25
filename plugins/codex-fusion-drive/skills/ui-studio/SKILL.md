---
name: ui-studio
description: WIP Fusion UI Studio sidecar. First-time use interviews the user and iterates the Lavish bank with them. Not a finished Figma.
---

# UI Studio (WIP)

**Status: work in progress.** Do not claim a finished editor, live Figma, or
pixels without a Lavish artifact path.

## First-time use (required)

On first use in a project (no `.fusion-studio/bank/.initialized`):

1. **Interview the user** (attended): which websites/apps to harvest, what they
   want the produced site to do, viewports, brand/color constraints, what to
   keep vs drop from the centrifuge.
2. Harvest with `source-picker`. Iterate the bank **with the user** — colors,
   type scale, sliders, merge styles, effects, drag elements in/out.
3. Deploy Lavish (`npx -y lavish-axi <bank/index.html>`) as an entity
   **separate from the produced site** (bottom-right / Shift+Tab).
4. Write `.fusion-studio/bank/.initialized` only after they accept the first
   harvest. Until then, studio is **WIP / NOT VERIFIED**.

Unattended e2e/GOAL/Stably: **do not interview**. Leave studio WIP unless a
bank already exists; do not block the rest of the evidence ladder on studio.

LLM instruction: treat every first-run as a collaborative design session.
Change the bank with the user. The shipped MVP HTML is a seed, not the product.

## Folder contract

```
.fusion-studio/bank/index.html      # Lavish sidecar (not the produced site)
.fusion-studio/bank/tokens.css      # CSS variables pushed onto the page
.fusion-studio/bank/harvest/*.json  # centrifuge of shared feature types
.fusion-studio/bank/.initialized    # written only after first-use accept
```

Produced site must not own the bank. Optional spawn: `studio-bank`.
