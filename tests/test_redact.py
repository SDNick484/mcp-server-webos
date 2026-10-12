"""webOS redaction on top of logsafe: keys always, identifiers with addresses."""

from __future__ import annotations

import logging

from webos_mcp.redact import Formatter, redact, redact_secrets

KEY = "0123456789abcdef0123456789abcdef"
UUID = "c5c5c5c5-0000-4000-8000-000000000025"


def test_keys_are_always_hidden():
    line = f"send: {{'payload': {{'client-key': '{KEY}'}}}} and {{\"client-key\": \"{KEY}\"}} and {KEY}"
    out = redact_secrets(line)
    assert KEY not in out and out.count("[key redacted]") == 3


def test_identifiers_follow_the_address_setting():
    line = f"recv(192.168.1.60): {{'serialNumber': '507ABCD00025', 'deviceUUID': '{UUID}'}} wss://x/resources/9f2c11aa/netinput.pointer.sock"
    out = redact(line)
    assert "507ABCD00025" not in out and UUID not in out and "9f2c11aa" not in out
    assert "x.x.x.60" in out and "[uuid …0025]" in out and "[serial]" in out
    assert "/resources/[id]/" in out


def test_fingerprints_are_not_keys():
    fp = "ab" * 32
    assert redact(fp) == fp


def test_formatter_without_address_redaction_still_hides_keys():
    record = logging.LogRecord(
        "aiowebostv", logging.DEBUG, __file__, 1, "key %s at %s %s", (KEY, "192.168.1.60", UUID), None
    )
    assert Formatter("%(message)s", addresses=False).format(record) == f"key [key redacted] at 192.168.1.60 {UUID}"
    assert Formatter("%(message)s").format(record) == "key [key redacted] at x.x.x.60 [uuid …0025]"
