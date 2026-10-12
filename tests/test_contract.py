"""Captured answers from the real TVs vs. what the simulator answers.

`mcp-server-webos capture --tv <name> --profile <p>` records a TV's answers to
read-only requests, redacted; committed to src/webos_mcp/sim/fixtures/, each
file becomes a test here. For every request, the simulator profile must give
the same kind of answer (a response or an error), and every field the
simulator puts in a response must be in the TV's too, with the same JSON
type: the simulator may know less than the TV, but it may not claim what the
TV doesn't do. Until there are captures, this skips, saying so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from webos_mcp.capture import exchanges
from webos_mcp.compat import CompatClient

pytestmark = pytest.mark.anyio

FIXTURES = sorted((Path(__file__).parent.parent / "src/webos_mcp/sim/fixtures").glob("*.json"))


def kind(value: object) -> str:
    return type(value).__name__.replace("int", "number").replace("float", "number")


@pytest.mark.skipif(not FIXTURES, reason="no captures from real TVs yet (HARDWARE_VALIDATION.md step 1)")
@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.stem for f in FIXTURES])
async def test_simulator_matches_capture(tvs, fixture):
    captured = json.loads(fixture.read_text())
    tv = await tvs(captured["profile"])
    tv.pair_with("k" * 32)
    client = CompatClient(tv.host, "k" * 32)
    await client.connect()
    try:
        ours = await exchanges(client, tuple(e["uri"] for e in captured["exchanges"]))
    finally:
        await client.disconnect()
    for real, fake in zip(captured["exchanges"], ours, strict=True):
        assert real["type"] == fake["type"], (real["uri"], real, fake)
        if real["type"] == "response":
            for key, value in fake["payload"].items():
                assert key in real["payload"], (real["uri"], key)
                assert kind(real["payload"][key]) == kind(value), (real["uri"], key)
