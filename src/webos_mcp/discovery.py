"""Find webOS TVs on the LAN with SSDP (ASSUMPTION W-SSDP).

An awake TV answers an M-SEARCH for its second-screen service with a
LOCATION URL on its own address. Only answers whose LOCATION is the address
that answered, and on a private network, are kept: an SSDP reply is
unauthenticated, and it shouldn't be able to point us anywhere else. (The
private-network rule is lgtv-mcp's.) A TV that is off doesn't answer.
"""

from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlparse

from .config import is_local_ipv4

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGET = "urn:lge-com:service:webos-second-screen:1"
M_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    f"ST: {SEARCH_TARGET}\r\n\r\n"
).encode()


def parse_ssdp(data: bytes, sender: str) -> str | None:
    """The TV's address from one SSDP reply, or None if it isn't one we trust."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - decode with "replace" doesn't raise
        return None
    headers = {}
    for line in text.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if SEARCH_TARGET not in headers.get("st", "") and SEARCH_TARGET not in headers.get("usn", ""):
        return None
    host = urlparse(headers.get("location", "")).hostname
    if host != sender or not is_local_ipv4(sender):
        return None
    return sender


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: list[str] = []

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        host = parse_ssdp(data, str(addr[0]))
        if host and host not in self.found:
            self.found.append(host)


async def discover(timeout: float = 3.0) -> list[str]:
    """Addresses of the webOS TVs that answered within timeout."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(_Collector, local_addr=("0.0.0.0", 0))
    try:
        for _ in range(2):  # UDP: say it twice
            with contextlib.suppress(OSError):
                transport.sendto(M_SEARCH, SSDP_ADDR)
            await asyncio.sleep(timeout / 2)
    finally:
        transport.close()
    return list(protocol.found)
