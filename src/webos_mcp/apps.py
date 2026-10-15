"""`mcp-server-webos apps`: see what each TV has installed, and choose what select_source may open.

The TV already reports its installed apps (listLaunchPoints, the list on its
home screen); the server keeps it to show `list_sources`. But select_source
only opens apps on the allow-list, because an app is the one way this server
could put arbitrary content on a TV. This command is how the allow-list grows:
you see everything installed and pick, and the model never can.

Every installed app falls in one of four groups:

  allowed    already on the allow-list (a default, or yours in config.json)
  excluded   refused, with the reason: the web browser (it opens any URL),
             settings, LG's store (it installs apps), inputs and Live TV
             (select_source handles inputs; Live TV is out of scope)
  system     LG's own apps (com.webos., com.palm., com.lge.): left out of
             --add-all, but you may --add one by name
  other      everything else, i.e. third-party apps: --add-all adds these

Which ids are LG's own is ASSUMPTION W-SYSTEM-APP-IDS: an LG app with another
id would land in "other", so read the listing before --add-all.

New entries go into config.json's "apps", as `"friendly-name": "app id"`. A
running server reads the allow-list at startup: restart it afterwards.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import aiohttp
from aiowebostv.exceptions import WebOsTvCommandError, WebOsTvPairError

from . import keystore
from .client import clean
from .compat import CompatClient
from .config import Settings, TvSettings, _read_json, config_dir, name_key, save_config

Group = Literal["allowed", "excluded", "system", "other"]

# (id pattern, why it is never added). fnmatch patterns, matched on the lowercased id.
EXCLUDED: tuple[tuple[str, str], ...] = (
    ("com.webos.app.browser", "the web browser opens any URL"),
    ("*browser*", "a web browser opens any URL"),
    ("*settings*", "system settings"),
    ("com.webos.app.discovery", "LG's app store: it installs apps"),
    ("com.webos.app.hdmi*", "an input: select_source switches inputs itself"),
    ("com.webos.app.externalinput.*", "an input: select_source switches inputs itself"),
    ("com.webos.app.livetv", "Live TV is out of scope"),
    ("com.webos.app.tvguide", "Live TV is out of scope"),
    ("com.webos.app.livemenu", "Live TV is out of scope"),
    ("com.webos.app.customersupport", "service and diagnostics"),
    ("com.webos.app.softwareupdate", "service and diagnostics"),
    ("*factory*", "service and diagnostics"),
    ("*instart*", "service menu"),
    ("*ezadjust*", "service menu"),
)
SYSTEM_PREFIXES = ("com.webos.", "com.palm.", "com.lge.")


@dataclass
class InstalledApp:
    id: str
    title: str
    group: Group
    name: str  # the friendly name it has, or would get
    reason: str = ""  # why it is excluded
    tvs: list[str] = field(default_factory=list)


def classify(app_id: str, allowed_ids: dict[str, str]) -> tuple[Group, str]:
    """(group, reason) for one app id. allowed_ids: id -> friendly name."""
    if app_id in allowed_ids:
        return "allowed", ""
    low = app_id.lower()
    for pattern, reason in EXCLUDED:
        if fnmatch.fnmatchcase(low, pattern):
            return "excluded", reason
    if low.startswith(SYSTEM_PREFIXES):  # ASSUMPTION W-SYSTEM-APP-IDS
        return "system", ""
    return "other", ""


def friendly_name(title: str, app_id: str, taken: dict[str, str]) -> str:
    """A short name for select_source, from the title: "Prime Video" -> "prime-video".
    taken: name -> app id already using it; a clash with another id gets a suffix."""
    base = re.sub(r"[^a-z0-9+]+", "-", title.lower()).strip("-") or re.sub(r"[^a-z0-9+]+", "-", app_id.lower())
    base = base[:32].strip("-") or "app"
    name, n = base, 2
    while taken.get(name, app_id) != app_id:
        name, n = f"{base}-{n}", n + 1
    return name


async def fetch(tv: TvSettings, timeout: float = 15.0) -> list[dict[str, Any]]:
    """The TV's launch points, over a short session with the stored key (nothing changes on the TV)."""
    key = keystore.get(tv.uuid)
    if key is None:
        raise WebOsTvPairError(f"{tv.name} isn't paired: run `mcp-server-webos pair` first")
    client = CompatClient(tv.host, key, expected_uuid=tv.uuid)
    try:
        async with asyncio.timeout(timeout):
            await client.connect()
            # Typed as a dict in aiowebostv, but it is the reply's launchPoints list
            apps: Any = await client.get_apps()
    finally:
        await client.disconnect()
    return [a for a in (apps or []) if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"]]


def build_listing(settings: Settings, found: dict[str, list[dict[str, Any]]]) -> list[InstalledApp]:
    """Merge each TV's launch points into one list, by app id, sorted by group then title."""
    allowed_ids = {app.id: name for name, app in settings.apps.items()}
    taken = {name: app.id for name, app in settings.apps.items()}
    out: dict[str, InstalledApp] = {}
    for tv_name, apps in found.items():
        for raw in apps:
            app_id = clean(raw["id"], 128)
            if app_id in out:
                out[app_id].tvs.append(tv_name)
                continue
            title = clean(raw.get("title") or app_id)
            group, reason = classify(app_id, allowed_ids)
            name = allowed_ids.get(app_id) or friendly_name(title, app_id, taken)
            taken.setdefault(name, app_id)
            out[app_id] = InstalledApp(app_id, title, group, name, reason, [tv_name])
    order = {"allowed": 0, "other": 1, "system": 2, "excluded": 3}
    return sorted(out.values(), key=lambda a: (order[a.group], a.title.lower()))


def select(listing: list[InstalledApp], refs: list[str], add_all: bool) -> tuple[list[InstalledApp], list[str]]:
    """The apps to add, and a problem sentence for each ref that can't be added."""
    chosen: dict[str, InstalledApp] = {}
    problems: list[str] = []
    if add_all:
        chosen.update({a.id: a for a in listing if a.group == "other"})
    for ref in refs:
        key = name_key(ref)
        matches = [a for a in listing if key in (name_key(a.name), name_key(a.id), name_key(a.title))]
        if not matches:
            problems.append(f"{ref!r}: no installed app by that name, id or title")
        elif len(matches) > 1:
            problems.append(f"{ref!r} matches several apps ({', '.join(a.id for a in matches)}): use the id")
        elif matches[0].group == "excluded":
            problems.append(f"{ref!r} ({matches[0].id}) is excluded: {matches[0].reason}")
        elif matches[0].group == "allowed":
            problems.append(f"{ref!r} ({matches[0].id}) is already allowed, as {matches[0].name!r}")
        else:
            chosen[matches[0].id] = matches[0]
    return list(chosen.values()), problems


def add_to_config(apps: list[InstalledApp]) -> None:
    """Merge into config.json's "apps" ({name: id}), keeping everything else there."""
    data = _read_json(config_dir() / "config.json")
    current = data.get("apps") if isinstance(data.get("apps"), dict) else {}
    assert isinstance(current, dict)
    current.update({a.name: a.id for a in apps})
    save_config(apps=current)


def render(listing: list[InstalledApp], several_tvs: bool) -> str:
    headings = {
        "allowed": "Allowed (select_source can open these)",
        "other": "Not allowed yet (--add NAME, or --add-all for all of these)",
        "system": "LG's own apps (not in --add-all; --add NAME if you want one)",
        "excluded": "Excluded (never added)",
    }
    lines: list[str] = []
    for group in ("allowed", "other", "system", "excluded"):
        rows = [a for a in listing if a.group == group]
        if not rows:
            continue
        lines += ["", headings[group]]
        width = max(len(a.name) for a in rows)
        for a in rows:
            where = f"  [{', '.join(a.tvs)}]" if several_tvs else ""
            if group == "excluded":  # no name: it can't be used
                lines.append(f"  {a.title} = {a.id}: {a.reason}{where}")
            else:
                lines.append(f"  {a.name:<{width}}  {a.title} = {a.id}{where}")
    return "\n".join(lines).lstrip("\n")


def to_json(listing: list[InstalledApp]) -> str:
    return json.dumps(
        [
            {"name": a.name, "id": a.id, "title": a.title, "group": a.group, "reason": a.reason, "tvs": a.tvs}
            for a in listing
        ],
        indent=1,
    )


FETCH_ERRORS = (WebOsTvPairError, WebOsTvCommandError, TimeoutError, OSError, aiohttp.ClientError)
