# mcp-server-webos: Step 1 plan

Status: approved 2026-10-12 and built on the `hardware-free` branch. The plan is kept as written;
"What changed during the build" at the end says where the code differs and why.
Onkyo on `hardware-free`, Harmony and Sofabaton on `main` plus Sofabaton's
`fix-mqtt-shutdown-hang`), aiowebostv 0.9.1, 0.9.2 and 0.10.0 from their wheels,
hernanc/lgtv-mcp, and bassidus/lgpowercontrol's `tests/virtual-webos-tv`.

## 0. What reading the siblings changed

Four findings change the plan.

- **The two shared modules aren't byte-identical across the siblings today.**
  - `remote.py` and `logsafe.py` on Shield TV and Onkyo `hardware-free` match each other.
    Harmony's and Sofabaton's are an older revision, and their two `logsafe.py` files also differ
    from each other.
  - The Shield/Onkyo `remote.py` is the newer one: Host/Origin checks stay on with `0.0.0.0`, and
    LAN IPs are accepted as public hosts. Its `logsafe.py` adds JWT, bearer, PEM and URL-password
    redaction.
  - So webos copies the **Shield/Onkyo `hardware-free`** versions. The patches you have for
    Harmony and Sofabaton will bring them to the same revision.
- **`stop_task()` exists only on Sofabaton's unmerged `fix-mqtt-shutdown-hang` branch**, in
  `mqtt.py`. I'll copy it into a small `tasks.py` here and note where it came from. It isn't a
  shared module yet.
- **aiowebostv's debug log leaks identifiers that `logsafe` doesn't catch.**
  - It logs the raw `getSystemInfo` reply (`serialNumber`) and the hello (`deviceUUID`).
  - The client key never appears in its logs: registration is logged as the bare word
    "registration".
  - `logsafe` doesn't match serial numbers, UUIDs or 32-hex keys (its bare-hex rule only matches
    12 uppercase digits).
  - Decision needed, see the questions at the end.
- **lgpowercontrol is GPL-3.0**, and this repo is MIT. Its virtual TV is a reference for
  *observed facts* (power-state strings seen on an OLED42C35LA, wake times), cited as sources.
  None of its code gets copied.

Two other things I couldn't use:

- **Pixelcade and Batocera repos:** not readable from here (private, or not under that name). Port
  8715 is your decision anyway.
- **lgtv-mcp's tests** mock `WebOsClient` as an object (`tests/fakes.py`), not on the wire. Its
  good ideas, used below:
  - an explicit IPv4 allow-list (not `ipaddress.is_private`, whose meaning has shifted between
    patch releases), which also blocks 169.254.169.254;
  - WoL sent to both the broadcast address and the last known IP;
  - SSDP discovery with only `LOCATION`s on the local network accepted.

## 1. Module layout

Package `webos_mcp`, env prefix `WEBOS_`, console script `mcp-server-webos`.

```
src/webos_mcp/
  config.py       Settings; TVs (name, aliases, host, MAC, deviceUUID, platform); key and app allow-lists;
                  per-TV volume cap and volume_owner override. Loading never raises: problems -> Settings.problems
  keystore.py     keys.json (0600), client keys by deviceUUID. Kept out of config.json so config can be
                  shown, diffed and pasted into an issue without a secret in it
  tvs.py          which TV a call means: names, aliases, "never guess", tv="all" for power off (Onkyo's receivers.py)
  client.py       TvClient: one per TV, a supervised aiowebostv WebOsClient, pushed-state cache, capabilities,
                  needs_pairing, rediscovery by deviceUUID, per-TV action lock
  compat.py       every touch of aiowebostv internals, in one place, with its own contract tests (section 4.2)
  tasks.py        stop_task() from Sofabaton's fix-mqtt-shutdown-hang branch
  wol.py          magic packet, target selection, interface/broadcast report for doctor (stdlib only)
  discovery.py    SSDP M-SEARCH for urn:lge-com:service:webos-second-screen:1, local-network LOCATIONs only
  redact.py       webOS-only redaction layered on logsafe (serials, UUIDs, 32-hex keys); see question 2
  server.py       tools, ActionResult, resources, prompts
  assumptions.py  W-* registry
  doctor.py       layer-by-layer checks
  capture.py      redacted transcripts for contract tests
  cli.py          serve, pair, check, doctor, discover, capture, simulate, call
  remote.py       byte-identical copy (Shield/Onkyo hardware-free revision)
  logsafe.py      byte-identical copy (same)
  sim/
    fake_tv.py    wire-level SSAP TV (aiohttp server) with Faults
    profiles.py   c7, g1, c5-webos25, c5-webos26: what each answers, and the source of each behavior
    fixtures/     captured transcripts (empty until the hardware pass)
deploy/alpine/    install.sh, mcp-server-webos.initd, env.example, run.sh, smoke-test.sh (sibling copies)
docs/             skill-webos.md (cheat sheet for the home-theater Skill), OUT_OF_SCOPE.md, this PLAN.md
tests/            conftest (simulator fixtures), test_wire, test_client, test_tools, test_resources,
                  test_assumptions, test_config, test_logsafe, test_remote, test_http_tools, test_stdio,
                  test_wol, test_discovery, test_compat
```

Dependencies:

- **Runtime:** `mcp>=2.3,<3`, `aiowebostv>=0.9.2,<0.11`, plus `pyjwt[crypto]`, `uvicorn` and
  `typing_extensions` like the siblings. aiohttp comes with aiowebostv and has musl wheels.
- **Nothing new for tests:** the simulator's self-signed cert is generated with `cryptography`,
  which `pyjwt[crypto]` already pulls in.
- **The upper bound on aiowebostv is deliberate.** `compat.py` relies on its internals, so a new
  minor version has to pass `test_compat` before the pin moves.

## 2. Tools

Ten tools. Annotation constants are the Shield's: `_READ`, `_ACT` (not idempotent),
`_ACT_IDEMPOTENT`. Nothing is `destructive_hint=True`: there's no reboot here, and turning a TV
off is reversible.

| Tool | Args | Annotation | Notes |
|---|---|---|---|
| `list_tvs` | none | `_READ` | Names, aliases, model, platform, room, paired, reachable. |
| `get_status` | `tv?` | `_READ` | See the field list below. With no `tv` and several TVs, it returns all of them; read-only tools covering every device is the Onkyo convention. |
| `set_power` | `state: on\|off`, `tv` | `_ACT_IDEMPOTENT` | Off: SSAP `system/turnOff`; `tv="all"` allowed. On: Wake-on-LAN, then wait up to ~15 s for the socket and an awake power state. If it doesn't come, the outcome is still `done`, with "unconfirmed" in `warnings`. Already in that state: `unchanged`, nothing sent. |
| `set_screen` | `state: on\|off`, `tv` | `_ACT_IDEMPOTENT` | `standbyMode: "active"` only (the value is hardcoded, not an argument). Refused with a reason where the capability is missing (likely the C7). |
| `list_sources` | `tv` | `_READ` | Allow-listed apps installed on that TV, plus its HDMI inputs, each with `kind` and `id`. |
| `select_source` | `source`, `tv` | `_ACT_IDEMPOTENT` | An app by friendly name or ID from the allow-list, or an input by label or `HDMI_n`. Success is confirmed by the foreground app (the Shield's lesson: never trust a send). |
| `send_key` | `key: Literal[...]`, `repeat 1-10`, `tv` | `_ACT` | Allow-list (below). Unavailable on a TV without the pointer socket, with a reason. |
| `media_control` | `action: play\|pause\|stop\|rewind\|fast_forward`, `tv` | `_ACT` | SSAP `media.controls/*`, which doesn't need the pointer socket. |
| `set_volume` | `level 0-100`, `tv` | `_ACT_IDEMPOTENT` | Clamped to the per-TV cap. With an external sound output, it's refused unless overridden (below). |
| `set_mute` | `muted: bool`, `tv` | `_ACT_IDEMPOTENT` | Same external-output rule as `set_volume`. |

`get_status` fields:

- `reachable`, `stale`, `as_of`, `error`
- `needs_pairing`, `dry_run`
- `power`: `on|screen_off|standby|off|unknown`, plus `power_raw` (the TV's own string and
  `processing`)
- `foreground_app` (ID and title)
- `input`
- `now_playing`
- `volume`, `muted`
- `sound_output`, `volume_owner`
- `platform` (from the hello)
- `capabilities`, `limitations`

### Now playing

`now_playing` lives inside `get_status`, not in a tool of its own. On webOS, the only now-playing
data is `com.webos.media/getForegroundAppInfo`: the app and a play state, with no title (W-NOW-PLAYING).
A separate tool would cost context for two fields.

### Apps and inputs: one pair of tools

Apps and HDMI inputs share one pair, `list_sources` and `select_source`, as HA's source list does.

- The model doesn't have to know whether "PS5" is an HDMI label or an app. That's exactly the
  kind of guess that goes wrong.
- One fewer tool definition in every client's context.
- "Never guess" applies across both kinds: if "Apple TV" matches an app *and* an input label, the
  call is refused and both are listed.
- Inputs need no allow-list: switching inputs is harmless. Apps do, since an app is the only way
  this server could open arbitrary content.

### Volume

- The volume owner is `receiver` when `sound_output` is `external_arc`, `external_optical` or
  another `external_*` value. In that case `set_volume` and `set_mute` refuse with: "Volume on
  Family Room belongs to the receiver (sound output external_arc). Use the Onkyo server; going
  through the TV would bypass its volume caps."
- A per-TV `volume_owner: "tv"` in config overrides this.
- With TV speakers, the cap applies.

### Keys

Allowed:

- `UP DOWN LEFT RIGHT ENTER BACK HOME EXIT`
- `PLAY PAUSE STOP REWIND FASTFORWARD`
- `INFO 0-9 RED GREEN YELLOW BLUE`
- `CHANNELUP CHANNELDOWN`, for a source that maps them

Left out, with the reason published in `webos://keys` the way the Shield publishes `shieldtv://keys`:

- `POWER`: a toggle; `set_power` is discrete.
- `IN_START`, `EZ_ADJUST`: service menus.
- `MENU`, `QMENU`, `ASPECT_RATIO`, `SCREEN_REMOTE`: settings and system overlays.
- `VOLUMEUP`, `VOLUMEDOWN`, `MUTE`: they would bypass the volume rule.
- `NETFLIX`, `AMAZON`: they would bypass the app allow-list.
- `INPUT_HUB`, `MYAPPS`, `RECENT`, `3D_MODE`, `RECORD`, `LIST`, `PROGRAM`, `GUIDE`, `DASH`: these
  belong to Live TV or the home dashboard, and channels are out of scope.

### Resources and prompts

- **Resources:**
  - `webos://tvs`: configured TVs and capabilities, redacted.
  - `webos://keys`.
  - `webos://apps`: the allow-list and the source of each entry.
- **One prompt:** `watch(app, tv)`. Cross-device workflows belong in the Skill.

### Errors and ActionResult

- `WebOsError(ToolError)`, with an `Unreachable` subclass, as in Onkyo.
- `ActionResult = {tv, outcome: done|unchanged|dry_run, detail, sent, warnings}`.
- `sent` lists the SSAP URIs, the button frames, or `wol <mac> -> <target>`.

## 3. Assumptions (W-*)

Confidence is defined as in the siblings.

- **high:** seen on hardware, or what aiowebostv does that HA's install base depends on.
- **medium:** another project or LG's docs.
- **low:** our guess.

All start `simulator-only`.

| Id | Claim | Source | Conf. |
|---|---|---|---|
| W-MANIFEST | The unsigned manifest (all permissions in the outer list) pairs on webOS 3.5, 6, 25 and 26. | aiowebostv 0.9.2 handshake.py diff; HA core #172703 (webOS 26 fixed, "no action required" reports) | high (26), medium (3.5/6/25) |
| W-SIGNED-403 | webOS 26 rejects the old signed manifest with `403 Pairing rejected: blacklisted certificate detected`, before any prompt. | HA core #172703 captures; lgpowercontrol v4.4 | high |
| W-KEY-SURVIVES | A key paired on webOS 25 still works after the C5 takes webOS 26. | HA #172703 (contradictory reports) | low |
| W-SWINFO-401 | `getCurrentSWInformation` is 401 for an unsigned key on webOS 26; aiowebostv ≥0.9.2 suppresses it. | HA #172703 (G5 11.2.0); aiowebostv 0.9.2 diff | high (26), low (others) |
| W-HELLO-VERSION | The hello carries `deviceOSReleaseVersion` (11.x on webOS 26, 10.x on 25), plus `deviceUUID` and `pairingTypes`. | HA #172703 debug log (G5) | high (26), low (C7, G1) |
| W-PORTS | The C7 answers ws:3000 only; the G1 and C5 answer wss:3001 only. | LGWebOSRemote (TLS required from Jan 2023 firmware, C1 listed as ssl); RTI driver notes | medium |
| W-PORT3000-CLOSE | Newest firmware accepts TCP on 3000, then closes, so aiowebostv falls back. A silent drop would *not* fall back: it only falls back on refused or failed handshakes, not timeouts. | HA #172703 (C6 43.21.60); aiowebostv `_create_main_ws` | medium (behavior), low (C5) |
| W-PAIR-WINDOW | The PROMPT flow waits `RECEIVE_TIMEOUT` = 10 s for acceptance on the TV. | aiowebostv 0.10.0 `_check_registration` | high |
| W-STALE-KEY | An unknown key gets `pairingType: PROMPT`, i.e. the dialog shows again. | LG SSAP behavior as used by every client | medium |
| W-STALE-TIMEOUT | aiowebostv reports an unanswered prompt as plain `TimeoutError`, and `tv_info.hello` stays set after a failed connect. | aiowebostv 0.10.0 source | high |
| W-INPUT-SOCKET | `getPointerInputSocket` is granted to an unsigned-manifest key on all three. | HA #172703 (G5 table, C2 button test); LGTVCompanion #351 (401 before moving permissions out) | medium |
| W-SUBSCRIBE | All eight state subscriptions succeed or return 404; any 401 fails `connect()`. | aiowebostv source | high (code), medium (TVs) |
| W-POWER-STATES | Power state strings: `Active`, `Screen Off`, `Screen Saver`, `Active Standby`, `Suspend` (+ `Power Off`), plus `processing: "Screen On"` while waking. | lgpowercontrol virtual TV (OLED42C35LA, 2026-08-09); aiowebostv `_is_tv_on` | high (newer), low (C7) |
| W-C7-NO-POWERSTATE | The C7 answers 404 to `getPowerState`; power is inferred from the foreground app. | aiowebostv 0.7.2/0.7.3 "older webOS" fixes | low |
| W-STANDBY-SOCKET | In Active Standby (Quick Start+ / Always Ready), the socket stays up and reports standby. | aiowebostv state handling; lgpowercontrol notes (marked GUESSED there for deep standby) | low |
| W-WOL | A magic packet to UDP 9 wakes each TV once "TV On With Mobile" / "Mobile TV On" / "Turn on via Wi-Fi" is on. | HA webostv docs; lgtv-mcp; lgpowercontrol | medium |
| W-WOL-PATH | The C7 needs Ethernet and LG Connect Apps; the G1 and C5 also wake over Wi-Fi. | HA webostv docs (webOS 3 era) | low |
| W-WAKE-TIME | About 4 s from Active Standby and 5 s from deep standby until awake. | lgpowercontrol (observed on a C3) | medium |
| W-TURNON-DEAD | `system/turnOn` doesn't wake newer webOS, so WoL is the only power-on. | bscpylgtv docstring | medium |
| W-OFF-GUARD | `system/turnOff` on a TV already in standby may toggle it on, which is why aiowebostv's `power_off()` returns early. | aiowebostv comment | low |
| W-MAC-SOURCE | `connectionmanager/getinfo` (aiowebostv `tv_info.connection`) gives the MAC of the interface in use. | aiowebostv 0.10.0 reads it; shape unknown | low |
| W-UUID-STABLE | `deviceUUID` stays the same across IP changes, reboots and firmware updates. | none seen; the rediscovery rule depends on it | low |
| W-SSDP | An awake TV answers M-SEARCH for `urn:lge-com:service:webos-second-screen:1`, with LOCATION on its own IP. | lgtv-mcp discovery.py; HA's webostv SSDP matcher | medium |
| W-SCREEN-OFF | `com.webos.service.tvpower/power/turnOffScreen` on webOS 5+ (`com.webos.service.tv.power/...` on 4.x), not on 3.5; only `standbyMode: "active"` is reversible. | aiowebostv/bscpylgtv endpoints; bscpylgtv docstring | medium (G1, C5), low (C7) |
| W-KEYS | The allow-listed button names act on all three; IN_START and EZ_ADJUST open service menus. | aiowebostv buttons.py; bscpylgtv buttons.py | medium |
| W-MEDIA-CONTROLS | `media.controls/play` and the rest act on streaming apps (Netflix, YouTube) as well as the built-in player. | none | low |
| W-SOUND-OUTPUT | `sound_output` reads `external_arc` with the receiver on eARC, and `tv_speaker` without it. | aiowebostv (`external_arc` step delay); lgtv-mcp fakes | medium |
| W-VOLUME-ARC | With `external_arc`, TV volume commands go to the receiver over CEC, which is why they'd bypass its caps. | aiowebostv's step delay for `external_arc` | medium |
| W-NOW-PLAYING | `com.webos.media/getForegroundAppInfo` gives the app and a play state, but no title. | aiowebostv models; lgtv-mcp | medium |
| W-APP-IDS | Default allow-list IDs (e.g. `netflix`, `youtube.leanback.v4`, `amazon`) match `listLaunchPoints` on each TV. | lgtv-mcp fakes; to be replaced by captures | low |
| W-INPUT-IDS | Inputs are `HDMI_n` with appId `com.webos.app.hdmiN`, a user label, and `connected`. | lgtv-mcp fakes; HA | medium |
| W-MULTI-CLIENT | HA and this server can both be connected to the same TV at once. | none | medium |
| W-TLS-CERT | The wss certificate is self-signed and stable across reboots. Firmware updates may change it. | none | low |

## 4. Answers to the four questions

### 4.1 The reconnect supervisor

There's one supervisor task per TV, modeled on `ShieldClient`'s `_connect_forever` and
`_watchdog`.

- **While the TV is off:**
  - The supervisor tries `connect()` every **10 s** with ±2 s jitter.
  - An attempt against an off TV costs one TCP SYN that times out after aiowebostv's 2 s
    `CONNECT_TIMEOUT`, so this costs nothing measurable.
  - 10 s means a TV turned on by Harmony's IR shows up within about 10 s.
  - The interval doesn't back off further. A longer interval would mostly make the Skill's
    "check status first" rule return stale answers.
- **After a drop from connected:** reconnect at 1, 2, 4, 8 s, then settle at the 10 s cadence. A
  drop is usually the TV turning off, but sometimes a firmware hiccup.
- **Logging:** one INFO line per state transition:
  - `Office: reachable -> unreachable (connection closed)`
  - `Office: unreachable -> reachable (webOS 10.3.1, wss:3001)`
  - `Office: needs pairing`

  Repeated failures in the same state are DEBUG only.
- **When the TV is stale,** `get_status` does one bounded refresh: a single connect attempt capped
  at 3 s, and only if the last attempt is more than 5 s old. It then answers with `stale`, `as_of`
  and `error`, like the Shield. A model calling `get_status` right after `set_power` gets fresh
  data, and ten parallel calls don't cause ten connects.
- **Stale key:** this is the case that would pop dialogs on the TV.
  - The supervisor treats `TimeoutError` with `tv_info.hello` set, after ≥ ~10 s
    (W-STALE-TIMEOUT), as "the TV is showing a pairing prompt".
  - It then sets `needs_pairing` and **stops retrying that TV entirely** until `pair` writes a new
    key.
  - The watchdog notices the new key through `keys.json`'s mtime, the Shield's credential-stamp
    trick.
  - `WebOsTvPairError` (prompt declined, or 403 blacklisted) does the same.
  - It never sends a registration without a key. A TV with no key is `needs_pairing` from the
    start.
- **Rediscovery:**
  - Trigger: the TV has been unreachable for 60 s and the last search was more than 60 s ago.
  - Search: an SSDP M-SEARCH.
  - Match: a candidate's hello `deviceUUID` must equal the saved one, and only then does the new
    address replace the old one in `config.json`.
  - A different UUID at the old address is an error, never adopted. That includes the TV's IP now
    belonging to another TV.

### 4.2 A TV that refuses the pointer socket

aiowebostv's `connect_handler` calls `_create_input_ws()` unconditionally, so a 401 there fails the
whole connection. Options:

1. **Live with it.** One permission denial takes down status, power-off and everything else on that
   TV.
2. **A narrow subclass in `compat.py`** (recommended).
   - Override only `_create_input_ws`. On a 401, return a stand-in whose `async for` waits on an
     Event, whose `close()` sets it, and whose `send_str` raises.
   - `connect_handler`'s `asyncio.wait(FIRST_COMPLETED)` keeps the main socket alive, because the
     stand-in never ends on its own.
   - The capability `keys` drops out and a limitation says why. `media_control` still works.
   - Costs:
     - It touches one private method. So the pin is `<0.11`.
     - `test_compat` asserts the internals it relies on: the method exists with that signature,
       `connect_handler` adds a receive task for it, and `self.input_connection` is what
       `button()` uses.
     - A new aiowebostv fails CI loudly rather than at runtime.
3. **Upstream:** a PR to aiowebostv making the input socket optional (a constructor flag). It's the
   real fix, and it's worth sending regardless. Option 2 can be deleted once it lands.

Recommendation: 2 now, with 3 in parallel. Expected to be dormant: aiowebostv ≥0.9.2 already uses
the manifest that LGTVCompanion found restores the socket (W-INPUT-SOCKET).

### 4.3 TLS pinning

- **Enforcing it isn't possible.** aiowebostv passes `ssl=False` on each `ws_connect`, which
  overrides any connector setting, so pinning can't be enforced without patching `_ws_connect`.
- **Detecting a change is possible.** `client.connection` (public) exposes
  `get_extra_info("ssl_object").getpeercert(binary_form=True)`, and the DER bytes are there even
  when the certificate wasn't verified (checked against aiohttp 3.14).
- **The limit of detection:** by the time we can see the certificate, the registration, and the
  key in it, has already crossed that connection. So it detects a swapped device; it doesn't stop
  a key from being captured.
- **The threat model:** a LAN-only device, and the C7 has no TLS at all on port 3000.

Plan:

- `pair` records the SHA-256 fingerprint.
- The client compares it on each connect. A mismatch is a warning in `get_status` and `doctor`;
  it doesn't block, since firmware updates may legitimately change it (W-TLS-CERT).
- `doctor` prints it.
- Revisit if the hardware pass shows the certificate is stable across updates.

### 4.4 One source tool pair

Yes: `list_sources` and `select_source` (section 2 has the reasoning).

## 5. Capabilities per TV (expected; HARDWARE_VALIDATION confirms each)

| | Bedroom C7 (OLED65C7P-U) | Family Room G1 (OLED77G1PUA) | Office C5 (OLED65C5PUA) |
|---|---|---|---|
| Platform | webOS 3.5 | webOS 6.0 | webOS 25 (33.31.61 per HA); webOS 26 rolling out |
| Control port | ws:3000 | wss:3001 | wss:3001 |
| Version source | hello (field unverified) | hello (field unverified) | hello (`deviceOSReleaseVersion` 10.x / 11.x) |
| Power state | inferred from foreground app | `getPowerState` | `getPowerState` |
| Standby with socket up | unknown | Quick Start+ | Always Ready / Quick Start+ |
| Power on | WoL, wired + LG Connect Apps likely | WoL ("Mobile TV On") | WoL ("Turn on via Wi-Fi") |
| Screen off | probably not | `tvpower` endpoint | `tvpower` endpoint |
| Keys (pointer socket) | yes | yes | yes on 25; expected on 26 |
| Software info | probably | probably | 25: yes; 26: 401 |
| Sound output | tv_speaker or ARC (unknown) | likely external_arc | likely external_arc |

Capabilities are computed per TV from the hello plus what actually answered:

- `getPowerState` 404 → `power_state: inferred`
- the screen-off endpoint 404 → no `screen`
- the input-socket 401 → no `keys`
- `sound_output` → `volume_owner`

The model year is never used to decide what's supported. The C7 isn't refused screen-off because
it's from 2017, but because it didn't answer.

## 6. Simulator

- **A real server:** `sim/fake_tv.py` is an aiohttp WebSocket server that the real aiowebostv
  connects to. Its behaviors:
  - `hello`;
  - `getSystemInfo` before registration;
  - registration: stored-key accept, PROMPT, accept/decline/timeout;
  - request/response by id, and `subscribe` with pushed updates;
  - error replies in the TV's exact shapes: `401 insufficient permissions`,
    `404 no such service or method`, and the 403 blacklisted pairing;
  - the pointer socket at the `socketPath` it hands out.
- **Profiles** set per-generation behavior. Each behavior names its source, as the Shield's
  simulator does.
- **Real ports, no monkeypatching:**
  - Each fake TV binds the real ports 3000 and/or 3001 on its own loopback address: 127.0.0.2 C7,
    .3 G1, .4 C5-25, .5 C5-26.
  - Linux routes all of 127/8 to `lo`. CI (ubuntu, and the Alpine container) and your WSL2 Gentoo
    are all Linux.
  - Tests that need parallel simulators take addresses from a pool.
  - The WoL listener uses a configurable high port. `wol.py` takes the port from settings, so
    tests don't need root for port 9.
- **TLS:** the wss certificate is generated at test time with `cryptography` (no checked-in key).
- **A power state machine** with the W-POWER-STATES strings:
  - WoL from standby → `processing: "Screen On"` → Active after a configurable delay (default
    4 s, 0.1 s in tests);
  - `turnOff` → Active Standby with the socket up, or a closed socket, per profile.
- **Faults:**
  - `refuse_3000`, `close_after_accept_3000`, `blackhole_3000` (which proves aiowebostv does *not*
    fall back on a timeout)
  - `blacklist_signature`
  - `pair_decline`, `pair_timeout`, `stale_key_prompts`
  - `deny_pointer_socket` (401), `deny_subscription=<uri>`
  - `drop_after=<n>`, `slow=<s>`, `garble_next`
- **`capture` and contract tests:** `capture` records redacted request/response pairs from a real
  TV. Each profile then has a contract test that replays the captured requests against the fake
  and compares response *shapes* (keys and types, not values). Until there are captures, those
  tests are skipped with a reason, not passing silently.

## 7. CLI

| Command | Behavior |
|---|---|
| `pair --host IP --name NAME` | Says "accept on the TV within 10 s", then saves the key (keys.json 0600), deviceUUID, MAC, platform version and cert fingerprint. Run as the service user. Retries once on timeout, with a prompt to try again. |
| `check` | Picks the TV and prints its platform, capabilities and limitations (Sofabaton's `check`). |
| `doctor [--json] [--dump DIR] [--tv NAME]` | Layer by layer: config → TCP 3000/3001 → TLS + fingerprint → WebSocket upgrade → hello → registration with the stored key → read-only permission probe → WoL readiness (MAC known, interface, broadcast address). Never pairs, presses, launches, wakes or powers off. If the stored key draws a PROMPT, it closes immediately and reports `needs_pairing` (HARDWARE_VALIDATION checks no dialog is left on screen). Each failure points to a HARDWARE_VALIDATION step number. |
| `discover` | SSDP, local-network results only. |
| `capture --tv NAME --out DIR` | Read-only requests only; redacted. |
| `simulate --profile c7\|g1\|c5-webos25\|c5-webos26 [--paired] [--config-dir DIR]` | As in the siblings. |
| `call <tool> key=value` | One tool through the MCP layer. |
| `serve [--http] [--dry-run]` | Port 8715, path `/webos/mcp`; `remote.py` behavior unchanged. |

## 8. Commit plan (feature branch `hardware-free`, one commit per step)

1. Skeleton: pyproject, package, `remote.py` and `logsafe.py` (byte-identical), CI workflow, LICENSE, `.gitignore`
2. `assumptions.py` and its consistency test (an empty HARDWARE_VALIDATION table to start)
3. `config.py`, `keystore.py`, `tvs.py`: the never-guess rule, aliases, `all`
4. `sim/fake_tv.py` with the four profiles; `test_wire`: real aiowebostv pairs, connects, subscribes
5. `compat.py` (tolerant input socket) and `test_compat`
6. `client.py`: supervisor, cache, capabilities, stale-key handling, rediscovery, `tasks.stop_task`
7. `wol.py`, `discovery.py`
8. `server.py`: tools, ActionResult, dry run
9. Resources and the prompt
10. CLI: `pair`, `check`, `discover`, `simulate`, `call`
11. `doctor`, `capture`, contract-test harness
12. `redact.py` (or a shared logsafe change; see question 2)
13. Deploy: Alpine/OpenRC and the smoke test in CI
14. Docs: README, CLAUDE.md, HARDWARE_VALIDATION.md (one column per TV, C5 webOS 25 capture first), `docs/skill-webos.md`, `docs/OUT_OF_SCOPE.md`

## 9. Questions before I start

1. **Seeding `main`:** the repo is empty, so a PR needs a base. May I push one commit to `main`
   with LICENSE, a stub README and `.gitignore`, and put everything else on `hardware-free` as a
   draft PR?
2. **Where the webOS redactions live:** `logsafe.py` doesn't catch serial numbers, UUIDs or 32-hex
   keys.
   - (a) A webOS-only `redact.py` layered on top, so the shared file stays identical (my
     recommendation for now).
   - (b) Add generic rules to the shared `logsafe.py` and sync all five repos.
3. **TV names:** "Family Room" (G1), "Office" (C5), "Bedroom" (C7), with the model names as
   aliases. Those are from HA areas; tell me if any is wrong.

Answers (2026-10-12): 1, yes, seed `main`; 2, `redact.py`; 3, yes, those names. Pixelcade and
Batocera were made public afterwards: neither has an HTTP transport, so 8715 is free.

## What changed during the build

- **Stale-key detection (4.1):** instead of inferring it from `tv_info.hello` after a
  `TimeoutError`, `compat.py` replaces `_check_registration` too. Owning that exchange tells the
  cases apart exactly, fails in 2 s rather than 10, keeps the TV's own error text (webOS 26's 403),
  checks the deviceUUID before the key is sent, and lets `pair` wait 60 s instead of
  aiowebostv's 10. It costs one more private method under the same contract tests.
- **Assumptions:** 34, not 33. Added W-REGISTERED-DIRECT and W-PROMPT-DISMISS (what the new
  registration relies on), dropped W-STALE-TIMEOUT (no longer relied on), and W-PAIR-WINDOW
  now says how long the TV keeps its dialog up.
- **Commit order:** the Wake-on-LAN, probe and discovery modules came before the client, which
  uses them, and the redaction layer before the CLI; resources and the prompt landed with the tools.
- **Probe:** a small keyless-hello module (`probe.py`) that pair, doctor and rediscovery share.
- **Volume:** `set_mute` follows the same receiver-owns-volume rule as `set_volume`.

