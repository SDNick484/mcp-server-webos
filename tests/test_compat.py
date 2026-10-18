"""compat.py: the two aiowebostv overrides, and the library internals they rely on.

The contract tests fail when an aiowebostv release moves what compat.py
overrides, so the version pin can only move once this file says it's safe.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from importlib.metadata import version

import pytest
from aiowebostv import WebOsClient, WebOsTvPairError
from aiowebostv import webos_client as wc
from aiowebostv.exceptions import WebOsTvCommandError

from webos_mcp.compat import HANDSHAKE_CLOSED, CompatClient, KeyNotAccepted, PairingRefused, WrongDevice
from webos_mcp.sim.fake_tv import BLACKLISTED, DENIED, Faults

pytestmark = pytest.mark.anyio

POINTER = "com.webos.service.networkinput/getPointerInputSocket"


# --- the contract with aiowebostv's internals --------------------------------------------
def test_version_is_inside_the_pin():
    major, minor = (int(x) for x in version("aiowebostv").split(".")[:2])
    assert (major, minor) >= (0, 9) and (major, minor) < (0, 11)


def test_connect_handler_still_calls_what_we_override():
    src = inspect.getsource(WebOsClient.connect_handler)
    assert "await self._check_registration(main_ws)" in src
    assert "input_ws = await self._create_input_ws()" in src
    assert "self._rx_msgs_input_ws(input_ws)" in src  # read in a task; its end closes everything
    assert "self.input_connection = input_ws" in src


def test_overridden_signatures_match():
    for name in ("_check_registration", "_create_input_ws"):
        ours, theirs = inspect.signature(getattr(CompatClient, name)), inspect.signature(getattr(WebOsClient, name))
        assert list(ours.parameters) == list(theirs.parameters), name


def test_what_we_read_from_the_library_exists():
    assert isinstance(wc.RECEIVE_TIMEOUT, int | float)
    client_src = inspect.getsource(WebOsClient)
    assert 'self.tv_info.hello = response["payload"]' in client_src  # hello lands before registration
    assert "await self.input_connection.send_str(message)" in inspect.getsource(WebOsClient.input_command)
    assert "input_ws.close()" in inspect.getsource(WebOsClient._closeout_tasks)


async def test_a_close_before_the_hello_raises_handshake_closed(tvs):
    # ASSUMPTION W-HELLO-1008: aiowebostv doesn't handle a close frame while waiting for the hello, so aiohttp's
    # WSMessageTypeError (a TypeError) comes out of connect(). If a release handles it, this fails: revisit.
    tv = await tvs("c5-webos25", faults=Faults(close_before_hello=1))
    tv.pair_with("k" * 32)
    client = CompatClient(tv.host, "k" * 32)
    try:
        with pytest.raises(HANDSHAKE_CLOSED):
            await client.connect()
        assert tv.events("closed-at-hello") == [("closed-at-hello",)]
    finally:
        await client.disconnect()


# --- registration ------------------------------------------------------------------------
async def test_pairing_waits_for_acceptance(tvs):
    tv = await tvs("c5-webos25", accept_after=0.3)  # TLS only: the session is on 3001
    client = CompatClient(tv.host, None, pair_timeout=5)
    await client.connect()
    try:
        assert client.client_key in tv.keys and client.prompted
        assert client.port() == 3001 and client.cert_sha256() and len(client.cert_sha256()) == 64
    finally:
        await client.disconnect()


async def test_plain_port_has_no_certificate(tvs):
    tv = await tvs("c7")
    client = CompatClient(tv.host, None, pair_timeout=5)
    await client.connect()
    try:
        assert (client.port(), client.cert_sha256()) == (3000, None)
    finally:
        await client.disconnect()


async def test_nobody_accepts(tvs):
    tv = await tvs("g1", pairing="ignore")
    with pytest.raises(WebOsTvPairError, match="nobody accepted"):
        await CompatClient(tv.host, None, pair_timeout=0.3).connect()


async def test_declined(tvs):
    tv = await tvs("g1", pairing="decline")
    with pytest.raises(WebOsTvPairError, match="403 User rejected"):
        await CompatClient(tv.host, None, pair_timeout=5).connect()


async def test_a_known_key_connects_without_a_dialog(tvs):
    tv = await tvs("c5-webos25")
    tv.pair_with("k" * 32)
    client = CompatClient(tv.host, "k" * 32, expected_uuid=tv.profile.uuid)
    await client.connect()
    await client.disconnect()
    assert tv.events("prompt") == [] and not client.prompted  # ASSUMPTION W-REGISTERED-DIRECT


async def test_prompt_then_registered_is_still_a_known_key(tvs):
    tv = await tvs("g1", faults=Faults(prompt_then_registered=True))
    tv.pair_with("k" * 32)
    client = CompatClient(tv.host, "k" * 32)
    await client.connect()
    await client.disconnect()


async def test_a_stale_key_fails_fast_and_closes_the_dialog(tvs):
    # ASSUMPTION W-STALE-KEY, W-PROMPT-DISMISS
    tv = await tvs("g1", accept_after=1.0)
    client = CompatClient(tv.host, "0" * 32, prompt_grace=0.2)
    t0 = time.monotonic()
    with pytest.raises(KeyNotAccepted):
        await client.connect()
    assert time.monotonic() - t0 < 1.0  # not aiowebostv's 10 s, and not a TimeoutError
    await asyncio.sleep(1.1)
    assert tv.events("prompt") == [("prompt",)] and tv.events("prompt-dismissed") == [("prompt-dismissed",)]


async def test_the_tvs_own_reason_is_kept(tvs):
    # aiowebostv would say "Client key not set, pairing failed." (ASSUMPTION W-SIGNED-403)
    tv = await tvs("c5-webos26", faults=Faults(reject_registration=BLACKLISTED))
    with pytest.raises(PairingRefused, match="blacklisted certificate detected"):
        await CompatClient(tv.host, None, pair_timeout=5).connect()


async def test_a_key_is_never_sent_to_another_device(tvs):
    # ASSUMPTION W-UUID-STABLE
    tv = await tvs("g1")
    with pytest.raises(WrongDevice):
        await CompatClient(tv.host, "k" * 32, expected_uuid="someone-else").connect()
    assert tv.events("register") == []


# --- the pointer socket --------------------------------------------------------------------
async def test_a_refused_pointer_socket_only_costs_buttons(tvs):
    # ASSUMPTION W-INPUT-SOCKET: plain aiowebostv fails the whole connection (test_wire).
    tv = await tvs("g1", faults=Faults(deny={POINTER: DENIED}))
    client = CompatClient(tv.host, None, pair_timeout=5)
    await client.connect()
    try:
        assert client.buttons_denied and client.is_connected()
        with pytest.raises(WebOsTvCommandError, match="refused the pointer socket"):
            await client.button("HOME")
        await client.set_volume(15)  # everything else still works
        await asyncio.sleep(0.2)
        assert client.is_connected() and tv.volume == 15
    finally:
        await client.disconnect()
    assert not client.is_connected()


async def test_other_errors_on_the_pointer_socket_still_fail(tvs):
    tv = await tvs("g1", faults=Faults(deny={POINTER: "500 Application error"}))
    with pytest.raises(WebOsTvCommandError):
        await CompatClient(tv.host, None, pair_timeout=5).connect()
