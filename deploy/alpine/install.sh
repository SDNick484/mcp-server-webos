#!/bin/sh
# Install mcp-server-webos as an OpenRC service on Alpine (e.g. a Proxmox LXC).
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# Layout (each part owned so the service can read its settings, keep its
# credentials to itself, and change nothing else):
#   /opt/mcp-server-webos/venv     the code                     root:root            0755
#   /opt/mcp-server-webos/run.sh   the service command          root:root            0755
#   /opt/mcp-server-webos/COMMIT   the installed commit, if any root:root            0644
#   /etc/mcp-server-webos/env      HTTP and Access settings     root:mcp-webos    0640
#   /var/lib/mcp-server-webos/     keys.json (client keys),     mcp-webos         0700 / files 0600
#                                     config.json (WEBOS_CONFIG_DIR)
#   /var/log/mcp-server-webos/     server.log                   mcp-webos         0750
#   /etc/init.d/mcp-server-webos   the OpenRC script
# The credentials live in /var/lib, not /etc, because they are the service's
# own: `pair` (run as the service user) writes them, and the server rewrites
# config.json when a TV moves to a new address. The LXC must be bridged onto
# the TVs' network for Wake-on-LAN broadcasts to reach them.
# Re-running upgrades the code and keeps settings and pairing.
# COMMIT (the SHA installed, and this repo) is what /healthz and /webos/mcp/healthz report; the
# default SOURCE is pinned to main's current commit first, so the stamp names what pip installed.
set -eu

SRC="${1:-git+https://github.com/SDNick484/mcp-server-webos}"
USER_NAME=mcp-webos
PREFIX=/opt/mcp-server-webos
ETC=/etc/mcp-server-webos
STATE=/var/lib/mcp-server-webos
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# python3 and its venv module; every compiled dependency ships a musl wheel,
# so no compiler is needed (see README: Alpine).
apk add --no-cache python3 py3-pip openrc git >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h "$STATE" -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

# Which commit SOURCE is, worked out before pip (commit.sh, shared with the sibling servers)
. "$HERE/commit.sh"
REPO=SDNick484/mcp-server-webos
pinned=$(commit_source "$SRC" "$REPO")
SRC=$(printf '%s\n' "$pinned" | sed -n 1p)
COMMIT=$(printf '%s\n' "$pinned" | sed -n 2p)

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
# Every commit carries the same version number, so pip calls an installed copy "already satisfied"
# and keeps it: re-running this script would upgrade nothing. Reinstall the package itself (not its
# dependencies) so it always gets the code from SOURCE.
"$PREFIX/venv/bin/pip" install --quiet --force-reinstall --no-deps "$SRC"
# Only now that pip succeeded (set -e stops before this otherwise), so COMMIT never names code that isn't installed
write_commit "$PREFIX" "$COMMIT" "$REPO"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/env" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/env.example" "$ETC/env"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/log/mcp-server-webos
install -m 0755 "$HERE/mcp-server-webos.initd" /etc/init.d/mcp-server-webos

AS_SERVICE="su -s /bin/sh $USER_NAME -c"
CMD="WEBOS_CONFIG_DIR=$STATE $PREFIX/venv/bin/mcp-server-webos"
echo "Installed $("$PREFIX/venv/bin/mcp-server-webos" --version)."
echo "Next: pair each TV as the service user (accept the request on the TV), check, then start:"
echo "  $AS_SERVICE '$CMD pair --host <tv ip> --name \"Family Room\"'"
echo "  $AS_SERVICE '$CMD doctor'"
echo "  rc-update add mcp-server-webos default && rc-service mcp-server-webos start"
