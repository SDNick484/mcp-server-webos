"""`mcp-server-webos capture`: record what a real TV answers, redacted, for contract tests.

The simulator's answers come from other people's captures and from reading
aiowebostv; this is how they get checked against the owner's TVs. capture
connects with the stored key, makes read-only requests only (nothing that
changes the TV), and writes one JSON file per TV:

    {"profile": "g1", "model": ..., "platform": ..., "hello": {...},
     "exchanges": [{"uri": "audio/getVolume", "type": "response", "payload": {...}}, ...]}

Addresses, MACs, serial numbers, UUIDs and keys are redacted before the file
is written, so it can be committed to src/webos_mcp/sim/fixtures/, where
tests/test_contract.py replays each request against the matching simulator
profile and compares the shape of the answers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiowebostv import endpoints as ep
from aiowebostv.exceptions import WebOsTvCommandError

from . import keystore
from .compat import CompatClient
from .config import TvSettings
from .redact import redact

# Read-only: each asks, none changes anything.
READ_ONLY_URIS = (
    ep.GET_SYSTEM_INFO,
    ep.GET_SOFTWARE_INFO,
    ep.GET_CONNECTION_INFO,
    ep.GET_POWER_STATE,
    ep.GET_CURRENT_APP_INFO,
    ep.GET_AUDIO_STATUS,
    ep.GET_VOLUME,
    ep.GET_APPS,
    ep.GET_INPUTS,
    ep.GET_SOUND_OUTPUT,
    ep.GET_MEDIA_FOREGROUND_APP_INFO,
    ep.INPUT_SOCKET,
)


async def exchanges(client: CompatClient, uris: tuple[str, ...] = READ_ONLY_URIS) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for uri in uris:
        try:
            payload = await client.request(uri)
            out.append({"uri": uri, "type": "response", "payload": payload})
        except WebOsTvCommandError as exc:
            response = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {"error": str(exc)}
            out.append({"uri": uri, "type": "error", "error": response.get("error") or str(exc)})
    return out


async def capture(tv: TvSettings, profile: str, out_dir: Path, timeout: float = 10.0) -> Path:
    client = CompatClient(tv.host, keystore.get(tv.uuid), expected_uuid=tv.uuid, connect_timeout=timeout)
    await client.connect()
    try:
        record = {
            "profile": profile,
            "model": tv.model,
            "platform": client.tv_info.hello.get("deviceOSReleaseVersion"),
            "port": client.port(),
            "hello": client.tv_info.hello,
            "exchanges": await exchanges(client),
        }
    finally:
        await client.disconnect()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{profile}.json"
    path.write_text(redact(json.dumps(record, indent=1, sort_keys=True)) + "\n")
    return path
