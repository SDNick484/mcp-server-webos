"""MCP tool definitions. Deliberately small and typed: allow-listed keys and apps, no URLs, bounded text.

How a decorated function becomes a tool the model can call:
  - ``@mcp.tool()`` registers it; it then appears in the client's ``tools/list``.
  - The function's *docstring* becomes the tool's ``description``. It is the
    model's only documentation, so it is written for the model, not for us.
  - The *signature* becomes the ``inputSchema`` (JSON Schema): ``Literal`` turns
    into an enum, ``Field(ge=..., le=...)`` into minimum/maximum, and defaults
    make arguments optional. The SDK validates every call against it.
  - The *return type* becomes the ``outputSchema`` when it is structured
    (a TypedDict, model or dict), and the value is sent as structured content.

Fourteen tools, on purpose: every tool definition costs context in every client.
What is left out, and why, is in docs/OUT_OF_SCOPE.md.

Two kinds of free text are allowed, deliberately and nowhere else: what to type
into a focused *system* text field (webos_insert_text), and a search query for
an allow-listed app whose search link the server builds itself (webos_search_app).
Both are one line of printable characters with a length cap; neither is a URL.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal, NotRequired

from mcp.server import MCPServer
from pydantic import Field
from typing_extensions import TypedDict

from . import __version__, keystore, tvs
from .client import MAX_DELETE, MAX_QUERY, MAX_TEXT, MEDIA_ACTIONS, SEARCH_LINKS, Result, Status, TvClient
from .config import ALLOWED_KEYS, DEFAULT_APPS, LEFT_OUT_KEYS, KeyName, Settings, load_settings
from .tools import ACT, ACT_IDEMPOTENT, READ_ONLY

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
    version=__version__,  # serverInfo.version in initialize, so a bug report says which build answered
    instructions=(
        "Physical role: Display & Video Sink. Controls LG webOS TVs. Connects downstream of the AVR (Onkyo) "
        "or switches video inputs directly; delegates master volume control to the AVR when volume_owner "
        "is 'external'. Call webos_get_status first: it says whether each TV is on, what is in front, and "
        "(capabilities, limitations) what works on it. With several TVs, name one with `tv`; a call without it "
        "is refused rather than guessed. When a TV's sound goes to a receiver (volume_owner 'external'), volume "
        "belongs to the receiver's own server (onkyo_set_volume), not to this one. A TV that is off doesn't answer: "
        "power 'off' means off or off the network, and webos_set_power on wakes it. To type, prefer "
        "webos_insert_text over walking an on-screen keyboard with webos_send_key, where get_status lists "
        "text_input; it only reaches system text fields (LG's search), so for YouTube use webos_search_app."
    ),
    lifespan=lifespan,
)

# Tool annotations are behavioral hints sent in tools/list. Clients use them to
# decide, e.g., whether to ask the user before a call. A tool without them is
# assumed by the spec to be destructive, non-idempotent and open-world.


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
    hint: NotRequired[str]  # only with no TV configured: how to pair one


class TvSummary(TypedDict):
    tv: str
    aliases: list[str]
    model: str | None
    platform: str | None
    paired: bool
    reachable: bool


class Source(TypedDict, total=False):
    kind: Literal["app", "input"]
    name: str  # what webos_select_source accepts
    id: str
    title: str
    connected: bool | None


@mcp.tool(title="List TVs", annotations=READ_ONLY)
def webos_list_tvs() -> list[TvSummary]:
    """List the configured TVs.

    <use_case>
    Enumerate all configured TV targets, discovering accepted identifiers for the `tv` parameter,
    hardware models, and webOS firmware versions.
    </use_case>

    <important_notes>
    - Returns friendly names and aliases usable in the `tv` argument across all tools.
    - Read-only; queries local configuration and current connection states without sending network commands.
    </important_notes>
    """
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


@mcp.tool(title="Get TV status", annotations=READ_ONLY)
async def webos_get_status(tv: TvArg = None) -> StatusReport:
    """Query current TV power state, foreground app or input, and audio configuration.

    <use_case>
    Inspect TV state before issuing commands. Determine whether the TV is on, which source is active,
    and whether audio is handled locally or routed to an AVR.
    </use_case>

    <important_notes>
    - Start here before issuing media or input commands.
    - When `volume_owner` is "external", volume control belongs to the AVR (`onkyo_set_volume`), not this TV.
    - Stale status indicates the TV is in standby or powered off; wake with `webos_set_power(state="on")`.
    </important_notes>
    """
    reg = registry()
    chosen = reg.clients if tv is None else [reg.pick(tv)]
    for c in chosen:
        await c.refresh()  # one bounded attempt on a stale TV, so the answer is fresh
    report: StatusReport = {"tvs": [c.snapshot() for c in chosen]}
    if not reg.clients:
        # A first run: an empty list alone reads as "you have no TVs", so say how to pair one.
        report["hint"] = tvs.NO_TVS
    return report


@mcp.tool(title="Turn a TV on or off", annotations=ACT_IDEMPOTENT)
async def webos_set_power(state: Literal["on", "off"], tv: TvArg = None) -> ActionResult:
    """Turn a TV on via Wake-on-LAN or off to standby.

    <use_case>
    Power on a TV for playback or turn it off to standby when finished.
    </use_case>

    <important_notes>
    - Powering on transmits Wake-on-LAN magic packets and awaits network readiness (up to 45s).
    - Rate limited to protect internal power relays from rapid power cycles.
    - To power off all configured TVs simultaneously, pass `tv="all"`.
    </important_notes>
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


@mcp.tool(title="Turn a TV's screen on or off", annotations=ACT_IDEMPOTENT)
async def webos_set_screen(state: Literal["on", "off"], tv: TvArg = None) -> ActionResult:
    """Blank or restore the TV display panel without interrupting audio.

    <use_case>
    Turn off the OLED/LED panel during audio-only streaming or background music to save power and prevent panel burn-in.
    </use_case>

    <important_notes>
    - Audio and running applications continue uninterrupted.
    - Rate limited alongside power state modifications.
    - Check TV capabilities in `webos_get_status`; older webOS revisions may lack screen-off support.
    </important_notes>
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.set_screen(state == "on"))


@mcp.tool(title="List a TV's sources", annotations=READ_ONLY)
def webos_list_sources(tv: TvArg = None) -> list[Source]:
    """List available apps and physical HDMI inputs on a TV.

    <use_case>
    Discover valid source names and inputs to supply to `webos_select_source`.
    </use_case>

    <important_notes>
    - Returns allow-listed installed apps and connected HDMI inputs.
    - Use the returned `name` values as inputs for `webos_select_source`.
    </important_notes>
    """
    return [Source(**s) for s in registry().pick(tv).sources()]  # type: ignore[typeddict-item]


@mcp.tool(title="Switch a TV to an app or input", annotations=ACT_IDEMPOTENT)
async def webos_select_source(
    source: Annotated[
        str,
        Field(description="An app or input name from webos_list_sources, e.g. 'netflix' or 'Onkyo'."),
    ],
    tv: TvArg = None,
) -> ActionResult:
    """Launch an application or switch to a physical HDMI input.

    <use_case>
    Switch video display to a streaming service (e.g. Netflix, YouTube) or an
    external HDMI input (e.g. AVR / game console).
    </use_case>

    <important_notes>
    - TV must be powered on.
    - Verifies the selected source is foregrounded before returning.
    - If switching to an external device connected through an AVR, ensure the
      AVR input is also switched (`onkyo_set_input`).
    </important_notes>
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.select_source(source))


# Constraints written into the type become JSON Schema the model sees
# ("minimum": 1, "maximum": 10), and the SDK validates calls against it
# before our function runs, so there is no hand-written range check below.
Repeat = Annotated[int, Field(ge=1, le=10, description="How many times to press the key (1-10).")]


@mcp.tool(title="Press a remote key", annotations=ACT)
async def webos_send_key(key: KeyName, repeat: Repeat = 1, tv: TvArg = None) -> ActionResult:
    """Send remote control key presses to the TV.

    <use_case>
    Navigate TV menus and on-screen dialogs (arrows, enter, back, home).
    </use_case>

    <important_notes>
    - Rate limited via token bucket (capacity 10, rate 3.0/s) to prevent runaway key flooding.
    - For media transport (play, pause, stop), prefer `webos_media_control`.
    - To type, prefer `webos_insert_text` (where get_status lists text_input) or, for YouTube,
      `webos_search_app`. Walk an app's own on-screen keyboard with arrows and ENTER only when they refuse.
    </important_notes>
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.send_key(key, repeat))


@mcp.tool(title="Control playback", annotations=ACT)
async def webos_media_control(
    action: Literal["play", "pause", "stop", "rewind", "fast_forward"], tv: TvArg = None
) -> ActionResult:
    """Send media playback commands (play, pause, stop, fast forward, rewind).

    <use_case>
    Control playback state of currently active media app on the TV.
    </use_case>

    <important_notes>
    - Direct media endpoints may be ignored by certain third-party apps; use `webos_send_key`
      with "PLAY" / "PAUSE" as fallback.
    - Operates on foreground media applications.
    </important_notes>
    """
    assert action in MEDIA_ACTIONS
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.media_control(action))


# --- text input -------------------------------------------------------------------------
# min_length/max_length become minLength/maxLength in the schema, so the SDK rejects an empty or
# oversized string before our code runs; client.checked_text also refuses control characters.
Text = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_TEXT,
        description=f"What to type: one line, up to {MAX_TEXT} characters (no newlines; submit with "
        "webos_submit_text).",
    ),
]
Replace = Annotated[bool, Field(description="True: replace everything in the field. False: add at the cursor.")]
Count = Annotated[int, Field(ge=1, le=MAX_DELETE, description=f"How many characters to delete (1-{MAX_DELETE}).")]


@mcp.tool(title="Type into a text field", annotations=ACT)
async def webos_insert_text(text: Text, replace: Replace = False, tv: TvArg = None) -> ActionResult:
    """Type text into the TV's focused text field, instead of walking an on-screen keyboard with keys.

    <use_case>
    Fill a search box or other text field on the TV in one call: e.g. open LG's search and type
    "lofi beats", then webos_submit_text.
    </use_case>

    <important_notes>
    - Only works where get_status lists `text_input`, and only in *system* text fields (LG's own
      search, and fields that use the TV's keyboard). The field must already have focus.
    - Refused, with nothing sent, when no system field has focus. YouTube and Netflix draw their
      own keyboards and never have one: use `webos_search_app` for YouTube, or `webos_send_key`
      (arrows, ENTER) on the app's keyboard.
    - Confirmed by the field's reported length where the TV reports it; a warning says when not.
    </important_notes>
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.insert_text(text, replace))


@mcp.tool(title="Delete text in a text field", annotations=ACT)
async def webos_delete_text(count: Count = 1, tv: TvArg = None) -> ActionResult:
    """Delete characters before the cursor in the TV's focused text field (like Backspace).

    <use_case>
    Correct a typo after webos_insert_text. To start over, webos_insert_text with replace=true is simpler.
    </use_case>

    <important_notes>
    - Same rules as `webos_insert_text`: a focused system text field only; refused otherwise.
    </important_notes>
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.delete_text(count))


@mcp.tool(title="Submit a text field", annotations=ACT)
async def webos_submit_text(tv: TvArg = None) -> ActionResult:
    """Press the keyboard's Enter in the TV's focused text field, e.g. to run a search.

    <use_case>
    Run the search after webos_insert_text.
    </use_case>

    <important_notes>
    - Same rules as `webos_insert_text`: a focused system text field only; refused otherwise.
    - Confirmed when the field gives up focus, as it does once the search runs.
    </important_notes>
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.submit_text())


# Spelled out so the schema has an enum; tests check it matches client.SEARCH_LINKS.
SearchApp = Annotated[Literal["youtube"], Field(description="Which app to search. Only YouTube has a search link.")]
Query = Annotated[
    str, Field(min_length=1, max_length=MAX_QUERY, description=f"What to search for, up to {MAX_QUERY} characters.")
]


@mcp.tool(title="Search inside an app", annotations=ACT_IDEMPOTENT)
async def webos_search_app(app: SearchApp, query: Query, tv: TvArg = None) -> ActionResult:
    """Open an app straight to its search results for a query (YouTube).

    <use_case>
    "Search YouTube for lofi beats": opens YouTube on its results, with no typing. YouTube's own
    keyboard doesn't take webos_insert_text, so this is the way to search it.
    </use_case>

    <important_notes>
    - Only works where get_status lists `app_search`: the app must be allow-listed and installed,
      and older TVs (webOS 3.x) ignore the search and just open the app.
    - The server builds the link from the query; no URL is accepted.
    - Confirms the app is in front. The TV can't report whether the app ran the search.
    </important_notes>
    """
    assert app in SEARCH_LINKS
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.search_app(app, query))


Level = Annotated[int, Field(ge=0, le=100, description="Volume 0-100 (capped per TV by max_volume).")]


@mcp.tool(title="Set a TV's volume", annotations=ACT_IDEMPOTENT)
async def webos_set_volume(level: Level, tv: TvArg = None) -> ActionResult:
    """Set the audio volume level on the TV.

    <use_case>
    Adjust listening volume when using internal TV speakers.
    </use_case>

    <important_notes>
    - When `volume_owner == 'external'`, calls are refused: audio is routed to an
      AVR/soundbar and `onkyo_set_volume` must be used instead.
    - Values above the TV's configured `max_volume` are safely clamped.
    </important_notes>
    """
    c = registry().pick(tv)
    async with c.lock:
        return _result(c, await c.set_volume(level))


@mcp.tool(title="Mute or unmute a TV", annotations=ACT_IDEMPOTENT)
async def webos_set_mute(muted: bool, tv: TvArg = None) -> ActionResult:
    """Mute or unmute the TV audio output.

    <use_case>
    Silence or restore TV audio output.
    </use_case>

    <important_notes>
    - When `volume_owner == 'external'`, calls are refused: use `onkyo_set_mute` on the audio receiver.
    </important_notes>
    """
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
def webos_watch(app: str, tv: str = "") -> str:
    """Turn a TV on and open an app on it."""
    which = f" on the {tv} TV" if tv else ""
    arg = f", tv={tv!r}" if tv else ""
    return (
        f"Open {app}{which}. Please:\n"
        f"1. Call webos_get_status{f' with tv={tv!r}' if tv else ''}. If it asks which TV, ask me.\n"
        f'2. If power is "off" or "standby", call webos_set_power with state="on"{arg}.\n'
        f"3. Call webos_select_source with source={app!r}{arg}. If it says the name is unknown or matches several, "
        "call webos_list_sources and ask me which one I meant.\n"
        "4. Tell me it's open. Don't change the volume: if volume_owner is 'external', the receiver owns it."
    )
