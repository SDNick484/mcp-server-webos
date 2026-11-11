# Changelog

## [Unreleased]

## [0.1.0]

First release on PyPI.

### Added

- MCP server for LG webOS TVs over SSAP (`aiowebostv`): status, power, screen off, sources and allow-listed apps, keys, media control, bounded text entry and in-app search, volume and mute, for several TVs at once, never guessing between them.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `pair`, `check`, `apps`, `discover`, `doctor`, `capture`, `call` and `simulate` (C7, G1 and C5 profiles, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script.
