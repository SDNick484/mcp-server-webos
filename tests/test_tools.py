"""MCP tools, called through a real MCP client connected in-process, against simulated TVs.

This tests the contract the model actually sees: tools/list (names, titles,
annotations, input and output schemas), argument validation, and tool
results, not just the Python functions underneath.
"""

from __future__ import annotations

import json

import pytest
from mcp import Client

from webos_mcp import server
from webos_mcp.client import TvClient
from webos_mcp.config import ALLOWED_KEYS

from .conftest import fast, register, wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
def fast_clients(monkeypatch):
    monkeypatch.setattr(server, "TvClient", lambda tv, s: fast(TvClient(tv, s)))


@pytest.fixture
async def two_tvs(home, tvs, fast_clients):
    """The Family Room G1 (sound to the receiver) and the Bedroom C7 (its own speakers)."""
    g1, c7 = await tvs("g1"), await tvs("c7")
    register(g1, aliases=["G1"])
    register(c7, aliases=["C7"])
    async with Client(server.mcp) as c:
        await wait_until(lambda: all(x.reachable and x.cache.sound_output for x in server.registry().clients))
        yield c, g1, c7


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


def text(result) -> str:
    return result.content[0].text


def ok(result) -> dict:
    assert not result.is_error, text(result)
    return result.structured_content


# --- tools/list --------------------------------------------------------------
EXPECTED = {
    "list_tvs",
    "get_status",
    "set_power",
    "set_screen",
    "list_sources",
    "select_source",
    "send_key",
    "media_control",
    "set_volume",
    "set_mute",
}


async def test_tool_names(two_tvs):
    c, *_ = two_tvs
    assert set(await tools(c)) == EXPECTED


async def test_every_tool_has_title_description_and_annotations(two_tvs):
    # Without annotations, the spec says a client should assume the worst
    # (destructive, non-idempotent, open-world). State them explicitly.
    c, *_ = two_tvs
    for t in (await tools(c)).values():
        assert t.title and t.description, t.name
        a = t.annotations
        assert a is not None and a.read_only_hint is not None and a.open_world_hint is False, t.name
        if not a.read_only_hint:
            assert a.destructive_hint is False and a.idempotent_hint is not None, t.name


async def test_annotations_match_behavior(two_tvs):
    c, *_ = two_tvs
    t = await tools(c)
    assert {n for n, x in t.items() if x.annotations.read_only_hint} == {"list_tvs", "get_status", "list_sources"}
    assert t["send_key"].annotations.idempotent_hint is False  # DOWN twice moves twice
    assert t["media_control"].annotations.idempotent_hint is False
    assert t["set_power"].annotations.idempotent_hint is True


async def test_every_tool_takes_tv_except_list_tvs(two_tvs):
    c, *_ = two_tvs
    for name, t in (await tools(c)).items():
        assert ("tv" in t.input_schema["properties"]) is (name != "list_tvs"), name


async def test_key_enum_is_the_allow_list(two_tvs):
    c, *_ = two_tvs
    schema = (await tools(c))["send_key"].input_schema
    key = schema["properties"]["key"]
    enum = key.get("enum") or schema["$defs"][key["$ref"].split("/")[-1]]["enum"]
    assert set(enum) == ALLOWED_KEYS and "POWER" not in enum and "IN_START" not in enum


async def test_no_url_or_text_arguments(two_tvs):
    # Decided: no URL tools, no arbitrary-text tools. The only free strings are names to resolve.
    c, *_ = two_tvs
    for name, t in (await tools(c)).items():
        for arg, prop in t.input_schema["properties"].items():
            if arg not in ("tv", "source"):
                assert "string" not in json.dumps(prop) or "enum" in json.dumps(prop) or "$ref" in prop, (name, arg)


# --- never guess ------------------------------------------------------------------
async def test_actions_without_tv_are_refused_when_there_are_several(two_tvs):
    c, g1, c7 = two_tvs
    result = await c.call_tool("set_volume", {"level": 10})
    assert result.is_error and "Which TV? Several are configured: Family Room" in text(result)
    assert "audio/setVolume" not in g1.requests + c7.requests


async def test_status_without_tv_covers_all(two_tvs):
    c, *_ = two_tvs
    status = ok(await c.call_tool("get_status", {}))
    assert [s["tv"] for s in status["tvs"]] == ["Family Room", "Bedroom"]
    one = ok(await c.call_tool("get_status", {"tv": "g1"}))
    assert [s["tv"] for s in one["tvs"]] == ["Family Room"]


async def test_unknown_tv(two_tvs):
    c, *_ = two_tvs
    result = await c.call_tool("set_mute", {"muted": True, "tv": "garage"})
    assert result.is_error and "No TV called 'garage'" in text(result)


async def test_list_tvs(two_tvs):
    c, *_ = two_tvs
    rows = ok(await c.call_tool("list_tvs", {}))["result"]
    assert rows[0] == {
        "tv": "Family Room",
        "aliases": ["G1"],
        "model": "OLED77G1PUA",
        "platform": "6.3.1",
        "paired": True,
        "reachable": True,
    }


# --- actions --------------------------------------------------------------------------
async def test_power_off_all_and_on(two_tvs):
    c, g1, c7 = two_tvs
    r = ok(await c.call_tool("set_power", {"state": "off", "tv": "all"}))
    assert r["tv"] == "all" and r["outcome"] == "done"
    assert g1.power == "Active Standby" and c7.power == "Off"
    r = ok(await c.call_tool("set_power", {"state": "on", "tv": "Bedroom"}))
    assert r["outcome"] == "done" and r["sent"][0].startswith("wol ") and c7.power == "Active"
    result = await c.call_tool("set_power", {"state": "on", "tv": "all"})
    assert result.is_error and "only works for set_power with state='off'" in text(result)


async def test_volume_refused_where_the_receiver_owns_it(two_tvs):
    c, g1, c7 = two_tvs
    result = await c.call_tool("set_volume", {"level": 10, "tv": "Family Room"})
    assert result.is_error and "belongs to the receiver" in text(result)
    r = ok(await c.call_tool("set_volume", {"level": 15, "tv": "Bedroom"}))
    assert (r["tv"], r["outcome"], r["sent"]) == ("Bedroom", "done", ["ssap://audio/setVolume 15"])
    assert c7.volume == 15


async def test_volume_range_is_validated_by_the_schema(two_tvs):
    c, *_ = two_tvs
    result = await c.call_tool("set_volume", {"level": 101, "tv": "Bedroom"})
    assert result.is_error


async def test_sources(two_tvs):
    c, g1, _ = two_tvs
    sources = ok(await c.call_tool("list_sources", {"tv": "g1"}))["result"]
    assert {"kind": "input", "name": "Onkyo", "id": "HDMI_1", "connected": True} in sources
    r = ok(await c.call_tool("select_source", {"source": "youtube", "tv": "g1"}))
    assert r["outcome"] == "done" and g1.foreground == "youtube.leanback.v4"
    result = await c.call_tool("select_source", {"source": "apple tv", "tv": "g1"})
    assert result.is_error and "matches several sources" in text(result)


async def test_keys_media_screen(two_tvs):
    c, g1, c7 = two_tvs
    r = ok(await c.call_tool("send_key", {"key": "DOWN", "repeat": 2, "tv": "g1"}))
    assert r["sent"] == ["button DOWN", "button DOWN"]
    result = await c.call_tool("send_key", {"key": "POWER", "tv": "g1"})
    assert result.is_error  # not in the enum: the SDK rejects it before our code runs
    ok(await c.call_tool("media_control", {"action": "pause", "tv": "g1"}))
    assert "media.controls/pause" in g1.requests
    assert ok(await c.call_tool("set_screen", {"state": "off", "tv": "g1"}))["outcome"] == "done"
    result = await c.call_tool("set_screen", {"state": "off", "tv": "c7"})
    assert result.is_error and "no screen-off command" in text(result)


async def test_dry_run(home, tvs, fast_clients, monkeypatch):
    monkeypatch.setenv("WEBOS_DRY_RUN", "1")
    c7 = await tvs("c7")
    register(c7)
    async with Client(server.mcp) as c:
        await wait_until(lambda: server.registry().clients[0].reachable)
        r = ok(await c.call_tool("set_volume", {"level": 20}))
        assert r["outcome"] == "dry_run" and r["detail"].startswith("DRY RUN, nothing sent: would set")
        assert ok(await c.call_tool("get_status", {}))["tvs"][0]["dry_run"] is True
    assert "audio/setVolume" not in c7.requests


async def test_no_tvs_configured(home, fast_clients):
    async with Client(server.mcp) as c:
        assert ok(await c.call_tool("get_status", {})) == {"tvs": []}
        result = await c.call_tool("set_power", {"state": "off"})
        assert result.is_error and "No TVs are configured" in text(result)


# --- resources and prompts --------------------------------------------------------------
async def test_resources(two_tvs):
    c, *_ = two_tvs
    keys = json.loads((await c.read_resource("webos://keys")).contents[0].text)
    assert "IN_START" in keys["left_out"] and set(keys["allowed"]) == ALLOWED_KEYS
    apps = json.loads((await c.read_resource("webos://apps")).contents[0].text)
    assert apps["netflix"] == {"id": "netflix", "title": "Netflix", "source": "default"}
    tvs = json.loads((await c.read_resource("webos://tvs")).contents[0].text)
    assert set(tvs) == {"Family Room", "Bedroom"} and "screen" in tvs["Family Room"]["capabilities"]


async def test_watch_prompt(two_tvs):
    c, *_ = two_tvs
    prompt = await c.get_prompt("watch", {"app": "netflix", "tv": "Bedroom"})
    body = prompt.messages[0].content.text
    assert "select_source with source='netflix', tv='Bedroom'" in body and "receiver owns it" in body
