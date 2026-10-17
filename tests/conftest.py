"""Shared fixtures. Tests run against the simulated TVs in sim/: no TV (or LAN) needed."""

from __future__ import annotations

import pytest


# Async tests use anyio's pytest plugin (marked with pytest.mark.anyio) rather
# than pytest-asyncio: it runs an async fixture's setup and teardown in the
# same task, which the MCP SDK's in-process Client (built on anyio) requires.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty config directory, used for the whole test."""
    d = tmp_path / "config"
    monkeypatch.setenv("WEBOS_CONFIG_DIR", str(d))
    monkeypatch.delenv("WEBOS_DRY_RUN", raising=False)
    return d


# --- simulated TVs ------------------------------------------------------------
# Each profile gets its own loopback address, so its fake TV can bind the real
# ports 3000/3001 that aiowebostv always dials (see sim/fake_tv.py).
HOSTS = {"c7": "127.0.0.2", "g1": "127.0.0.3", "c5-webos25": "127.0.0.4", "c5-webos26": "127.0.0.5"}


@pytest.fixture
async def tvs():
    """Start simulated TVs on demand: ``tv = await tvs("g1")``. All are stopped afterwards."""
    from webos_mcp.sim.fake_tv import make

    started = []

    async def start(profile: str, **kwargs):
        tv = make(profile, HOSTS[profile], **kwargs)
        await tv.start()
        started.append(tv)
        return tv

    yield start
    for tv in started:
        await tv.stop()


def no_power_state():
    """A TV without getPowerState (404), as pre-2017 sets are reported to be (ASSUMPTION W-C7-NO-POWERSTATE).
    None of the owner's TVs is one: the C7 turned out to answer it."""
    from webos_mcp.sim.fake_tv import NOT_FOUND, Faults

    return Faults(deny={"com.webos.service.tvpower/power/getPowerState": NOT_FOUND})


KEY = "0123456789abcdef0123456789abcdef"
NAMES = {"c7": "Bedroom", "g1": "Family Room", "c5-webos25": "Office", "c5-webos26": "Office"}


def register(tv, name: str | None = None, **extra) -> str:
    """Pair a simulated TV the way `pair` would leave things: its key in keys.json, its entry in config.json.
    Wake-on-LAN goes straight to the simulator's listener."""
    from webos_mcp import keystore
    from webos_mcp.config import save_config, upsert_tv

    name = name or NAMES[tv.profile.key]
    tv.pair_with(KEY)
    keystore.put(tv.profile.uuid, KEY)
    entry = {
        "name": name,
        "host": tv.host,
        "uuid": tv.profile.uuid,
        "macs": [tv.mac],
        "model": tv.profile.model,
        "platform": tv.profile.release_version,
        **extra,
    }
    upsert_tv(entry)
    save_config(wol_port=tv.wol_port, wol_broadcast=tv.host)
    return name


def fast(client):
    """Shrink a TvClient's timers so tests don't wait like a real deployment would."""
    client.poll_interval = 0.2
    client.jitter = 0.0
    client.drop_retries = (0.05, 0.1)
    client.fast_poll = 0.05
    client.refresh_min_age = 0.0
    client.watch_interval = 0.05
    client.wake_wait = 5.0
    client.confirm_wait = 3.0
    return client


async def wait_until(cond, timeout: float = 5.0) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)
