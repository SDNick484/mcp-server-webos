#!/bin/sh
# What the OpenRC service runs (as user mcp-webos). Loads the environment
# file, then replaces itself with the server, so supervise-daemon watches the
# server process itself. Run it by hand to see exactly what the service does:
#   su -s /bin/sh mcp-webos -c /opt/mcp-server-webos/run.sh
set -eu
ENV_FILE="${MCP_WEBOS_ENV:-/etc/mcp-server-webos/env}"
if [ -r "$ENV_FILE" ]; then
	set -a  # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
fi
export WEBOS_CONFIG_DIR="${WEBOS_CONFIG_DIR:-/var/lib/mcp-server-webos}"
# MCP_SERVE_ARGS is split into words on purpose (e.g. "--dry-run --no-redact")
# shellcheck disable=SC2086
exec /opt/mcp-server-webos/venv/bin/mcp-server-webos serve --http ${MCP_SERVE_ARGS:-}
