"""pair, check, doctor, capture and call, against the simulated TVs."""

from __future__ import annotations

import argparse
import json
import stat

import pytest

from webos_mcp import cli, keystore
from webos_mcp.config import load_settings
from webos_mcp.doctor import STEP, render, run_doctor, to_json

from .conftest import KEY, register

pytestmark = pytest.mark.anyio


def pair_args(host: str, name: str = "Family Room", **kw) -> argparse.Namespace:
    return argparse.Namespace(
        host=host, name=name, alias=kw.get("alias", []), mac=kw.get("mac", []), timeout=kw.get("timeout", 5.0)
    )


async def test_pair(home, tvs, capsys):
    # ASSUMPTION W-MAC-SOURCE: the MACs come from connection info, the connected one first
    tv = await tvs("g1", accept_after=0.2)
    assert await cli._cmd_pair(pair_args(tv.host, alias=["G1"])) == 0
    (saved,) = load_settings().tvs
    assert (saved.name, saved.uuid, saved.platform, saved.model) == (
        "Family Room",
        tv.profile.uuid,
        "6.5.3",
        "OLED77G1PUA",
    )
    assert saved.macs == (tv.profile.wifi_mac, tv.profile.wired_mac)  # the G1 is on Wi-Fi
    # The G1 talks plain ws on 3000 (W-PORTS), so there is no TLS certificate to remember
    assert set(saved.aliases) == {"G1", "OLED77G1PUA"} and saved.cert_sha256 is None
    assert keystore.get(tv.profile.uuid) in tv.keys
    assert stat.S_IMODE(keystore.path().stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "Accept the pairing request on the TV" in out and keystore.get(tv.profile.uuid) not in out


async def test_pair_saves_the_tls_fingerprint(home, tvs):
    # ASSUMPTION W-TLS-CERT: on a TLS-only TV (the C5) pair remembers the certificate, so a change warns
    tv = await tvs("c5-webos25", accept_after=0.2)
    assert await cli._cmd_pair(pair_args(tv.host, name="Office")) == 0
    (saved,) = load_settings().tvs
    assert saved.cert_sha256 and len(saved.cert_sha256) == 64


async def test_pair_again_updates_the_same_tv(home, tvs):
    tv = await tvs("g1", accept_after=0.05)
    assert await cli._cmd_pair(pair_args(tv.host)) == 0
    assert await cli._cmd_pair(pair_args(tv.host, mac=["A8:23:FE:99:99:99"])) == 0
    (saved,) = load_settings().tvs
    assert saved.macs == ("a8:23:fe:99:99:99",)


async def test_pair_declined_or_ignored(home, tvs, capsys):
    tv = await tvs("g1", pairing="decline")
    assert await cli._cmd_pair(pair_args(tv.host)) == 1
    assert "Pairing didn't complete" in capsys.readouterr().err and not keystore.path().exists()


async def test_pair_nobody_home(home, capsys):
    assert await cli._cmd_pair(pair_args("127.0.0.9")) == 1
    assert "No webOS TV answers" in capsys.readouterr().err


async def test_pair_refuses_public_addresses(home, capsys):
    assert await cli._cmd_pair(pair_args("8.8.8.8")) == 1


async def test_pair_refuses_a_name_another_tv_has(home, tvs, capsys):
    g1, c7 = await tvs("g1"), await tvs("c7", accept_after=0.05)
    register(g1)
    assert await cli._cmd_pair(pair_args(c7.host, name="family room")) == 1
    assert "already Family Room" in capsys.readouterr().err


def test_macs_from_connection_info():
    info = {
        "wiredInfo": {"macAddress": "A8:23:FE:00:00:01", "state": "disconnected"},
        "wifiInfo": {"macAddress": "a8:23:fe:00:00:02", "state": "connected"},
    }
    assert cli.macs_from(info) == ["a8:23:fe:00:00:02", "a8:23:fe:00:00:01"]
    assert cli.macs_from({}) == []


# --- doctor -------------------------------------------------------------------------
async def doctor(**kw):
    return await run_doctor(load_settings(), timeout=1.0, ssdp=False, **kw)


def layers(report, i=0) -> dict[str, bool]:
    return {c.layer: c.ok for c in report.tvs[i].checks}


async def test_doctor_all_good(home, tvs):
    tv = await tvs("c5-webos26")
    register(tv)
    report = await doctor()
    assert report.ok and layers(report) == {"config": True, "tcp": True, "hello": True, "session": True, "wol": True}
    text = render(report)
    assert "webOS 11.2.0 on port 3001" in text and "3000 open, 3001 open" in text
    assert "refused: getCurrentSWInformation (401 insufficient permissions)" in text  # ASSUMPTION W-SWINFO-401
    assert tv.host in render(report, redacted=False)
    assert json.loads(to_json(report))["ok"] is True
    # read-only: nothing that changes the TV
    assert not [u for u in tv.requests if u.startswith(("audio/set", "system/turn", "system.launcher", "tv/switch"))]


async def test_doctor_not_paired(home, tvs):
    tv = await tvs("g1")
    register(tv)
    keystore.path().unlink()
    report = await doctor()
    assert not report.ok and layers(report) == {"config": False}
    assert f"step {STEP['config']}" in report.tvs[0].checks[0].hint


async def test_doctor_loose_permissions(home, tvs):
    tv = await tvs("g1")
    register(tv)
    keystore.path().chmod(0o644)
    report = await doctor()
    assert "readable by other users: keys.json" in report.tvs[0].checks[0].detail


async def test_doctor_tv_off(home, tvs):
    tv = await tvs("c7")
    register(tv)
    await tv.unplug()
    report = await doctor()
    assert layers(report) == {"config": True, "tcp": False}


async def test_doctor_stale_key_hangs_up(home, tvs):
    # ASSUMPTION W-STALE-KEY, W-PROMPT-DISMISS
    tv = await tvs("g1", accept_after=60)
    register(tv)
    tv.keys.clear()
    report = await doctor()
    session = next(c for c in report.tvs[0].checks if c.layer == "session")
    assert not session.ok and "hung up at once" in session.detail
    assert tv.events("prompt") == [("prompt",)]


async def test_doctor_another_device_at_the_address(home, tvs):
    g1, c7 = await tvs("g1"), await tvs("c7")
    register(g1, host=c7.host)
    report = await doctor()
    assert layers(report)["hello"] is False and "different device" in report.tvs[0].checks[-1].detail
    assert c7.events("register") == []


async def test_doctor_no_tvs(home):
    report = await doctor()
    assert not report.ok and "no TVs configured" in report.problems[0]


# --- capture, check, call ---------------------------------------------------------------
async def test_capture_is_redacted(home, tvs, tmp_path):
    from webos_mcp.capture import capture

    tv = await tvs("g1")
    register(tv)
    path = await capture(load_settings().tv("Family Room"), "g1", tmp_path)
    text = path.read_text()
    record = json.loads(text)
    assert record["profile"] == "g1" and len(record["exchanges"]) == 12
    for secret in (KEY, tv.profile.uuid, tv.profile.serial, tv.profile.wired_mac, tv.socket_id):
        assert secret not in text
    assert not [u for u in tv.requests if u.startswith(("audio/set", "system/turn", "system.launcher"))]


async def test_check(home, tvs, capsys):
    tv = await tvs("c7")
    register(tv)
    assert await cli._cmd_check(argparse.Namespace(tv=None)) == 0
    out = capsys.readouterr().out
    assert "Bedroom: OLED65C7P-U, webOS 3.9.3" in out and "can't: screen" not in out
    assert "power state: inferred" not in out  # the C7 reports one after all (W-C7-NO-POWERSTATE)


async def test_call(home, tvs, capsys):
    tv = await tvs("c7")
    register(tv)
    assert await cli._cmd_call("set_volume", ["level=7"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "done" and tv.volume == 7
    assert await cli._cmd_call("set_power", ["state=on", "tv=garage"]) == 1


def test_no_subcommand_means_serve():
    assert cli.normalize(["--http"]) == ["serve", "--http"]
    assert cli.normalize([]) == ["serve"]
    assert cli.normalize(["pair", "--host", "x"]) == ["pair", "--host", "x"]
    from webos_mcp import remote

    cfg = remote.http_config(cli.build_parser().parse_args(["serve", "--http"]), env={})
    assert (cfg.port, cfg.path) == (8715, "/webos/mcp")
