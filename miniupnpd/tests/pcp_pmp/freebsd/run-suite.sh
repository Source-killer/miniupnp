#!/bin/sh
# Runs inside the FreeBSD VM as root, from the daemon tree, after setup.sh.
#
#   run-suite.sh RESULTS_DIR
#
# Runs the whole YAML suite against a long-running miniupnpd.  The client must
# be a different host: the daemon works out which LAN a request came from by
# routing to the sender, and for the router's own address that route is lo0, so
# a runner on the router itself is ignored over IPv6 ("not from a LAN").
# epair0b therefore moves into a vnet jail that shares the root filesystem (and
# so the runner and Python), which plays the Docker harness's client container.
set -eu

[ "$#" -eq 1 ] || { echo "usage: $0 RESULTS_DIR" >&2; exit 2; }
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
RESULTS=$(CDPATH='' cd -- "$1" && pwd)
WORK=/tmp/pcp-fixture
JAIL=pcpclient

daemon=
cleanup() {
	[ -n "$daemon" ] && kill "$daemon" 2>/dev/null || true
	jail -r "$JAIL" >/dev/null 2>&1 || true
	ifconfig epair0b up >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

miniupnpd/miniupnpd -d -f "$WORK/miniupnpd.conf" >"$RESULTS/suite-miniupnpd.log" 2>&1 &
daemon=$!

jail -c name="$JAIL" path=/ host.hostname="$JAIL" persist vnet vnet.interface=epair0b
jexec "$JAIL" ifconfig lo0 127.0.0.1/8 up
jexec "$JAIL" ifconfig epair0b up
jexec "$JAIL" ifconfig epair0b inet 192.168.1.2/24
jexec "$JAIL" ifconfig epair0b inet6 fd42:1::2/64
sleep 3 # let IPv6 DAD finish

runner() { jexec "$JAIL" python3 "$HERE/run_pcp_natpmp_cases.py" --port 5351 "$@"; }

ready=0
attempt=0
while [ "$attempt" -lt 20 ]; do
	if runner --host 192.168.1.1 --protocol natpmp --case public-address \
	    --status-file "$WORK/readiness.md" >/dev/null 2>&1; then
		ready=1
		break
	fi
	kill -0 "$daemon" 2>/dev/null || break
	attempt=$((attempt + 1))
	sleep 1
done
if [ "$ready" -ne 1 ]; then
	echo "miniupnpd did not answer on 192.168.1.1:5351" >&2
	cat "$RESULTS/suite-miniupnpd.log" >&2
	exit 1
fi

status=0
runner --host 192.168.1.1 --family ipv4 --mutating --platform freebsd --unsupported pcp-peer \
	--status-file "$RESULTS/status.md" || status=1
runner --host fd42:1::1 --family ipv6 --protocol pcp --allow-empty-selection --platform freebsd \
	--unsupported pcp-peer \
	--status-file "$RESULTS/status.md" || status=1
exit "$status"
