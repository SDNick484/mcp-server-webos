"""Redaction for what webOS adds to logs, layered on the shared logsafe.py.

logsafe.py is byte-identical across the sibling servers, so webOS-specific
rules live here instead of in it. What it doesn't catch, and aiowebostv's
debug log carries:

    "client-key": "0123...cdef"          -> "client-key": "[key redacted]"   (always: it is the credential)
    0123456789abcdef0123456789abcdef     -> [key redacted]                   (a bare 32-hex key, always)
    'serialNumber': '507ABCD00025'       -> 'serialNumber': '[serial]'       (the getSystemInfo reply)
    c5c5c5c5-0000-4000-8000-000000000025 -> [uuid …0025]                     (deviceUUID, in every hello)
    /resources/9f2c.../netinput.pointer.sock -> /resources/[id]/netinput.pointer.sock

Keys are secrets, so they are hidden even with --no-redact. Serial numbers,
UUIDs and socket ids identify a TV the way an address does, so they follow
the address setting.
"""

from __future__ import annotations

import logging
import re
import sys

from . import logsafe

_KEY_FIELD = re.compile(r"""(["']client-key["']\s*:\s*["'])[^"']*(["'])""")
_BARE_KEY = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{32}(?![0-9A-Za-z])")
_SERIAL = re.compile(r"""(["']serial[_]?[Nn]umber["']\s*:\s*["'])[^"']*(["'])""")
_UUID = re.compile(
    r"(?<![0-9A-Za-z-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{8}([0-9a-fA-F]{4})(?![0-9A-Za-z-])"
)
_SOCKET = re.compile(r"/resources/[0-9A-Za-z]+/")


def redact_secrets(text: str) -> str:
    text = logsafe.redact_secrets(text)
    text = _KEY_FIELD.sub(r"\1[key redacted]\2", text)
    return _BARE_KEY.sub("[key redacted]", text)


def redact(text: str) -> str:
    # Before logsafe: its bare-MAC rule would take the last 12 digits of a UUID
    text = redact_secrets(text)
    text = _UUID.sub(lambda m: f"[uuid …{m.group(1)}]", text)
    text = _SERIAL.sub(r"\1[serial]\2", text)
    text = _SOCKET.sub("/resources/[id]/", text)
    return logsafe.redact(text)


class Formatter(logsafe.RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        # logging.Formatter's, not logsafe's: our rules must run before its address rules
        text = logging.Formatter.format(self, record)
        return redact(text) if self.addresses else redact_secrets(text)


def setup_logging(level: int, *, redacted: bool = True, fmt: str = "%(levelname)s %(name)s: %(message)s") -> None:
    """Log to stderr (stdout belongs to the MCP stdio transport). Addresses and
    identifiers are redacted unless asked not to; secrets always are."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(Formatter(fmt, addresses=redacted))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
