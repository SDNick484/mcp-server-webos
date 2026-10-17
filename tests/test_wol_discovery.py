"""Wake-on-LAN packets, SSDP parsing, and the keyless hello probe."""

from __future__ import annotations

import socket

import pytest

from webos_mcp import discovery, probe, wol

pytestmark = pytest.mark.anyio


def test_magic_packet():
    p = wol.magic_packet("A8-23-FE-01-02-03")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("a823fe010203")
    with pytest.raises(ValueError):
        wol.magic_packet("nope")


def test_targets_broadcast_and_the_tv_itself():
    assert wol.targets("192.168.1.50", "255.255.255.255", 9) == [("255.255.255.255", 9), ("192.168.1.50", 9)]


def test_send_reaches_a_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
        rx.bind(("127.0.0.1", 0))
        port = rx.getsockname()[1]
        sent = wol.send(["a8:23:fe:01:02:03"], "127.0.0.1", "127.0.0.1", port)
        assert sent == [f"wol a8:23:fe:01:02:03 -> 127.0.0.1:{port}"]
        assert rx.recv(200) == wol.magic_packet("a8:23:fe:01:02:03")


def test_route_source():
    assert wol.route_source("127.0.0.3") == "127.0.0.1"


REPLY = (
    "HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\n"
    "LOCATION: http://192.168.1.50:1844/abc/description.xml\r\n"
    "ST: urn:lge-com:service:webos-second-screen:1\r\nUSN: uuid:abc::urn:lge-com:service:webos-second-screen:1\r\n\r\n"
)


def test_ssdp_reply_from_the_tv_itself():
    # ASSUMPTION W-SSDP
    assert discovery.parse_ssdp(REPLY.encode(), "192.168.1.50") == "192.168.1.50"


@pytest.mark.parametrize(
    ("reply", "sender"),
    [
        (REPLY, "192.168.1.99"),  # LOCATION points somewhere else
        (REPLY.replace("192.168.1.50", "8.8.8.8"), "8.8.8.8"),  # not a private network
        (REPLY.replace("webos-second-screen", "something-else"), "192.168.1.50"),  # not a webOS TV
    ],
)
def test_ssdp_replies_we_dont_trust(reply, sender):
    assert discovery.parse_ssdp(reply.encode(), sender) is None


async def test_hello_needs_no_key(tvs):
    # ASSUMPTION W-HELLO-VERSION: the platform and identity come before pairing
    g1, c5, c7 = await tvs("g1"), await tvs("c5-webos25"), await tvs("c7")
    h = await probe.any_hello(c5.host)  # TLS only
    assert (h.port, h.uuid, h.platform) == (3001, c5.profile.uuid, "10.3.1") and len(h.cert_sha256) == 64
    h = await probe.any_hello(g1.host)  # both open: plain ws first, as aiowebostv does (W-PORTS)
    assert (h.port, h.uuid, h.platform, h.cert_sha256) == (3000, g1.profile.uuid, "6.5.3", None)
    h = await probe.any_hello(c7.host)
    assert (h.port, h.cert_sha256) == (3000, None)
    assert g1.events("register") == [] and g1.events("prompt") == []


async def test_nobody_home():
    assert await probe.any_hello("127.0.0.9", timeout=0.5) is None
