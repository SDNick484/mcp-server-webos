"""The real aiowebostv library against the simulated TVs, over real sockets.

These pin down what the library itself does with each generation's behavior,
before any of this server's code is involved. Several are the reasons
compat.py exists: they show the library failing in ways the server must not.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiowebostv import WebOsClient, WebOsTvPairError
from aiowebostv import webos_client as wc
from aiowebostv.exceptions import WebOsTvResponseTypeError

from webos_mcp.sim.fake_tv import BLACKLISTED, DENIED, Faults

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("profile", "port", "power_state"),
    [("c7", 3000, False), ("g1", 3001, True), ("c5-webos25", 3001, True), ("c5-webos26", 3001, True)],
)
async def test_pairs_and_subscribes(tvs, profile, port, power_state):
    # ASSUMPTION W-PORTS, W-MANIFEST, W-SUBSCRIBE
    tv = await tvs(profile)
    client = WebOsClient(tv.host, None)
    try:
        await client.connect()
        assert client.client_key in tv.keys and tv.events("prompt") == [("prompt",)]
        assert [e for e in tv.events("connect") if e[2] == "ws"] == [("connect", port, "ws")]
        assert client.tv_state.is_on and client.tv_state.sound_output == tv.sound_output
        assert bool(client.tv_state.power_state) is power_state  # ASSUMPTION W-C7-NO-POWERSTATE
        assert client.tv_info.hello["deviceUUID"] == tv.profile.uuid  # ASSUMPTION W-HELLO-VERSION
        await client.set_volume(20)
        for _ in range(50):
            if client.tv_state.volume == 20:
                break
            await asyncio.sleep(0.02)
        assert client.tv_state.volume == 20  # pushed back over the subscription
    finally:
        await client.disconnect()


async def test_aiowebostv_sends_no_signature(tvs):
    tv = await tvs("c5-webos26")
    client = WebOsClient(tv.host, None)
    await client.connect()
    await client.disconnect()
    assert tv.events("register") == [("register", {"signed": False, "with_key": False})]


async def test_webos26_blacklists_the_old_signed_manifest(tvs):
    # What every client sent until 2026 (ASSUMPTION W-SIGNED-403): rejected before any dialog.
    tv = await tvs("c5-webos26")
    old = {
        "type": "register",
        "id": "register_0",
        "payload": {
            "pairingType": "PROMPT",
            "manifest": {
                "signed": {"appId": "com.lge.test"},
                "signatures": [{"signature": "x", "signatureVersion": 1}],
            },
        },
    }
    async with aiohttp.ClientSession() as http, http.ws_connect(f"wss://{tv.host}:3001", ssl=False) as ws:
        await ws.send_json(old)
        reply = await ws.receive_json()
    assert reply["error"] == BLACKLISTED and tv.events("prompt") == []


async def test_a_silent_port_3000_is_not_a_reason_to_fall_back(tvs, monkeypatch):
    # ASSUMPTION W-PORT3000-CLOSE: aiowebostv tries 3001 only after a refusal or a failed
    # handshake. A port that accepts and then says nothing ends in a timeout instead.
    import dataclasses

    from webos_mcp.sim.fake_tv import FakeTv
    from webos_mcp.sim.profiles import PROFILES

    tv = FakeTv(dataclasses.replace(PROFILES["c5-webos25"], port3000="blackhole"), host="127.0.0.6")
    await tv.start()
    try:
        client = WebOsClient(tv.host, None, connect_timeout=0.3)
        with pytest.raises(TimeoutError):
            await client.connect()
        assert not [e for e in tv.events("connect") if e[2] == "ws"]
    finally:
        await tv.stop()


async def test_a_stale_key_looks_like_an_unreachable_tv(tvs, monkeypatch):
    # ASSUMPTION W-STALE-KEY: the TV shows the dialog again; plain aiowebostv then waits and
    # reports a bare TimeoutError, the same as a TV that is off. compat.py exists for this.
    monkeypatch.setattr(wc, "RECEIVE_TIMEOUT", 0.3)
    tv = await tvs("g1", pairing="ignore")
    client = WebOsClient(tv.host, "0" * 32)
    with pytest.raises(TimeoutError):
        await client.connect()
    assert tv.events("prompt") == [("prompt",)]


async def test_a_refused_pointer_socket_fails_the_whole_connection(tvs):
    # ASSUMPTION W-INPUT-SOCKET: plain aiowebostv gives up on everything, not just buttons.
    tv = await tvs("g1", faults=Faults(deny={"com.webos.service.networkinput/getPointerInputSocket": DENIED}))
    client = WebOsClient(tv.host, None)
    with pytest.raises(WebOsTvResponseTypeError):
        await client.connect()


async def test_declined_pairing(tvs):
    tv = await tvs("g1", pairing="decline")
    with pytest.raises(WebOsTvPairError):
        await WebOsClient(tv.host, None).connect()


async def test_wake_on_lan_brings_an_unplugged_tv_back(tvs):
    # ASSUMPTION W-WOL, W-WAKE-TIME (shortened)
    import socket

    tv = await tvs("c7")
    await tv.unplug()
    packet = b"\xff" * 6 + bytes.fromhex(tv.mac.replace(":", "")) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(packet, (tv.host, tv.wol_port))
    for _ in range(100):
        if tv.power == "Active":
            break
        await asyncio.sleep(0.02)
    assert tv.power == "Active" and tv.events("wol") == [("wol", tv.mac)]
    client = WebOsClient(tv.host, None)
    await client.connect()
    await client.disconnect()


async def test_turn_off_keeps_the_socket_in_standby_or_drops_it(tvs):
    # ASSUMPTION W-STANDBY-SOCKET, W-POWER-STATES
    g1, c7 = await tvs("g1"), await tvs("c7")
    for tv, expect in ((g1, "Active Standby"), (c7, "Off")):
        client = WebOsClient(tv.host, None)
        await client.connect()
        await client.power_off()
        for _ in range(100):
            if tv.power == expect:
                break
            await asyncio.sleep(0.02)
        assert tv.power == expect
        await client.disconnect()


def test_hello_payload_is_json_serializable():
    from webos_mcp.sim.fake_tv import make

    assert json.dumps(make("g1", "127.0.0.3")._hello())
