#!/bin/sh
# Run the test suite in a network namespace of its own, for WSL2 with mirrored networking.
#
#   scripts/test-wsl.sh [pytest args]        e.g. scripts/test-wsl.sh -q tests/test_wire.py
#
# Why: the simulated TVs listen on 127.0.0.2-5, on the real ports 3000/3001 (aiowebostv always
# dials those, so each TV needs its own address). With networkingMode=mirrored in .wslconfig,
# WSL2 hands loopback to Windows, which answers only 127.0.0.1: connections to 127.0.0.2-5 hang
# and nearly every simulator test times out. A new network namespace has an ordinary Linux
# loopback, where all of 127.0.0.0/8 works. It has no other network, which the tests don't need
# (run pip and git outside it).
#
# Needs root for unshare (sudo is used when you aren't root); the tests run as the owner of this
# checkout, so nothing it writes ends up owned by root.
set -eu
cd "$(dirname "$0")/.."
[ -x .venv/bin/pytest ] || {
	echo "no .venv here: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
	exit 1
}
command -v ip >/dev/null || { echo "needs ip(8), from iproute2" >&2; exit 1; }
owner=$(stat -c %U .)
sudo=""
[ "$(id -u)" = 0 ] || sudo=sudo
# Arguments are joined with spaces: fine for test paths and pytest flags, not for arguments that
# contain spaces themselves.
exec $sudo unshare --net sh -c 'ip link set lo up && exec su "$1" -s /bin/sh -c "cd \"$2\" && exec .venv/bin/pytest $3"' \
	sh "$owner" "$PWD" "$*"
