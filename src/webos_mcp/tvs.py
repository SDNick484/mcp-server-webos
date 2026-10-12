"""Which TV a tool call means.

**Several TVs: never guess.** Every tool takes an optional ``tv`` (a
configured name, an alias, or an address). Without one, an action is only
taken when exactly one TV is configured; otherwise the answer is an error
naming the TVs, so the model can ask. Guessing would mean turning off the
wrong room. Read-only tools that can cover every TV at once (get_status) do
that instead of asking, as mcp-server-onkyo's get_status does.

``tv="all"`` is accepted only where a tool says so (set_power off), the way
mcp-server-harmony accepts ``hub="all"``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from .config import TvSettings
from .errors import WebOsError


class HasSettings(Protocol):
    @property
    def tv(self) -> TvSettings: ...


T = TypeVar("T", bound=HasSettings)

ALL = "all"


def names(items: Sequence[HasSettings]) -> str:
    return ", ".join(i.tv.describe() for i in items)


def pick(items: Sequence[T], ref: str | None) -> T:
    """The one TV ref means, or a WebOsError saying why there isn't one."""
    if not items:
        raise WebOsError("No TVs are configured. Pair one with `mcp-server-webos pair --host <ip> --name <room>`.")
    if ref is None or not ref.strip():
        if len(items) == 1:
            return items[0]
        raise WebOsError(f"Which TV? Several are configured: {names(items)}. Pass tv=<name>.")
    if ref.strip().lower() == ALL:
        raise WebOsError("tv='all' only works for set_power with state='off'. Name one TV.")
    matches = [i for i in items if i.tv.answers_to(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise WebOsError(f"No TV called {ref!r}. Configured: {names(items)}.")
    raise WebOsError(f"{ref!r} matches several TVs: {names(matches)}. Use the full name.")


def pick_many(items: Sequence[T], ref: str | None) -> list[T]:
    """Like pick, but ref may be "all" (every TV, in config order)."""
    if ref is not None and ref.strip().lower() == ALL:
        if not items:
            raise WebOsError("No TVs are configured.")
        return list(items)
    return [pick(items, ref)]
