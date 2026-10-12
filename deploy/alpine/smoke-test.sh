#!/bin/sh
# CI: install and run the service in a stock python:3.12-alpine container,
# against a simulated TV. Proves the dependencies install on musl
# (aiohttp, cryptography, pydantic-core), the install layout and
# permissions, and that OpenRC starts and stops it.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

# On failure, say where as a GitHub annotation (the job log isn't always at
# hand; annotations are), with the end of the service log if there is one.
report() {
	status=$?
	if [ "$status" != 0 ]; then
		tail_log=$(tail -n 30 "${STEP_LOG:-/var/log/mcp-server-webos/server.log}" 2>/dev/null | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
		echo "::error title=Alpine smoke test failed (exit $status)::last step: ${STEP:-?}%0A$tail_log"
	fi
}
trap report EXIT

STEP='install'
apk add --no-cache openrc curl >/dev/null
sh deploy/alpine/install.sh /src

STEP='pytest on musl'
# The full test suite on musl, from the installed venv
/opt/mcp-server-webos/venv/bin/pip install --quiet "/src[dev]"
STEP_LOG=/tmp/pytest.log
cd /src
/opt/mcp-server-webos/venv/bin/pytest -q -p no:cacheprovider -rf >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
tail -n 3 "$STEP_LOG"
cd /
STEP_LOG=''

STATE=/var/lib/mcp-server-webos
BIN=/opt/mcp-server-webos/venv/bin/mcp-server-webos
STEP='simulator'
# A simulated G1, already paired, writing its key as the service user
su -s /bin/sh mcp-webos -c "$BIN simulate --config-dir $STATE --paired --profile g1 --host 127.0.0.3 --name 'Family Room'" &
for _ in $(seq 100); do grep -q '"macs"' "$STATE/config.json" 2>/dev/null && break; sleep 0.1; done

STEP='service start'
# OpenRC in a container: it needs to believe it has booted, and that the
# network (which the service `need`s) is provided by the host
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
rc-service mcp-server-webos start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8715/healthz && break; sleep 0.2; done
rc-service mcp-server-webos status

STEP='service user'
# The process runs as the service user, not root. (busybox ps truncates user
# names to 8 characters, so ask /proc for the owner of the server's process.)
# shellcheck disable=SC2009
pid=$(ps -o pid,args | grep '[m]cp-server-webos serve' | awk 'NR==1 {print $1}')
[ -n "$pid" ] && [ "$(stat -c %U "/proc/$pid")" = mcp-webos ]
STEP='file modes'
# The client key and config are the service user's alone
[ "$(stat -c %a $STATE)" = 700 ]
[ "$(stat -c '%a %U' $STATE/keys.json)" = "600 mcp-webos" ]
[ "$(stat -c '%a %U' $STATE/config.json)" = "600 mcp-webos" ]
STEP='doctor'
# doctor as the service user, against the simulator
su -s /bin/sh mcp-webos -c "WEBOS_CONFIG_DIR=$STATE $BIN doctor --no-ssdp --timeout 2"

STEP='service stop'
rc-service mcp-server-webos stop
cat /var/log/mcp-server-webos/server.log
echo "Alpine smoke test passed"
