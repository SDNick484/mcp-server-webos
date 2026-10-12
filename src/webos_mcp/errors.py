"""Errors a tool call can end with.

Subclassing the SDK's ToolError matters: only ToolError messages reach the
model. Any other exception is reported as just "Error executing tool <name>",
which would hide advice like "run pair first" or "which TV?".
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class WebOsError(ToolError):
    """A problem the model (and user) can act on."""


class Unreachable(WebOsError):
    """The TV isn't answering: off, asleep, or off the network. A state of
    the TV, not a mistake in the call."""
