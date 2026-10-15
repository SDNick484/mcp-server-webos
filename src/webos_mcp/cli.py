"""Entry point: ``mcp-server-webos [serve] | pair | check | apps | discover | doctor | capture | simulate | call``.

``serve`` is the default, so ``mcp-server-webos`` alone runs the MCP server
over stdio, and ``mcp-server-webos --http`` works as well as
``mcp-server-webos serve --http`` (the spelling the sibling servers accept).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from aiowebostv.exceptions import WebOsTvCommandError, WebOsTvPairError

from . import __version__, keystore, probe, remote
from .compat import CompatClient, PairingRefused
from .config import config_dir, ensure_private_dir, is_local_ipv4, load_settings, normalize_mac, save_config, upsert_tv
from .redact import setup_logging


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _unredacted(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_redact", False)) or _flag(os.environ.get("WEBOS_LOG_UNREDACTED"))


def _setup_logging(args: argparse.Namespace) -> None:
    """stderr only (stdout belongs to the MCP stdio transport), redacted: LAN
    addresses, MACs, serials and UUIDs are masked unless --no-redact /
    WEBOS_LOG_UNREDACTED=1; client keys, certificates and JWTs always are."""
    debug = getattr(args, "debug", False) or _flag(os.environ.get("WEBOS_DEBUG"))
    setup_logging(logging.DEBUG if debug else logging.INFO, redacted=not _unredacted(args))
    if not debug:
        # The library logs every message it sends and receives at DEBUG; keep INFO readable.
        logging.getLogger("aiowebostv").setLevel(logging.INFO)


def macs_from(connection: dict[str, Any]) -> list[str]:
    """The TV's MACs from connectionmanager/getinfo, the connected interface first.
    ASSUMPTION W-MAC-SOURCE: the reply's shape is a guess until a capture shows it."""
    found: list[tuple[bool, str]] = []
    for key in ("wiredInfo", "wifiInfo"):
        info = connection.get(key)
        if isinstance(info, dict):
            mac = normalize_mac(str(info.get("macAddress") or ""))
            if mac:
                found.append((info.get("state") != "connected", mac))
    return [mac for _, mac in sorted(found)]


async def _cmd_pair(args: argparse.Namespace) -> int:
    """Pair once, so later connections are trusted without a dialog.

    1. Ask the TV who it is (the keyless hello): its deviceUUID and platform.
    2. Register without a key: the TV shows a dialog; accept it with the remote.
       ASSUMPTION W-PAIR-WINDOW: we wait up to --timeout seconds for that.
    3. Read its model and MACs over the new session, then save the key in
       keys.json (by deviceUUID) and the TV in config.json. A running server
       notices the new key and connects with it; no restart needed.
    """
    host, name = args.host.strip(), args.name.strip()
    if not is_local_ipv4(host):
        print(f"{host} is not a private IPv4 address; TVs are only reached on the local network.", file=sys.stderr)
        return 1
    hello = await probe.any_hello(host, timeout=5.0)
    if hello is None or not hello.uuid:
        print(
            f"No webOS TV answers at {host} (ports 3000 and 3001). Is it on, and on this network? Older TVs need "
            "'LG Connect Apps' turned on in their network settings.",
            file=sys.stderr,
        )
        return 1
    settings = load_settings()
    taken = next((t for t in settings.tvs if t.answers_to(name) and t.uuid != hello.uuid), None)
    if taken is not None:
        print(f"The name {name!r} is already {taken.describe()}. Pick another, or edit config.json.", file=sys.stderr)
        return 1
    print(f"Found a webOS {hello.platform or '?'} TV on port {hello.port}.")
    print(f"Accept the pairing request on the TV with its remote (waiting up to {args.timeout:.0f} s)...", flush=True)
    client = CompatClient(host, None, expected_uuid=hello.uuid, pair_timeout=args.timeout)
    try:
        await client.connect()
    except PairingRefused as exc:
        print(f"The TV refused the registration before showing a dialog: {exc}", file=sys.stderr)
        return 1
    except (WebOsTvPairError, WebOsTvCommandError, TimeoutError, OSError) as exc:
        print(f"Pairing didn't complete: {str(exc) or type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        key = client.client_key
        model = client.tv_info.system.get("modelName")
        macs = (
            [m for m in (normalize_mac(x) for x in args.mac) if m] if args.mac else macs_from(client.tv_info.connection)
        )
        cert = client.cert_sha256()
    finally:
        await client.disconnect()
    assert key is not None
    ensure_private_dir()
    keystore.put(hello.uuid, key)
    existing = next((t for t in settings.tvs if t.uuid == hello.uuid), None)
    aliases = sorted({*(existing.aliases if existing else ()), *args.alias, *([model] if model else [])})
    upsert_tv(
        {
            "name": name,
            "aliases": aliases,
            "host": host,
            "uuid": hello.uuid,
            "macs": macs,
            "model": model,
            "platform": hello.platform,
            "cert_sha256": cert,
        },
        match_uuid=hello.uuid,
    )
    print(f"Paired {name}: {model or 'model unknown'}, webOS {hello.platform or '?'}.")
    if macs:
        print(f"Wake-on-LAN will use {', '.join(macs)}.")
    else:
        print("Couldn't read its MAC address, so set_power on won't work. Re-run with --mac AA:BB:CC:DD:EE:FF.")
    print(f"Saved in {config_dir()} (the key is in keys.json; keep it private).")
    return 0


async def _cmd_check(args: argparse.Namespace) -> int:
    """Connect once to each TV (or one) and show what works on it."""
    from .client import TvClient

    settings = load_settings()
    for problem in settings.problems:
        print(f"Config: {problem}", file=sys.stderr)
    chosen = [t for t in settings.tvs if args.tv is None or t.answers_to(args.tv)]
    if not chosen:
        print("No TVs configured (or none by that name). Run `mcp-server-webos pair`.", file=sys.stderr)
        return 1
    status = 0
    for tv in chosen:
        client = TvClient(tv, settings)
        await client.connect_once(10.0)
        s = client.snapshot()
        await client.stop()
        print(f"{s['tv']}: {s['model'] or '?'}, webOS {s['platform'] or '?'} at {s['host']}")
        if not s["reachable"]:
            print(f"  not reachable: {s['error']}")
            status = 1
            continue
        print(f"  power {s['power']}, sound output {s['sound_output']} (volume owner: {s['volume_owner']})")
        print(f"  can: {', '.join(s['capabilities'])}")
        for lim in s["limitations"]:
            print(f"  can't: {lim}")
    return status


async def _cmd_apps(args: argparse.Namespace) -> int:
    """List each TV's installed apps by group, and add chosen ones to the allow-list (see apps.py)."""
    from . import apps

    settings = load_settings()
    if args.all_tvs:
        chosen = list(settings.tvs)
    elif args.tv:
        chosen = [t for t in settings.tvs if t.answers_to(args.tv)]
    else:
        chosen = list(settings.tvs) if len(settings.tvs) == 1 else []
        if len(settings.tvs) > 1:  # never guess which TV
            names = ", ".join(t.name for t in settings.tvs)
            print(f"Which TV? Pass --tv NAME or --all-tvs. Configured: {names}.", file=sys.stderr)
            return 2
    if not chosen:
        print("No TVs configured (or none by that name). Run `mcp-server-webos pair`.", file=sys.stderr)
        return 1
    found: dict[str, list[dict[str, Any]]] = {}
    status = 0
    for tv in chosen:
        try:
            found[tv.name] = await apps.fetch(tv, args.timeout)
        except apps.FETCH_ERRORS as exc:
            print(f"{tv.name}: couldn't read its apps ({str(exc) or type(exc).__name__}). Is it on?", file=sys.stderr)
            status = 1
    if not found:
        return 1
    listing = apps.build_listing(settings, found)
    if args.json:
        print(apps.to_json(listing))
    else:
        print(apps.render(listing, several_tvs=len(chosen) > 1))
    refs = [r.strip() for value in args.add for r in value.split(",") if r.strip()]
    if not refs and not args.add_all:
        return status
    adding, problems = apps.select(listing, refs, args.add_all)
    for problem in problems:
        print(f"Not added: {problem}", file=sys.stderr)
    if not adding:
        print("\nNothing to add.")
        return 1 if problems else status
    apps.add_to_config(adding)
    print("\nAdded to the allow-list in config.json:")
    for a in adding:
        print(f"  {a.name} = {a.id}")
    print("Restart the server to use them (rc-service mcp-server-webos restart).")
    return 1 if problems else status


async def _cmd_discover(timeout: float) -> int:
    from .discovery import discover

    print(f"Searching for webOS TVs ({timeout:.0f}s)...")
    found = await discover(timeout)
    if not found:
        print(
            "Nothing answered. TVs that are off don't; on WSL2, multicast needs mirrored networking. "
            "Pair by address with `pair --host`."
        )
        return 1
    for host in found:
        hello = await probe.any_hello(host)
        what = f"webOS {hello.platform or '?'} on port {hello.port}" if hello else "no hello"
        print(f"  {host:<15}  {what}")
    return 0


async def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import render, run_doctor, to_json

    report = await run_doctor(load_settings(), timeout=args.timeout, only=args.tv, ssdp=not args.no_ssdp)
    redacted = not _unredacted(args)
    text = to_json(report, redacted) if args.json else render(report, redacted)
    print(text)
    if args.dump:
        path = Path(args.dump)
        path.mkdir(parents=True, exist_ok=True)
        (path / "doctor.json").write_text(to_json(report, True) + "\n")
    return 0 if report.ok else 1


async def _cmd_capture(args: argparse.Namespace) -> int:
    from .capture import capture

    settings = load_settings()
    tv = next((t for t in settings.tvs if t.answers_to(args.tv)), None)
    if tv is None:
        print(f"No TV called {args.tv!r}.", file=sys.stderr)
        return 1
    try:
        path = await capture(tv, args.profile, Path(args.out))
    except (WebOsTvPairError, WebOsTvCommandError, TimeoutError, OSError) as exc:
        print(f"Couldn't capture {tv.name}: {str(exc) or type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"Wrote {path} (redacted). Copy it into src/webos_mcp/sim/fixtures/ to make it a contract test.")
    return 0


async def _cmd_simulate(args: argparse.Namespace) -> int:
    """Run a simulated TV and point a config directory at it."""
    from .sim.fake_tv import make
    from .sim.profiles import SOURCES

    home = Path(args.config_dir).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    os.environ["WEBOS_CONFIG_DIR"] = str(home)

    def prompt() -> None:
        print(f"\n  The TV shows a pairing request; 'pressing OK' in {args.accept_after:.0f} s.\n", flush=True)

    tv = make(args.profile, args.host, on_prompt=prompt, accept_after=args.accept_after)
    try:
        await tv.start()
    except OSError as exc:
        print(f"Couldn't start the simulated TV on {args.host}: {exc}", file=sys.stderr)
        return 1
    save_config(wol_port=tv.wol_port, wol_broadcast=args.host)
    if args.paired:
        key = os.urandom(16).hex()
        tv.pair_with(key)
        keystore.put(tv.profile.uuid, key)
        upsert_tv(
            {
                "name": args.name,
                "aliases": [tv.profile.model],
                "host": args.host,
                "uuid": tv.profile.uuid,
                "macs": [tv.mac],
                "model": tv.profile.model,
                "platform": tv.profile.release_version,
            },
            match_uuid=tv.profile.uuid,
        )
    print(f"Simulated {tv.profile.model} (profile {args.profile}, webOS {tv.profile.release_version}) on {args.host}")
    print(
        f"  ports: 3000 {tv.profile.port3000}, 3001 {'wss' if tv.profile.wss else 'closed'}; "
        f"Wake-on-LAN on UDP {tv.wol_port}"
    )
    print("  where its behavior comes from:")
    for what, source in SOURCES.items():
        print(f"    {what}: {source}")
    print("\nIn another terminal:")
    print(f"  export WEBOS_CONFIG_DIR={home}")
    if not args.paired:
        print(f"  mcp-server-webos pair --host {args.host} --name '{args.name}'")
    print("  mcp-server-webos doctor --no-ssdp")
    print("  mcp-server-webos call get_status")
    print("Ctrl+C to stop.", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await tv.stop()
    return 0


async def _cmd_call(tool: str, raw_args: list[str], wait_s: float = 8.0) -> int:
    """One tool call through the real MCP layer, as the model would make it."""
    from mcp import Client

    from .server import mcp, registry

    tool_args: dict[str, object] = {}
    for pair in raw_args:
        key, sep, raw = pair.partition("=")
        if not sep:
            print(f"arguments are key=value, got {pair!r}", file=sys.stderr)
            return 2
        try:
            tool_args[key] = json.loads(raw)  # repeat=3 -> 3
        except json.JSONDecodeError:
            tool_args[key] = raw  # source=netflix -> a string
    async with Client(mcp) as c:
        if tool == "tools":
            for t in (await c.list_tools()).tools:
                print(f"{t.name:<14} {(t.description or '').splitlines()[0]}")
            return 0
        # The server connects in the background; give it a moment, as a long-running server would have had
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while loop.time() < deadline and not all(x.reachable or x.needs_pairing for x in registry().clients):
            await asyncio.sleep(0.1)
        result = await c.call_tool(tool, tool_args)
    if result.is_error:
        print(" ".join(getattr(part, "text", "") for part in result.content) or "error", file=sys.stderr)
        return 1
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body["result"]
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-server-webos", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", help="log protocol traffic to stderr (WEBOS_DEBUG=1)")
    common.add_argument(
        "--no-redact",
        action="store_true",
        help="show addresses, MACs, serials and UUIDs in logs (WEBOS_LOG_UNREDACTED=1); keys always stay hidden",
    )
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", parents=[common], help="run the MCP server (stdio by default, or --http)")
    p_serve.add_argument(
        "--dry-run", action="store_true", help="read the TVs, but send nothing that changes anything (WEBOS_DRY_RUN=1)"
    )
    remote.add_http_arguments(p_serve, default_port=8715, default_path="/webos/mcp")
    p_pair = sub.add_parser("pair", parents=[common], help="one-time pairing with a TV (accept on the TV)")
    p_pair.add_argument("--host", required=True, help="the TV's IP address")
    p_pair.add_argument("--name", required=True, help="what to call it, e.g. a room: 'Family Room'")
    p_pair.add_argument("--alias", action="append", default=[], help="another name it answers to; repeatable")
    p_pair.add_argument(
        "--mac", action="append", default=[], help="MAC for Wake-on-LAN, if it can't be read; repeatable"
    )
    p_pair.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for the TV's OK (default 60)")
    p_check = sub.add_parser("check", parents=[common], help="connect once to each TV and show what works on it")
    p_check.add_argument("--tv", help="only this TV")
    p_apps = sub.add_parser(
        "apps", parents=[common], help="list a TV's installed apps, and add chosen ones to the allow-list"
    )
    p_apps.add_argument("--tv", help="which TV (required with several, unless --all-tvs)")
    p_apps.add_argument("--all-tvs", action="store_true", help="every configured TV, merged into one list")
    p_apps.add_argument(
        "--add", action="append", default=[], metavar="NAME", help="add these (names, ids or titles; comma-separated)"
    )
    p_apps.add_argument("--add-all", action="store_true", help="add every third-party app not yet allowed")
    p_apps.add_argument("--json", action="store_true", help="machine-readable listing")
    p_apps.add_argument("--timeout", type=float, default=15.0, help="seconds per TV (default 15)")
    p_disc = sub.add_parser("discover", parents=[common], help="list webOS TVs that answer SSDP")
    p_disc.add_argument("--timeout", type=float, default=3.0)
    doc = sub.add_parser(
        "doctor", parents=[common], help="check config, network and pairing, layer by layer (read-only)"
    )
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument("--timeout", type=float, default=3.0, help="seconds per step (default 3)")
    doc.add_argument("--tv", help="only this TV")
    doc.add_argument("--no-ssdp", action="store_true", help="skip the SSDP search")
    doc.add_argument("--dump", metavar="DIR", help="also write the report (redacted) to DIR/doctor.json")
    cap = sub.add_parser(
        "capture", parents=[common], help="record a TV's read-only answers (redacted) for contract tests"
    )
    cap.add_argument("--tv", required=True)
    cap.add_argument("--profile", required=True, choices=["c7", "g1", "c5-webos25", "c5-webos26"])
    cap.add_argument("--out", default=".", metavar="DIR")
    sim = sub.add_parser("simulate", parents=[common], help="run a simulated TV, for testing without one")
    sim.add_argument("--profile", default="g1", choices=["c7", "g1", "c5-webos25", "c5-webos26"])
    sim.add_argument("--config-dir", required=True, metavar="DIR", help="config directory to point at the simulator")
    sim.add_argument("--host", default="127.0.0.3", help="loopback address to listen on (default 127.0.0.3)")
    sim.add_argument("--name", default="Simulated TV", help="its name in config.json (with --paired)")
    sim.add_argument("--paired", action="store_true", help="skip pairing: make a key and trust it")
    sim.add_argument(
        "--accept-after", type=float, default=2.0, help="seconds before 'OK' is pressed on a pairing request"
    )
    cal = sub.add_parser("call", parents=[common], help="call one tool as the model would and print the result")
    cal.add_argument("tool", help="tool name, or 'tools' to list them")
    cal.add_argument("args", nargs="*", metavar="key=value", help="tool arguments (values are JSON if they parse)")
    cal.add_argument("--dry-run", action="store_true", help="send nothing that changes anything")
    return parser


def normalize(argv: list[str]) -> list[str]:
    """No subcommand means serve: `mcp-server-webos --http` is `serve --http`."""
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        return ["serve", *argv]
    return list(argv)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(normalize(sys.argv[1:] if argv is None else argv))
    if getattr(args, "dry_run", False):
        os.environ["WEBOS_DRY_RUN"] = "1"  # the server's lifespan reads settings from the environment
    _setup_logging(args)

    commands = {
        "pair": lambda: _cmd_pair(args),
        "check": lambda: _cmd_check(args),
        "apps": lambda: _cmd_apps(args),
        "discover": lambda: _cmd_discover(args.timeout),
        "doctor": lambda: _cmd_doctor(args),
        "capture": lambda: _cmd_capture(args),
        "call": lambda: _cmd_call(args.tool, args.args),
    }
    if args.cmd in commands:
        sys.exit(asyncio.run(commands[args.cmd]()))
    if args.cmd == "simulate":
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(asyncio.run(_cmd_simulate(args)))
        return

    from .server import mcp  # imported late: `pair` and `doctor` don't need the server code

    # stdio by default (the client launches us); --http runs a long-lived
    # service for an LXC behind Cloudflare Access (see remote.py).
    if args.http:
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-webos: {exc}\n")
    else:
        mcp.run()
