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
