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
from webos_mcp.client import MAX_QUERY, MAX_TEXT, SEARCH_LINKS, TvClient
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
    "webos_list_tvs",
    "webos_get_status",
    "webos_set_power",
    "webos_set_screen",
    "webos_list_sources",
    "webos_select_source",
    "webos_send_key",
    "webos_media_control",
    "webos_set_volume",
    "webos_set_mute",
    "webos_insert_text",
    "webos_delete_text",
    "webos_submit_text",
    "webos_search_app",
}


async def test_initialize_reports_the_package_version(two_tvs):
    from webos_mcp import __version__

    c, *_ = two_tvs
    assert c.server_info.version == __version__


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
    expected_read_only = {"webos_list_tvs", "webos_get_status", "webos_list_sources"}
    assert {n for n, x in t.items() if x.annotations.read_only_hint} == expected_read_only
    assert t["webos_send_key"].annotations.idempotent_hint is False  # DOWN twice moves twice
    assert t["webos_media_control"].annotations.idempotent_hint is False
    assert t["webos_set_power"].annotations.idempotent_hint is True
    for name in ("webos_insert_text", "webos_delete_text", "webos_submit_text"):
        assert t[name].annotations.idempotent_hint is False, name  # typing twice types twice
    assert t["webos_search_app"].annotations.idempotent_hint is True


async def test_every_tool_takes_tv_except_list_tvs(two_tvs):
    c, *_ = two_tvs
    for name, t in (await tools(c)).items():
        assert ("tv" in t.input_schema["properties"]) is (name != "webos_list_tvs"), name


async def test_key_enum_is_the_allow_list(two_tvs):
    c, *_ = two_tvs
    schema = (await tools(c))["webos_send_key"].input_schema
    key = schema["properties"]["key"]
    enum = key.get("enum") or schema["$defs"][key["$ref"].split("/")[-1]]["enum"]
    assert set(enum) == ALLOWED_KEYS and "POWER" not in enum and "IN_START" not in enum


# The only free strings: names to resolve, and two bounded kinds of text (decided 2026-11-09):
# what to type into a focused system field, and a query whose link the server builds.
FREE_TEXT = {("webos_insert_text", "text"): MAX_TEXT, ("webos_search_app", "query"): MAX_QUERY}


async def test_no_url_arguments_and_text_only_where_decided(two_tvs):
    c, *_ = two_tvs
    for name, t in (await tools(c)).items():
        for arg, prop in t.input_schema["properties"].items():
            if (name, arg) in FREE_TEXT:
                assert prop["maxLength"] == FREE_TEXT[name, arg] and prop["minLength"] == 1, (name, arg)
            elif arg not in ("tv", "source"):
                closed = any(k in json.dumps(prop) for k in ('"enum"', '"const"')) or "$ref" in prop
                assert "string" not in json.dumps(prop) or closed, (name, arg)
        assert "url" not in json.dumps(t.input_schema["properties"]).lower(), name


async def test_search_app_enum_is_the_link_table(two_tvs):
    c, *_ = two_tvs
    schema = (await tools(c))["webos_search_app"].input_schema
    app = schema["properties"]["app"]
    assert set(app.get("enum") or [app["const"]]) == set(SEARCH_LINKS)  # one value is a "const"


# --- never guess ------------------------------------------------------------------
async def test_actions_without_tv_are_refused_when_there_are_several(two_tvs):
    c, g1, c7 = two_tvs
    result = await c.call_tool("webos_set_volume", {"level": 10})
    assert result.is_error and "Which TV? Several are configured: Family Room" in text(result)
    assert "audio/setVolume" not in g1.requests + c7.requests


async def test_status_without_tv_covers_all(two_tvs):
    c, *_ = two_tvs
    status = ok(await c.call_tool("webos_get_status", {}))
    assert [s["tv"] for s in status["tvs"]] == ["Family Room", "Bedroom"]
    one = ok(await c.call_tool("webos_get_status", {"tv": "g1"}))
    assert [s["tv"] for s in one["tvs"]] == ["Family Room"]


async def test_unknown_tv(two_tvs):
    c, *_ = two_tvs
    result = await c.call_tool("webos_set_mute", {"muted": True, "tv": "garage"})
    assert result.is_error and "No TV called 'garage'" in text(result)


async def test_list_tvs(two_tvs):
    c, *_ = two_tvs
    rows = ok(await c.call_tool("webos_list_tvs", {}))["result"]
    assert rows[0] == {
        "tv": "Family Room",
        "aliases": ["G1"],
        "model": "OLED77G1PUA",
        "platform": "6.5.3",
        "paired": True,
        "reachable": True,
    }


# --- actions --------------------------------------------------------------------------
async def test_power_off_all_and_on(two_tvs):
    c, g1, c7 = two_tvs
    r = ok(await c.call_tool("webos_set_power", {"state": "off", "tv": "all"}))
    assert r["tv"] == "all" and r["outcome"] == "done"
    assert g1.power == "Active Standby" and c7.power == "Off"
    r = ok(await c.call_tool("webos_set_power", {"state": "on", "tv": "Bedroom"}))
    assert r["outcome"] == "done" and r["sent"][0].startswith("wol ") and c7.power == "Active"
    result = await c.call_tool("webos_set_power", {"state": "on", "tv": "all"})
    assert result.is_error and "only works for webos_set_power with state='off'" in text(result)


async def test_volume_refused_where_the_receiver_owns_it(two_tvs):
    c, g1, c7 = two_tvs
    result = await c.call_tool("webos_set_volume", {"level": 10, "tv": "Family Room"})
    assert result.is_error and "belongs to the receiver" in text(result)
    r = ok(await c.call_tool("webos_set_volume", {"level": 15, "tv": "Bedroom"}))
    assert (r["tv"], r["outcome"], r["sent"]) == ("Bedroom", "done", ["ssap://audio/setVolume 15"])
    assert c7.volume == 15


async def test_volume_range_is_validated_by_the_schema(two_tvs):
    c, *_ = two_tvs
    result = await c.call_tool("webos_set_volume", {"level": 101, "tv": "Bedroom"})
    assert result.is_error


async def test_sources(two_tvs):
    c, g1, _ = two_tvs
    sources = ok(await c.call_tool("webos_list_sources", {"tv": "g1"}))["result"]
    assert {"kind": "input", "name": "Onkyo", "id": "HDMI_1", "connected": True} in sources
    r = ok(await c.call_tool("webos_select_source", {"source": "youtube", "tv": "g1"}))
    assert r["outcome"] == "done" and g1.foreground == "youtube.leanback.v4"
    result = await c.call_tool("webos_select_source", {"source": "apple tv", "tv": "g1"})
    assert result.is_error and "matches several sources" in text(result)


async def test_keys_media_screen(two_tvs):
    c, g1, c7 = two_tvs
    r = ok(await c.call_tool("webos_send_key", {"key": "DOWN", "repeat": 2, "tv": "g1"}))
    assert r["sent"] == ["button DOWN", "button DOWN"]
    result = await c.call_tool("webos_send_key", {"key": "POWER", "tv": "g1"})
    assert result.is_error  # not in the enum: the SDK rejects it before our code runs
    ok(await c.call_tool("webos_media_control", {"action": "pause", "tv": "g1"}))
    assert "media.controls/pause" in g1.requests
    assert ok(await c.call_tool("webos_set_screen", {"state": "off", "tv": "g1"}))["outcome"] == "done"
    # The C7 blanks its screen too (ASSUMPTION W-SCREEN-OFF, seen 2026-10-17)
    assert ok(await c.call_tool("webos_set_screen", {"state": "off", "tv": "c7"}))["outcome"] == "done"
    assert c7.power == "Screen Off"


async def test_dry_run(home, tvs, fast_clients, monkeypatch):
    monkeypatch.setenv("WEBOS_DRY_RUN", "1")
    c7 = await tvs("c7")
    register(c7)
    async with Client(server.mcp) as c:
        await wait_until(lambda: server.registry().clients[0].reachable)
        r = ok(await c.call_tool("webos_set_volume", {"level": 20}))
        assert r["outcome"] == "dry_run" and r["detail"].startswith("DRY RUN, nothing sent: would set")
        assert ok(await c.call_tool("webos_get_status", {}))["tvs"][0]["dry_run"] is True
    assert "audio/setVolume" not in c7.requests


async def test_no_tvs_configured(home, fast_clients):
    async with Client(server.mcp) as c:
        status = ok(await c.call_tool("webos_get_status", {}))
        assert status["tvs"] == [] and "pair --host" in status["hint"]
        result = await c.call_tool("webos_set_power", {"state": "off"})
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
    prompt = await c.get_prompt("webos_watch", {"app": "netflix", "tv": "Bedroom"})
    body = prompt.messages[0].content.text
    assert "webos_select_source with source='netflix', tv='Bedroom'" in body and "receiver owns it" in body


async def test_power_rate_limit_refusal(two_tvs):
    c, g1, _ = two_tvs
    # Capacity is 2.0; cycling screen off -> on -> off rapidly is refused on the third call
    res1 = await c.call_tool("webos_set_screen", {"state": "off", "tv": "g1"})
    assert not res1.is_error
    res2 = await c.call_tool("webos_set_screen", {"state": "on", "tv": "g1"})
    assert not res2.is_error
    res3 = await c.call_tool("webos_set_screen", {"state": "off", "tv": "g1"})
    assert res3.is_error
    assert "Rate limit on Family Room" in text(res3)
    assert "try again in" in text(res3)


async def test_key_rate_limit_refusal(two_tvs):
    c, g1, _ = two_tvs
    # Capacity is 10.0; repeat=10 succeeds (10 tokens), subsequent repeat=10 immediately fails
    res1 = await c.call_tool("webos_send_key", {"key": "DOWN", "repeat": 10, "tv": "g1"})
    assert not res1.is_error
    res2 = await c.call_tool("webos_send_key", {"key": "DOWN", "repeat": 10, "tv": "g1"})
    assert res2.is_error
    assert "Rate limit on Family Room" in text(res2)


async def test_source_fuzzy_suggestions(two_tvs):
    c, g1, _ = two_tvs
    # Typo in source name suggests the closest matches
    result = await c.call_tool("webos_select_source", {"source": "netfli", "tv": "g1"})
    assert result.is_error
    assert "Closest matches: 'netflix'" in text(result)


# --- text input and app search ------------------------------------------------------------
IME = "com.webos.service.ime/"


def ime_requests(tv) -> list[str]:
    return [r for r in tv.requests if r.startswith(IME) and not r.endswith("registerRemoteKeyboard")]


async def test_typing_into_lgs_search(two_tvs):
    # ASSUMPTION W-IME-TEXT, W-IME-LENGTH: the G1 confirms each step by the field's length
    c, g1, _ = two_tvs
    await g1.focus_field()
    r = ok(await c.call_tool("webos_insert_text", {"text": "hello", "tv": "g1"}))
    assert (r["outcome"], r["warnings"]) == ("done", []) and g1.text_field == "hello"
    assert r["sent"] == ["ssap://com.webos.service.ime/insertText 'hello'"]
    ok(await c.call_tool("webos_delete_text", {"count": 2, "tv": "g1"}))
    ok(await c.call_tool("webos_insert_text", {"text": " world", "tv": "g1"}))
    assert g1.text_field == "hel world"
    r = ok(await c.call_tool("webos_insert_text", {"text": "lofi beats", "replace": True, "tv": "g1"}))
    assert r["warnings"] == [] and g1.text_field == "lofi beats"
    r = ok(await c.call_tool("webos_submit_text", {"tv": "g1"}))
    assert r["warnings"] == [] and g1.submitted == ["lofi beats"]
    # The subscription is only held for the call (ASSUMPTION W-IME-FOCUS)
    assert not [s for s in g1._subs if s.uri.endswith("registerRemoteKeyboard")]


async def test_no_focused_field_is_refused_and_nothing_is_typed(two_tvs):
    # In YouTube or Netflix the TV would answer true and type nothing (ASSUMPTION W-IME-FOCUS)
    c, g1, _ = two_tvs
    for tool, args in (("webos_insert_text", {"text": "hi"}), ("webos_delete_text", {}), ("webos_submit_text", {})):
        result = await c.call_tool(tool, {**args, "tv": "g1"})
        assert result.is_error and "No text field has focus on Family Room" in text(result), tool
        assert "webos_search_app" in text(result)
    assert ime_requests(g1) == []


async def test_a_tv_that_doesnt_report_lengths_warns(two_tvs):
    # The C7 says a field has focus but not what it holds (ASSUMPTION W-IME-LENGTH)
    c, _, c7 = two_tvs
    result = await c.call_tool("webos_insert_text", {"text": "hi", "tv": "c7"})
    assert result.is_error and "No text field has focus on Bedroom" in text(result)  # no currentWidget at all
    await c7.focus_field()
    r = ok(await c.call_tool("webos_insert_text", {"text": "hi", "tv": "c7"}))
    assert c7.text_field == "hi" and r["warnings"][0].startswith("unconfirmed: this TV doesn't report")
    status = ok(await c.call_tool("webos_get_status", {"tv": "c7"}))["tvs"][0]
    assert "text_input" in status["capabilities"]
    assert any("sent unconfirmed" in lim for lim in status["limitations"])


async def test_text_is_bounded_and_printable(two_tvs):
    c, g1, _ = two_tvs
    await g1.focus_field()
    for bad in ("", "x" * (MAX_TEXT + 1)):  # the schema refuses these before our code runs
        assert (await c.call_tool("webos_insert_text", {"text": bad, "tv": "g1"})).is_error
    result = await c.call_tool("webos_insert_text", {"text": "two\nlines", "tv": "g1"})
    assert result.is_error and "use webos_submit_text" in text(result)
    assert (await c.call_tool("webos_insert_text", {"text": "   ", "tv": "g1"})).is_error
    assert (await c.call_tool("webos_delete_text", {"count": 0, "tv": "g1"})).is_error
    assert ime_requests(g1) == [] and g1.text_field == ""


async def test_delete_in_an_empty_field_sends_nothing(two_tvs):
    c, g1, _ = two_tvs
    await g1.focus_field()
    r = ok(await c.call_tool("webos_delete_text", {"count": 3, "tv": "g1"}))
    assert (r["outcome"], r["sent"]) == ("unchanged", []) and ime_requests(g1) == []


async def test_text_tools_dry_run(home, tvs, fast_clients, monkeypatch):
    monkeypatch.setenv("WEBOS_DRY_RUN", "1")
    g1 = await tvs("g1")
    register(g1)
    await g1.focus_field()
    async with Client(server.mcp) as c:
        await wait_until(lambda: server.registry().clients[0].reachable)
        r = ok(await c.call_tool("webos_insert_text", {"text": "lofi"}))
        assert r["outcome"] == "dry_run" and r["detail"] == "DRY RUN, nothing sent: would type 'lofi' on Family Room"
        assert r["warnings"] == ["not checked in a dry run: whether a text field has focus"]
        for tool in ("webos_delete_text", "webos_submit_text"):
            assert ok(await c.call_tool(tool, {}))["outcome"] == "dry_run"
        r = ok(await c.call_tool("webos_search_app", {"app": "youtube", "query": "lofi"}))
        assert r["outcome"] == "dry_run" and r["sent"][0].endswith("contentTarget=https://www.youtube.com/tv?q=lofi")
    assert not [r for r in g1.requests if r.startswith(IME)] and g1.text_field == "" and g1.searches == []


async def test_search_app(two_tvs):
    # ASSUMPTION W-YOUTUBE-SEARCH: the server builds the link; the query is URL-encoded into it
    c, g1, c7 = two_tvs
    r = ok(await c.call_tool("webos_search_app", {"app": "youtube", "query": "lofi beats & rain", "tv": "g1"}))
    target = "https://www.youtube.com/tv?q=lofi%20beats%20%26%20rain"
    assert r["outcome"] == "done" and g1.searches == [target] and g1.foreground == "youtube.leanback.v4"
    assert r["sent"] == [f"ssap://system.launcher/launch youtube.leanback.v4 contentTarget={target}"]
    status = ok(await c.call_tool("webos_get_status", {"tv": "g1"}))["tvs"][0]
    assert {"app_search", "text_input"} <= set(status["capabilities"])
    # The C7 would just open YouTube's home: refused, and nothing sent
    result = await c.call_tool("webos_search_app", {"app": "youtube", "query": "lofi", "tv": "c7"})
    assert result.is_error and "ignores search links" in text(result)
    assert "system.launcher/launch" not in c7.requests
    status = ok(await c.call_tool("webos_get_status", {"tv": "c7"}))["tvs"][0]
    assert "app_search" not in status["capabilities"]
    assert any(lim.startswith("app_search: YouTube on webOS 3.9.3") for lim in status["limitations"])
    result = await c.call_tool("webos_search_app", {"app": "netflix", "query": "x", "tv": "g1"})
    assert result.is_error  # not in the enum


async def test_search_needs_the_app_installed(two_tvs):
    c, g1, _ = two_tvs
    client = server.registry().pick("g1")
    client.cache.apps.pop("youtube.leanback.v4")
    result = await c.call_tool("webos_search_app", {"app": "youtube", "query": "lofi", "tv": "g1"})
    assert result.is_error and "isn't installed on Family Room" in text(result)
    status = ok(await c.call_tool("webos_get_status", {"tv": "g1"}))["tvs"][0]
    assert "app_search" not in status["capabilities"] and not any(
        x.startswith("app_search") for x in status["limitations"]
    )
