"""Ask a TV who it is, without a key: the SSAP hello.

The hello is the one exchange a webOS TV answers before pairing. It carries
the TV's deviceUUID and platform version (ASSUMPTION W-HELLO-VERSION), so it
is how `pair` learns what it is pairing with, how rediscovery recognizes a
TV at a new address, and how doctor checks each layer. It sends no key and
puts nothing on screen.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

import aiohttp

PLAIN, TLS = 3000, 3001


@dataclass
class Hello:
    host: str
    port: int
    payload: dict[str, Any] = field(default_factory=dict)
    cert_sha256: str | None = None

    @property
    def uuid(self) -> str | None:
        v = self.payload.get("deviceUUID")
        return v if isinstance(v, str) and v else None

    @property
    def platform(self) -> str | None:
        v = self.payload.get("deviceOSReleaseVersion")
        return v if isinstance(v, str) and v else None


async def hello(host: str, port: int, timeout: float = 3.0) -> Hello:
    """The hello on one port (3000: ws, 3001: wss). Raises OSError/TimeoutError/aiohttp errors."""
    scheme = "wss" if port == TLS else "ws"
    async with asyncio.timeout(timeout):
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(f"{scheme}://{host}:{port}", ssl=False) as ws,
        ):
            ssl_object = ws.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
            await ws.send_json({"id": "hello", "type": "hello", "payload": {}})
            reply = await ws.receive_json()
    if not isinstance(reply, dict) or reply.get("type") != "hello":
        raise ValueError(f"not a webOS hello: {str(reply)[:80]}")
    raw = reply.get("payload")
    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return Hello(host, port, payload, hashlib.sha256(der).hexdigest() if der else None)


async def any_hello(host: str, timeout: float = 3.0) -> Hello | None:
    """Try 3000, then 3001 (unlike aiowebostv, also after a timeout). None if neither answers."""
    for port in (PLAIN, TLS):
        try:
            return await hello(host, port, timeout)
        except (OSError, TimeoutError, ValueError, aiohttp.ClientError):
            continue
    return None
