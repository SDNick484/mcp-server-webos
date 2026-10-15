# CLAUDE.md

## What this is

`mcp-server-webos`: an MCP server that controls LG webOS TVs over SSAP (WebSocket, ports 3000/3001)
via `aiowebostv`, Home Assistant's library. Sibling of `mcp-server-onkyo`, `mcp-server-shieldtv`
(the closest model), `mcp-server-harmony` and `mcp-server-sofabaton`; match their conventions.

**This is a learning project.** The owner wants to understand how MCP servers are built, so
explain the reasoning behind non-obvious changes instead of only making them. He's a
software/systems engineer: be technical and concrete, with specific examples.

Targets: the owner's 2017 C7 (webOS 3.5, Bedroom), 2021 G1 (webOS 6.0, Family Room) and 2025 C5
(webOS 25, webOS 26 on offer, Office). Other webOS TVs are best-effort.

## Layout

- `src/webos_mcp/config.py`: settings (several TVs), file locations, the key and app allow-lists
  and the keys left out; loading never raises (problems go to `Settings.problems`)
- `src/webos_mcp/keystore.py`: client keys in `keys.json` by deviceUUID, 0600; never in config.json
- `src/webos_mcp/tvs.py`: which TV a call means (never guess; `all` only for power off)
- `src/webos_mcp/compat.py`: **every** dependency on aiowebostv internals (`_check_registration`,
  `_create_input_ws`), guarded by `tests/test_compat.py`; why is in its docstring
- `src/webos_mcp/client.py`: `TvClient`, one supervised connection per TV, the pushed-state cache,
  capabilities, rediscovery, and every action (`_call` asserts dry run is off)
- `src/webos_mcp/server.py`: the ten tools (`MCPServer` from `mcp` 2.x), `ActionResult`,
  resources (`webos://...`), the `watch` prompt
- `src/webos_mcp/probe.py` (keyless hello), `wol.py`, `discovery.py` (SSDP)
- `src/webos_mcp/assumptions.py`: every protocol detail relied on (W-* ids). Cite ids in code
  (`ASSUMPTION W-...`); `tests/test_assumptions.py` keeps code, README and HARDWARE_VALIDATION.md
  in step with it
- `src/webos_mcp/apps.py`: the `apps` command: each TV's installed apps by group (allowed, third-party,
  LG's own, excluded) and adding chosen ones to config.json's allow-list. CLI only, never a tool
- `src/webos_mcp/cli.py`: `serve` (default), `pair`, `check`, `apps` (`apps.py`), `discover`, `doctor` (`doctor.py`),
  `capture` (`capture.py`), `simulate`, `call`
- **Shared with the siblings; the source of truth is mcp-server-shared.** `src/webos_mcp/remote.py`,
  `logsafe.py`, `tasks.py` (`stop_task`), `scripts/sync_shared.py`, and `tests/test_remote.py`,
  `test_logsafe.py`, `test_tasks.py` (rendered with this package's name); mcp-server-shared's
  `shared.toml` is the full list. Don't edit them here: change them in mcp-server-shared, then run
  `python scripts/sync_shared.py --update` here and commit the files together with `shared.lock`.
  `python scripts/sync_shared.py --check` (CI's `shared-drift` job) fails on any difference from the
  locked commit; a weekly scheduled run also fails once mcp-server-shared `main` has moved past it.
  webOS-only redaction is in `redact.py`, layered on top of `logsafe.py`
- `src/webos_mcp/sim/`: `fake_tv.py` (the TV on the wire, with `Faults`), `profiles.py` (c7, g1,
  c5-webos25, c5-webos26, each behavior with its source), `fixtures/` (captures from real TVs)
- `deploy/alpine/`: OpenRC service, install script, and the container smoke test CI runs
- `docs/`: `skill-webos.md` (for the home-theater Skill), `OUT_OF_SCOPE.md`, `PLAN.md`
- `tests/`: simulator fixtures in `conftest.py` (`tvs`, `register`, `fast`); `test_wire` (the
  library vs the simulator), `test_compat`, `test_client`, `test_tools`, `test_cli`,
  `test_contract` (skips until there are captures), shared `test_remote`/`test_logsafe`/`test_tasks`. Async
  tests use anyio's plugin, not pytest-asyncio.

## Rules for changes

- **No escape hatches.** No raw SSAP/luna passthrough, no URL or free-text tools; keys and apps
  are allow-lists. Widen them deliberately, with README and tests in the same commit. Users grow
  the app allow-list with `apps`; never give the model a tool for it, and keep `apps.EXCLUDED` out
  of `--add`.
- **Never guess a device or a source.** With several TVs, an action without `tv` is refused; a
  key is only ever sent after the hello shows the paired deviceUUID; rediscovery adopts an address
  only on a deviceUUID match; a source name matching an app and an input is refused.
- **Never loop the pairing dialog.** A stale key means needs_pairing and no more attempts until
  keys.json changes.
- **The receiver owns volume** when the sound output is external; keep that refusal.
- **Screen off sends standbyMode "active" only** ("passive" needs the TV unplugged to undo).
- Raise `WebOsError` (a `ToolError`) for anything the model or user can act on. Other exceptions
  reach the model only as "Error executing tool".
- Log to **stderr only**. Client keys are never logged, printed or returned; `keys.json` stays 0600.
- Every tool has a `title`, explicit `ToolAnnotations` and constrained args; `test_tools.py`
  enforces it. Actions return `ActionResult` and honor dry run.
- aiowebostv calls state callbacks once more with a cleared state when it disconnects; `TvClient`
  ignores that call (stale values must survive).
- **Don't invent protocol details.** Anything not seen on these TVs goes in `assumptions.py`, is
  cited where the code relies on it, and gets a step in HARDWARE_VALIDATION.md. The simulator
  names the source of each behavior.
- The simulator binds 127.0.0.2-5 on the real ports: Linux only.

## Status

Built entirely away from the TVs: every assumption is `simulator-only` until HARDWARE_VALIDATION.md
is run. Treat that file's results, once recorded in `assumptions.py`, as authoritative over
anything written here.

## Commands

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-webos pair --host IP --name ROOM [--alias A] | check | discover | doctor | (serve [--http] [--dry-run])
mcp-server-webos simulate --profile g1 --config-dir /tmp/sim [--paired]   # then WEBOS_CONFIG_DIR=/tmp/sim ...
mcp-server-webos apps --tv ROOM | --all-tvs [--add NAME,... | --add-all] [--json]   # the app allow-list
mcp-server-webos call <tool> key=value                                    # one tool through the MCP layer
mcp-server-webos capture --tv ROOM --profile c5-webos25 --out captures/
python scripts/sync_shared.py --check [--latest]                          # shared files vs mcp-server-shared
python scripts/sync_shared.py --update                                    # take mcp-server-shared main
```
