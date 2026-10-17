"""TvClient against the simulated TVs: the supervisor, the state cache, and each action."""

from __future__ import annotations

import asyncio
import json

import pytest

from webos_mcp import keystore
from webos_mcp.client import TvClient, clean
from webos_mcp.config import load_settings
from webos_mcp.errors import Unreachable, WebOsError
from webos_mcp.probe import any_hello

from .conftest import fast, no_power_state, no_screen_off, register, wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
async def running():
    """Start TvClients for configured TVs: ``c = await running("Family Room")``."""
    started: list[TvClient] = []

    async def start(name: str, **kwargs) -> TvClient:
        settings = load_settings()
        client = fast(TvClient(settings.tv(name), settings, **kwargs))
        await client.start()
        started.append(client)
        return client

    yield start
    for c in started:
        await c.stop()


async def connected(client: TvClient) -> TvClient:
    await wait_until(lambda: client.reachable and client.cache.sound_output is not None)
    return client


async def test_status_from_pushed_state(home, tvs, running):
    tv = await tvs("g1")
    c = await connected(await running(register(tv)))
    s = c.snapshot()
    assert (s["tv"], s["power"], s["power_raw"], s["stale"], s["needs_pairing"]) == (
        "Family Room",
        "on",
        "Active",
        False,
        False,
    )
    assert s["input"] == "Onkyo" and s["foreground_app"] == {"id": "com.webos.app.hdmi1", "title": "Onkyo"}
    assert s["volume_owner"] == "external" and s["sound_output"] == "external_arc"  # ASSUMPTION W-SOUND-OUTPUT
    assert {"power_on", "power_off", "screen", "keys", "sources", "media_control"} <= set(s["capabilities"])
    assert any(lim.startswith("volume: belongs to the receiver") for lim in s["limitations"])


async def test_c7_reports_its_power_state(home, tvs, running):
    # ASSUMPTION W-C7-NO-POWERSTATE was wrong: the C7 answers getPowerState like the newer TVs
    tv = await tvs("c7")
    c = await connected(await running(register(tv)))
    s = c.snapshot()
    assert s["power"] == "on" and s["power_raw"] == "Active" and s["volume_owner"] == "tv"
    assert not any("inferred" in lim for lim in s["limitations"])


async def test_volume_is_not_offered_before_the_sound_output_is_known(home, tvs, running):
    # A TV that has been off since the server started: no sound output yet, so no volume (it may be the receiver's)
    tv = await tvs("g1")
    await tv.unplug()
    c = await running(register(tv))
    caps, limits = c.capabilities()
    assert c.volume_owner() == "unknown" and "volume" not in caps
    assert any(lim.startswith("volume: not known yet") for lim in limits)


async def test_tv_without_power_state(home, tvs, running):
    # ASSUMPTION W-C7-NO-POWERSTATE: an older set without getPowerState; power comes from the foreground app
    tv = await tvs("c7", faults=no_power_state())
    c = await connected(await running(register(tv)))
    s = c.snapshot()
    assert s["power"] == "on" and s["power_raw"] is None
    assert any("inferred from the foreground app" in lim for lim in s["limitations"])


async def test_unplugged_tv_goes_stale_and_keeps_its_last_values(home, tvs, running):
    tv = await tvs("c7")
    c = await connected(await running(register(tv)))
    await tv.unplug()
    await wait_until(lambda: not c.reachable)
    s = c.snapshot()
    assert (s["reachable"], s["stale"], s["power"]) == (False, True, "off")
    assert s["volume"] == 12 and s["as_of"] is not None and s["error"]
    with pytest.raises(Unreachable, match="set_power with state='on' wakes it"):
        await c.set_volume(10)


async def test_power_on_with_wake_on_lan(home, tvs, running):
    # ASSUMPTION W-WOL, W-WAKE-TIME
    tv = await tvs("c7")
    c = await connected(await running(register(tv)))
    await tv.unplug()
    await wait_until(lambda: not c.reachable)
    result = await c.power_on()
    assert result.outcome == "done" and "is on" in result.detail and not result.warnings
    assert result.sent[0].startswith(f"wol {tv.mac} -> ")
    assert tv.events("wol") and c.reachable
    again = await c.power_on()
    assert (again.outcome, again.sent) == ("unchanged", [])


async def test_power_on_unconfirmed(home, tvs, running):
    tv = await tvs("c7")
    register(tv)
    c = await connected(await running("Bedroom"))
    await tv.stop()  # gone for good: the packet reaches nobody
    await wait_until(lambda: not c.reachable)
    c.wake_wait = 0.3
    result = await c.power_on()
    assert result.outcome == "done" and result.warnings[0].startswith("unconfirmed")


async def test_power_off_standby_and_unchanged(home, tvs, running):
    # ASSUMPTION W-STANDBY-SOCKET, W-OFF-GUARD
    tv = await tvs("g1")
    c = await connected(await running(register(tv)))
    result = await c.power_off()
    assert result.outcome == "done" and tv.power == "Active Standby" and c.power() == "standby"
    assert (await c.power_off()).outcome == "unchanged"
    assert tv.requests.count("system/turnOff") == 1


async def test_power_off_when_the_socket_goes_away(home, tvs, running):
    tv = await tvs("c7")
    c = await connected(await running(register(tv)))
    assert (await c.power_off()).outcome == "done"
    assert c.power() == "off"


async def test_a_stale_key_never_loops_the_pairing_dialog(home, tvs, running):
    # ASSUMPTION W-STALE-KEY, W-KEY-SURVIVES
    tv = await tvs("g1", accept_after=60)
    register(tv)
    tv.keys.clear()  # the TV forgot us (reset, or a firmware update)
    c = await running("Family Room")
    await wait_until(lambda: c.needs_pairing)
    await asyncio.sleep(0.6)  # several poll intervals
    assert tv.events("prompt") == [("prompt",)]
    s = c.snapshot()
    assert s["needs_pairing"] and "no longer accepts the stored key" in s["error"] and "pair --host" in s["error"]
    with pytest.raises(WebOsError, match="needs pairing"):
        await c.set_volume(5)
    # `pair` writes a key the TV knows: the running client picks it up
    tv.pair_with("f" * 32)
    keystore.put(tv.profile.uuid, "f" * 32)
    await connected(c)
    assert not c.needs_pairing


async def test_not_paired_waits_quietly(home, tvs, running):
    tv = await tvs("g1")
    register(tv)
    keystore.path().unlink()
    c = await running("Family Room")
    await wait_until(lambda: c.needs_pairing)
    await asyncio.sleep(0.3)
    assert tv.events("register") == [] and "not paired" in c.snapshot()["error"]


async def test_key_never_goes_to_another_device_and_the_tv_is_found_again(home, tvs, running):
    # ASSUMPTION W-UUID-STABLE, W-SSDP: the G1 moved; the C7 now has its old address
    g1, c7 = await tvs("g1"), await tvs("c7")
    register(g1, host=c7.host)

    async def finder(timeout):
        return [c7.host, g1.host]

    c = await running("Family Room", finder=finder, prober=any_hello)
    await connected(c)
    assert c.tv.host == g1.host and c.moved_from == c7.host
    assert c7.events("register") == []  # the key never went to the C7
    saved = json.loads((home / "config.json").read_text())["tvs"][0]
    assert saved["host"] == g1.host
    assert "found at a new address" in c.snapshot()["warnings"][0]


async def test_platform_change_is_saved(home, tvs, running):
    # The C5 takes webOS 26 (ASSUMPTION W-HELLO-VERSION)
    tv = await tvs("c5-webos26")
    register(tv, platform="10.3.1")
    c = await connected(await running("Office"))
    assert c.tv.platform == "11.2.0" and load_settings().tv("Office").platform == "11.2.0"


async def test_certificate_change_warns_but_connects(home, tvs, running):
    # ASSUMPTION W-TLS-CERT (on the C5: TLS only, where the G1's session uses plain ws on 3000)
    tv = await tvs("c5-webos25")
    c = await connected(await running(register(tv, cert_sha256="00" * 32)))
    assert "TLS certificate differs" in c.snapshot()["warnings"][0]


async def test_refresh_is_one_bounded_attempt(home, tvs, running):
    tv = await tvs("c7")
    c = await connected(await running(register(tv)))
    await tv.unplug()
    await wait_until(lambda: not c.reachable)
    c.poll_interval = 60.0  # the supervisor won't try again on its own soon
    await asyncio.sleep(0.3)
    tv.power = "Active"
    await tv._network_up()
    await c.refresh()
    assert c.reachable


async def test_sources_and_select(home, tvs, running):
    # ASSUMPTION W-APP-IDS, W-INPUT-IDS (on the C5, which reports now_playing; the G1 doesn't)
    tv = await tvs("c5-webos25")
    c = await connected(await running(register(tv)))
    sources = c.sources()
    apps = {s["name"] for s in sources if s["kind"] == "app"}
    assert "netflix" in apps and "Web Browser" not in str(sources)  # not allow-listed: never offered
    assert {s["name"] for s in sources if s["kind"] == "input"} == {"Onkyo", "HDMI 2", "Apple TV"}

    r = await c.select_source("Netflix")
    assert (r.outcome, tv.foreground) == ("done", "netflix")
    assert (await c.select_source("netflix")).outcome == "unchanged"
    assert c.snapshot()["now_playing"] == {"app": "netflix", "title": "Netflix", "play_state": "playing"}
    r = await c.select_source("onkyo")
    assert tv.foreground == "com.webos.app.hdmi1" and r.sent == ["ssap://tv/switchInput HDMI_1"]
    with pytest.raises(WebOsError, match="matches several sources"):
        await c.select_source("Apple TV")  # an app and an input label: never guess
    with pytest.raises(WebOsError, match="No source called 'browser'"):
        await c.select_source("browser")


async def test_keys_and_media(home, tvs, running):
    # ASSUMPTION W-KEYS, W-MEDIA-CONTROLS
    tv = await tvs("c5-webos25")
    c = await connected(await running(register(tv)))
    r = await c.send_key("DOWN", 3)
    assert r.sent == ["button DOWN"] * 3
    await wait_until(lambda: len(tv.events("button")) == 3)
    await c.select_source("youtube")
    await c.media_control("pause")
    assert "media.controls/pause" in tv.requests
    await wait_until(lambda: c.snapshot()["now_playing"]["play_state"] == "paused")


async def test_no_now_playing_on_webos6(home, tvs, running):
    # ASSUMPTION W-NOW-PLAYING, W-SUBSCRIBE: the G1 answers 404 to the media subscription and still
    # connects; media controls still work, only the play state is never known
    tv = await tvs("g1")
    c = await connected(await running(register(tv)))
    await c.select_source("netflix")
    await c.media_control("pause")
    assert "media.controls/pause" in tv.requests and tv.foreground == "netflix"
    assert c.snapshot()["now_playing"] is None and c.reachable


async def test_volume_belongs_to_the_receiver_on_external_outputs(home, tvs, running):
    # ASSUMPTION W-VOLUME-ARC
    tv = await tvs("g1")
    c = await connected(await running(register(tv)))
    with pytest.raises(WebOsError, match="belongs to the receiver"):
        await c.set_volume(10)
    with pytest.raises(WebOsError, match="belongs to the receiver"):
        await c.set_mute(True)
    assert "audio/setVolume" not in tv.requests


async def test_volume_capped_on_tv_speakers(home, tvs, running):
    tv = await tvs("c7")
    c = await connected(await running(register(tv, max_volume=30)))
    r = await c.set_volume(80)
    assert tv.volume == 30 and r.warnings == ["capped at 30 (max_volume for Bedroom)"]
    assert (await c.set_volume(30)).outcome == "unchanged"
    assert (await c.set_mute(True)).outcome == "done" and tv.muted


async def test_volume_owner_override(home, tvs, running):
    tv = await tvs("g1")
    c = await connected(await running(register(tv, volume_owner="tv")))
    assert (await c.set_volume(20)).outcome == "done" and tv.volume == 20


async def test_screen(home, tvs, running):
    # ASSUMPTION W-SCREEN-OFF: "active" only, never "passive"
    g1, old = await tvs("g1"), await tvs("c7", faults=no_screen_off())  # a set without the endpoints
    c = await connected(await running(register(g1)))
    assert (await c.set_screen(False)).outcome == "done" and c.power() == "screen_off"
    assert (await c.set_screen(False)).outcome == "unchanged"
    assert (await c.set_screen(True)).outcome == "done" and g1.power == "Active"
    assert not g1.screen_unrecoverable
    assert all(p == {"standbyMode": "active"} for _, uri, p in g1.events("request") if "Screen" in uri)

    b = await connected(await running(register(old)))
    with pytest.raises(WebOsError, match="no screen-off command"):
        await b.set_screen(False)
    assert "screen: this TV has no screen-off command" in b.snapshot()["limitations"]


async def test_standby_refuses_actions_that_need_the_screen(home, tvs, running):
    tv = await tvs("g1")
    c = await connected(await running(register(tv)))
    await c.power_off()
    with pytest.raises(WebOsError, match="in standby"):
        await c.select_source("netflix")


async def test_dry_run_sends_nothing(home, tvs, running, monkeypatch):
    monkeypatch.setenv("WEBOS_DRY_RUN", "1")
    tv = await tvs("c7")
    c = await connected(await running(register(tv)))
    before = list(tv.requests)
    for result in (
        await c.set_volume(20),
        await c.select_source("netflix"),
        await c.send_key("HOME", 1),
        await c.media_control("play"),
        await c.power_off(),
        await c.set_mute(True),
    ):
        assert result.outcome == "dry_run" and result.sent
    assert tv.requests == before and tv.events("button") == []


async def test_stop_is_bounded(home, tvs, running):
    tv = await tvs("g1")
    c = await connected(await running(register(tv)))
    await asyncio.wait_for(c.stop(), 6)
    assert c.client is None


def test_clean_strings_from_the_tv():
    assert clean("Net\x00flix‮  Plus\n") == "Netflix Plus"
    assert len(clean("x" * 500)) == 64
