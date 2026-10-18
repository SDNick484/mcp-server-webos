"""MCP tool definitions. Deliberately small and typed: allow-listed keys and apps, no URLs, no text.

How a decorated function becomes a tool the model can call:
  - ``@mcp.tool()`` registers it; it then appears in the client's ``tools/list``.
  - The function's *docstring* becomes the tool's ``description``. It is the
    model's only documentation, so it is written for the model, not for us.
  - The *signature* becomes the ``inputSchema`` (JSON Schema): ``Literal`` turns
    into an enum, ``Field(ge=..., le=...)`` into minimum/maximum, and defaults
    make arguments optional. The SDK validates every call against it.
  - The *return type* becomes the ``outputSchema`` when it is structured
    (a TypedDict, model or dict), and the value is sent as structured content.

Ten tools, on purpose: every tool definition costs context in every client.
What is left out, and why, is in docs/OUT_OF_SCOPE.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict

from . import keystore, tvs
from .client import MEDIA_ACTIONS, Result, Status, TvClient
from .config import ALLOWED_KEYS, DEFAULT_APPS, LEFT_OUT_KEYS, KeyName, Settings, load_settings

log = logging.getLogger(__name__)


class Registry:
    """Every configured TV's client. Built once per server run, in the lifespan."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.clients = [TvClient(tv, settings) for tv in settings.tvs]

    def pick(self, ref: str | None) -> TvClient:
        return tvs.pick(self.clients, ref)

    def pick_many(self, ref: str | None) -> list[TvClient]:
        return tvs.pick_many(self.clients, ref)


_registry: Registry | None = None


def registry() -> Registry:
    assert _registry is not None, "server lifespan has not started"
    return _registry


# The lifespan runs once per server run: the code before `yield` at startup,
# the code after it at shutdown. That is where the long-lived TV connections
# are opened and closed, so tools never connect on their own.
@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    global _registry
    settings = load_settings()
    for problem in settings.problems:
        log.warning("Config: %s", problem)
    _registry = Registry(settings)
    for c in _registry.clients:
        await c.start()
    try:
        yield
    finally:
        for c in _registry.clients:
            await c.stop()
        _registry = None


# `instructions` is sent to the client in the initialize handshake; clients
# typically add it to the model's context as guidance for using these tools.
mcp = MCPServer(
    "webos",
    instructions=(
        "Controls LG webOS TVs. Call get_status first: it says whether each TV is on, what is in front, and "
        "(capabilities, limitations) what works on it. With several TVs, name one with `tv`; a call without it "
        "is refused rather than guessed. When a TV's sound goes to a receiver (volume_owner 'external'), volume "
        "belongs to the receiver's own server, not to this one. A TV that is off doesn't answer: power 'off' "
        "means off or off the network, and set_power on wakes it."
    ),
    lifespan=lifespan,
)

# Tool annotations are behavioral hints sent in tools/list. Clients use them to
# decide, e.g., whether to ask the user before a call. A tool without them is
# assumed by the spec to be destructive, non-idempotent and open-world, so
# every tool here states its hints explicitly:
#   read_only_hint   - changes nothing (status, sources)
#   destructive_hint - False: nothing here deletes data or is irreversible
#                      (turning a TV off is undone by turning it on)
#   idempotent_hint  - calling twice with the same args == calling once
#                      (true for power, source, volume; false for key presses
#                      and playback: two DOWNs move twice)
#   open_world_hint  - False: we only talk to TVs we were paired with
# They are hints, not enforcement: the allow-lists and caps bound the model.
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


# --- what an action returns ----------------------------------------------------------------
# Every tool that changes something returns an ActionResult, the same shape as
# the sibling servers': structured, so a client can read `outcome` and `sent`
# without parsing prose, with `detail` one sentence for the user. Failures
# aren't an outcome: they're isError results (WebOsError).
Outcome = Literal["done", "unchanged", "dry_run"]


class ActionResult(TypedDict):
    tv: str  # its configured name, or "all"
    outcome: Outcome
    detail: str
    sent: list[str]  # what went to the TV (or, in a dry run, would have)
    warnings: list[str]


def _result(c: TvClient, r: Result) -> ActionResult:
    detail, warnings = r.detail, list(r.warnings)
    if r.outcome == "dry_run":
        detail = f"DRY RUN, nothing sent: would {detail}"
        if not c.reachable and r.sent and not r.sent[0].startswith("wol "):
            warnings.append(f"{c.name} isn't reachable right now, so the real call would fail.")
    return {"tv": c.name, "outcome": r.outcome, "detail": detail, "sent": r.sent, "warnings": warnings}


TvArg = Annotated[
    str | None,
    Field(
        description="Which TV: its name (e.g. a room), an alias, or its address. Omit when there is only one; "
        "with several, a call without it is refused rather than guessed."
    ),
]


class StatusReport(TypedDict):
    tvs: list[Status]


class TvSummary(TypedDict):
    tv: str
    aliases: list[str]
    model: str | None
    platform: str | None
    paired: bool
    reachable: bool


class Source(TypedDict, total=False):
    kind: Literal["app", "input"]
    name: str  # what select_source accepts
    id: str
    title: str
    connected: bool | None


@mcp.tool(title="List TVs", annotations=_READ)
def list_tvs() -> list[TvSummary]:
    """List the configured TVs: the names (and aliases) the `tv` argument accepts, model, and webOS platform version."""
    return [
        {
            "tv": c.name,
            "aliases": list(c.tv.aliases),
            "model": c.tv.model,
            "platform": c.tv.platform,
            "paired": keystore.get(c.tv.uuid) is not None,
            "reachable": c.reachable,
        }
        for c in registry().clients
    ]


@mcp.tool(title="Get TV status", annotations=_READ)
async def get_status(tv: TvArg = None) -> StatusReport:
    """Start here. For one TV (or all, when tv is omitted): power, what is in front, volume, and what works.

    power: "on", "screen_off" (sound on, picture off), "standby", or "off" (not answering: off, or off the
    network). stale true: not reachable now, values are the last known (as_of says when; error says why).
    input is the HDMI input's label when one is in front. now_playing is the app and play state (webOS doesn't
    report titles). volume_owner "external": sound goes to a receiver, whose own server owns volume.
    capabilities and limitations say what works on this TV. needs_pairing true: run `mcp-server-webos pair`.
    """
    reg = registry()
    chosen = reg.clients if tv is None else [reg.pick(tv)]
    for c in chosen:
        await c.refresh()  # one bounded attempt on a stale TV, so the answer is fresh
    return {"tvs": [c.snapshot() for c in chosen]}


@mcp.tool(title="Turn a TV on or off", annotations=_ACT_IDEMPOTENT)
async def set_power(state: Literal["on", "off"], tv: TvArg = None) -> ActionResult:
    """Turn a TV on (Wake-on-LAN) or off (to standby). tv="all" turns every TV off (off only).

    On waits up to 45 s for the TV to answer; if it doesn't, warnings says so (Wake-on-LAN must be allowed in
    the TV's settings). Already in that state: outcome "unchanged", nothing sent.
    """
    reg = registry()
    if state == "on":
        c = reg.pick(tv)
        async with c.lock:
            return _result(c, await c.power_on())
    results = []
    for c in reg.pick_many(tv):
        async with c.lock:
            results.append((c, await c.power_off()))
    if len(results) == 1 and (tv or "").strip().lower() != tvs.ALL:
        return _result(*results[0])
    parts = [_result(c, r) for c, r in results]
    outcomes = {p["outcome"] for p in parts}
    outcome: Outcome = "dry_run" if "dry_run" in outcomes else "done" if "done" in outcomes else "unchanged"
    return {
        "tv": tvs.ALL,
        "outcome": outcome,
        "detail": "; ".join(p["detail"] for p in parts),
        "sent": [f"{p['tv']}: {s}" for p in parts for s in p["sent"]],
        "warnings": [f"{p['tv']}: {w}" for p in parts for w in p["warnings"]],
    }


@mcp.tool(title="Turn a TV's screen on or off", annotations=_ACT_IDEMPOTENT)
async def set_screen(state: Literal["on", "off"], tv: TvArg = None) -> ActionResult:
    """Blank the picture while sound keeps playing ('off'), or bring it back ('on'). The TV stays on.

    Useful for music through the TV. Not every TV has it (limitations in get_status says so).
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.set_screen(state == "on"))


@mcp.tool(title="List a TV's sources", annotations=_READ)
def list_sources(tv: TvArg = None) -> list[Source]:
    """List what select_source can switch a TV to: allow-listed apps it has installed, and its HDMI inputs.

    name is what to pass to select_source. Titles and labels come from the TV (labels are the user's names
    for each input, e.g. "Onkyo").
    """
    return [Source(**s) for s in registry().pick(tv).sources()]  # type: ignore[typeddict-item]


@mcp.tool(title="Switch a TV to an app or input", annotations=_ACT_IDEMPOTENT)
async def select_source(
    source: Annotated[str, Field(description="An app or input name from list_sources, e.g. 'netflix' or 'Onkyo'.")],
    tv: TvArg = None,
) -> ActionResult:
    """Open an app (e.g. 'netflix') or switch to an HDMI input (e.g. 'Onkyo', 'HDMI_2') on a TV.

    Succeeds only once that app or input is confirmed in front. A name that matches both an app and an input
    is refused, with both listed: ask which one. The TV must be on. An app that has just opened may miss the
    first key press while it loads: if send_key seems to do nothing right after, press it again.
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.select_source(source))


# Constraints written into the type become JSON Schema the model sees
# ("minimum": 1, "maximum": 10), and the SDK validates calls against it
# before our function runs, so there is no hand-written range check below.
Repeat = Annotated[int, Field(ge=1, le=10, description="How many times to press the key (1-10).")]


@mcp.tool(title="Press a remote key", annotations=_ACT)
async def send_key(key: KeyName, repeat: Repeat = 1, tv: TvArg = None) -> ActionResult:
    """Press a remote-control key on a TV, e.g. DOWN with repeat=3 to move down three rows, then ENTER.

    You can't see the screen: ask the user what's on it when you're unsure where you are.
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.send_key(key, repeat))


@mcp.tool(title="Control playback", annotations=_ACT)
async def media_control(
    action: Literal["play", "pause", "stop", "rewind", "fast_forward"], tv: TvArg = None
) -> ActionResult:
    """Play, pause, stop, rewind or fast-forward what's playing on a TV.

    Some apps ignore these; send_key PLAY or PAUSE is the fallback.
    """
    assert action in MEDIA_ACTIONS
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.media_control(action))


Level = Annotated[int, Field(ge=0, le=100, description="Volume 0-100 (capped per TV by max_volume).")]


@mcp.tool(title="Set a TV's volume", annotations=_ACT_IDEMPOTENT)
async def set_volume(level: Level, tv: TvArg = None) -> ActionResult:
    """Set the volume of a TV that plays sound through its own speakers.

    Refused when the TV's sound goes to a receiver (volume_owner "external" in get_status): use the receiver's
    server then. Levels above the TV's max_volume are capped, with a warning.
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.set_volume(level))


@mcp.tool(title="Mute or unmute a TV", annotations=_ACT_IDEMPOTENT)
async def set_mute(muted: bool, tv: TvArg = None) -> ActionResult:
    """Mute (true) or unmute (false) a TV's own speakers. Refused when its sound goes to a receiver."""
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.set_mute(muted))


# ---------------------------------------------------------------------------
# Resources: context a *client* reads (e.g. @-mentioned in Claude Code), as
# opposed to tools the model calls. Reference data that costs nothing to
# read: no command goes to a TV.
#
#   webos://tvs    the configured TVs, their capabilities and limitations
#   webos://keys   the keys send_key accepts, and the ones deliberately left out
#   webos://apps   the app allow-list, and where each entry comes from
# ---------------------------------------------------------------------------
@mcp.resource("webos://tvs", name="tvs", title="Configured TVs and what works on each", mime_type="application/json")
def tvs_resource() -> str:
    out = {}
    for c in registry().clients:
        caps, limits = c.capabilities()
        out[c.name] = {
            "aliases": list(c.tv.aliases),
            "model": c.tv.model,
            "platform": c.tv.platform,
            "max_volume": c.tv.max_volume,
            "volume_owner": c.tv.volume_owner,
            "capabilities": caps,
            "limitations": limits,
        }
    return json.dumps(out, indent=1)


@mcp.resource("webos://keys", name="keys", title="Keys send_key can press", mime_type="application/json")
def keys_resource() -> str:
    return json.dumps({"allowed": sorted(ALLOWED_KEYS), "left_out": LEFT_OUT_KEYS}, indent=1)


@mcp.resource("webos://apps", name="apps", title="Apps select_source may open", mime_type="application/json")
def apps_resource() -> str:
    settings = registry().settings
    return json.dumps(
        {
            name: {
                "id": app.id,
                "title": app.title,
                # Defaults are community-sourced ids (ASSUMPTION W-APP-IDS); config.json entries are yours
                "source": "default" if DEFAULT_APPS.get(name) == app else "config.json",
            }
            for name, app in sorted(settings.apps.items())
        },
        indent=1,
    )


# ---------------------------------------------------------------------------
# Prompts: workflows the *user* picks (e.g. /mcp__webos__watch in Claude
# Code). Only this server's tools: cross-device workflows (movie night, the
# receiver's volume) belong in the home-theater Skill, not here.
# ---------------------------------------------------------------------------
@mcp.prompt(title="Watch something on a TV")
def watch(app: str, tv: str = "") -> str:
    """Turn a TV on and open an app on it."""
    which = f" on the {tv} TV" if tv else ""
    arg = f", tv={tv!r}" if tv else ""
    return (
        f"Open {app}{which}. Please:\n"
        f"1. Call get_status{f' with tv={tv!r}' if tv else ''}. If it asks which TV, ask me.\n"
        f'2. If power is "off" or "standby", call set_power with state="on"{arg}.\n'
        f"3. Call select_source with source={app!r}{arg}. If it says the name is unknown or matches several, "
        "call list_sources and ask me which one I meant.\n"
        "4. Tell me it's open. Don't change the volume: if volume_owner is 'external', the receiver owns it."
    )
