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
