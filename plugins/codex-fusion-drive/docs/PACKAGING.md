# Grok Build Packaging and Platform Contract

The authoritative marketplace index is the repository-root `.grok-plugin/marketplace.json`. The actual plugin is `plugins/grok-fusion-drive/`. `.claude-plugin` files are compatibility metadata only.

Grok Build exposes skills and `commands/` in the slash menu. `/grok-fusion-drive:settings` (and Claude `/fusion-drive:settings`) opens the curses e2e_policy TUI via `scripts/open_settings_tui.sh`. This plugin ships the original fusion slash surfaces plus `fusion-e2e-unattended`, `fusion-workflow-author`, and `settings`. Spawn-protocol companion skills document named-agent spawn; they are not additional fusion engines.

## Native binary limitation

Grok plugins distribute files. They do not install Rust or provide a portable native runtime. Version `0.1.0` stages one current-platform artifact:

- OS: macOS
- Architecture: arm64
- Binary: `bin/grok-fusion-drive-mcp`
- SHA-256: `3f4630ef63b2f52c3f5312efac2ebc0e94451654710687956a3551b4bac4980f`

Do not install that artifact on another OS or architecture. Build on the target platform and update compatibility metadata before distribution.

## Source build

An existing Rust toolchain is required:

```sh
./scripts/build-macos-arm64.sh
```

The script builds the root Cargo workspace in release mode and copies the resulting binary into the plugin bundle. It does not install Rust.

## Local marketplace and installation

After building:

```sh
grok plugin marketplace add /absolute/path/to/grok-fusion-drive
grok plugin install grok-fusion-drive --trust
grok plugin enable grok-fusion-drive
```

Alternatively, install the actual plugin subdirectory as a trusted local source. Adding a marketplace does not install anything. Installing and enabling are distinct, and hooks/MCP remain blocked without trust. Press `r` in the Plugins tab or start a new session after installation.

The runtime writes only to `GROK_PLUGIN_DATA`; wrappers reject a data path under `GROK_PLUGIN_ROOT`. Provider execution additionally requires environment credentials and explicit billable-call confirmation. Installation does not verify those credentials or perform a model call.
