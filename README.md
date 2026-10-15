# mcp-server-webos

An [MCP](https://modelcontextprotocol.io) server for LG webOS TVs: status, power, screen, apps and
inputs, remote keys, playback and volume, from Claude Code, Claude Desktop or the mobile app. It
talks to each TV directly over LG's SSAP WebSocket protocol, through
[aiowebostv](https://github.com/home-assistant-libs/aiowebostv) (the library Home Assistant uses),
with no dependency on Home Assistant.

A sibling of [mcp-server-onkyo](https://github.com/SDNick484/mcp-server-onkyo),
[mcp-server-shieldtv](https://github.com/SDNick484/mcp-server-shieldtv),
[mcp-server-harmony](https://github.com/SDNick484/mcp-server-harmony) and
[mcp-server-sofabaton](https://github.com/SDNick484/mcp-server-sofabaton), with the same
conventions: typed tools with annotations, an `ActionResult` from every action, dry run, `doctor`,
a wire-level simulator, an assumptions registry, Streamable HTTP behind Cloudflare Access, and an
OpenRC service for an Alpine LXC.

> **Status: built and tested against a simulator only.** Nothing here has run against a real TV
> yet. [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) is the checklist that changes that, and
> the table at the end of this README says what has been confirmed.

## The TVs it targets

The owner's three OLEDs, one per generation. Other webOS TVs (2014 onwards) speak the same
protocol and will probably work, but nothing is promised for them.

| Room | Model | Platform | Control port | Notes |
| --- | --- | --- | --- | --- |
| Bedroom | OLED65C7P-U (2017 C7) | webOS 3.5 | 3000 (ws) | no power-state call: power is inferred; probably no screen-off |
| Family Room | OLED77G1PUA (2021 G1) | webOS 6.0 | 3001 (wss) | |
| Office | OLED65C5PUA (2025 C5) | webOS 25 (26 on offer) | 3001 (wss) | webOS 26 changed pairing (see below) |

A TV usually sits on one HDMI input from an Onkyo receiver, and **volume belongs to the
receiver**: when a TV's sound goes out over eARC/ARC, this server refuses to change its volume and
points at the receiver's own server instead. Going through the TV would bypass the receiver
server's volume caps (the TV forwards its volume to the receiver over HDMI-CEC). A separate
home-theater Skill coordinates the servers; [docs/skill-webos.md](docs/skill-webos.md) is this
server's cheat sheet for it.

### webOS 26 and pairing

In August 2026 LG's webOS 26 firmware started rejecting the signed registration manifest that
every SSAP client had sent since 2014 (`403 Pairing rejected: blacklisted certificate detected`,
before any dialog). aiowebostv 0.9.2 switched to an unsigned manifest, which webOS 26 accepts;
this server requires at least that version. LG has been rolling webOS 26 out to 2025 sets since
late August, and there is no way back, so HARDWARE_VALIDATION.md captures the C5 on webOS 25
before it updates.

## Install

```sh
git clone https://github.com/SDNick484/mcp-server-webos.git && cd mcp-server-webos
python -m venv .venv && . .venv/bin/activate && pip install -e .
```

Python 3.11 or newer. Every dependency ships a musl wheel, so Alpine needs no compiler.

## Pair each TV

The TV must be on. On a webOS 3.x set (the C7), turn on **LG Connect Apps** in its network
settings first. For `set_power on` to work, also turn on the setting that lets the network wake
it: "Mobile TV On", "TV On With Mobile" or "Turn on via Wi-Fi", depending on the generation.

```sh
mcp-server-webos pair --host 192.168.1.50 --name "Family Room" --alias G1
```

The TV shows a pairing request: accept it with the remote (`pair` waits up to 60 s). `pair` reads
the TV's identity first (its deviceUUID, from the keyless hello), then saves:

- the client key, in `keys.json` (0600), by deviceUUID. It is full control of the TV, so it is
  never logged, never in `config.json`, never in a tool result;
- the TV in `config.json`: name, aliases (your `--alias`es and the model), address, MACs for
  Wake-on-LAN (the connected interface first; `--mac` if they can't be read), model, platform,
  and the TLS certificate's fingerprint.

Both live in `~/.config/mcp-server-webos` (or `$WEBOS_CONFIG_DIR`), 0700. A running server
notices a new key and connects with it, without a restart. Re-running `pair` for the same TV
updates its entry and keeps your hand edits.

```sh
mcp-server-webos check        # connect once to each TV: platform, what works, what doesn't
mcp-server-webos doctor       # layer by layer, read-only; each failure names a validation step
mcp-server-webos discover     # which webOS TVs answer SSDP (TVs that are off don't)
```

### Choose the apps

`select_source` only opens apps on the allow-list: a few defaults, plus yours. To see what a TV
actually has installed, and add from it:

```sh
mcp-server-webos apps --tv "Office TV"                 # listed by group, with the name select_source will use
mcp-server-webos apps --tv "Office TV" --add crunchyroll,"Photo & Video"
mcp-server-webos apps --all-tvs --add-all             # every third-party app on any TV
```

| Group | Means | Added by |
| --- | --- | --- |
| Allowed | already on the allow-list | |
| Not allowed yet | third-party apps | `--add NAME` or `--add-all` |
| LG's own apps | `com.webos.`, `com.palm.`, `com.lge.` ids (W-SYSTEM-APP-IDS) | `--add NAME` only |
| Excluded | the web browser (it opens any URL), settings, LG's store (it installs apps), inputs (`select_source` handles them), Live TV, service menus | never |

It asks the TV over a short session with the stored key and changes nothing on it. New entries go
into `config.json`'s `"apps"`; restart the server to use them. Run it as the service user on the
LXC (`su -s /bin/sh mcp-webos -c '…'` with `WEBOS_CONFIG_DIR=/var/lib/mcp-server-webos`), since
`keys.json` and `config.json` are that user's. There is no tool for this: the model can't widen the
allow-list.

### config.json

`pair` writes it; you may edit names, aliases, `max_volume` and `volume_owner`:

```json
{
  "tvs": [
    {"name": "Bedroom", "aliases": ["C7", "OLED65C7P-U"], "host": "192.168.1.52",
     "uuid": "...", "macs": ["a8:23:fe:..."], "model": "OLED65C7P-U", "platform": "3.5.0",
     "max_volume": 30, "volume_owner": "auto"}
  ],
  "apps": {"crunchyroll": "crunchyroll.app.id"},
  "max_volume": 40
}
```

- `max_volume` (0-100, default 40): `set_volume` caps at it, per TV or for all.
- `volume_owner`: `auto` (from the TV's sound output: external outputs mean the receiver owns
  volume), `tv` (always allow), or `external` (never).
- `apps`: extra apps for the allow-list, `name: app id` (ids from `list_sources` or a capture).
- `wol_broadcast` / `wol_port`: where magic packets go (default 255.255.255.255:9).
- Hosts must be private IPv4 addresses. A name or alias that would mean two TVs is refused.

## Use it from Claude

```sh
claude mcp add webos -- /path/to/.venv/bin/mcp-server-webos            # Claude Code, stdio
```

### Tools

Ten, on purpose: every tool definition costs context in every client.

| Tool | Does | Annotations |
| --- | --- | --- |
| `list_tvs` | names and aliases `tv` accepts, model, platform | read-only |
| `get_status` | power, what is in front, input, now playing, volume, sound output, capabilities and limitations; all TVs when `tv` is omitted | read-only |
| `set_power` | on (Wake-on-LAN, confirmed) or off (standby); `tv="all"` for off | idempotent |
| `set_screen` | blank the picture while sound plays, or bring it back | idempotent |
| `list_sources` | allow-listed apps the TV has installed, and its HDMI inputs | read-only |
| `select_source` | open an app or switch input, confirmed in front | idempotent |
| `send_key` | one allow-listed remote key, up to 10 times | not idempotent |
| `media_control` | play, pause, stop, rewind, fast-forward | not idempotent |
| `set_volume` | 0-100, capped; refused when the receiver owns volume | idempotent |
| `set_mute` | mute or unmute; same rule | idempotent |

Nothing is `destructiveHint`: turning a TV off is undone by turning it on. Every action returns an
`ActionResult`, the siblings' shape:

```json
{"tv": "Bedroom", "outcome": "done", "detail": "Bedroom's volume is 15",
 "sent": ["ssap://audio/setVolume 15"], "warnings": []}
```

`outcome` is `done`, `unchanged` (already so; nothing sent) or `dry_run`. Failures are tool
errors whose message says what to do ("Which TV? Several are configured: ...", "Volume on Family
Room belongs to the receiver ...", "... needs pairing: run pair").

Resources: `webos://tvs` (capabilities per TV), `webos://keys` (the key allow-list, and the keys
left out with the reason for each), `webos://apps` (the app allow-list). One prompt, `watch`.

### What the model can't do

- **No raw SSAP or luna calls.** The same reasoning as no `adb shell` on the Shield: one generic
  call would reach everything the TV exposes, including its internal settings services.
- **No URL or text tools:** no browser, no "play this URL", no typing.
- **Keys are an allow-list.** `POWER` (a toggle), the `IN_START`/`EZ_ADJUST` service menus,
  settings menus, the volume keys (they would bypass the cap) and app shortcuts (they would
  bypass the app allow-list) are left out; `webos://keys` lists why.
- **Apps are an allow-list** of ids the TV itself lists. Strings from the TV (app titles, input
  labels) are cleaned of control characters, bounded, and treated as data.
- **Never guess a TV:** with several configured, an action without `tv` is refused and the TVs are
  listed. **Never guess a source:** a name that matches an app and an input is refused.
- **Screen off uses standbyMode "active" only.** "passive" can't be undone without unplugging the
  TV; it isn't an argument anywhere.

[docs/OUT_OF_SCOPE.md](docs/OUT_OF_SCOPE.md) lists the rest (picture settings, channels, LG's IP
control) and why.

### Dry run

`serve --dry-run` (or `WEBOS_DRY_RUN=1`) reads the TVs but sends nothing that changes anything:
every action returns `outcome: "dry_run"` with what it would have sent.

## How it stays connected

Each TV has one long-lived WebSocket (the TV pushes changes over subscriptions) and a supervisor:

- **Off:** a TV that is off has no socket. The supervisor retries every ~10 s, so a TV turned on
  by its remote shows up within ~10 s. `get_status` on a stale TV first makes one connect attempt
  of at most 3 s, then answers with `stale`, `as_of` and `error`.
- **Stale key:** if the TV no longer accepts the stored key (a reset, or a firmware update), it
  wants to show its pairing dialog. The server hangs up at once, marks the TV `needs_pairing` and
  stops trying until `pair` writes a new key. That's one dialog, not one every 10 s.
- **Moved:** the key is only sent after the TV's hello shows the paired deviceUUID. If another
  device has the address, the server looks for the TV by SSDP and adopts a new address only on a
  deviceUUID match.
- **Pointer socket refused:** aiowebostv would fail the whole connection; here only the keys stop
  working (see `compat.py`).

`compat.py` holds every dependency on aiowebostv internals, with contract tests; that is why the
dependency is pinned `<0.11`.

## HTTP and Cloudflare Access

To share one server between Claude Code, Claude Desktop and the mobile app, run it as a service
and reach it through a Cloudflare Tunnel with Access in front, exactly as the siblings do. Claude's
connectors reach it from the internet, so the URL is public; Access is what keeps it yours.

| Flag | Variable | Default |
|---|---|---|
| `--bind` | `MCP_HTTP_BIND` | `127.0.0.1` |
| `--port` | `MCP_HTTP_PORT` | `8715` (Onkyo 8711, Shield 8712, Harmony 8713, Sofabaton 8714) |
| `--path` | `MCP_HTTP_PATH` | `/webos/mcp` |
| `--public-host` | `MCP_PUBLIC_HOSTS` | none: only loopback Host headers pass, so **Cloudflare's requests get 421** |
| | `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | none: no Access checks |
| | `MCP_ALLOWED_EMAILS` | anyone Access lets in |

With Access configured, every request needs a valid `Cf-Access-Jwt-Assertion`; anything else
gets a plain 403. The server refuses to start on a non-loopback address without Access unless
you pass `--insecure-no-auth`. `GET /healthz` answers without auth. `remote.py` and `logsafe.py`
are shared with the sibling servers, from
[mcp-server-shared](https://github.com/SDNick484/mcp-server-shared).

### Setting up Cloudflare

This is the setup that works, verified with Claude Code and the Claude app connector on
2026-10-14. Do the Access application first, so the URL is never public.

1. **One Access application for the whole hostname**, shared by every MCP server on it. Zero Trust
   → Access controls → Applications → Add → Self-hosted:
   - Destination: subdomain `mcp`, your domain, **path empty**. Managed OAuth serves its sign-in
     discovery from the root of the hostname (`/.well-known/...`), so an app limited to
     `/webos` may not cover it; one app also means one sign-in for all the servers.
   - Policy: Allow → Emails → you. Login method: One-time PIN (or your identity provider).
   - Edit the app → **Advanced settings → Managed OAuth: on**. Allowed redirect URIs:
     `https://claude.ai/*`. Turn on **Allow localhost clients** (Claude Code signs in through
     localhost). Access token 15 min, grant session 1-2 weeks.
   - Copy the app's **Application Audience (AUD) tag**.
   - Make sure no *other* Access app covers a path on the same hostname: Access applies the most
     specific match, and a Bypass policy there would let requests through without a token.
2. **A route on the tunnel.** Networks → Tunnels → the tunnel → Published application routes →
   Add: hostname `mcp.example.com`, path `^/webos/` (a regex), service
   `http://<address>:8715`. Leave the HTTP Host Header override unset: the server checks the
   public Host.
   - The service address is resolved **where cloudflared runs**. If cloudflared is a Docker
     container (the tunnel page lists the replica's hostname as a 12-character hex container ID), `localhost`
     is that container, not the LXC: use the LXC's LAN address or the Docker bridge's
     (`172.17.0.1`, reachable only from containers on that host), and set `MCP_HTTP_BIND` to the same address.
3. **`/etc/mcp-server-webos/env`** (see `deploy/alpine/env.example`), then
   `rc-service mcp-server-webos restart`:
   ```sh
   MCP_HTTP_BIND=192.168.1.10          # e.g. the LXC's address: the one the route points at
   MCP_PUBLIC_HOSTS=mcp.example.com # required: without it every request through Cloudflare gets 421
   CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
   CF_ACCESS_AUD=<the AUD tag from step 1>
   MCP_ALLOWED_EMAILS=<the email you sign in to Access with>
   ```
4. **Test**, then add `https://mcp.example.com/webos/mcp` as a custom connector in Claude (and
   `claude mcp add --transport http webos <url>` for Claude Code):
   ```sh
   curl -s http://192.168.1.10:8715/healthz                               # {"status": "ok"}
   curl -si -X POST https://mcp.example.com/webos/mcp -d '{}' | head -5 # 401 + www-authenticate
   ```

### When it doesn't connect

Watch the server while you press Connect: `tail -n 0 -f /var/log/mcp-server-webos/server.log`.

| You see | Cause | Fix |
| --- | --- | --- |
| The outside `curl` gets a 302 to `…cloudflareaccess.com` | Managed OAuth is off: Claude gets a login page | Turn it on (step 1) |
| Claude: "couldn't reach" / "authorized, but … returned an error"; Claude Code: `HTTP 502`; nothing in the log | The tunnel can't reach the server: the route's service address isn't where the server listens (usually `localhost` with cloudflared in a container) | Step 2's address and `MCP_HTTP_BIND` must match |
| Claude Code: `HTTP 421`; log: `Invalid Host header: mcp.example.com` | `MCP_PUBLIC_HOSTS` isn't set | Step 3 |
| Log: `Denied … no Cloudflare Access assertion` | The request didn't come through Access: a local test with `curl`, or another Access app/Bypass policy on the path | Ignore it for local tests; otherwise see step 1's last point |
| Log: `Denied … Audience doesn't match` / `Invalid issuer` | `CF_ACCESS_AUD` is another app's tag, or the team domain is wrong | Copy both again from the app in step 1 |
| Log: `Denied … not in MCP_ALLOWED_EMAILS` | You signed in to Access with another address | Make them match |

### As a service on Alpine (Proxmox LXC)

```sh
sh deploy/alpine/install.sh                      # as root; or: install.sh /path/to/checkout
su -s /bin/sh mcp-webos -c 'WEBOS_CONFIG_DIR=/var/lib/mcp-server-webos \
  /opt/mcp-server-webos/venv/bin/mcp-server-webos pair --host <tv ip> --name "Family Room"'
vi /etc/mcp-server-webos/env                     # HTTP and Access settings
rc-update add mcp-server-webos default && rc-service mcp-server-webos start
```

| Path | Owner, mode | Holds |
| --- | --- | --- |
| `/opt/mcp-server-webos/venv` | root, 0755 | The code |
| `/etc/mcp-server-webos/env` | root:mcp-webos, 0640 | HTTP and Access settings |
| `/var/lib/mcp-server-webos/` | mcp-webos, 0700 (files 0600) | `keys.json`, `config.json` |
| `/var/log/mcp-server-webos/server.log` | mcp-webos, 0750 | Logs, redacted |
| `/etc/init.d/mcp-server-webos` | root, 0755 | OpenRC script (`supervise-daemon`) |

**Wake-on-LAN from an LXC:** the magic packet is a broadcast (plus a copy to the TV's last
address), so the container must be bridged onto the TVs' network, not NATed. `doctor` shows the
local address it would leave from.

## Logs

stderr only (stdout is the MCP stdio transport). Addresses, MACs, serial numbers, deviceUUIDs and
pointer-socket ids are redacted unless `--no-redact` (or `WEBOS_LOG_UNREDACTED=1`); client keys,
JWTs and certificates always are. `--debug` (`WEBOS_DEBUG=1`) adds aiowebostv's protocol traffic.

## Testing without a TV

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-webos simulate --profile g1 --config-dir /tmp/sim --paired       # then, in another terminal:
WEBOS_CONFIG_DIR=/tmp/sim mcp-server-webos call get_status
WEBOS_CONFIG_DIR=/tmp/sim mcp-server-webos call select_source source=netflix
```

The simulator (`src/webos_mcp/sim/`) speaks SSAP over real WebSockets: the hello, registration
and the pairing dialog, requests and subscriptions with pushed updates, the TV's error replies,
the pointer socket, and Wake-on-LAN. The real aiowebostv connects to it. Profiles: `c7`, `g1`,
`c5-webos25`, `c5-webos26`, each field naming its source. aiowebostv always dials ports 3000 and
3001, so each simulated TV binds the real ports on its own loopback address (127.0.0.2-5): on
Linux all of 127.0.0.0/8 is loopback. (macOS needs loopback aliases for that.)

`mcp-server-webos capture --tv <name> --profile <p>` records a real TV's answers to read-only
requests, redacted; committed to `src/webos_mcp/sim/fixtures/`, each capture becomes a contract
test against its simulator profile.

## What has been confirmed on hardware

Every protocol detail this server relies on is in `src/webos_mcp/assumptions.py`, with its source
and how confident that source is. `doctor` prints the unconfirmed ones; HARDWARE_VALIDATION.md
says which step confirms each.

| Assumption | Confidence | Status |
| --- | --- | --- |
| `W-MANIFEST` | high | simulator-only |
| `W-SIGNED-403` | high | simulator-only |
| `W-KEY-SURVIVES` | low | simulator-only |
| `W-REGISTERED-DIRECT` | high | simulator-only |
| `W-STALE-KEY` | medium | simulator-only |
| `W-PROMPT-DISMISS` | low | simulator-only |
| `W-PAIR-WINDOW` | low | simulator-only |
| `W-HELLO-VERSION` | high | simulator-only |
| `W-SWINFO-401` | high | simulator-only |
| `W-UUID-STABLE` | medium | simulator-only |
| `W-PORTS` | medium | simulator-only |
| `W-PORT3000-CLOSE` | medium | simulator-only |
| `W-TLS-CERT` | low | simulator-only |
| `W-MULTI-CLIENT` | medium | simulator-only |
| `W-INPUT-SOCKET` | medium | simulator-only |
| `W-SUBSCRIBE` | high | simulator-only |
| `W-POWER-STATES` | high | simulator-only |
| `W-C7-NO-POWERSTATE` | low | simulator-only |
| `W-STANDBY-SOCKET` | low | simulator-only |
| `W-OFF-GUARD` | low | simulator-only |
| `W-TURNON-DEAD` | medium | simulator-only |
| `W-WOL` | medium | simulator-only |
| `W-WOL-PATH` | low | simulator-only |
| `W-WAKE-TIME` | medium | simulator-only |
| `W-MAC-SOURCE` | low | simulator-only |
| `W-SCREEN-OFF` | medium | simulator-only |
| `W-KEYS` | medium | simulator-only |
| `W-MEDIA-CONTROLS` | low | simulator-only |
| `W-NOW-PLAYING` | medium | simulator-only |
| `W-SOUND-OUTPUT` | medium | simulator-only |
| `W-VOLUME-ARC` | medium | simulator-only |
| `W-APP-IDS` | low | simulator-only |
| `W-SYSTEM-APP-IDS` | medium | simulator-only |
| `W-INPUT-IDS` | medium | simulator-only |
| `W-SSDP` | medium | simulator-only |

## License

MIT
