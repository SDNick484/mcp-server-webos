"""Client keys, one per TV, in keys.json (0600), kept apart from config.json.

A client key is what the TV issued when someone accepted the pairing dialog,
and it is full control of that TV: anyone who has it can do anything this
server can, and more. So it never goes in config.json (which you might show
or paste), never in a log line, and never in a tool result.

Keys are stored by deviceUUID, the identity the TV reports in its hello
(ASSUMPTION W-UUID-STABLE), not by name or address: a renamed TV keeps its
key, and a key is never offered to a different TV that took over an address.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import config_dir, ensure_private_dir, write_private


def path() -> Path:
    return config_dir() / "keys.json"


def load() -> dict[str, str]:
    """uuid -> client key. Missing or unreadable means no keys (doctor reports the file)."""
    try:
        data = json.loads(path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def get(uuid: str | None) -> str | None:
    return load().get(uuid) if uuid else None


def put(uuid: str, key: str) -> None:
    ensure_private_dir()
    keys = load()
    keys[uuid] = key
    write_private(path(), json.dumps(keys, indent=2) + "\n")


def stamp() -> tuple[int, int] | None:
    """Changes whenever `pair` writes keys.json, so a running server notices a
    new key (the Shield's credential-stamp idea) and stops waiting for one."""
    try:
        st = path().stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)
