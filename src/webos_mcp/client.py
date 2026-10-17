"""One long-lived connection per TV, plus a cache of the state the TV pushes.

Like the Shield's Android TV Remote protocol, and unlike the Onkyo's eISCP, a
webOS TV keeps one WebSocket open and *pushes* changes over subscriptions
(power, foreground app, volume, inputs, what is playing). So each TvClient
owns that connection for the life of the server, keeps the latest values for
get_status, and a supervisor task keeps it up. What can go wrong, and what
this class does about it:

  - The TV is off. Its WebSocket is gone (unless it is in a networked standby,
    ASSUMPTION W-STANDBY-SOCKET), so the supervisor tries again every ~10 s,
    with jitter. A try against a TV that is off is one TCP SYN that times out
    after 2 s: cheap, and it means a TV turned on by a remote shows up here
    within ~10 s. After a drop it retries at 1, 2, 4 and 8 s first, and right
    after set_power on it tries every second.
  - While it is down, get_status shows the last values, marked stale with
    as_of, plus why (error). A get_status on a stale TV first makes one
    connect attempt of at most 3 s (unless one was made in the last 5 s), so
    "check status first" gets a fresh answer.
  - The TV no longer accepts the stored key (reset, or a firmware update:
    ASSUMPTION W-KEY-SURVIVES). It would show the pairing dialog on every
    attempt, so the first KeyNotAccepted marks it needs_pairing and the
    supervisor stops trying until `pair` writes a new key to keys.json, which
    it notices (no restart needed).
  - Another device answers at the TV's address (DHCP gave it away). The key
    is never sent to it (compat.WrongDevice); the supervisor looks for the TV
    by SSDP and recognizes it only by the deviceUUID `pair` saved
    (ASSUMPTION W-UUID-STABLE). The same search runs every 5 minutes while
    the TV has been unreachable for over a minute. Never guess a device.
  - Home Assistant (or LG's phone app) may hold its own connection to the same
    TV at the same time; each client pairs separately (ASSUMPTION W-MULTI-CLIENT).
  - Log lines only on a change of state ("reachable -> unreachable"), not on
    every failed attempt against a TV that is simply off.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import random
import time
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import aiohttp
from aiowebostv import endpoints as ep
from aiowebostv.exceptions import WebOsTvCommandError, WebOsTvPairError, WebOsTvServiceNotFoundError
from aiowebostv.models import WebOsTvState
from typing_extensions import TypedDict

from . import config, discovery, keystore, probe, wol
from .compat import CompatClient, KeyNotAccepted, PairingRefused, WrongDevice
from .config import Settings, TvSettings, name_key
from .errors import Unreachable, WebOsError
from .tasks import stop_task

log = logging.getLogger(__name__)

Power = Literal["on", "screen_off", "standby", "off"]
Outcome = Literal["done", "unchanged", "dry_run"]

# The screen-off endpoints: webOS 5 and later, then webOS 4 (ASSUMPTION W-SCREEN-OFF).
SCREEN_OFF_URIS = (ep.TURN_OFF_SCREEN, "com.webos.service.tv.power/turnOffScreen")
SCREEN_ON_URIS = (ep.TURN_ON_SCREEN, "com.webos.service.tv.power/turnOnScreen")
# "active" only: "passive" can't be undone without unplugging the TV. Not an argument anywhere.
SCREEN_PAYLOAD = {"standbyMode": "active"}

MEDIA_ACTIONS: dict[str, str] = {
    "play": ep.MEDIA_PLAY,
    "pause": ep.MEDIA_PAUSE,
    "stop": ep.MEDIA_STOP,
    "rewind": ep.MEDIA_REWIND,
    "fast_forward": ep.MEDIA_FAST_FORWARD,
}

# Sound outputs where volume belongs to another device (ASSUMPTION W-SOUND-OUTPUT, W-VOLUME-ARC).
EXTERNAL_OUTPUTS = ("external_", "bt_soundbar", "lineout")


def clean(text: Any, limit: int = 64) -> str:
    """A string from the TV (an app title, an input label), made safe to hand to the model:
    no control or format characters, whitespace collapsed, bounded. It is data, never instructions."""
    s = "".join(ch for ch in str(text) if unicodedata.category(ch)[0] != "C")
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


class Foreground(TypedDict):
    id: str
    title: str | None


class NowPlaying(TypedDict):
    app: str
    title: str | None
    play_state: str


class Status(TypedDict):
    tv: str
    model: str | None
    host: str
    platform: str | None  # deviceOSReleaseVersion, e.g. "10.3.1" (webOS 25)
    paired: bool
    needs_pairing: bool
    dry_run: bool  # true: actions are reported, not sent
    reachable: bool
    # True when not reachable now: the values are the last ones the TV pushed, at as_of.
    stale: bool
    as_of: str | None
    error: str | None
    # "off" means not answering: off, or off the network. "standby": answering, screen and sound off.
    power: Power
    power_raw: str | None  # the TV's own power state string, if it reports one
    foreground_app: Foreground | None
    input: str | None  # the HDMI input's label, when one is in front
    now_playing: NowPlaying | None
    volume: int | None
    muted: bool | None
    sound_output: str | None
    volume_owner: Literal["tv", "external", "unknown"]
    capabilities: list[str]
    limitations: list[str]
    warnings: list[str]


@dataclasses.dataclass
class Result:
    """What an action did: server.py turns it into an ActionResult."""

    detail: str
    sent: list[str]
    outcome: Outcome = "done"
    warnings: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _Cache:
    """The TV's last pushed state. Kept across disconnects (that is what stale means)."""

    power: dict[str, Any] = dataclasses.field(default_factory=dict)
    app_id: str | None = None
    volume: int | None = None
    muted: bool | None = None
    sound_output: str | None = None
    apps: dict[str, Any] = dataclasses.field(default_factory=dict)
    inputs: dict[str, Any] = dataclasses.field(default_factory=dict)
    media: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    is_on: bool = False
    is_screen_on: bool = False


ClientFactory = Callable[..., CompatClient]
Finder = Callable[[float], Awaitable[list[str]]]
Prober = Callable[[str], Awaitable[probe.Hello | None]]


async def _ssdp(timeout: float) -> list[str]:
    return await discovery.discover(timeout)


async def _probe(host: str) -> probe.Hello | None:
    return await probe.any_hello(host)


class TvClient:
    poll_interval = 10.0  # while off
    jitter = 2.0
    drop_retries = (1.0, 2.0, 4.0, 8.0)  # right after a connection drops
    fast_poll = 1.0  # after set_power on
    connect_timeout = 25.0  # one whole attempt, a backstop over aiowebostv's own timeouts
    refresh_timeout = 3.0  # get_status's own attempt on a stale TV
    refresh_min_age = 5.0
    watch_interval = 5.0  # how often a TV waiting for pairing checks keys.json
    rediscover_after = 60.0
    rediscover_every = 300.0
    wake_wait = 20.0  # ASSUMPTION W-WAKE-TIME: ~4-5 s on real TVs
    confirm_wait = 10.0

    def __init__(
        self,
        tv: TvSettings,
        settings: Settings,
        client_factory: ClientFactory = CompatClient,
        finder: Finder = _ssdp,
        prober: Prober = _probe,
    ) -> None:
        self.tv = tv
        self.settings = settings
        self._factory = client_factory
        self._finder = finder
        self._prober = prober
        self.client: CompatClient | None = None
        self.cache = _Cache()
        self.reachable = False
        self.as_of: float | None = None  # wall clock of the last pushed value
        self.error: str | None = None
        self.needs_pairing = False
        self.screen_unsupported = False
        self.cert_warning: str | None = None
        self.moved_from: str | None = None
        self.last_attempt = 0.0
        self.down_since = time.monotonic()
        self._last_search = float("-inf")
        self._fast_until = 0.0
        self._kick = asyncio.Event()
        self._connect_lock = asyncio.Lock()
        self.lock = asyncio.Lock()  # one action at a time on this TV
        self._task: asyncio.Task[None] | None = None
        self._pending: CompatClient | None = None
        self._stamp = keystore.stamp()

    # --- lifecycle -------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.tv.name

    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"webos-{self.name}")

    async def stop(self) -> None:
        await stop_task(self._task, 5.0, f"{self.name}'s connection")
        self._task = None
        await self._drop_client()

    def kick(self) -> None:
        """Wake the supervisor now instead of at its next scheduled try."""
        self._kick.set()

    # --- the supervisor ----------------------------------------------------------------
    async def _supervise(self) -> None:
        delays: list[float] = []
        while True:
            try:
                if self.client is not None and self.client.is_connected():
                    await self._until_disconnected()
                    delays = list(self.drop_retries)
                    continue
                key = keystore.get(self.tv.uuid)
                if key is None or self.needs_pairing:
                    if key is None:
                        self.needs_pairing = True
                        self.error = (
                            f"not paired: run `mcp-server-webos pair --host {self.tv.host} --name '{self.name}'`"
                        )
                    await self._wait_for_new_key()
                    continue
                if await self._attempt(key, self.connect_timeout):
                    continue
                await self._maybe_rediscover()
                if self.needs_pairing:
                    continue
                if time.monotonic() < self._fast_until:
                    delay = self.fast_poll
                elif delays:
                    delay = delays.pop(0)
                else:
                    delay = self.poll_interval + random.uniform(-self.jitter, self.jitter)
                await self._sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: the connection supervisor failed; it will try again", self.name)
                await self._sleep(self.poll_interval)

    async def _sleep(self, delay: float) -> None:
        self._kick.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._kick.wait(), max(0.0, delay))

    async def _until_disconnected(self) -> None:
        client = self.client
        task = client.connect_task if client is not None else None
        if task is not None:
            await asyncio.wait({task})
        await self._drop_client()
        self._set_reachable(False, "the connection closed")

    async def _wait_for_new_key(self) -> None:
        """Sleep until `pair` writes keys.json, then try again with what it wrote."""
        while keystore.stamp() == self._stamp:
            await self._sleep(self.watch_interval)
        self._stamp = keystore.stamp()
        fresh = config.load_settings().tv(self.name)
        if fresh is not None:
            self.tv = fresh
        self.needs_pairing = False
        self.error = None
        log.info("%s: new pairing found; connecting", self.name)

    async def _attempt(self, key: str, timeout: float) -> bool:
        """One connect attempt. True if connected."""
        async with self._connect_lock:
            if self.client is not None and self.client.is_connected():
                return True
            self.last_attempt = time.monotonic()
            client = self._factory(self.tv.host, key, expected_uuid=self.tv.uuid)
            self._pending = client
            await client.register_state_update_callback(functools.partial(self._on_state, client))
            try:
                async with asyncio.timeout(timeout):
                    await client.connect()
            except KeyNotAccepted:
                # ASSUMPTION W-STALE-KEY: retrying would show the dialog again and again
                await self._discard(client)
                self._pairing_lost("the TV no longer accepts the stored key (reset, or a firmware update?)")
                return False
            except PairingRefused as exc:
                await self._discard(client)
                self._pairing_lost(f"the TV refused the registration: {exc}")
                return False
            except WrongDevice:
                await self._discard(client)
                self._set_reachable(False, f"another device answers at {self.tv.host}; looking for the TV")
                self._last_search = float("-inf")  # search now
                return False
            except WebOsTvPairError as exc:
                await self._discard(client)
                self._pairing_lost(str(exc) or "pairing failed")
                return False
            except (TimeoutError, OSError, aiohttp.ClientError, WebOsTvCommandError) as exc:
                await self._discard(client)
                self._set_reachable(False, _describe(exc))
                return False
            finally:
                self._pending = None
            self.client = client
            self._connected(client, key)
            return True

    async def _discard(self, client: CompatClient) -> None:
        with contextlib.suppress(Exception):
            await client.disconnect()

    async def _drop_client(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await self._discard(client)

    def _pairing_lost(self, why: str) -> None:
        self.needs_pairing = True
        self._stamp = keystore.stamp()
        self._set_reachable(
            False, f"{why}. Re-pair: `mcp-server-webos pair --host {self.tv.host} --name '{self.name}'`"
        )
        log.warning("%s: needs pairing: %s", self.name, why)

    def _connected(self, client: CompatClient, key: str) -> None:
        if client.client_key and client.client_key != key and self.tv.uuid:
            keystore.put(self.tv.uuid, client.client_key)  # accepted on the TV during the grace period
            self._stamp = keystore.stamp()
        cert = client.cert_sha256()
        if self.tv.cert_sha256 and cert and cert != self.tv.cert_sha256:
            # ASSUMPTION W-TLS-CERT: a warning, never a block
            self.cert_warning = (
                "the TV's TLS certificate differs from the one seen at pairing (a firmware update can do this; "
                "re-run pair to accept it)"
            )
        else:
            self.cert_warning = None
        platform = probe.Hello(self.tv.host, 0, client.tv_info.hello).platform
        if platform and platform != self.tv.platform:
            log.info("%s: platform is now %s (was %s)", self.name, platform, self.tv.platform)
            config.update_tv(self.name, platform=platform)
            self.tv = dataclasses.replace(self.tv, platform=platform)
        self._set_reachable(True, None)
        self.as_of = time.time()

    def _set_reachable(self, value: bool, error: str | None) -> None:
        if value != self.reachable:
            if value:
                log.info("%s: unreachable -> reachable (platform %s)", self.name, self.tv.platform or "?")
            else:
                log.info("%s: reachable -> unreachable (%s)", self.name, error)
                self.down_since = time.monotonic()
        self.reachable = value
        self.error = error

    async def _maybe_rediscover(self) -> None:
        now = time.monotonic()
        if not self.tv.uuid or self.needs_pairing:
            return
        due = now - self.down_since >= self.rediscover_after or self._last_search == float("-inf")
        if not due or now - self._last_search < self.rediscover_every:
            return
        self._last_search = now
        await self.rediscover()

    async def rediscover(self) -> str | None:
        """Look for the TV by SSDP; adopt a new address only on a deviceUUID match."""
        try:
            hosts = await self._finder(3.0)
        except OSError as exc:
            log.debug("%s: SSDP search failed: %s", self.name, exc)
            return None
        for host in hosts:
            if host == self.tv.host:
                continue
            hello = await self._prober(host)
            if hello is not None and hello.uuid and hello.uuid == self.tv.uuid:
                old = self.tv.host
                config.update_tv(self.name, host=host)
                self.tv = dataclasses.replace(self.tv, host=host)
                self.moved_from = old
                log.info("%s: found at a new address (was %s)", self.name, old)
                self.kick()
                return host
        return None

    async def connect_once(self, timeout: float = 10.0) -> bool:
        """One attempt, outside the supervisor (the `check` command)."""
        key = keystore.get(self.tv.uuid)
        if key is None:
            self.needs_pairing = True
            self.error = "not paired"
            return False
        return await self._attempt(key, timeout)

    async def refresh(self) -> None:
        """get_status's bounded refresh of a stale TV: one try, at most refresh_timeout."""
        if self.reachable or self.needs_pairing or time.monotonic() - self.last_attempt < self.refresh_min_age:
            return
        key = keystore.get(self.tv.uuid)
        if key is not None:
            await self._attempt(key, self.refresh_timeout)

    # --- pushed state ------------------------------------------------------------------
    async def _on_state(self, client: CompatClient, state: WebOsTvState) -> None:
        # aiowebostv calls this once more with a cleared state as it disconnects; keep the
        # last real values instead (that is what stale means). The first real call comes
        # before connect() returns, while the client is still pending.
        if client not in (self.client, self._pending) or not client.do_state_update or client.connection is None:
            return
        self.cache = _Cache(
            power=dict(state.power_state or {}),
            app_id=state.current_app_id,
            volume=state.volume,
            muted=state.muted,
            sound_output=state.sound_output,
            apps=dict(state.apps or {}),
            inputs=dict(state.inputs or {}),
            media=list(state.media_state or []),
            is_on=state.is_on,
            is_screen_on=state.is_screen_on,
        )
        self.as_of = time.time()

    def power(self) -> Power:
        if not self.reachable:
            return "off"
        raw = self.cache.power.get("state")
        if raw in ("Active", "Screen Saver"):
            return "on"
        if raw == "Screen Off":
            return "screen_off"
        if raw in ("Active Standby", "Suspend", "Power Off"):
            return "standby"
        # No getPowerState (ASSUMPTION W-C7-NO-POWERSTATE): aiowebostv infers it from the foreground app
        return "on" if self.cache.is_on else "standby"

    def volume_owner(self) -> Literal["tv", "external", "unknown"]:
        if self.tv.volume_owner != "auto":
            return self.tv.volume_owner
        out = self.cache.sound_output
        if out is None:
            return "unknown"
        return "external" if out.startswith(EXTERNAL_OUTPUTS) else "tv"

    def _app_title(self, app_id: str | None) -> str | None:
        if not app_id:
            return None
        # An input's label is the user's name for it ("Onkyo"); the TV may also list the input as a
        # launch point, with a generic title ("HDMI 1"), so the input wins.
        inp = self.cache.inputs.get(app_id)
        if isinstance(inp, dict) and inp.get("label"):
            return clean(inp["label"])
        app = self.cache.apps.get(app_id)
        if isinstance(app, dict):
            return clean(app.get("title") or app_id)
        return None

    def _input_label(self) -> str | None:
        inp = self.cache.inputs.get(self.cache.app_id or "")
        return clean(inp.get("label") or inp.get("id")) if isinstance(inp, dict) else None

    def _now_playing(self) -> NowPlaying | None:
        # ASSUMPTION W-NOW-PLAYING: app and play state only; webOS doesn't say the title
        for m in self.cache.media:
            state = m.get("playState")
            if isinstance(m, dict) and isinstance(state, str) and state:
                app = str(m.get("appId") or "")
                return {"app": app, "title": self._app_title(app), "play_state": clean(state, 20)}
        return None

    def capabilities(self) -> tuple[list[str], list[str]]:
        caps: list[str] = ["status"]
        limits: list[str] = []
        if self.needs_pairing:
            limits.append(f"everything: {self.error or 'not paired'}")
            return caps, limits
        if self.tv.macs:
            caps.append("power_on")
        else:
            limits.append("power_on: no MAC address saved (re-run pair while the TV is on, or pair --mac)")
        caps.append("power_off")
        if self.reachable and not self.cache.power:
            limits.append("power state: inferred from the foreground app (this TV doesn't report one)")
        if self.screen_unsupported:
            limits.append("screen: this TV has no screen-off command")
        else:
            caps.append("screen")
        caps += ["sources", "media_control"]
        if self.client is not None and self.client.buttons_denied:
            limits.append("keys: the TV refused the button socket; media_control still works")
        else:
            caps.append("keys")
        owner = self.volume_owner()
        if owner == "external":
            limits.append(
                f"volume: belongs to the receiver (sound output {self.cache.sound_output}); use its own server"
            )
        elif owner == "unknown":  # e.g. a TV that has been off since the server started: _require_volume refuses
            limits.append("volume: not known yet where this TV's sound goes (it reports that once it answers)")
        else:
            caps.append("volume")
        return caps, limits

    def snapshot(self) -> Status:
        caps, limits = self.capabilities()
        warnings = [w for w in (self.cert_warning,) if w]
        if self.moved_from:
            warnings.append(f"found at a new address (was {self.moved_from}); config.json was updated")
        app_id = self.cache.app_id or None
        return {
            "tv": self.name,
            "model": self.tv.model,
            "host": self.tv.host,
            "platform": self.tv.platform,
            "paired": keystore.get(self.tv.uuid) is not None,
            "needs_pairing": self.needs_pairing,
            "dry_run": self.settings.dry_run,
            "reachable": self.reachable,
            "stale": not self.reachable,
            "as_of": datetime.fromtimestamp(self.as_of, UTC).isoformat(timespec="seconds") if self.as_of else None,
            "error": self.error,
            "power": self.power(),
            "power_raw": clean(self.cache.power["state"], 32) if self.cache.power.get("state") else None,
            "foreground_app": {"id": clean(app_id, 128), "title": self._app_title(app_id)} if app_id else None,
            "input": self._input_label(),
            "now_playing": self._now_playing(),
            "volume": self.cache.volume,
            "muted": self.cache.muted,
            "sound_output": self.cache.sound_output,
            "volume_owner": self.volume_owner(),
            "capabilities": caps,
            "limitations": limits,
            "warnings": warnings,
        }

    # --- actions -----------------------------------------------------------------------
    def _require(self) -> CompatClient:
        if self.needs_pairing:
            raise WebOsError(f"{self.name} needs pairing: {self.error}")
        client = self.client
        if client is None or not client.is_connected() or not self.reachable:
            raise Unreachable(
                f"{self.name} isn't answering ({self.error or 'not connected yet'}). If it is off, "
                "set_power with state='on' wakes it."
            )
        return client

    def _require_awake(self) -> CompatClient:
        client = self._require()
        if self.power() == "standby":
            raise WebOsError(f"{self.name} is in standby. Turn it on first (set_power state='on').")
        return client

    async def _wait_for(self, condition: Callable[[], bool], timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not condition():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _call(self, fn: Callable[[CompatClient], Awaitable[Any]]) -> Any:
        """Send something to the TV. Every state change goes through here."""
        assert not self.settings.dry_run, "dry run must not send anything"
        client = self._require()
        try:
            return await fn(client)
        except WebOsTvServiceNotFoundError:
            raise
        except (WebOsTvCommandError, OSError, aiohttp.ClientError) as exc:
            raise WebOsError(f"{self.name} didn't accept that: {_describe(exc)}") from exc

    async def power_on(self) -> Result:
        if self.reachable and self.power() in ("on", "screen_off"):
            return Result(f"{self.name} is already on", [], "unchanged")
        if not self.tv.macs:
            raise WebOsError(
                f"No MAC address saved for {self.name}, so it can't be woken (Wake-on-LAN). Re-run pair while "
                "it is on, or pass pair --mac."
            )
        s = self.settings
        targets = [
            f"wol {mac} -> {a}:{p}"
            for mac in self.tv.macs
            for a, p in wol.targets(self.tv.host, s.wol_broadcast, s.wol_port)
        ]
        if s.dry_run:
            return Result(f"wake {self.name} with Wake-on-LAN", targets, "dry_run")
        # ASSUMPTION W-WOL, W-TURNON-DEAD: a magic packet is the only way in
        try:
            sent = wol.send(self.tv.macs, self.tv.host, s.wol_broadcast, s.wol_port)
        except OSError as exc:
            raise WebOsError(f"Couldn't send the Wake-on-LAN packet: {exc}") from exc
        self._fast_until = time.monotonic() + self.wake_wait
        self.kick()
        t0 = time.monotonic()
        if await self._wait_for(lambda: self.reachable and self.power() == "on", self.wake_wait):
            return Result(f"{self.name} is on (awake after {time.monotonic() - t0:.0f} s)", sent)
        return Result(
            f"Sent Wake-on-LAN to {self.name}",
            sent,
            warnings=[
                f"unconfirmed: {self.name} didn't answer within {self.wake_wait:.0f} s. Check that its 'TV On With "
                "Mobile' / 'Turn on via Wi-Fi' setting is on (older sets may need Ethernet)."
            ],
        )

    async def power_off(self) -> Result:
        if not self.reachable:
            return Result(
                f"{self.name} isn't answering, so it is off or off the network",
                [],
                "unchanged",
                [f"not confirmed: {self.error}"] if self.error else [],
            )
        if self.power() == "standby":
            return Result(f"{self.name} is already off (standby)", [], "unchanged")
        sent = [f"ssap://{ep.POWER_OFF}"]
        if self.settings.dry_run:
            return Result(f"turn {self.name} off", sent, "dry_run")
        # ASSUMPTION W-OFF-GUARD: only sent to a TV that is on
        await self._call(lambda c: c.power_off())
        if await self._wait_for(lambda: not self.reachable or self.power() == "standby", self.confirm_wait):
            return Result(f"{self.name} is off", sent)
        return Result(f"Asked {self.name} to turn off", sent, warnings=["unconfirmed: it still reports being on"])

    async def set_screen(self, on: bool) -> Result:
        self._require_awake()
        if self.screen_unsupported:
            raise WebOsError(f"{self.name} has no screen-off command (some older webOS sets lack it).")
        if self.cache.power and self.power() == ("on" if on else "screen_off"):
            return Result(f"{self.name}'s screen is already {'on' if on else 'off'}", [], "unchanged")
        uris = SCREEN_ON_URIS if on else SCREEN_OFF_URIS
        if self.settings.dry_run:
            return Result(f"turn {self.name}'s screen {'on' if on else 'off'}", [f"ssap://{uris[0]}"], "dry_run")
        sent: list[str] = []
        for uri in uris:  # ASSUMPTION W-SCREEN-OFF: webOS 5+ first, then webOS 4
            sent.append(f"ssap://{uri}")
            try:
                await self._call(functools.partial(_screen_request, uri=uri))
                break
            except WebOsTvServiceNotFoundError:
                continue
        else:
            self.screen_unsupported = True
            raise WebOsError(f"{self.name} has no screen-off command (some older webOS sets lack it).")
        want = "on" if on else "screen_off"
        if not self.cache.power or await self._wait_for(lambda: self.power() == want, 5.0):
            return Result(f"{self.name}'s screen is {'on' if on else 'off'}", sent)
        return Result(f"Asked {self.name} to turn its screen {'on' if on else 'off'}", sent, warnings=["unconfirmed"])

    # sources: allow-listed apps the TV has installed, and its HDMI inputs ---------------
    def sources(self) -> list[dict[str, Any]]:
        self._require()
        out: list[dict[str, Any]] = []
        for friendly, app in sorted(self.settings.apps.items()):
            installed = self.cache.apps.get(app.id)
            if isinstance(installed, dict):  # ASSUMPTION W-APP-IDS: only ids the TV lists
                out.append(
                    {"kind": "app", "name": friendly, "id": app.id, "title": clean(installed.get("title") or app.title)}
                )
        for inp in self.cache.inputs.values():
            if isinstance(inp, dict) and inp.get("id"):
                out.append(
                    {
                        "kind": "input",
                        "name": clean(inp.get("label") or inp["id"]),
                        "id": clean(inp["id"], 32),
                        "connected": inp.get("connected"),
                    }
                )
        return out

    def _resolve_source(self, ref: str) -> tuple[str, str, str]:
        """(kind, id to send, appId that will be in front). Never guesses between matches."""
        key = name_key(ref)
        matches: dict[tuple[str, str], tuple[str, str, str]] = {}
        for friendly, app in self.settings.apps.items():
            installed = self.cache.apps.get(app.id)
            if not isinstance(installed, dict):
                continue
            words = {friendly, app.id, app.title, str(installed.get("title") or "")}
            if key in {name_key(w) for w in words if w}:
                matches[("app", app.id)] = ("app", app.id, app.id)
        for app_id, inp in self.cache.inputs.items():
            if not isinstance(inp, dict) or not inp.get("id"):
                continue
            words = {str(inp["id"]), str(inp.get("label") or ""), str(app_id)}
            if key in {name_key(w) for w in words if w}:
                matches[("input", inp["id"])] = ("input", str(inp["id"]), str(app_id))
        if len(matches) == 1:
            return next(iter(matches.values()))
        listing = ", ".join(f"{s['name']} ({s['kind']})" for s in self.sources())
        if not matches:
            raise WebOsError(f"No source called {clean(ref)!r} on {self.name}. Sources: {listing}.")
        both = ", ".join(f"{k} {i}" for k, i in matches)
        raise WebOsError(f"{clean(ref)!r} matches several sources on {self.name} ({both}). Say which one.")

    async def select_source(self, ref: str) -> Result:
        self._require_awake()
        kind, ident, front = self._resolve_source(ref)
        label = self._app_title(front) or ident
        if self.cache.app_id == front:
            return Result(f"{label} is already in front on {self.name}", [], "unchanged")
        sent = [f"ssap://{ep.LAUNCH} {ident}" if kind == "app" else f"ssap://{ep.SET_INPUT} {ident}"]
        if self.settings.dry_run:
            return Result(f"switch {self.name} to {label}", sent, "dry_run")
        if kind == "app":
            await self._call(lambda c: c.launch_app(ident))
        else:
            await self._call(lambda c: c.set_input(ident))  # ASSUMPTION W-INPUT-IDS
        if await self._wait_for(lambda: self.cache.app_id == front, self.confirm_wait):
            return Result(f"{label} is in front on {self.name}", sent)
        showing = self._app_title(self.cache.app_id) or "something else"
        raise WebOsError(f"Sent the switch to {label}, but {self.name} still shows {showing}.")

    async def send_key(self, key: str, repeat: int) -> Result:
        client = self._require_awake()
        if client.buttons_denied:
            raise WebOsError(f"{self.name} refused the button socket, so keys don't work. media_control still does.")
        sent = [f"button {key}"] * repeat
        if self.settings.dry_run:
            return Result(f"press {key} x{repeat} on {self.name}", sent, "dry_run")
        for i in range(repeat):  # ASSUMPTION W-KEYS
            await self._call(lambda c: c.button(key))
            if i + 1 < repeat:
                await asyncio.sleep(0.15)
        return Result(f"Pressed {key} x{repeat} on {self.name}", sent)

    async def media_control(self, action: str) -> Result:
        self._require_awake()
        uri = MEDIA_ACTIONS[action]
        sent = [f"ssap://{uri}"]
        if self.settings.dry_run:
            return Result(f"{action} on {self.name}", sent, "dry_run")
        await self._call(lambda c: c.request(uri))  # ASSUMPTION W-MEDIA-CONTROLS
        return Result(f"Sent {action} to {self.name}", sent)

    def _require_volume(self) -> None:
        owner = self.volume_owner()
        if owner == "external":
            raise WebOsError(
                f"Volume on {self.name} belongs to the receiver (sound output {self.cache.sound_output}). Use the "
                "receiver's server: going through the TV would bypass its volume caps. (To let this TV set it, "
                'set "volume_owner": "tv" for it in config.json.)'
            )
        if owner == "unknown":
            raise WebOsError(f"{self.name} hasn't said where its sound goes yet; try again in a moment.")

    async def set_volume(self, level: int) -> Result:
        self._require_awake()
        self._require_volume()
        warnings: list[str] = []
        if level > self.tv.max_volume:
            warnings.append(f"capped at {self.tv.max_volume} (max_volume for {self.name})")
            level = self.tv.max_volume
        if self.cache.volume == level:
            return Result(f"{self.name}'s volume is already {level}", [], "unchanged", warnings)
        sent = [f"ssap://{ep.SET_VOLUME} {level}"]
        if self.settings.dry_run:
            return Result(f"set {self.name}'s volume to {level}", sent, "dry_run", warnings)
        await self._call(lambda c: c.set_volume(level))
        if not await self._wait_for(lambda: self.cache.volume == level, 5.0):
            warnings.append("unconfirmed: the TV hasn't reported the new level")
        return Result(f"{self.name}'s volume is {level}", sent, warnings=warnings)

    async def set_mute(self, muted: bool) -> Result:
        self._require_awake()
        self._require_volume()
        word = "muted" if muted else "unmuted"
        if self.cache.muted is muted:
            return Result(f"{self.name} is already {word}", [], "unchanged")
        sent = [f"ssap://{ep.SET_MUTE} {str(muted).lower()}"]
        if self.settings.dry_run:
            return Result(f"{word[:-1]} {self.name}", sent, "dry_run")
        await self._call(lambda c: c.set_mute(muted))
        await self._wait_for(lambda: self.cache.muted is muted, 5.0)
        return Result(f"{self.name} is {word}", sent)


async def _screen_request(client: CompatClient, uri: str) -> Any:
    return await client.request(uri, dict(SCREEN_PAYLOAD))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "no answer (off, asleep, or off the network)"
    if isinstance(exc, aiohttp.ClientConnectorError | ConnectionRefusedError):
        return "connection refused (off, or the control ports are closed)"
    return str(exc) or type(exc).__name__
