# Changelog

## [Unreleased]

### Added

- `initialize` reports the package version as `serverInfo.version` (it was empty), so a bug report says which build answered.
- `webos_get_status` with no TV configured adds a `hint` saying how to pair one, instead of an empty list alone.
- `scripts/smoke_stdio.py`: launches an installed server over stdio as an MCP client does and checks the first exchange. CI's new `wheel-smoke` job runs it against the built wheel on Linux, macOS and Windows; the release workflow runs it before publishing.
- Issue templates: a bug report and a hardware report.

### Documentation

- README: tool names carry their `webos_` prefix (as they have since 0.1.0), so `call` examples work; the status reflects the hardware runs from 2026-10-14 to 2026-11-10; Claude Desktop and Claude Code configs using `uvx`.
- HARDWARE_VALIDATION.md: no `hardware-free` branch to check out; the feature table and platform versions reflect what has run on the three TVs.
- W-INPUT-IDS is hardware-verified (Office C5, 2026-10-17).

## [0.1.0]

First release on PyPI.

### Added

- MCP server for LG webOS TVs over SSAP (`aiowebostv`): status, power, screen off, sources and allow-listed apps, keys, media control, bounded text entry and in-app search, volume and mute, for several TVs at once, never guessing between them.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `pair`, `check`, `apps`, `discover`, `doctor`, `capture`, `call` and `simulate` (C7, G1 and C5 profiles, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script.
