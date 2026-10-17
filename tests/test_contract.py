"""Captured answers from the real TVs vs. what the simulator answers.

`mcp-server-webos capture --tv <name> --profile <p>` records a TV's answers to
read-only requests, redacted; committed to src/webos_mcp/sim/fixtures/, each
file becomes a test here. For every request, the simulator profile must give
the same kind of answer (a response or an error), and every field the
simulator puts in a response must be in the TV's too, with the same JSON
type: the simulator may know less than the TV, but it may not claim what the
TV doesn't do. The session must also end up on the same port (3000 or 3001).
Until there are captures, this skips, saying so.
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
        port = client.port()
    finally:
        await client.disconnect()
    # Every difference at once, not just the first: each capture is a trip to a real TV, so one run
    # should say everything the simulator gets wrong about it.
    problems: list[str] = []
    # Which port aiowebostv settles on says which ports the TV answers (ASSUMPTION W-PORTS)
    if captured.get("port") is not None and captured["port"] != port:
        problems.append(f"port: the TV's session was on {captured['port']}, the simulator's on {port}")
    for real, fake in zip(captured["exchanges"], ours, strict=True):
        uri = real["uri"]
        if real["type"] != fake["type"]:
            said = real.get("error") or "a response"
            problems.append(f"{uri}: the TV gave {real['type']} ({said}), the simulator gave {fake['type']}")
            continue
        if real["type"] != "response":
            continue
        for key, value in fake["payload"].items():
            if key not in real["payload"]:
                problems.append(f"{uri}: the simulator sends {key!r}, the TV doesn't")
            elif kind(real["payload"][key]) != kind(value):
                problems.append(
                    f"{uri}: {key!r} is {kind(real['payload'][key])} on the TV, {kind(value)} in the simulator"
                )
    assert not problems, f"{fixture.name}: {len(problems)} difference(s)\n  " + "\n  ".join(problems)
