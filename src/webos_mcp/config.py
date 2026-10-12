"""Settings, file locations, and the allow-lists that bound what the model can do.

Everything the server persists lives in one directory (default
``~/.config/mcp-server-webos``, or ``WEBOS_CONFIG_DIR``):

  config.json  the TVs (name, aliases, address, deviceUUID, MACs, model,
               platform, certificate fingerprint, volume cap), extra apps,
               Wake-on-LAN settings. No secrets: safe to show, diff or paste
               into an issue (redacted, since it holds addresses and MACs).
  keys.json    the client key for each TV, by deviceUUID (keystore.py).

A client key *is* the credential: anyone holding it can control that TV. So
the directory is created 0700 and both files 0600.

Example config.json, as `pair` writes it (you may edit names, aliases,
max_volume and volume_owner):

    {
      "tvs": [
        {"name": "Family Room", "aliases": ["G1", "OLED77G1PUA"], "host": "192.168.1.50",
         "uuid": "3f2b8c1e-...", "macs": ["a8:23:fe:..."], "model": "OLED77G1PUA",
         "platform": "6.3.1", "cert_sha256": "9c1f...", "max_volume": 40, "volume_owner": "auto"}
      ],
      "apps": {"crunchyroll": "crunchyroll.app.id"}
    }
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

CLIENT_NAME = "mcp-server-webos"

log = logging.getLogger(__name__)

# --- Allow-lists -----------------------------------------------------------
# send_key can only press these. Each is a button name the TV's pointer socket
# accepts (aiowebostv's buttons.py lists them). ASSUMPTION W-KEYS.
KeyName = Literal[
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "ENTER",
    "BACK",
    "HOME",
    "EXIT",
    "INFO",
    "PLAY",
    "PAUSE",
    "STOP",
    "REWIND",
    "FASTFORWARD",
    "RED",
    "GREEN",
    "YELLOW",
    "BLUE",
    "CHANNELUP",
    "CHANNELDOWN",
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
]
ALLOWED_KEYS: frozenset[str] = frozenset(get_args(KeyName))

# Buttons the TV knows that send_key deliberately won't press, and why. Shown
# to clients in the webos://keys resource, so the model can explain a refusal.
LEFT_OUT_KEYS: dict[str, str] = {
    "POWER": "a toggle: its result depends on a state the model can't see; set_power is discrete",
    "IN_START": "opens the service menu (factory settings)",
    "EZ_ADJUST": "opens the service menu (factory settings)",
    "MENU": "opens settings",
    "QMENU": "opens quick settings",
    "ASPECT_RATIO": "opens the picture-size menu",
    "SCREEN_REMOTE": "a system overlay",
    "VOLUMEUP": "would bypass set_volume's cap and the receiver-owns-volume rule",
    "VOLUMEDOWN": "would bypass set_volume's cap and the receiver-owns-volume rule",
    "MUTE": "would bypass the receiver-owns-volume rule; use set_mute",
    "NETFLIX": "launches an app, bypassing the app allow-list; use select_source",
    "AMAZON": "launches an app, bypassing the app allow-list; use select_source",
    "INPUT_HUB": "home dashboard; use select_source",
    "MYAPPS": "home dashboard; use select_source",
    "RECENT": "home dashboard",
    "3D_MODE": "picture setting",
    "RECORD": "Live TV (out of scope)",
    "LIST": "Live TV (out of scope)",
    "PROGRAM": "Live TV guide (out of scope)",
    "GUIDE": "Live TV guide (out of scope)",
    "DASH": "Live TV (out of scope)",
    "TELETEXT": "Live TV (out of scope)",
    "TEXTOPTION": "Live TV (out of scope)",
    "AD": "audio description setting",
    "SAP": "audio language setting",
    "CC": "captions setting",
    "LIVE_ZOOM": "picture setting",
    "MAGNIFIER_ZOOM": "accessibility overlay",
    "ASTERISK": "no use without Live TV",
}


@dataclass(frozen=True)
class App:
    """An app select_source may open: its webOS app id, and a display name."""

    id: str
    title: str


# Friendly name -> app. Only ids the TV itself lists (listLaunchPoints) are
# ever offered, so a wrong id here is harmless: it just never appears.
# Extend per-user through "apps" in config.json ({"name": "app.id"}); user
# entries win. ASSUMPTION W-APP-IDS.
DEFAULT_APPS: dict[str, App] = {
    "netflix": App("netflix", "Netflix"),
    "youtube": App("youtube.leanback.v4", "YouTube"),
    "prime-video": App("amazon", "Prime Video"),
    "disney+": App("com.disney.disneyplus-prod", "Disney+"),
    "plex": App("cdp-30", "Plex"),
    "apple-tv": App("com.apple.appletv", "Apple TV"),
    "spotify": App("spotify-beehive", "Spotify"),
}

VolumeOwner = Literal["auto", "tv", "external"]
VOLUME_OWNERS: frozenset[str] = frozenset(get_args(VolumeOwner))
# TV volume runs 0-100. Even through the TV's own speakers, 40 is loud.
DEFAULT_MAX_VOLUME = 40
WOL_PORT = 9


# --- Addresses -------------------------------------------------------------
# An explicit list rather than ipaddress.is_private, whose meaning has
# changed between Python patch releases (an idea from lgtv-mcp). webOS TVs
# are reached over IPv4. 127/8 is allowed for the simulator; the cloud
# metadata address is never a TV.
_LOCAL_NETWORKS = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
)
_BLOCKED = frozenset({ipaddress.IPv4Address("169.254.169.254")})


def is_local_ipv4(text: str) -> bool:
    """True for an IPv4 address on a private, link-local or loopback network."""
    try:
        addr = ipaddress.IPv4Address(text.strip())
    except ValueError:
        return False
    return addr not in _BLOCKED and any(addr in net for net in _LOCAL_NETWORKS)


def name_key(name: str) -> str:
    """Names match ignoring case, spaces and punctuation: "Family Room" = "family-room"."""
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def normalize_mac(text: str) -> str | None:
    digits = "".join(ch for ch in text.lower() if ch in "0123456789abcdef")
    if len(digits) != 12:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


# --- Locations -------------------------------------------------------------
def config_dir() -> Path:
    override = os.environ.get("WEBOS_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mcp-server-webos"


@dataclass(frozen=True)
class TvSettings:
    name: str
    host: str
    # From the TV's hello at pairing: how it is recognized (ASSUMPTION W-UUID-STABLE)
    uuid: str | None = None
    macs: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    model: str | None = None
    platform: str | None = None  # deviceOSReleaseVersion, e.g. "10.3.1"
    cert_sha256: str | None = None  # the TLS certificate `pair` saw (port 3001 only)
    max_volume: int = DEFAULT_MAX_VOLUME
    volume_owner: VolumeOwner = "auto"

    def answers_to(self, ref: str) -> bool:
        key = name_key(ref)
        return bool(key) and (
            key == name_key(self.name) or any(key == name_key(a) for a in self.aliases) or ref.strip() == self.host
        )

    def describe(self) -> str:
        extra = f", {self.model}" if self.model else ""
        return f"{self.name} ({self.host}{extra})"


@dataclass(frozen=True)
class Settings:
    tvs: tuple[TvSettings, ...]
    apps: dict[str, App]
    dry_run: bool = False
    wol_port: int = WOL_PORT
    wol_broadcast: str = "255.255.255.255"
    # Anything wrong with config.json, in words (logged at startup, shown by doctor).
    problems: tuple[str, ...] = field(default=())

    def tv(self, name: str) -> TvSettings | None:
        return next((t for t in self.tvs if t.name == name), None)

    def resolve_app(self, ref: str) -> tuple[str, App] | None:
        """A friendly name, an app id, or an app title -> (friendly name, App)."""
        key = name_key(ref)
        for friendly, app in self.apps.items():
            if key in (name_key(friendly), name_key(app.id), name_key(app.title)):
                return friendly, app
        return None


def _read_json(path: Path, problems: list[str] | None = None) -> dict[str, Any]:
    """config.json as a dict. A missing file is normal; a broken one is a
    problem worth saying out loud (silently ignoring it looks like "not
    paired", which sends people to re-pair for nothing)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        if problems is not None:
            problems.append(f"{path} is not valid JSON (line {exc.lineno}: {exc.msg}); ignoring it")
        return {}
    except OSError as exc:
        if problems is not None:
            problems.append(f"can't read {path} ({exc})")
        return {}
    if not isinstance(data, dict):
        if problems is not None:
            problems.append(f"{path} should hold a JSON object; ignoring it")
        return {}
    return data


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str) and v.strip())
    return ()


def _int(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi:
        return value
    return None


def _parse_tv(raw: Any, default_cap: int, problems: list[str]) -> TvSettings | None:
    if not isinstance(raw, dict):
        problems.append("ignoring a TV in config.json: expected an object")
        return None
    name, host = raw.get("name"), raw.get("host")
    if not isinstance(name, str) or not name.strip():
        problems.append("ignoring a TV in config.json with no name")
        return None
    if not isinstance(host, str) or not is_local_ipv4(host):
        problems.append(f"ignoring TV {name!r}: host {host!r} is not a private IPv4 address")
        return None
    macs = tuple(m for m in (normalize_mac(x) for x in _strings(raw.get("macs"))) if m)
    cap = raw.get("max_volume", default_cap)
    max_volume = _int(cap, 0, 100)
    if max_volume is None:
        problems.append(f"TV {name!r}: max_volume must be 0-100; using {default_cap}")
        max_volume = default_cap
    owner = raw.get("volume_owner", "auto")
    if owner not in VOLUME_OWNERS:
        problems.append(f"TV {name!r}: volume_owner must be auto, tv or external; using auto")
        owner = "auto"

    def opt(key: str) -> str | None:
        v = raw.get(key)
        return v if isinstance(v, str) and v else None

    return TvSettings(
        name=name.strip(),
        host=host.strip(),
        uuid=opt("uuid"),
        macs=macs,
        aliases=_strings(raw.get("aliases")),
        model=opt("model"),
        platform=opt("platform"),
        cert_sha256=opt("cert_sha256"),
        max_volume=max_volume,
        volume_owner=owner,
    )


def _check_names(tvs: Iterable[TvSettings], problems: list[str]) -> tuple[TvSettings, ...]:
    """Drop a TV whose name or alias collides with an earlier one: a name that
    means two TVs would make "never guess" impossible to honor."""
    kept: list[TvSettings] = []
    seen: dict[str, str] = {}
    for tv in tvs:
        keys = {name_key(tv.name), *(name_key(a) for a in tv.aliases)} - {""}
        clash = next((seen[k] for k in keys if k in seen), None)
        if clash:
            problems.append(f"ignoring TV {tv.name!r}: its name or an alias is also {clash!r}'s")
            continue
        seen.update(dict.fromkeys(keys, tv.name))
        kept.append(tv)
    return tuple(kept)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_settings() -> Settings:
    """Never raises: anything wrong becomes a sentence in Settings.problems."""
    problems: list[str] = []
    data = _read_json(config_dir() / "config.json", problems)
    default_cap = _int(data.get("max_volume", DEFAULT_MAX_VOLUME), 0, 100)
    if default_cap is None:
        problems.append(f"max_volume must be 0-100; using {DEFAULT_MAX_VOLUME}")
        default_cap = DEFAULT_MAX_VOLUME
    raw_tvs = data.get("tvs", [])
    if not isinstance(raw_tvs, list):
        problems.append('"tvs" in config.json should be a list; ignoring it')
        raw_tvs = []
    tvs = _check_names((t for t in (_parse_tv(r, default_cap, problems) for r in raw_tvs) if t), problems)

    apps = dict(DEFAULT_APPS)
    user_apps = data.get("apps", {})
    if isinstance(user_apps, dict):
        for name, value in user_apps.items():
            if isinstance(value, str) and value.strip():
                apps[name.strip().lower()] = App(value.strip(), name.strip())
            else:
                problems.append(f"ignoring app {name!r} in config.json: expected an app id string")
    else:
        problems.append('"apps" in config.json should be an object; ignoring it')

    wol_port = _int(data.get("wol_port", WOL_PORT), 1, 65535)
    if wol_port is None:
        problems.append(f"wol_port must be a port number; using {WOL_PORT}")
        wol_port = WOL_PORT
    broadcast = data.get("wol_broadcast", "255.255.255.255")
    if not isinstance(broadcast, str) or not (broadcast == "255.255.255.255" or is_local_ipv4(broadcast)):
        problems.append("wol_broadcast must be 255.255.255.255 or a private IPv4 address; using 255.255.255.255")
        broadcast = "255.255.255.255"
    return Settings(
        tvs=tvs,
        apps=apps,
        dry_run=_flag(os.environ.get("WEBOS_DRY_RUN")),
        wol_port=wol_port,
        wol_broadcast=broadcast,
        problems=tuple(problems),
    )


def ensure_private_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def write_private(path: Path, text: str) -> None:
    """Write a file that only its owner can read, atomically: it is private
    from the moment it exists (umask), and readers never see half of it."""
    tmp = path.with_name(path.name + ".tmp")
    old = os.umask(0o077)
    try:
        tmp.write_text(text)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old)


def save_config(**values: Any) -> None:
    """Merge top-level values into config.json, keeping whatever else is there."""
    d = ensure_private_dir()
    path = d / "config.json"
    data = _read_json(path)
    data.update(values)
    write_private(path, json.dumps(data, indent=2) + "\n")


def upsert_tv(entry: dict[str, Any], match_uuid: str | None = None) -> None:
    """Add a TV to config.json, or update the one with the same deviceUUID (or
    name). Fields not in entry (e.g. a hand-edited max_volume) are kept."""
    d = ensure_private_dir()
    path = d / "config.json"
    data = _read_json(path)
    tvs = data.get("tvs") if isinstance(data.get("tvs"), list) else []
    assert isinstance(tvs, list)
    uuid = match_uuid or entry.get("uuid")
    for i, raw in enumerate(tvs):
        if isinstance(raw, dict) and ((uuid and raw.get("uuid") == uuid) or raw.get("name") == entry.get("name")):
            tvs[i] = {**raw, **entry}
            break
    else:
        tvs.append(entry)
    data["tvs"] = tvs
    write_private(path, json.dumps(data, indent=2) + "\n")


def update_tv(name: str, **values: Any) -> None:
    """Change fields of the TV called name (e.g. its host after rediscovery)."""
    d = ensure_private_dir()
    path = d / "config.json"
    data = _read_json(path)
    tvs = data.get("tvs")
    if not isinstance(tvs, list):
        return
    for raw in tvs:
        if isinstance(raw, dict) and raw.get("name") == name:
            raw.update(values)
    write_private(path, json.dumps(data, indent=2) + "\n")
