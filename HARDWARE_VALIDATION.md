# Hardware validation (LG webOS TVs)

Everything in this repo was built away from the TVs and is **verified against the simulator
only** until you run the step that covers it. The simulator (`src/webos_mcp/sim/`) is a
wire-level fake that the real aiowebostv library pairs and connects with; what it answers comes
from other people's captures and from aiowebostv's source, and each step below names the
assumptions it confirms (ids, claims and sources are in `src/webos_mcp/assumptions.py`).

Three TVs, one column each in every table:

| | Bedroom | Family Room | Office |
| --- | --- | --- | --- |
| Model | OLED65C7P-U (2017) | OLED77G1PUA (2021) | OLED65C5PUA (2025) |
| Platform | webOS 3.5 | webOS 6.0 | webOS 25, webOS 26 on offer |
| Firmware HA last saw | 06.10.65 | 03.53.45 | 33.31.61 |

Plan on about an hour and a half for all three. `doctor` points at steps 3, 4, 6 and 11 by
number, so keep the numbering if you edit this file. Tick each cell as you go, and record what
you saw in `assumptions.py` (see "Recording results" at the end).

## What's new and unconfirmed

| Feature | Status | Step |
| --- | --- | --- |
| `pair`: hello first, 60 s for OK on the TV, MACs and certificate saved | verified against simulator only | 3 |
| `doctor` (config, ports, hello, session, permissions, Wake-on-LAN, SSDP) | verified against simulator only | 4 |
| `get_status`: power, input, now playing, sound output, capabilities | verified against simulator only | 5 |
| `set_power`: off from on only; on by Wake-on-LAN, confirmed | verified against simulator only | 6 |
| `set_screen` with standbyMode "active" | verified against simulator only | 7 |
| `list_sources` / `select_source` (allow-listed apps, HDMI inputs, never guess) | verified against simulator only | 8 |
| `send_key` (allow-list), `media_control` | verified against simulator only | 9 |
| `set_volume` / `set_mute`: per-TV cap, refused when the receiver owns volume | verified against simulator only | 10 |
| Stale key: one dialog, then needs_pairing; re-pair picked up live | verified against simulator only | 11 |
| Rediscovery by deviceUUID after an address change | verified against simulator only | 11 |
| A refused pointer socket costs only the keys (compat.py) | verified against simulator only | 4 |
| `capture` and the contract tests | verified against simulator only | 1 |
| Streamable HTTP + Cloudflare Access; OpenRC service on Alpine | verified on hardware (2026-10-14, CT 210, Office TV; mobile app not yet) | 13 |
| Log redaction (addresses, MACs, serials, UUIDs; keys always) | verified against simulator only | 4 |

## Before you start

- Each TV on, on the same network as this machine, and its IP address (your router's client
  list, or the TV's network settings). DHCP reservations help, but aren't required.
- The TV in view: steps 3, 7 and 11 put things on screen, or must not.
- The remote for each TV, to accept pairing.

```sh
git clone https://github.com/SDNick484/mcp-server-webos.git && cd mcp-server-webos
git checkout hardware-free
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && pytest -q        # all green before you start
export WEBOS_CONFIG_DIR=~/.config/mcp-server-webos-validation   # keep this run's pairing apart
```

## 1. Capture the C5 before it takes webOS 26

There is no rollback from webOS 26, and it changes pairing (ASSUMPTION W-SIGNED-403, W-SWINFO-401).
So the C5 gets captured on webOS 25 first, and again after the update.

1. On the C5: Settings > Support > Software Update. Note the version (33.x is webOS 25, 43.x is
   webOS 26; HA can't read it on webOS 26, so trust the TV). Turn **off** automatic updates for now.
2. If it is still on webOS 25: pair it (step 3), then
   `mcp-server-webos capture --tv Office --profile c5-webos25 --out captures/`.
3. Capture the other two the same way after pairing them: `--profile c7`, `--profile g1`.
4. Copy the files into `src/webos_mcp/sim/fixtures/` and run `pytest tests/test_contract.py`.
   A failure there means the simulator claims something the TV doesn't do: fix the profile.
5. Later, let the C5 update, run step 11's "after the update" part, and capture again as
   `--profile c5-webos26`.

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| Version noted, auto-update off | n/a | n/a | |
| Captured, contract test passes | | | |
| `hello.deviceOSReleaseVersion` (W-HELLO-VERSION) | | | |

## 2. Settings on each TV

Names and menus differ by generation; record the path you actually used.

| Setting | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| LG Connect Apps (webOS 3.x network settings; needed for any control) | | n/a? | n/a? |
| "Mobile TV On" / "TV On With Mobile" / "Turn on via Wi-Fi" (W-WOL) | | | |
| Quick Start+ / Always Ready, as you normally run it (W-STANDBY-SOCKET) | | | |
| Wired or Wi-Fi (W-WOL-PATH) | | | |

## 3. Pairing

```sh
mcp-server-webos pair --host <tv ip> --name "Family Room" --alias G1
```

- The TV shows a pairing request: accept it with the remote. `pair` waits up to 60 s. Note how
  long the TV keeps the dialog up if you don't answer (ASSUMPTION W-PAIR-WINDOW).
- It prints the model, platform and the MACs it will use. Check the MAC against the TV's
  network settings: the *connected* interface must be first (W-MAC-SOURCE).
- Run `pair` again: no dialog should appear the second time (W-REGISTERED-DIRECT; the key is
  sent with the registration).
- `mcp-server-webos doctor`: the session layer says "registered without a dialog" (W-MANIFEST).

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| Dialog appeared; accepted; key saved (W-MANIFEST) | | | |
| Dialog timeout on the TV, if unanswered (W-PAIR-WINDOW) | | | |
| MAC printed = the connected interface (W-MAC-SOURCE) | | | |
| Second `pair` shows no dialog (W-REGISTERED-DIRECT) | | | |

Then the stale-key case, on one TV (the G1 is fine): in the TV's settings, remove this server
from the list of connected/paired devices (or factory-reset its LG Connect pairings), then run
`doctor`.

- The session layer must say the key isn't accepted and that doctor hung up at once.
- **Look at the TV**: the pairing dialog should be gone again within a second or two
  (W-STALE-KEY, W-PROMPT-DISMISS). If it stays up, note for how long.
- Re-pair with `pair` afterwards.

## 4. doctor

```sh
mcp-server-webos doctor             # all TVs; --tv Office for one; --json; --dump DIR
mcp-server-webos doctor --no-redact # addresses shown (keys never are)
```

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| tcp: which ports are open (W-PORTS) | | | |
| Port 3000 on a TLS-only set: refused, or accepts then closes? (W-PORT3000-CLOSE) | n/a | | |
| hello: platform, pairing types | | | |
| hello: TLS fingerprint; same after a TV reboot? (W-TLS-CERT) | n/a | | |
| session: buttons allowed (W-INPUT-SOCKET) | | | |
| session: refused calls (W-SWINFO-401; the C5 refuses it on webOS 25 too) | | | 401, 2026-10-16 |
| session: connected at all, i.e. no subscription refused (W-SUBSCRIBE) | | | |
| deviceUUID unchanged after a TV reboot (W-UUID-STABLE) | | | |
| Log lines redacted (no serial, UUID or key in `--debug` output) | | | |

## 5. Status and pushed state

```sh
mcp-server-webos call get_status tv=Bedroom
```

Change things with the TV's own remote while `mcp-server-webos serve` runs under Claude Code (or
call `get_status` repeatedly): the values should follow within a second.

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| power_raw strings seen (W-POWER-STATES) | | | |
| No power_raw, power inferred (W-C7-NO-POWERSTATE) | | n/a | n/a |
| sound_output, with and without the receiver (W-SOUND-OUTPUT) | | | |
| now_playing app and play_state in Netflix, YouTube (W-NOW-PLAYING) | | | |
| input label when on HDMI | | | |

## 6. Power off and Wake-on-LAN

```sh
mcp-server-webos call set_power state=off tv=Bedroom
mcp-server-webos call get_status tv=Bedroom     # power: standby (socket up) or off (gone)
mcp-server-webos call set_power state=on tv=Bedroom
```

- Off: with Quick Start+/Always Ready on, does the TV stay reachable in "Active Standby"?
  Off, does the connection close? (W-STANDBY-SOCKET)
- A second `set_power off` must say "unchanged" and send nothing (W-OFF-GUARD).
- On: time from the call to "is on" in `detail` (W-WAKE-TIME). If `warnings` says unconfirmed,
  check step 2's settings, then wired vs Wi-Fi (W-WOL, W-WOL-PATH).
- From the Alpine LXC as well: `doctor` shows the local address the packet goes out from; it
  must be on the TVs' network (the container bridged, not NAT).
- Nothing other than Wake-on-LAN turns a TV on (W-TURNON-DEAD): don't expect `system/turnOn`.

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| After off: standby with socket, or gone (W-STANDBY-SOCKET) | | | |
| Second off: unchanged (W-OFF-GUARD) | | | |
| Wakes by WoL from this machine (W-WOL) | | | |
| Wakes by WoL from the LXC | | | |
| Seconds to awake (W-WAKE-TIME) | | | |
| Works over Wi-Fi? (W-WOL-PATH) | | | |

## 7. Screen off and on

```sh
mcp-server-webos call set_screen state=off tv="Family Room"   # sound continues, picture off
mcp-server-webos call set_screen state=on tv="Family Room"
```

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| Off: picture blanks, sound continues (W-SCREEN-OFF) | expect "no screen-off command" | | |
| On: picture returns | | | |
| power reads screen_off while blank | | | |

## 8. Sources

```sh
mcp-server-webos call list_sources tv=Office
mcp-server-webos call select_source source=netflix tv=Office
mcp-server-webos call select_source source=Onkyo tv=Office     # your label for the receiver's input
```

- `mcp-server-webos apps --all-tvs` lists every installed app by group. Compare with the defaults
  in `config.py` (W-APP-IDS), check nothing of LG's lands in "Not allowed yet" and nothing you'd
  want is wrongly "Excluded" (W-SYSTEM-APP-IDS), then `--add` what you want.
- Inputs: id `HDMI_n`, your labels, and switching brings `com.webos.app.hdmiN` to the front
  (W-INPUT-IDS).

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| Default app ids that match (W-APP-IDS) | | | 2026-10-14: 5 of 7 (no plex, spotify) |
| `apps` groups LG's apps as system or excluded (W-SYSTEM-APP-IDS) | | | |
| Inputs listed with your labels (W-INPUT-IDS) | | | |
| select_source confirms an app | | | |
| select_source confirms an input | | | |

## 9. Keys and media controls

```sh
mcp-server-webos call send_key key=HOME tv=Bedroom
mcp-server-webos call send_key key=DOWN repeat=3 tv=Bedroom
mcp-server-webos call media_control action=pause tv=Bedroom   # with something playing
```

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| HOME, arrows, ENTER, BACK act (W-KEYS) | | | |
| pause/play act in Netflix (W-MEDIA-CONTROLS) | | | |
| pause/play act in YouTube (W-MEDIA-CONTROLS) | | | |

## 10. Volume and the receiver

With the receiver on eARC/ARC, `get_status` should show `volume_owner: "external"`, and
`set_volume` must refuse, naming the receiver. The refusal exists because the TV forwards volume
to the receiver over HDMI-CEC (W-VOLUME-ARC): check that this is really what happens by pressing
volume on the TV's own remote and watching the receiver.

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| volume_owner as expected (W-SOUND-OUTPUT) | | | |
| TV remote volume moves the receiver's (W-VOLUME-ARC) | | | |
| On TV speakers: set_volume capped at max_volume | | | |

## 11. Reconnects, re-pairing and rediscovery

Run the server (`mcp-server-webos serve --http --insecure-no-auth --bind 127.0.0.1` or under Claude
Code) and watch its log:

- Turn a TV off and on with its own remote: one log line each way, and `get_status` fresh within
  ~10 s of it coming back.
- Stale key while running (as in step 3): **one** dialog on the TV, then `needs_pairing`, and no
  more dialogs however long you wait (W-STALE-KEY). Run `pair` in another terminal: the running
  server reconnects on its own.
- Rediscovery: give a TV a new address (change its DHCP reservation, or set a static IP on the
  TV), wait over a minute with it on. The log should say it was found at a new address, and
  config.json should hold the new one (W-UUID-STABLE, W-SSDP). `mcp-server-webos discover` lists
  what answers SSDP.
- After the C5 takes webOS 26 (step 1): does the old key still work (W-KEY-SURVIVES)? If not,
  `get_status` says needs_pairing; re-pair. Does the TV ever answer with the blacklisted-certificate
  403 (W-SIGNED-403)? It shouldn't: this server never sends a signed manifest.

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| Off/on with its remote: noticed within ~10 s | | | |
| Stale key: one dialog only (W-STALE-KEY) | | | |
| Re-pair picked up without a restart | | | |
| Found at a new address (W-UUID-STABLE, W-SSDP) | | | |
| C5 key survives webOS 26 (W-KEY-SURVIVES) | n/a | n/a | |
| No 403 on webOS 26 (W-SIGNED-403) | n/a | n/a | |

## 12. Home Assistant alongside

Home Assistant keeps its own pairing. With both running, change the volume or input from HA's
media player card and from this server: both should see each other's changes, and neither
should drop (W-MULTI-CLIENT).

| Check | Bedroom C7 | Family Room G1 | Office C5 |
| --- | --- | --- | --- |
| Both connected at once (W-MULTI-CLIENT) | | | |

## 13. HTTP and the Alpine service

In the Alpine LXC:

```sh
sh deploy/alpine/install.sh
su -s /bin/sh mcp-webos -c 'WEBOS_CONFIG_DIR=/var/lib/mcp-server-webos /opt/mcp-server-webos/venv/bin/mcp-server-webos pair --host <ip> --name "Family Room"'
vi /etc/mcp-server-webos/env      # HTTP and Access settings (port 8715, path /webos/mcp)
rc-update add mcp-server-webos default && rc-service mcp-server-webos start
```

Then set up Cloudflare as in the README's "Setting up Cloudflare": the Access application for all
of `mcp.nickandrade.com` with Managed OAuth, and a route for `^/webos/` on the "Dinner Log, Steam,
& Wishlist" tunnel. That tunnel's cloudflared runs as a Docker container in CT 210, so the route
is `http://192.168.1.174:8715` and `MCP_HTTP_BIND=192.168.1.174`; `MCP_PUBLIC_HOSTS` must be set.

| Check | Result |
| --- | --- |
| Service starts, `/healthz` answers | 2026-10-14: yes (CT 210, bound to 192.168.1.174) |
| Through Access from Claude Code | 2026-10-14: yes, after the two fixes below |
| Through Access from the Claude app connector | 2026-10-14: yes (`list_tvs`, `list_sources` on Office TV) |
| Through Access from the mobile app | |
| doctor as the service user passes for all three TVs | |
| Wake-on-LAN from the LXC (step 6) | |

What it took on 2026-10-14, in the order it showed up (the README's "When it doesn't connect"
table has all of them):

1. A route to `http://localhost:8715` gave HTTP 502: cloudflared is a container, so `localhost`
   was the container. Fixed by routing to, and binding on, the LXC's address.
2. Then HTTP 421, `Invalid Host header: mcp.nickandrade.com`: `MCP_PUBLIC_HOSTS` was missing from
   the env file.

## Recording results

For each assumption a step confirmed or contradicted, change its `status` in
`src/webos_mcp/assumptions.py` to `"hardware-verified"` or `"hardware-contradicted"`, and say what
you saw, and on which TV, in `note`. Update the matching row of the README's verification table
(`pytest tests/test_assumptions.py` fails until they agree). A contradicted claim usually means a
change to the simulator profile and to the code that relies on it, in the same commit.
