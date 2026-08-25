---
name: source-picker
description: WIP. Pick harvest sources for Fusion UI Studio with the user on first use.
---

# Source picker (WIP)

Companion to `ui-studio`. First-time use: **ask the user** which sites and
what they want, then harvest. Do not freeze the seed bank as the product.

## Contract

- Enumerate start-dev origins (localhost sites, Storybook, preview URLs) and
  user-named prompts. Do not scrape credentials.
- Centrifuge shared feature types: navigation, forms, tables, empty, error,
  marketing hero. Write one JSON per type under `.fusion-studio/bank/harvest/`.
- The produced site does not own the bank. Isolation `none` in the owning tree.
- In pane editors, spawn as a **side terminal teammate** (Orca `terminal create`
  / cmux split), not a nested worktree.
- Unattended e2e: skip the interview; leave studio WIP unless harvest already
  exists.

Output: harvest paths + SHA-256s. Missing harvest is **NOT VERIFIED** for studio.
