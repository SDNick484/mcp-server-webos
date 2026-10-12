"""What each simulated TV answers, and where each behavior comes from.

A profile is one generation of TV as far as anyone knows it. Every field that
differs between generations names its source in SOURCES (or is marked
GUESSED), the way mcp-server-shieldtv's simulator does: a green test run is
only worth what these claims are worth, and HARDWARE_VALIDATION.md is how
they get checked.

The four profiles are the owner's TVs: the C7 and the G1 as they are, and the
C5 before and after the webOS 26 update LG has been rolling out to 2025 sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Port3000 = Literal["ws", "refuse", "accept_close", "blackhole"]


def _app(app_id: str, title: str) -> dict[str, Any]:
    return {"id": app_id, "title": title}


def _input(n: int, label: str, connected: bool) -> dict[str, Any]:
    # ASSUMPTION W-INPUT-IDS: id HDMI_n, appId com.webos.app.hdmiN, the user's label
    return {"id": f"HDMI_{n}", "label": label, "appId": f"com.webos.app.hdmi{n}", "connected": connected}


# Installed apps on every simulated TV: the allow-listed ones, Live TV and a
# browser (to show that apps outside the allow-list are never offered).
APPS: list[dict[str, Any]] = [
    _app("netflix", "Netflix"),
    _app("youtube.leanback.v4", "YouTube"),
    _app("amazon", "Prime Video"),
    _app("com.disney.disneyplus-prod", "Disney+"),
    _app("cdp-30", "Plex"),
    _app("com.apple.appletv", "Apple TV"),
    _app("com.webos.app.livetv", "Live TV"),
    _app("com.webos.app.browser", "Web Browser"),
]

# The TV usually sits on one HDMI input from the receiver. HDMI 3 is labeled
# "Apple TV" on purpose: the same words as an app, so "never guess" is tested.
INPUTS: list[dict[str, Any]] = [
    _input(1, "Onkyo", True),
    _input(2, "HDMI 2", False),
    _input(3, "Apple TV", True),
]


@dataclass(frozen=True)
class Profile:
    key: str
    model: str
    os_version: str  # hello deviceOSVersion
    release_version: str  # hello deviceOSReleaseVersion (ASSUMPTION W-HELLO-VERSION)
    uuid: str
    serial: str
    wired_mac: str
    wifi_mac: str
    wired: bool  # which interface the TV uses
    port3000: Port3000  # ASSUMPTION W-PORTS, W-PORT3000-CLOSE
    wss: bool  # answers TLS WebSocket on 3001
    power_state: bool  # getPowerState exists (ASSUMPTION W-C7-NO-POWERSTATE)
    screen_off_uri: str | None  # None: no screen-off endpoint (ASSUMPTION W-SCREEN-OFF)
    standby_socket: bool  # the socket stays up in standby (ASSUMPTION W-STANDBY-SOCKET)
    blacklist_signature: bool  # 403 for a signed manifest (ASSUMPTION W-SIGNED-403)
    software_info: dict[str, Any] | None  # None: 401 (ASSUMPTION W-SWINFO-401)
    pairing_types: tuple[str, ...]
    sound_output: str  # ASSUMPTION W-SOUND-OUTPUT
    volume_flat: bool  # audio/getVolume without the volumeStatus wrapper (older webOS)
    apps: list[dict[str, Any]] = field(default_factory=lambda: [dict(a) for a in APPS])
    inputs: list[dict[str, Any]] = field(default_factory=lambda: [dict(i) for i in INPUTS])


TVPOWER = "com.webos.service.tvpower/power/turnOffScreen"

PROFILES: dict[str, Profile] = {
    "c7": Profile(
        key="c7",
        model="OLED65C7P-U",
        os_version="1.2.0",  # GUESSED
        release_version="3.5.0",  # GUESSED: the C7's hello has never been captured
        uuid="7c7c7c7c-0000-4000-8000-000000000017",
        serial="707ABCD00017",
        wired_mac="a8:23:fe:00:17:01",
        wifi_mac="a8:23:fe:00:17:02",
        wired=True,
        port3000="ws",
        wss=False,
        power_state=False,
        screen_off_uri=None,
        standby_socket=False,
        blacklist_signature=False,
        software_info={"product_name": "webOSTV 3.5", "major_ver": "06", "minor_ver": "10.65"},
        pairing_types=("PROMPT", "PIN"),
        sound_output="tv_speaker",
        volume_flat=True,
    ),
    "g1": Profile(
        key="g1",
        model="OLED77G1PUA",
        os_version="4.1.0",  # GUESSED
        release_version="6.3.1",  # GUESSED
        uuid="61616161-0000-4000-8000-000000000021",
        serial="107ABCD00021",
        wired_mac="a8:23:fe:00:21:01",
        wifi_mac="a8:23:fe:00:21:02",
        wired=False,
        port3000="refuse",
        wss=True,
        power_state=True,
        screen_off_uri=TVPOWER,
        standby_socket=True,
        blacklist_signature=False,
        software_info={"product_name": "webOSTV 6.0", "major_ver": "03", "minor_ver": "53.45"},
        pairing_types=("PROMPT", "PIN", "COMBINED"),
        sound_output="external_arc",
        volume_flat=False,
    ),
    "c5-webos25": Profile(
        key="c5-webos25",
        model="OLED65C5PUA",
        os_version="4.1.0",  # GUESSED
        release_version="10.3.1",  # a C5 owner's TV Information screen, HA core #172703
        uuid="c5c5c5c5-0000-4000-8000-000000000025",
        serial="507ABCD00025",
        wired_mac="a8:23:fe:00:25:01",
        wifi_mac="a8:23:fe:00:25:02",
        wired=False,
        port3000="refuse",
        wss=True,
        power_state=True,
        screen_off_uri=TVPOWER,
        standby_socket=True,
        blacklist_signature=False,
        software_info={"product_name": "webOSTV 25", "major_ver": "33", "minor_ver": "31.61"},
        pairing_types=("PROMPT", "PIN", "COMBINED"),
        sound_output="external_arc",
        volume_flat=False,
    ),
    "c5-webos26": Profile(
        key="c5-webos26",
        model="OLED65C5PUA",
        os_version="4.1.0",  # a G5 on webOS 26, HA core #172703
        release_version="11.2.0",  # same capture
        uuid="c5c5c5c5-0000-4000-8000-000000000025",  # the same TV, updated (ASSUMPTION W-UUID-STABLE)
        serial="507ABCD00025",
        wired_mac="a8:23:fe:00:25:01",
        wifi_mac="a8:23:fe:00:25:02",
        wired=False,
        port3000="accept_close",  # a 2026 C6 on 43.21.60, HA core #172703
        wss=True,
        power_state=True,
        screen_off_uri=TVPOWER,
        standby_socket=True,
        blacklist_signature=True,  # HA core #172703, lgpowercontrol v4.4
        software_info=None,  # 401 for an unsigned key, HA core #172703
        pairing_types=("PIN", "PROMPT", "COMBINED", "LGSWITCH-PIN"),  # G5 hello, HA core #172703
        sound_output="external_arc",
        volume_flat=False,
    ),
}

# Where each simulated behavior comes from (shown by `simulate`, cited in tests).
SOURCES: dict[str, str] = {
    "hello": "HA core #172703 debug log (a G5 on webOS 26); fields for webOS 3.5/6 GUESSED",
    "registration": "aiowebostv 0.10.0 _check_registration; the 403 text from HA core #172703",
    "errors": "aiowebostv _parse_response ('404 no such service or method'); 401 texts from HA core #172703",
    "power states": "lgpowercontrol's virtual TV (observed on an OLED42C35LA); aiowebostv _is_tv_on",
    "wake time": "lgpowercontrol: ~4 s from Active Standby (shortened in tests)",
    "connection info shape": "GUESSED (ASSUMPTION W-MAC-SOURCE)",
    "launch points, inputs, media": "aiowebostv's parsers; lgtv-mcp's fakes",
}
