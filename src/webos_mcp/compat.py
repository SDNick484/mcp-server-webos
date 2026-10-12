"""Every place this server depends on aiowebostv's internals, in one file.

aiowebostv is the right library (Home Assistant's, with a large install base
behind it), but two of its behaviors would hurt here, and neither has a
public switch:

1. **Registration.** When the TV doesn't know our key it shows the pairing
   dialog (ASSUMPTION W-STALE-KEY), and the library waits 10 s and then
   raises a bare TimeoutError, exactly what an unreachable TV produces. A
   server that reconnects on a timer would put that dialog on the TV every
   few seconds, forever. And when the TV refuses the registration outright
   (webOS 26's "403 Pairing rejected: blacklisted certificate detected",
   ASSUMPTION W-SIGNED-403), the library drops the TV's reason and says
   "Client key not set". ``CompatClient._check_registration`` replaces it:
     - the TV's own error text is raised (``PairingRefused``);
     - with a stored key, a PROMPT means the key isn't accepted: after a short
       grace (in case "registered" follows at once, W-REGISTERED-DIRECT) it
       raises ``KeyNotAccepted`` and the connection closes, which should take
       the dialog down with it (W-PROMPT-DISMISS);
     - `pair` passes ``pair_timeout`` and waits for someone to accept;
     - before any key is sent, the TV's hello must carry the deviceUUID we
       paired with (``WrongDevice``), so a key never goes to another device
       that took over the address.
2. **The pointer socket.** connect() opens the button socket unconditionally,
   so a TV that refuses it (401) fails the whole connection: status, power
   off, everything (ASSUMPTION W-INPUT-SOCKET). ``_create_input_ws`` turns a
   permission refusal into a stand-in socket that never ends (so the main
   connection stays up) and makes button presses fail with a clear error.

Both are private methods. pyproject pins aiowebostv <0.11, and
tests/test_compat.py checks the internals these rely on, so a new release
that moves them fails CI instead of failing at a user's TV. The proper fix is
upstream (an option to make the pointer socket optional, and the TV's error
text in WebOsTvPairError); this file shrinks when that lands.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from aiohttp import ClientWebSocketResponse
from aiowebostv import WebOsClient
from aiowebostv import webos_client as _wc
from aiowebostv.exceptions import WebOsTvCommandError, WebOsTvPairError, WebOsTvResponseTypeError


class PairingRefused(WebOsTvPairError):
    """The TV answered the registration with an error, before any dialog."""


class KeyNotAccepted(WebOsTvPairError):
    """The TV doesn't know our key: it wanted to show the pairing dialog."""


class WrongDevice(WebOsTvPairError):
    """Something answers at the address, but it isn't the TV we paired with."""


class _NoInputSocket:
    """Stands in for the pointer socket a TV refused.

    aiowebostv reads the input socket in a task and closes the whole
    connection when that task ends, so this never ends until closed.
    """

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def __aiter__(self) -> _NoInputSocket:
        return self

    async def __anext__(self) -> Any:
        await self._closed.wait()
        raise StopAsyncIteration

    async def close(self) -> bool:
        self._closed.set()
        return True

    async def send_str(self, data: str) -> None:
        raise WebOsTvCommandError("buttons are unavailable: this TV refused the pointer socket")


def _permission_error(exc: WebOsTvResponseTypeError) -> bool:
    response = exc.args[0] if exc.args else None
    text = response.get("error", "") if isinstance(response, dict) else str(response)
    return str(text).startswith("401")


class CompatClient(WebOsClient):
    """aiowebostv's WebOsClient, with the two changes described above.

    pair_timeout None: a stored key is expected to work, and a pairing
    dialog is a failure (KeyNotAccepted). A number: wait that long for
    someone to accept the dialog (used by `pair`).
    """

    def __init__(
        self,
        host: str,
        client_key: str | None = None,
        *,
        expected_uuid: str | None = None,
        pair_timeout: float | None = None,
        prompt_grace: float = 2.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(host, client_key, **kwargs)
        self.expected_uuid = expected_uuid
        self.pair_timeout = pair_timeout
        self.prompt_grace = prompt_grace
        self.prompted = False  # the TV showed (or wanted to show) the pairing dialog
        self.buttons_denied = False

    async def _check_registration(self, ws: ClientWebSocketResponse) -> None:
        uuid = self.tv_info.hello.get("deviceUUID")
        if self.expected_uuid and uuid != self.expected_uuid:
            raise WrongDevice(f"the device at {self.host} reports a different identity than the paired TV")
        await ws.send_json(self.registration_msg())
        async with asyncio.timeout(_wc.RECEIVE_TIMEOUT):
            response = await ws.receive_json()
        kind = response.get("type")
        if kind == "error":
            raise PairingRefused(str(response.get("error") or "the TV refused the registration"))
        if kind == "response" and (response.get("payload") or {}).get("pairingType") == "PROMPT":
            self.prompted = True
            wait = self.prompt_grace if self.pair_timeout is None else self.pair_timeout
            try:
                response = await ws.receive_json(timeout=wait)
            except TimeoutError:
                if self.pair_timeout is None:
                    raise KeyNotAccepted("the TV doesn't accept the stored key and asked to pair again") from None
                raise WebOsTvPairError(f"nobody accepted the pairing request on the TV within {wait:.0f}s") from None
            kind = response.get("type")
            if kind == "error":
                raise WebOsTvPairError(str(response.get("error") or "the pairing request was declined"))
        if kind == "registered":
            key = (response.get("payload") or {}).get("client-key")
            if isinstance(key, str) and key:
                self.client_key = key
        if not self.client_key:
            raise WebOsTvPairError("the TV didn't issue a client key")

    async def _create_input_ws(self) -> ClientWebSocketResponse:
        try:
            return await super()._create_input_ws()
        except WebOsTvResponseTypeError as exc:
            if not _permission_error(exc):
                raise
            self.buttons_denied = True
            return _NoInputSocket()  # type: ignore[return-value]

    # --- public facts about the live connection ------------------------------------------
    def port(self) -> int | None:
        """Which port the main connection uses (3000 plain, 3001 TLS)."""
        conn = self.connection
        peer = conn.get_extra_info("peername") if conn is not None else None
        return int(peer[1]) if peer else None

    def cert_sha256(self) -> str | None:
        """SHA-256 of the TV's TLS certificate, or None on plain port 3000.

        aiowebostv connects with verification off (the certificate is self-signed),
        but the certificate is still on the connection: compare, don't trust.
        ASSUMPTION W-TLS-CERT.
        """
        conn = self.connection
        ssl_object = conn.get_extra_info("ssl_object") if conn is not None else None
        if ssl_object is None:
            return None
        der = ssl_object.getpeercert(binary_form=True)
        return hashlib.sha256(der).hexdigest() if der else None
