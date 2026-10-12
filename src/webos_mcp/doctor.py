"""`mcp-server-webos doctor`: check everything between this machine and each TV, layer by layer.

"Can't reach the TV" has a handful of causes, and the server can only say
which one it saw last. doctor checks each layer in order, per TV, and the
first failure says what it means, what to try, and which
HARDWARE_VALIDATION.md step covers it (STEP below):

  config   - config.json parses; the TV has an entry and a key; files are private (0600, dir 0700)
  tcp      - which of ports 3000 (ws) and 3001 (wss) accept a connection (W-PORTS)
  hello    - the keyless hello answers: platform version, deviceUUID matches the paired TV
             (a different one means the address now belongs to another device), TLS fingerprint (W-TLS-CERT)
  session  - a session with the stored key: registration without a dialog, power state, sound output,
             whether buttons are allowed, which read-only calls the TV permits (W-INPUT-SOCKET, W-SWINFO-401)
  wol      - a MAC is saved, and which local address and broadcast the magic packet would use (W-WOL)
  ssdp     - (once) which webOS TVs answer SSDP, and whether each configured one does (W-SSDP)

Nothing here changes anything: no keys, no launches, no power, no pairing.
If the stored key isn't accepted, the TV wants to show its pairing dialog;
doctor closes the connection at once (ASSUMPTION W-PROMPT-DISMISS says the
dialog goes with it; step 3 checks that) and tells you to re-pair.
"""

from __future__ import annotations

import asyncio
import json
import platform
import stat
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import aiohttp
from aiowebostv import endpoints as ep
from aiowebostv.exceptions import WebOsTvCommandError, WebOsTvPairError

from . import keystore, probe, wol
from .assumptions import unverified
from .compat import CompatClient, KeyNotAccepted, PairingRefused, WrongDevice
from .config import Settings, TvSettings, config_dir
from .redact import redact

# HARDWARE_VALIDATION.md step that covers each layer, for the hints.
STEP = {"config": 3, "tcp": 4, "hello": 4, "session": 3, "wol": 6, "ssdp": 11}

# Read-only calls the session check tries, to show what the TV permits an unsigned key.
PERMISSION_PROBES = (
    ep.GET_SOFTWARE_INFO,
    ep.GET_POWER_STATE,
    ep.GET_SOUND_OUTPUT,
    ep.GET_APPS,
    ep.GET_INPUTS,
    ep.GET_MEDIA_FOREGROUND_APP_INFO,
)


@dataclass
class Check:
    layer: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class TvReport:
    name: str
    host: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


@dataclass
class Report:
    versions: dict[str, str]
    dry_run: bool
    problems: list[str] = field(default_factory=list)
    tvs: list[TvReport] = field(default_factory=list)
    ssdp: list[str] = field(default_factory=list)
    unverified_assumptions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.tvs) and all(t.ok for t in self.tvs)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("mcp-server-webos", "mcp", "aiowebostv", "aiohttp"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def _hint(layer: str, text: str) -> str:
    return f"{text} (HARDWARE_VALIDATION.md step {STEP[layer]})"


def _config_check(tv: TvSettings) -> Check:
    pair_cmd = f"`mcp-server-webos pair --host {tv.host} --name '{tv.name}'`"
    if not tv.uuid or keystore.get(tv.uuid) is None:
        return Check("config", False, "no client key for this TV", _hint("config", f"Run {pair_cmd}"))
    loose = [
        p.name
        for p in (config_dir() / "config.json", keystore.path())
        if p.exists() and stat.S_IMODE(p.stat().st_mode) & 0o077
    ]
    if loose:
        return Check(
            "config",
            False,
            f"readable by other users: {', '.join(loose)}",
            f"chmod 600 {' '.join(loose)} in {config_dir()}: a client key is full control of the TV",
        )
    macs = ", ".join(tv.macs) or "no MAC"
    return Check("config", True, f"paired ({tv.model or 'model unknown'}, {macs}); files are private")


async def _tcp(host: str, port: int, timeout: float) -> str | None:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as exc:
        return str(exc) or type(exc).__name__
    writer.close()
    return None


async def _session(tv: TvSettings, timeout: float) -> Check:
    key = keystore.get(tv.uuid)
    client = CompatClient(tv.host, key, expected_uuid=tv.uuid, connect_timeout=timeout)
    try:
        async with asyncio.timeout(4 * timeout):
            await client.connect()
    except KeyNotAccepted:
        await client.disconnect()
        return Check(
            "session",
            False,
            "the TV doesn't accept the stored key and wanted to show its pairing dialog; doctor hung up at once",
            _hint("session", f"Re-pair: `mcp-server-webos pair --host {tv.host} --name '{tv.name}'`"),
        )
    except PairingRefused as exc:
        await client.disconnect()
        return Check("session", False, f"the TV refused the registration: {exc}", _hint("session", "Re-pair"))
    except WrongDevice as exc:
        await client.disconnect()
        return Check("session", False, str(exc), _hint("hello", "Re-pair with the TV's current address"))
    except (WebOsTvPairError, WebOsTvCommandError, TimeoutError, OSError, aiohttp.ClientError) as exc:
        await client.disconnect()
        return Check("session", False, f"no session: {str(exc) or type(exc).__name__}", _hint("session", ""))
    try:
        state = client.tv_state
        power = (state.power_state or {}).get("state") or ("on (inferred)" if state.is_on else "standby (inferred)")
        allowed, denied = [], []
        for uri in PERMISSION_PROBES:
            try:
                await client.request(uri)
                allowed.append(uri.rsplit("/", 1)[-1])
            except WebOsTvCommandError as exc:
                denied.append(f"{uri.rsplit('/', 1)[-1]} ({_error_text(exc)})")
        buttons = "refused (keys won't work)" if client.buttons_denied else "allowed"
        detail = (
            f"registered without a dialog on port {client.port()}; power {power}; sound output "
            f"{state.sound_output}; buttons {buttons}; allowed: {', '.join(allowed) or 'none'}"
            + (f"; refused: {', '.join(denied)}" if denied else "")
        )
        return Check("session", True, detail)
    finally:
        await client.disconnect()


def _error_text(exc: Exception) -> str:
    response = exc.args[0] if exc.args else None
    if isinstance(response, dict):
        return str(response.get("error") or response.get("payload", {}).get("errorText") or "error")
    return str(exc)


async def check_tv(tv: TvSettings, settings: Settings, timeout: float) -> TvReport:
    report = TvReport(tv.name, tv.host)
    cfg = _config_check(tv)
    report.checks.append(cfg)
    if not cfg.ok:
        return report

    ports = {port: await _tcp(tv.host, port, timeout) for port in (probe.PLAIN, probe.TLS)}
    open_ports = [p for p, err in ports.items() if err is None]
    if not open_ports:
        report.checks.append(
            Check(
                "tcp",
                False,
                f"no connection on 3000 or 3001 ({ports[probe.TLS]})",
                _hint(
                    "tcp",
                    "Is the TV on, on this network? An off TV doesn't answer (set_power on, or the remote). A "
                    "firewall, guest network or IoT VLAN in between shows up the same way",
                ),
            )
        )
        return report
    shown = ", ".join(f"{p} {'open' if err is None else 'closed'}" for p, err in ports.items())
    report.checks.append(Check("tcp", True, shown))

    hello: probe.Hello | None = None
    for port in open_ports[::-1]:  # 3001 first when both answer: that's where newer TVs talk
        try:
            hello = await probe.hello(tv.host, port, timeout)
            break
        except (OSError, TimeoutError, ValueError, aiohttp.ClientError):
            continue
    if hello is None:
        report.checks.append(Check("hello", False, "ports open, but no webOS hello on either", _hint("hello", "")))
        return report
    if tv.uuid and hello.uuid != tv.uuid:
        report.checks.append(
            Check(
                "hello",
                False,
                f"{tv.host} is a different device (deviceUUID {hello.uuid}), not the paired TV",
                _hint("hello", "The address now belongs to another device. Re-pair with the TV's new address"),
            )
        )
        return report
    cert = ""
    if hello.cert_sha256:
        same = tv.cert_sha256 in (None, hello.cert_sha256)
        cert = f"; TLS certificate {hello.cert_sha256[:16]}…" + ("" if same else " (CHANGED since pairing)")
    report.checks.append(
        Check(
            "hello",
            True,
            f"webOS {hello.platform or '?'} on port {hello.port}, pairing types "
            f"{', '.join(hello.payload.get('pairingTypes') or []) or '?'}{cert}",
        )
    )

    report.checks.append(await _session(tv, timeout))
    if not tv.macs:
        report.checks.append(
            Check(
                "wol", False, "no MAC saved: set_power on can't work", _hint("wol", "Re-pair while on, or pair --mac")
            )
        )
    else:
        source = wol.route_source(tv.host) or "no route"
        report.checks.append(
            Check(
                "wol",
                True,
                f"magic packet for {', '.join(tv.macs)} to {settings.wol_broadcast}:{settings.wol_port} and "
                f"{tv.host}:{settings.wol_port}, from local address {source}",
            )
        )
    return report


async def run_doctor(settings: Settings, timeout: float = 3.0, only: str | None = None, ssdp: bool = True) -> Report:
    report = Report(
        versions=versions(),
        dry_run=settings.dry_run,
        problems=list(settings.problems),
        unverified_assumptions=[f"{a.id} ({a.confidence}): {a.claim}" for a in unverified()],
    )
    chosen = [t for t in settings.tvs if only is None or t.answers_to(only)]
    if not settings.tvs:
        report.problems.append("no TVs configured: run `mcp-server-webos pair --host <ip> --name <room>`")
    elif not chosen:
        report.problems.append(f"no TV called {only!r}")
    for tv in chosen:
        report.tvs.append(await check_tv(tv, settings, timeout))
    if ssdp:
        from .discovery import discover

        try:
            found = await discover(min(timeout, 3.0))
            known = {t.host: t.name for t in settings.tvs}
            report.ssdp = [f"{h}" + (f" ({known[h]})" if h in known else "") for h in found]
        except OSError as exc:
            report.ssdp = [f"SSDP search failed: {exc}"]
    else:
        report.ssdp = ["skipped"]
    return report


def render(report: Report, redacted: bool = True) -> str:
    lines = ["mcp-server-webos doctor", ""]
    lines += [f"  {k}: {v}" for k, v in report.versions.items()]
    if report.dry_run:
        lines += ["", "  note: dry run is on (WEBOS_DRY_RUN): actions are reported, not sent"]
    for p in report.problems:
        lines += ["", f"  x  {p}"]
    for tv in report.tvs:
        lines += ["", f"{tv.name} ({tv.host})"]
        for c in tv.checks:
            lines.append(f"   {'ok' if c.ok else 'x '} {c.layer:<8} {c.detail}")
            if c.hint:
                lines.append(f"      -> {c.hint}")
    lines += ["", "SSDP (webOS second-screen)"]
    lines += [f"   {s}" for s in report.ssdp] or [
        "   nothing answered: TVs that are off don't, and multicast may be filtered here (WSL2 NAT, VLANs). "
        f"Pairing by address still works. (HARDWARE_VALIDATION.md step {STEP['ssdp']})"
    ]
    lines += ["", f"Not yet confirmed on hardware ({len(report.unverified_assumptions)}):"]
    lines += [f"   {u}" for u in report.unverified_assumptions]
    lines += ["", "OK" if report.ok else "PROBLEMS FOUND"]
    text = "\n".join(lines)
    return redact(text) if redacted else text


def to_json(report: Report, redacted: bool = True) -> str:
    data: dict[str, Any] = {**asdict(report), "ok": report.ok}
    for tv, raw in zip(report.tvs, data["tvs"], strict=True):
        raw["ok"] = tv.ok
    text = json.dumps(data, indent=2)
    return redact(text) if redacted else text
