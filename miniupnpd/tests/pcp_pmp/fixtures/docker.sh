#!/bin/sh
# Linux (Docker) fixture for the runner's daemon-restart cases.
#
#   docker.sh daemon-restart DIRECTORY FAMILY
#
# Starts a fresh router container (WAN default route, as on a real gateway),
# lets its entrypoint record outgoing PCP multicast on the LAN and WAN
# interfaces from before miniupnpd starts, and copies lan-0.pcap and
# wan-0.pcap into DIRECTORY.  The image must already exist (see
# testpcp_natpmp_docker.sh).
set -eu

if [ "$#" -ne 3 ] || [ "$1" != daemon-restart ]; then
	echo "usage: $0 daemon-restart DIRECTORY FAMILY" >&2
	exit 2
fi
DIRECTORY=$2
DOCKER_BIN=${DOCKER:-docker}
IMAGE=${PCP_TEST_IMAGE:-miniupnpd-pcp-test:local}
CONTAINER=${PCP_TEST_CONTAINER:-miniupnpd-pcp-restart-$$}
NETWORK=miniupnpd-pcp-restart-net-$$
WAN_NETWORK=$NETWORK-wan

cleanup() {
	status=$?
	trap - EXIT HUP INT TERM
	if [ "$status" -ne 0 ]; then
		"$DOCKER_BIN" logs "$CONTAINER" >&2 2>/dev/null || true
	fi
	"$DOCKER_BIN" rm -f "$CONTAINER" >/dev/null 2>&1 || true
	"$DOCKER_BIN" network rm "$WAN_NETWORK" >/dev/null 2>&1 || true
	"$DOCKER_BIN" network rm "$NETWORK" >/dev/null 2>&1 || true
	exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$DOCKER_BIN" network create --driver bridge --ipv6 \
	--opt com.docker.network.bridge.gateway_mode_ipv6=routed \
	--subnet fd42:5351:5351::/64 "$NETWORK" >/dev/null
"$DOCKER_BIN" network create --driver bridge --ipv6 \
	--opt com.docker.network.bridge.gateway_mode_ipv6=routed \
	--subnet 198.51.100.0/24 --gateway 198.51.100.254 \
	--subnet fd42:5351:5352::/64 --gateway fd42:5351:5352::ff "$WAN_NETWORK" >/dev/null
"$DOCKER_BIN" create --name "$CONTAINER" --network "$NETWORK" \
	--network-alias pcp-router --cap-add NET_ADMIN --cap-add NET_RAW \
	--env PCP_TEST_REAL_WAN=1 --env PCP_TEST_WAN_DEFAULT_ROUTE=1 \
	--env PCP_TEST_IPV6_LISTENING_IP=1 --env PCP_TEST_CAPTURE_DIR=/capture \
	--sysctl net.ipv4.ip_forward=1 --sysctl net.ipv6.conf.all.forwarding=1 \
	"$IMAGE" >/dev/null
"$DOCKER_BIN" network connect --ip 198.51.100.1 --ip6 fd42:5351:5352::1 \
	"$WAN_NETWORK" "$CONTAINER"
"$DOCKER_BIN" start "$CONTAINER" >/dev/null

# Wait for the daemon to answer, then give the startup announce time to land.
ready=0
attempt=0
while [ "$attempt" -lt 20 ]; do
	if "$DOCKER_BIN" run --rm --network "$NETWORK" \
		--entrypoint python3 "$IMAGE" \
		/src/miniupnpd/tests/pcp_pmp/testpcp_natpmp.py \
		--host pcp-router --port 5351 \
		--protocol natpmp --case public-address >/dev/null 2>&1; then
		ready=1
		break
	fi
	attempt=$((attempt + 1))
	sleep 1
done
if [ "$ready" -ne 1 ]; then
	echo "Docker PCP server did not become ready on UDP port 5351." >&2
	exit 1
fi
sleep 2

# Flush the captures (tcpdump writes the pcap trailer on SIGINT), then copy out.
"$DOCKER_BIN" exec "$CONTAINER" pkill -INT tcpdump || true
sleep 1
mkdir -p "$DIRECTORY"
"$DOCKER_BIN" cp "$CONTAINER:/capture/." "$DIRECTORY"
