"""The `apps` command: what each TV has installed, and growing the allow-list from it."""

from __future__ import annotations

import argparse
import json

import pytest

from webos_mcp import apps, cli
from webos_mcp.config import load_settings

from .conftest import register

pytestmark = pytest.mark.anyio


def args(**kw) -> argparse.Namespace:
    base = {"tv": None, "all_tvs": False, "add": [], "add_all": False, "json": False, "timeout": 10.0}
    return argparse.Namespace(**{**base, **kw})


def groups(listing) -> dict[str, str]:
    return {a.id: a.group for a in listing}


# --- classifying ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("app_id", "group"),
    [
        ("netflix", "allowed"),
        ("crunchyroll", "other"),
        ("com.webos.app.photovideo", "system"),  # ASSUMPTION W-SYSTEM-APP-IDS
        ("com.palm.app.something", "system"),
        ("com.webos.app.browser", "excluded"),
        ("com.example.webbrowser", "excluded"),  # any browser, LG's or not
        ("com.webos.app.discovery", "excluded"),
        ("com.webos.app.settings", "excluded"),
        ("com.webos.app.hdmi3", "excluded"),
        ("com.webos.app.externalinput.av1", "excluded"),
        ("com.webos.app.livetv", "excluded"),
        ("com.webos.app.factorywin", "excluded"),
    ],
)
def test_classify(app_id, group):
    assert apps.classify(app_id, {"netflix": "netflix"})[0] == group


def test_friendly_names():
    taken = {"netflix": "netflix"}
    assert apps.friendly_name("Prime Video", "amazon", taken) == "prime-video"
    assert apps.friendly_name("Disney+", "x", taken) == "disney+"
    assert apps.friendly_name("Netflix", "netflix", taken) == "netflix"  # its own name
    assert apps.friendly_name("Netflix", "netflix.kids", taken) == "netflix-2"  # someone else's
    assert apps.friendly_name("★★★", "com.foo.bar", taken) == "com-foo-bar"


# --- against the simulated TVs ------------------------------------------------------------
async def test_listing_one_tv(home, tvs, capsys):
    tv = await tvs("g1")
    register(tv)
    assert await cli._cmd_apps(args()) == 0  # one TV configured: no need to name it
    out = capsys.readouterr().out
    assert out.index("Allowed") < out.index("Not allowed yet") < out.index("LG's own apps") < out.index("Excluded")
    assert "crunchyroll  Crunchyroll = crunchyroll" in out
    assert "Web Browser = com.webos.app.browser: the web browser opens any URL" in out
    assert tv.requests.count("com.webos.applicationManager/listLaunchPoints") >= 1
    # read-only: nothing that changes the TV
    assert not [u for u in tv.requests if u.startswith(("system.launcher", "audio/set", "system/turn"))]


async def test_json_listing(home, tvs, capsys):
    tv = await tvs("g1")
    register(tv)
    assert await cli._cmd_apps(args(json=True)) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["netflix"]["group"] == "allowed" and rows["crunchyroll"]["group"] == "other"
    assert rows["com.webos.app.browser"]["reason"] and rows["netflix"]["tvs"] == ["Family Room"]


async def test_never_guess_the_tv(home, tvs, capsys):
    g1, c7 = await tvs("g1"), await tvs("c7")
    register(g1)
    register(c7)
    assert await cli._cmd_apps(args()) == 2
    assert "Which TV?" in capsys.readouterr().err


async def test_all_tvs_merges_and_says_where(home, tvs, capsys):
    g1, c7 = await tvs("g1"), await tvs("c7")
    register(g1)
    register(c7)
    c7.apps = [a for a in c7.apps if a["id"] != "crunchyroll"]
    assert await cli._cmd_apps(args(all_tvs=True, json=True)) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["crunchyroll"]["tvs"] == ["Family Room"]
    assert sorted(rows["netflix"]["tvs"]) == ["Bedroom", "Family Room"]


async def test_add_by_name_and_add_all(home, tvs, capsys):
    tv = await tvs("g1")
    register(tv)
    assert await cli._cmd_apps(args(add=["Photo & Video"])) == 0  # a system app, by title
    saved = json.loads((home / "config.json").read_text())["apps"]
    assert saved == {"photo-video": "com.webos.app.photovideo"}
    assert await cli._cmd_apps(args(add_all=True)) == 0
    saved = json.loads((home / "config.json").read_text())["apps"]
    assert saved == {"photo-video": "com.webos.app.photovideo", "crunchyroll": "crunchyroll"}
    s = load_settings()
    assert s.resolve_app("crunchyroll")[1].id == "crunchyroll" and "Restart the server" in capsys.readouterr().out


async def test_excluded_and_unknown_are_refused(home, tvs, capsys):
    tv = await tvs("g1")
    register(tv)
    assert await cli._cmd_apps(args(add=["web browser,tiktok,netflix"])) == 1
    err = capsys.readouterr().err
    assert "is excluded: the web browser opens any URL" in err
    assert "'tiktok': no installed app" in err and "already allowed" in err
    assert "apps" not in json.loads((home / "config.json").read_text())


async def test_added_apps_are_offered_by_the_server(home, tvs):
    from webos_mcp.client import TvClient

    from .conftest import fast, wait_until

    tv = await tvs("g1")
    register(tv)
    assert await cli._cmd_apps(args(add=["crunchyroll"])) == 0
    settings = load_settings()
    client = fast(TvClient(settings.tv("Family Room"), settings))
    await client.start()
    try:
        await wait_until(lambda: client.reachable and client.cache.apps)
        assert "crunchyroll" in {s["name"] for s in client.sources()}
        await client.select_source("crunchyroll")
        assert tv.foreground == "crunchyroll"
    finally:
        await client.stop()


async def test_unreachable_tv(home, tvs, capsys):
    tv = await tvs("c7")
    register(tv)
    await tv.unplug()
    assert await cli._cmd_apps(args(timeout=2.0)) == 1
    assert "couldn't read its apps" in capsys.readouterr().err
