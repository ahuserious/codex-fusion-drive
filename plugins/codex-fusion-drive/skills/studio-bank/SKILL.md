---
name: studio-bank
description: Optional spawn for WIP ui-studio harvest. First-use iterates the bank with the user.
---

# Spawn: studio-bank (optional, WIP)

- **Type:** `grok-fusion-drive:studio-bank`
- **Writes:** `.fusion-studio/bank/` including a Lavish `index.html`.
- **First use:** interview + iterate with the user; stamp `.initialized` only
  after accept. Unattended e2e skips interview and leaves studio WIP.
- **Rule:** do not claim UI Studio pixels without `lavish_artifact_path`.
  Studio is **WIP**, not a finished Figma.
