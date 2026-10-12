"""End to end: launch the real entry point as a subprocess and talk MCP over stdio.

The in-process tests hand the MCPServer object straight to the client. This
one covers what they skip: the installed console script, `serve` being the
default subcommand, real settings loading, and the stdio transport across a
process boundary. No TVs are configured, so no network is needed.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from mcp import Client, StdioServerParameters

from .test_tools import EXPECTED

pytestmark = pytest.mark.anyio


async def test_serve_over_stdio(tmp_path):
    exe = shutil.which("mcp-server-webos", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {**os.environ, "WEBOS_CONFIG_DIR": str(tmp_path)}
    params = StdioServerParameters(command=exe, args=[], env=env)
    async with Client(params) as c:
        assert {t.name for t in (await c.list_tools()).tools} == EXPECTED
        assert (await c.call_tool("get_status", {})).structured_content == {"tvs": []}
