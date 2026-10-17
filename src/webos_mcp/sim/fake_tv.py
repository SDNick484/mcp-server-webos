"""A simulated LG webOS TV on the wire: the SSAP WebSocket, the pointer socket and Wake-on-LAN.

The real aiowebostv library connects to it, pairs with it and subscribes to
it, so the tests exercise the library's own code, not a mock of it (the
approach mcp-server-harmony's fake hub and mcp-server-shieldtv's fake Shield
take). What it answers is set by a Profile (sim/profiles.py), whose fields
name their sources; anything not derived from a source is marked GUESSED.

Addresses: aiowebostv always dials ports 3000 and 3001 on the host it is
given, so each simulated TV binds the real ports on its own loopback address
(127.0.0.2, 127.0.0.3, ...). On Linux all of 127.0.0.0/8 reaches the loopback
interface, so several TVs can listen at once and nothing in the library is
patched. (macOS only answers 127.0.0.1 unless aliases are added.)

Messages are JSON over WebSocket:
    -> {"type": "hello", "id": ..., "payload": {}}
    <- {"type": "hello", "id": ..., "payload": {"deviceUUID": ..., "deviceOSReleaseVersion": ...}}
    -> {"type": "register", "id": "register_0", "payload": {"client-key": ..., "manifest": {...}}}
    <- {"type": "registered", ...}                                  key known
    <- {"type": "response", "payload": {"pairingType": "PROMPT"}}   key unknown: dialog on screen
       then {"type": "registered", "payload": {"client-key": ...}} once accepted on the TV
    -> {"type": "request" | "subscribe", "id": 3, "uri": "ssap://audio/getVolume", "payload": {}}
    <- {"type": "response", "id": 3, "payload": {..., "returnValue": true}}
    <- {"type": "error", "id": 3, "error": "404 no such service or method", "payload": {}}
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import secrets
import ssl
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aiohttp import WSMsgType, web

from .profiles import PROFILES, Profile

log = logging.getLogger(__name__)

PairingMode = Literal["accept", "decline", "ignore"]

# ASSUMPTION W-POWER-STATES: the strings a real TV reports.
ACTIVE = "Active"
SCREEN_OFF = "Screen Off"
ACTIVE_STANDBY = "Active Standby"
SUSPEND = "Suspend"
OFF = "Off"  # not a TV string: the TV is off the network entirely

NOT_FOUND = "404 no such service or method"
NOT_REGISTERED = "401 insufficient permissions (not registered)"
DENIED = "401 insufficient permissions"
BLACKLISTED = "403 Pairing rejected: blacklisted certificate detected"
DECLINED = "403 User rejected pairing"  # GUESSED wording


class SsapError(Exception):
    """An error reply: the text goes in the "error" field."""


@dataclass
class Faults:
    """Misbehavior on purpose. All off by default."""

    deny: dict[str, str] = field(default_factory=dict)  # uri (no ssap://) -> error text
    drop_after: int | None = None  # close the main socket after this many requests
    slow: float = 0.0  # seconds before every reply
    garble_next: bool = False  # send one frame that isn't JSON, once
    prompt_then_registered: bool = False  # a known key still gets PROMPT, then "registered" at once
    reject_registration: str | None = None  # answer every registration with this error, before any dialog


@dataclass
class _Sub:
    ws: web.WebSocketResponse
    uid: Any
    uri: str


def _self_signed(host: str) -> tuple[bytes, bytes]:
    """A throwaway certificate for wss://, as a real TV presents its own self-signed one (W-TLS-CERT)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-webos-tv")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(serialization.Encoding.PEM), pem_key


@dataclass
class FakeTv:
    profile: Profile
    host: str = "127.0.0.2"
    wol_port: int = 0  # 0: a free port
    wake_seconds: float = 0.2  # ~4 s on a real TV (W-WAKE-TIME); short so tests are quick
    pairing: PairingMode = "accept"
    accept_after: float = 0.05
    faults: Faults = field(default_factory=Faults)
    # Called with the TV's name when the pairing dialog appears (simulate prints it).
    on_prompt: Callable[[], None] | None = None
    ws_port: int = 3000
    wss_port: int = 3001

    def __post_init__(self) -> None:
        p = self.profile
        self.power = ACTIVE
        self.foreground = "com.webos.app.hdmi1"
        self.volume = 12
        self.muted = False
        self.sound_output = p.sound_output
        self.apps = [dict(a) for a in p.apps]
        self.inputs = [dict(i) for i in p.inputs]
        self.media: list[dict[str, Any]] = []
        self.keys: set[str] = set()
        self.screen_unrecoverable = False
        self.journal: list[tuple[Any, ...]] = []
        self._subs: list[_Sub] = []
        self._sockets: set[web.WebSocketResponse] = set()
        self._raw_writers: set[asyncio.StreamWriter] = set()
        self._runner: web.AppRunner | None = None
        self._raw: asyncio.Server | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._requests = 0
        self._tls_dir: tempfile.TemporaryDirectory[str] | None = None
        self.cert_pem: bytes | None = None
        self.socket_id = secrets.token_hex(8)

    # --- what tests look at -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.profile.model

    @property
    def mac(self) -> str:
        return self.profile.wired_mac if self.profile.wired else self.profile.wifi_mac

    def events(self, kind: str) -> list[tuple[Any, ...]]:
        return [e for e in self.journal if e[0] == kind]

    @property
    def requests(self) -> list[str]:
        return [e[1] for e in self.journal if e[0] == "request"]

    @property
    def online(self) -> bool:
        return self.power != OFF

    def pair_with(self, key: str) -> None:
        """Trust a key without a dialog (simulate --paired, fixtures)."""
        self.keys.add(key)

    # --- lifecycle ----------------------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: _WolProtocol(self), local_addr=(self.host, self.wol_port)
        )
        self.wol_port = self._udp.get_extra_info("sockname")[1]
        if self.online:
            await self._network_up()

    async def stop(self) -> None:
        await self._network_down()
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        for task in list(self._tasks):
            task.cancel()
        if self._tls_dir is not None:
            self._tls_dir.cleanup()
            self._tls_dir = None

    async def unplug(self) -> None:
        """Gone from the network (power cut, or off without Quick Start+)."""
        self.power = OFF
        await self._network_down()

    async def _network_up(self) -> None:
        p = self.profile
        app = web.Application()
        app.router.add_get("/", self._main)
        app.router.add_get("/resources/{sid}/netinput.pointer.sock", self._pointer)
        self._runner = web.AppRunner(app, handle_signals=False, shutdown_timeout=0.1)
        await self._runner.setup()
        if p.port3000 == "ws":
            await web.TCPSite(self._runner, self.host, self.ws_port, reuse_address=True).start()
        elif p.port3000 in ("accept_close", "blackhole"):
            self._raw = await asyncio.start_server(self._raw_3000, self.host, self.ws_port, reuse_address=True)
        if p.wss:
            await web.TCPSite(
                self._runner, self.host, self.wss_port, ssl_context=self._tls(), reuse_address=True
            ).start()
        self.journal.append(("network", "up"))

    async def _network_down(self) -> None:
        # Close every connection ourselves, as a TV leaving the network would.
        # (Since Python 3.12, a server's wait_closed() waits for them all.)
        for ws in list(self._sockets):
            with contextlib.suppress(Exception):
                await ws.close()
        self._sockets.clear()
        self._subs.clear()
        for writer in list(self._raw_writers):
            writer.close()
        self._raw_writers.clear()
        if self._raw is not None:
            self._raw.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._raw.wait_closed(), 1.0)
            self._raw = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self.journal.append(("network", "down"))

    def _tls(self) -> ssl.SSLContext:
        if self._tls_dir is None:
            self._tls_dir = tempfile.TemporaryDirectory(prefix="fake-webos-tls-")
            cert, key = _self_signed(self.host)
            self.cert_pem = cert
            (Path(self._tls_dir.name) / "cert.pem").write_bytes(cert)
            (Path(self._tls_dir.name) / "key.pem").write_bytes(key)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(Path(self._tls_dir.name) / "cert.pem", Path(self._tls_dir.name) / "key.pem")
        return ctx

    async def _raw_3000(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Port 3000 on firmware that no longer serves it (ASSUMPTION W-PORT3000-CLOSE)."""
        self.journal.append(("connect", 3000, self.profile.port3000))
        if self.profile.port3000 == "accept_close":
            writer.close()
            return
        self._raw_writers.add(writer)  # blackhole: accept, then say nothing
        with contextlib.suppress(Exception):
            await reader.read()

    # --- power --------------------------------------------------------------------------
    async def _set_power(self, state: str, processing: str | None = None) -> None:
        self.power = state
        self.journal.append(("power", state))
        payload: dict[str, Any] = {"state": state}
        if processing:
            payload["processing"] = processing
        await self._notify("com.webos.service.tvpower/power/getPowerState", payload)

    async def _turn_off(self) -> None:
        await asyncio.sleep(0.05)  # the reply goes out first
        if self.profile.standby_socket:
            await self._set_power(ACTIVE_STANDBY)
        else:
            await self.unplug()

    def magic_packet(self, data: bytes) -> None:
        mac = bytes.fromhex(self.mac.replace(":", ""))
        if data != b"\xff" * 6 + mac * 16:
            return
        self.journal.append(("wol", self.mac))
        if self.power in (ACTIVE, SCREEN_OFF):
            return
        self._spawn(self._wake())

    async def _wake(self) -> None:
        if not self.online:
            self.power = SUSPEND
            await self._network_up()
        await self._set_power(self.power, processing="Screen On")
        await asyncio.sleep(self.wake_seconds)
        self.foreground = "com.webos.app.hdmi1"
        await self._set_power(ACTIVE)
        await self._notify("com.webos.applicationManager/getForegroundAppInfo", {"appId": self.foreground})

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the main socket ----------------------------------------------------------------
    async def _main(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.add(ws)
        port = request.transport.get_extra_info("sockname")[1] if request.transport else 0
        self.journal.append(("connect", port, "ws"))
        registered = False
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                kind, uid = data.get("type"), data.get("id")
                if self.faults.slow:
                    await asyncio.sleep(self.faults.slow)
                if self.faults.garble_next:
                    self.faults.garble_next = False
                    await ws.send_str("{not json")
                    continue
                if kind == "hello":
                    await ws.send_json({"type": "hello", "id": uid, "payload": self._hello()})
                elif kind == "register":
                    registered = await self._register(ws, uid, data.get("payload") or {})
                elif kind in ("request", "subscribe"):
                    uri = str(data.get("uri", "")).removeprefix("ssap://")
                    await self._request(ws, kind, uid, uri, data.get("payload") or {}, registered)
                    self._requests += 1
                    if self.faults.drop_after is not None and self._requests >= self.faults.drop_after:
                        self.journal.append(("drop",))
                        await ws.close()
                        break
                elif kind == "unsubscribe":
                    self._subs = [s for s in self._subs if not (s.ws is ws and s.uid == uid)]
        finally:
            self._sockets.discard(ws)
            self._subs = [s for s in self._subs if s.ws is not ws]
        return ws

    def _hello(self) -> dict[str, Any]:
        p = self.profile
        return {
            "protocolVersion": 1,
            "deviceType": "tv",
            "deviceOS": "webOS",
            "deviceOSVersion": p.os_version,
            "deviceOSReleaseVersion": p.release_version,
            "deviceUUID": p.uuid,
            "pairingTypes": list(p.pairing_types),
        }

    async def _register(self, ws: web.WebSocketResponse, uid: Any, payload: dict[str, Any]) -> bool:
        manifest = payload.get("manifest") or {}
        signed = bool(manifest.get("signatures") or manifest.get("signed"))
        key = payload.get("client-key")
        self.journal.append(("register", {"signed": signed, "with_key": bool(key)}))
        if self.faults.reject_registration:
            await ws.send_json({"type": "error", "id": uid, "error": self.faults.reject_registration, "payload": {}})
            return False
        if signed and self.profile.blacklist_signature:
            # Before any dialog, and before looking at the key (ASSUMPTION W-SIGNED-403)
            await ws.send_json({"type": "error", "id": uid, "error": BLACKLISTED, "payload": {}})
            return False
        if key and key in self.keys:
            if self.faults.prompt_then_registered:
                await ws.send_json(
                    {"type": "response", "id": uid, "payload": {"pairingType": "PROMPT", "returnValue": True}}
                )
            # ASSUMPTION W-REGISTERED-DIRECT
            await ws.send_json({"type": "registered", "id": uid, "payload": {"client-key": key}})
            return True
        # An unknown key (or none): the dialog appears on the TV (ASSUMPTION W-STALE-KEY)
        self.journal.append(("prompt",))
        if self.on_prompt:
            self.on_prompt()
        await ws.send_json({"type": "response", "id": uid, "payload": {"pairingType": "PROMPT", "returnValue": True}})
        if self.pairing == "ignore":
            return False
        await asyncio.sleep(self.accept_after)
        try:
            if ws.closed:
                raise ConnectionResetError
            if self.pairing == "decline":
                await ws.send_json({"type": "error", "id": uid, "error": DECLINED, "payload": {}})
                return False
            new_key = secrets.token_hex(16)
            await ws.send_json({"type": "registered", "id": uid, "payload": {"client-key": new_key}})
        except (ConnectionResetError, OSError):
            # The client hung up while the dialog was up: it goes away (ASSUMPTION W-PROMPT-DISMISS)
            self.journal.append(("prompt-dismissed",))
            return False
        self.keys.add(new_key)
        return True

    async def _request(
        self, ws: web.WebSocketResponse, kind: str, uid: Any, uri: str, payload: dict[str, Any], registered: bool
    ) -> None:
        self.journal.append(("request", uri, payload))
        try:
            if not registered and uri != "system/getSystemInfo":
                raise SsapError(NOT_REGISTERED)
            if uri in self.faults.deny:
                raise SsapError(self.faults.deny[uri])
            result = await self._handle(uri, payload)
        except SsapError as exc:
            await ws.send_json({"type": "error", "id": uid, "error": str(exc), "payload": {}})
            return
        if kind == "subscribe":
            result = {**result, "subscribed": True}
            self._subs.append(_Sub(ws, uid, uri))
        await ws.send_json({"type": "response", "id": uid, "payload": {"returnValue": True, **result}})

    async def _notify(self, uri: str, payload: dict[str, Any]) -> None:
        for sub in [s for s in self._subs if s.uri == uri]:
            with contextlib.suppress(Exception):
                await sub.ws.send_json({"type": "response", "id": sub.uid, "payload": {"returnValue": True, **payload}})

    async def _handle(self, uri: str, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: C901 - one table
        p = self.profile
        if uri == "system/getSystemInfo":
            info: dict[str, Any] = {"modelName": p.model, "features": {}, "receiverType": "atsc"}
            if p.serial is not None:
                info["serialNumber"] = p.serial
            return info
        if uri == "com.webos.service.update/getCurrentSWInformation":
            if p.software_info is None:
                raise SsapError(DENIED)  # ASSUMPTION W-SWINFO-401
            return dict(p.software_info)
        if uri == "com.webos.service.connectionmanager/getinfo":
            # GUESSED shape (ASSUMPTION W-MAC-SOURCE)
            return {
                "wiredInfo": {"macAddress": p.wired_mac, "state": "connected" if p.wired else "disconnected"},
                "wifiInfo": {"macAddress": p.wifi_mac, "state": "disconnected" if p.wired else "connected"},
            }
        if uri == "com.webos.service.networkinput/getPointerInputSocket":
            scheme, port = ("wss", self.wss_port) if p.wss else ("ws", self.ws_port)
            return {"socketPath": f"{scheme}://{self.host}:{port}/resources/{self.socket_id}/netinput.pointer.sock"}
        if uri == "com.webos.service.tvpower/power/getPowerState":
            if not p.power_state:
                raise SsapError(NOT_FOUND)  # ASSUMPTION W-C7-NO-POWERSTATE
            return {"state": self.power}
        if uri == "com.webos.applicationManager/getForegroundAppInfo":
            return {"appId": self.foreground if self.power in (ACTIVE, SCREEN_OFF) else ""}
        if uri == "audio/getStatus":
            return {"mute": self.muted, "volume": self.volume}
        if uri == "audio/getVolume":
            if p.volume_flat:
                return {"volume": self.volume, "muted": self.muted}
            return {"volumeStatus": {"volume": self.volume, "muteStatus": self.muted, "soundOutput": self.sound_output}}
        if uri == "com.webos.applicationManager/listLaunchPoints":
            return {"launchPoints": self.apps}
        if uri == "tv/getExternalInputList":
            return {"devices": self.inputs}
        if uri == "com.webos.service.apiadapter/audio/getSoundOutput":
            return {"soundOutput": self.sound_output}
        if uri == "com.webos.media/getForegroundAppInfo":
            if not p.media_info:
                raise SsapError(NOT_FOUND)  # ASSUMPTION W-NOW-PLAYING: webOS 6 has no such service
            return {"foregroundAppInfo": self.media}
        if uri == "system.launcher/launch":
            app_id = payload.get("id")
            if not any(a["id"] == app_id for a in self.apps):
                return {"returnValue": False, "errorCode": -101, "errorText": "no such app"}  # GUESSED wording
            await self._foreground(str(app_id))
            return {"id": app_id}
        if uri == "tv/switchInput":
            match = next((i for i in self.inputs if i["id"] == payload.get("inputId")), None)
            if match is None:
                return {"returnValue": False, "errorText": "no such input"}  # GUESSED wording
            await self._foreground(match["appId"])
            return {}
        if uri == "audio/setVolume":
            self.volume = int(payload.get("volume", self.volume))
            await self._audio_changed()
            return {}
        if uri == "audio/setMute":
            self.muted = bool(payload.get("mute"))
            await self._audio_changed()
            return {}
        if uri == "system/turnOff":
            self._spawn(self._turn_off())
            return {}
        if p.screen_off_uri and uri == p.screen_off_uri:
            if payload.get("standbyMode") == "passive":
                self.screen_unrecoverable = True  # ASSUMPTION W-SCREEN-OFF: only unplugging undoes this
            await self._set_power(SCREEN_OFF)
            return {}
        if p.screen_off_uri and uri == p.screen_off_uri.replace("turnOffScreen", "turnOnScreen"):
            if self.screen_unrecoverable:
                raise SsapError("500 Application error")  # GUESSED
            await self._set_power(ACTIVE)
            return {}
        if uri.startswith("media.controls/"):
            action = uri.split("/", 1)[1]
            states = {"play": "playing", "pause": "paused", "stop": "stopped"}
            for m in self.media:
                m["playState"] = states.get(action, m.get("playState"))
            await self._notify("com.webos.media/getForegroundAppInfo", {"foregroundAppInfo": self.media})
            return {}
        raise SsapError(NOT_FOUND)

    async def _foreground(self, app_id: str) -> None:
        self.foreground = app_id
        streaming = not app_id.startswith("com.webos.app.")
        self.media = [{"appId": app_id, "playState": "playing", "type": "media"}] if streaming else []
        await self._notify("com.webos.applicationManager/getForegroundAppInfo", {"appId": app_id})
        await self._notify("com.webos.media/getForegroundAppInfo", {"foregroundAppInfo": self.media})

    async def _audio_changed(self) -> None:
        await self._notify("audio/getStatus", {"mute": self.muted, "volume": self.volume})
        volume = (
            {"volume": self.volume}
            if self.profile.volume_flat
            else {"volumeStatus": {"volume": self.volume, "muteStatus": self.muted}}
        )
        await self._notify("audio/getVolume", volume)

    # --- the pointer (button) socket ----------------------------------------------------
    async def _pointer(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                fields = dict(line.split(":", 1) for line in msg.data.splitlines() if ":" in line)
                if fields.get("type") == "button":
                    self.journal.append(("button", fields.get("name")))
        finally:
            self._sockets.discard(ws)
        return ws


class _WolProtocol(asyncio.DatagramProtocol):
    def __init__(self, tv: FakeTv) -> None:
        self.tv = tv

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.tv.magic_packet(data)


def make(profile: str, host: str, **kwargs: Any) -> FakeTv:
    return FakeTv(PROFILES[profile], host=host, **kwargs)
