"""Every protocol detail this server relies on, and whether a real TV has confirmed it.

Why a registry instead of comments: a comment saying "unverified" is easy to
miss and never goes away. Here each claim has an id that:
  - code cites next to where it depends on it (``ASSUMPTION W-PORTS``),
  - HARDWARE_VALIDATION.md cites in the step that confirms it,
  - the README's verification table lists, with its status,
  - ``mcp-server-webos doctor`` prints.

tests/test_assumptions.py fails if they drift apart.

Nothing here has been seen on the owner's TVs yet (a 2017 C7, a 2021 G1 and a
2025 C5): the whole server was built away from them. Every claim is what the
simulator (sim/fake_tv.py) implements, and the source says where it came from.

To record a hardware result, change ``status`` to "hardware-verified" (or
"hardware-contradicted", with what you saw in ``note``) and commit it. Several
claims differ per TV; ``note`` says which TV a result was seen on.

Confidence is about the *claim*, judged from its source:
  high   - what aiowebostv (used by Home Assistant against a large install base)
           does and depends on, or a capture someone published from real hardware
  medium - from another project or from LG's documentation, not seen here
  low    - our own guess, or reports that contradict each other; the simulator
           implements it but nothing confirms it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]


@dataclass(frozen=True)
class Assumption:
    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""


ASSUMPTIONS: tuple[Assumption, ...] = (
    # --- pairing ------------------------------------------------------------------------
    Assumption(
        "W-MANIFEST",
        "An unsigned registration manifest (every permission in the outer list, no signatures block) pairs on "
        "webOS 3.5, 6, 25 and 26, and grants what this server uses.",
        "aiowebostv 0.9.2 switched to it (diff of handshake.py 0.9.1 -> 0.9.2); Home Assistant core issue "
        "#172703 reports it working on webOS 26 sets (G5, C2, G3) with no other change",
        "high",
        note="Paired on all three TVs (2026-10-16/17: the C7 on 3.9.3, the G1 on 6.5.3, the C5 on webOS 25), "
        "registered later without a dialog, and granted everything used here. Only webOS 26 is left: the C5 "
        "hasn't taken it yet.",
    ),
    Assumption(
        "W-SIGNED-403",
        "webOS 26 rejects the old signed manifest (appId com.lge.test plus LG's test-signing signature) with "
        "the error '403 Pairing rejected: blacklisted certificate detected', before showing any dialog.",
        "HA core issue #172703 (websocket captures from a G5 on 11.2.0); lgpowercontrol v4.4 release notes",
        "high",
        note="Matters only for the C5 after it takes webOS 26. This server never sends a signed manifest; the "
        "simulator's c5-webos26 profile rejects one so a regression would show.",
    ),
    Assumption(
        "W-KEY-SURVIVES",
        "A client key paired while the C5 runs webOS 25 still works after it updates to webOS 26.",
        "HA core issue #172703: contradictory reports (one G5's pre-update key was rejected; other users needed "
        "no re-pairing)",
        "low",
        note="If it doesn't survive, the server reports needs_pairing (W-STALE-KEY) rather than looping.",
    ),
    Assumption(
        "W-REGISTERED-DIRECT",
        "A registration carrying a client key the TV knows is answered with type 'registered' at once, with no dialog.",
        "aiowebostv's _check_registration only waits for a second message after a PROMPT response",
        "high",
        status="hardware-verified",
        note="2026-10-16: the G1's doctor run says 'registered without a dialog', and the C5's service reconnects "
        "without one. compat.py also accepts a PROMPT followed promptly by 'registered', in case a TV does that.",
    ),
    Assumption(
        "W-STALE-KEY",
        "A registration carrying a key the TV doesn't know is answered with pairingType PROMPT, and the TV "
        "shows the pairing dialog.",
        "how every SSAP client pairs (aiowebostv, bscpylgtv, LGTVCompanion): the PROMPT flow is the only one",
        "medium",
        note="The server treats it as 'needs pairing' and stops retrying, so it can't keep showing dialogs.",
    ),
    Assumption(
        "W-PROMPT-DISMISS",
        "Closing the connection while the pairing dialog is up removes the dialog from the screen.",
        "none: a guess, and what doctor and the server rely on when a stored key is refused",
        "low",
        note="If the dialog stays, it's one dialog, not a loop; HARDWARE_VALIDATION step 3 checks.",
    ),
    Assumption(
        "W-PAIR-WINDOW",
        "The TV keeps the pairing dialog up for at least 30 s, longer than aiowebostv's own 10 s wait; `pair` "
        "waits up to 60 s.",
        "none for the TV's side; aiowebostv 0.10.0 waits RECEIVE_TIMEOUT = 10 s",
        "low",
    ),
    Assumption(
        "W-HELLO-VERSION",
        "The unauthenticated hello reply carries deviceOSReleaseVersion (6.x on webOS 6, 10.x on webOS 25, 11.x on "
        "webOS 26), "
        "deviceUUID and pairingTypes, so the platform is known without a key.",
        "HA core issue #172703 debug log (a G5 on webOS 26: 11.2.0); a C5 owner there reports 10.3.1 on webOS 25",
        "high",
        status="hardware-verified",
        note="2026-10-16: the C5 says 10.3.1 and the G1 6.5.3, each with its deviceUUID and pairing types (G1: PIN, "
        "PROMPT, COMBINED). 2026-10-17: the C7 (sold as webOS 3.5) says 3.9.3.",
    ),
    Assumption(
        "W-SWINFO-401",
        "com.webos.service.update/getCurrentSWInformation answers 401 to an unsigned-manifest key (not only on "
        "webOS 26: the C7 on webOS 3.5, the G1 on webOS 6 and the C5 on webOS 25 do too); aiowebostv >=0.9.2 "
        "ignores that, and this "
        "server never needs it.",
        "HA core issue #172703 (G5, 11.2.0); aiowebostv 0.9.2's suppress() around get_software_info; the Office C5's "
        "and Family Room G1's captures",
        "high",
        status="hardware-verified",
        note="2026-10-16, Office C5 on webOS 25 (10.3.1): '401 insufficient permissions', and get_status worked "
        "regardless; the Family Room G1 on webOS 6.5.3 the same. This used to say webOS 26 only; the contract "
        "test caught it. 2026-10-17, the Bedroom C7 on webOS 3.5: the same. So the unsigned manifest never had "
        "this permission, on any webOS version.",
    ),
    Assumption(
        "W-UUID-STABLE",
        "deviceUUID in the hello stays the same across address changes, reboots and firmware updates, so it "
        "identifies the TV.",
        "none seen; Home Assistant identifies webOS TVs by it",
        "medium",
        note="Rediscovery and the 'same TV at this address' check depend on it.",
    ),
    # --- transport ----------------------------------------------------------------------
    Assumption(
        "W-PORTS",
        "The C7 answers plain WebSocket on port 3000 only; the G1 answers both plain WebSocket on 3000 and TLS "
        "on 3001, and aiowebostv uses 3000 when it is open; the C5 answers TLS on 3001 only.",
        "LGWebOSRemote README (TLS required from January 2023 firmware; a C1 listed as ssl); an RTI driver's "
        "notes (3000 only for 2014-2017 sets); the Family Room G1's doctor run",
        "medium",
        note="2026-10-16, Family Room G1 (webOS 6.5.3): 3000 and 3001 both open, and the session registered on "
        "3000. This used to say 3001 only for the G1. So on the G1 the client key crosses the LAN unencrypted, as "
        "it does for Home Assistant. The C5's capture has its session on 3001, as claimed; whether its 3000 "
        "refuses or accepts-then-closes isn't known yet (W-PORT3000-CLOSE). The C7 is not checked yet.",
    ),
    Assumption(
        "W-PORT3000-CLOSE",
        "The newest firmware accepts a TCP connection on 3000 and then closes it, which aiowebostv treats as a "
        "reason to try 3001. A port that accepts and then says nothing is not: aiowebostv only falls back on a "
        "refused connection or a failed handshake, never on a timeout.",
        "HA core issue #172703 (a 2026 C6 on 43.21.60); aiowebostv 0.10.0 _create_main_ws",
        "medium",
        note="The library's half is certain (the simulator's blackhole test shows it). The C5's own behavior "
        "on 3000 is unknown.",
    ),
    Assumption(
        "W-TLS-CERT",
        "The TV's TLS certificate on 3001 is self-signed and the same across reboots; a firmware update may change it.",
        "none: aiowebostv connects with verification off because the certificate isn't trusted",
        "low",
        note="The server only warns when the fingerprint `pair` saved changes; it never blocks on it.",
    ),
    Assumption(
        "W-MULTI-CLIENT",
        "Home Assistant and this server can be connected to the same TV at the same time.",
        "none seen; LG's own phone app works alongside Home Assistant",
        "medium",
    ),
    # --- what connect() needs -----------------------------------------------------------
    Assumption(
        "W-INPUT-SOCKET",
        "getPointerInputSocket (needed for button presses) is granted to an unsigned-manifest key on all three TVs.",
        "HA core issue #172703 (a G5 on webOS 26 and a C2 pressing buttons); LGTVCompanion issue #351 (401 on "
        "43.x firmware until permissions moved to the outer list, which is what aiowebostv 0.9.2 sends)",
        "medium",
        status="hardware-verified",
        note="Granted on all three: the G1 (doctor: 'buttons allowed'), the C5 and C7 (their captures), and the C5"
        " pressed RIGHT in Netflix (2026-10-17). If one refuses it, compat.py keeps the connection without "
        "buttons instead of failing it all.",
    ),
    Assumption(
        "W-SUBSCRIBE",
        "The eight state subscriptions aiowebostv opens on connect each succeed or answer 404; any other error "
        "(e.g. 401) fails the whole connection.",
        "aiowebostv 0.10.0 _get_states_and_subscribe_state_updates (only WebOsTvServiceNotFoundError is suppressed)",
        "high",
        status="hardware-verified",
        note="All three TVs connect with all eight subscriptions (2026-10-17). The C7 and G1 answer 404 to one of "
        "them (com.webos.media/getForegroundAppInfo) and connect anyway; no TV answered any of them with "
        "another error.",
    ),
    Assumption(
        "W-HELLO-1008",
        "A TV that is still starting can accept the WebSocket and close it with code 1008 before answering the "
        "hello; the next attempt a moment later works.",
        "the Office C5's service log, 2026-10-17 (aiohttp: 'Received message 8:1008 is not WSMsgType.TEXT')",
        "medium",
        status="hardware-verified",
        note="Seen once on the C5, probably during a Wake-on-LAN (the log had no timestamps then). It reached "
        "the supervisor as an unexpected exception and cost a 10 s wait; compat.HANDSHAKE_CLOSED now makes it an "
        "ordinary failed attempt, retried at the wake's 1 s pace.",
    ),
    # --- power --------------------------------------------------------------------------
    Assumption(
        "W-POWER-STATES",
        "getPowerState reports one of Active, Screen Off, Screen Saver, Active Standby, Suspend (or Power Off), "
        "with processing 'Screen On' while waking.",
        "lgpowercontrol's virtual TV (strings observed on an OLED42C35LA, 2026-08); aiowebostv _is_tv_on",
        "high",
        note="Seen 2026-10-17: Active (all three), Screen Saver (C5), Screen Off (C7), Active Standby (C5, just "
        "before it closed the connection). Suspend, Power Off and the waking 'Screen On' are not seen yet.",
    ),
    Assumption(
        "W-C7-NO-POWERSTATE",
        "The C7 answers 404 to getPowerState; aiowebostv then treats the TV as on whenever an app is in front.",
        "aiowebostv 0.7.2 and 0.7.3 'power state for older webOS' fixes",
        "low",
        status="hardware-contradicted",
        note="2026-10-17, Bedroom C7 (webOS 3.5): it answers getPowerState like the newer TVs. The fallback "
        "(power inferred from the foreground app) stays for older sets that really lack the call; the tests "
        "reach it with a fault (conftest.no_power_state), not the c7 profile.",
    ),
    Assumption(
        "W-STANDBY-SOCKET",
        "With Quick Start+ (Always Ready on newer sets), the WebSocket stays up after power off and reports "
        "Active Standby; without it the TV closes the connection.",
        "aiowebostv's state handling; lgpowercontrol marks deep-standby behavior as guessed",
        "low",
        note="2026-10-17, Office C5 with Always Ready off: it reported Active Standby and then closed the "
        "connection, as claimed for 'without it'. Not seen with Always Ready on.",
    ),
    Assumption(
        "W-OFF-GUARD",
        "system/turnOff sent to a TV that is already in standby can turn it back on, which is why aiowebostv's "
        "power_off() does nothing unless it thinks the TV is on.",
        "aiowebostv power_off() comment",
        "low",
        note="The server checks first too, and reports 'unchanged'.",
    ),
    Assumption(
        "W-TURNON-DEAD",
        "system/turnOn doesn't wake newer webOS TVs, so Wake-on-LAN is the only way to turn one on.",
        "bscpylgtv's power_on docstring",
        "medium",
    ),
    Assumption(
        "W-WOL",
        "A magic packet to UDP port 9 wakes each TV once its 'TV On With Mobile' / 'Mobile TV On' / 'Turn on "
        "via Wi-Fi' setting is on.",
        "Home Assistant webostv docs; lgtv-mcp; lgpowercontrol",
        "medium",
        note="2026-10-17: the Office C5 wakes, wired, twice out of two. The G1 and C7 are not tried yet.",
    ),
    Assumption(
        "W-WOL-PATH",
        "The C7 wakes only over Ethernet with LG Connect Apps on; the G1 and C5 also wake over Wi-Fi.",
        "Home Assistant webostv docs (webOS 3 era)",
        "low",
    ),
    Assumption(
        "W-WAKE-TIME",
        "A TV is awake about 4 s after a magic packet from Active Standby, and about 5 s from deep standby.",
        "lgpowercontrol (measured on a C3)",
        "medium",
        status="hardware-contradicted",
        note="2026-10-17, Office C5 (wired, Always Ready off): the picture was back about 10 s after the magic "
        "packet, and set_power reported it awake after 17 and 18 s. Part of that gap was a server bug (a "
        "close before the hello, W-HELLO-1008, waited 10 s instead of retrying at once). set_power now waits "
        "45 s.",
    ),
    Assumption(
        "W-MAC-SOURCE",
        "connectionmanager/getinfo (aiowebostv tv_info.connection) carries the TV's MAC addresses, as "
        "wiredInfo/wifiInfo macAddress, with a state saying which is connected.",
        "aiowebostv 0.10.0 reads the endpoint; the reply's shape is a guess",
        "low",
        note="`pair --mac` sets it by hand if this is wrong.",
    ),
    Assumption(
        "W-SCREEN-OFF",
        "com.webos.service.tvpower/power/turnOffScreen (com.webos.service.tv.power/turnOffScreen on webOS 4) "
        "blanks the screen with standbyMode 'active', and turnOnScreen undoes it; standbyMode 'passive' can't be "
        "undone without unplugging the TV.",
        "aiowebostv and bscpylgtv endpoints; bscpylgtv's turn_screen_off docstring for 'passive'",
        "medium",
        note="2026-10-17: on the C7 (webOS 3.9.3) and the C5 (webOS 25) the first endpoint blanked the picture "
        "with the sound on and turnOnScreen brought it back; on the C7 getPowerState read 'Screen Off'. It "
        "was expected to be absent on the C7. The G1 is not tried yet. A TV with neither endpoint gets a "
        "clear refusal (tests: conftest.no_screen_off). 'passive' is never sent.",
    ),
    # --- control ------------------------------------------------------------------------
    Assumption(
        "W-KEYS",
        "The allow-listed button names act on all three TVs over the pointer socket; IN_START and EZ_ADJUST "
        "open service menus.",
        "aiowebostv buttons.py; bscpylgtv buttons.py (its comments name the service menus)",
        "medium",
        note="2026-10-17: RIGHT moved the highlight in Netflix on the C5; the first press, right after Netflix "
        "opened, was lost while it loaded (select_source's description now says so). The G1 and C7 are not "
        "tried yet.",
    ),
    Assumption(
        "W-MEDIA-CONTROLS",
        "media.controls/play, pause, stop, rewind and fastForward act on streaming apps (Netflix, YouTube) as "
        "well as the TV's own player.",
        "none: a guess",
        "low",
        note="2026-10-17, Office C5: pause and play acted on Netflix, and now_playing followed (playing, paused, "
        "playing). YouTube and the other TVs are not tried yet. If an app ignores them, send_key PLAY/PAUSE "
        "is the fallback.",
    ),
    Assumption(
        "W-NOW-PLAYING",
        "com.webos.media/getForegroundAppInfo gives the app and a playState (e.g. playing, paused), and no title, "
        "on webOS 25; webOS 3.5 and 6 don't have it (404), so now_playing is always null on the C7 and G1.",
        "aiowebostv's media_state; lgtv-mcp; the Office C5's and Family Room G1's captures",
        "medium",
        status="hardware-verified",
        note="2026-10-16: the C5 (webOS 25) answers it in the simulator's shape; the G1 (webOS 6.5.3) answers "
        "'404 no such service or method', and aiowebostv's subscription to it fails quietly (W-SUBSCRIBE). "
        "2026-10-17: the C7 (webOS 3.5) answers 404 as well.",
    ),
    Assumption(
        "W-SOUND-OUTPUT",
        "soundOutput reads external_arc (or another external_* value) with the receiver on eARC/ARC, and "
        "tv_speaker on the TV's own speakers.",
        "aiowebostv (special-cases external_arc); lgtv-mcp's fakes",
        "medium",
        status="hardware-verified",
        note="Seen 2026-10-16/17: external_arc on the G1 and C5 (sound to the Onkyo receivers), tv_speaker on the "
        "C7. With the TV off, the last known value is kept, so volume stays refused for a receiver-owned TV.",
    ),
    Assumption(
        "W-VOLUME-ARC",
        "With an external sound output, the TV forwards its volume commands to the receiver over HDMI-CEC, so "
        "TV volume would bypass the Onkyo server's caps.",
        "aiowebostv delays consecutive volume steps on external_arc for this reason",
        "medium",
    ),
    Assumption(
        "W-APP-IDS",
        "The default allow-list's app ids (netflix, youtube.leanback.v4, amazon, ...) match what listLaunchPoints "
        "reports on each TV.",
        "lgtv-mcp's fakes and community lists; to be replaced by ids captured from the TVs",
        "low",
        note="A wrong id is harmless: only allow-listed ids the TV itself lists are offered. 2026-10-14, Office "
        "C5 (webOS 25): apple-tv, disney+, netflix, prime-video and youtube matched; plex and spotify weren't "
        "listed (not installed, or other ids). `mcp-server-webos apps` shows each TV's real ids.",
    ),
    Assumption(
        "W-SYSTEM-APP-IDS",
        "LG's own apps use ids starting com.webos., com.palm. or com.lge.; the browser is com.webos.app.browser, "
        "LG's store com.webos.app.discovery, inputs com.webos.app.hdmiN, Live TV com.webos.app.livetv; settings "
        "and service apps have settings/factory in their ids.",
        "aiowebostv (com.webos.app.livetv, com.webos.app.hdmiN); lgtv-mcp's fakes; community app-id lists",
        "medium",
        note="`apps --add-all` only adds apps outside those namespaces and never an excluded one. An LG app with "
        "another id would be offered as third-party: read the listing before --add-all.",
    ),
    Assumption(
        "W-INPUT-IDS",
        "Inputs are listed as id HDMI_n with appId com.webos.app.hdmiN, the user's label and a connected flag; "
        "switching to one brings that appId to the foreground.",
        "lgtv-mcp's fakes; Home Assistant's source handling",
        "medium",
        note="2026-10-17, Office C5: four inputs HDMI_1..4 with the user's label on HDMI_2 ('Onkyo Home Theater', "
        "connected), and tv/switchInput HDMI_2 brought com.webos.app.hdmi2 to the front. The G1 and C7 are "
        "not tried yet.",
    ),
    Assumption(
        "W-SSDP",
        "An awake TV answers an SSDP M-SEARCH for urn:lge-com:service:webos-second-screen:1 with a LOCATION on "
        "its own address.",
        "lgtv-mcp discovery; Home Assistant's webostv SSDP matcher",
        "medium",
        status="hardware-verified",
        note="2026-10-16: the G1 and C5 both answered doctor's search, each on its own address. A TV that is off "
        "doesn't answer, so rediscovery only finds a TV once it is on.",
    ),
)

BY_ID = {a.id: a for a in ASSUMPTIONS}


def unverified() -> list[Assumption]:
    return [a for a in ASSUMPTIONS if a.status != "hardware-verified"]
