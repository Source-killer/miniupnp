#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../../.." && pwd)
DIR=$ROOT/miniupnpd/tests/pcp_e2e
DOCKER=${DOCKER:-docker}
IMAGE=${PCP_E2E_IMAGE:-miniupnpd-pcp-e2e:local}
SUFFIX=$$
LAN=pcp-e2e-lan-$SUFFIX
WAN=pcp-e2e-wan-$SUFFIX
ROUTER=pcp-e2e-router-$SUFFIX
ALLOW=pcp-e2e-allow-$SUFFIX
DENY=pcp-e2e-deny-$SUFFIX
THIRD=pcp-e2e-third-$SUFFIX
RUNNER=pcp-e2e-runner-$SUFFIX
KEEP=${PCP_E2E_KEEP:-0}
ARTIFACTS=${PCP_E2E_ARTIFACT_DIR:-$ROOT/.tmp/pcp-e2e}
STATUS_FILE=${PCP_E2E_STATUS_FILE:-$DIR/status.md}
RUN_ID=$(date -u +%Y-%m-%dT%H-%M-%SZ)
case "$ARTIFACTS" in /*) ;; *) ARTIFACTS=$ROOT/$ARTIFACTS ;; esac
STATUS=0
EXECUTED=0

. "$ROOT/miniupnpd/tests/common/pcp_e2e_lib.sh"
e2e_parse_args "$@"

RUN_DIR=$ARTIFACTS/$RUN_ID
mkdir -p "$RUN_DIR"

cleanup() {
  result=$?
  trap - EXIT INT TERM
  set_nat66 0 >/dev/null 2>&1 || true
  if [ "$KEEP" != 1 ]; then
    "$DOCKER" rm -f "$RUNNER" "$ALLOW" "$DENY" "$THIRD" "$ROUTER" >/dev/null 2>&1 || true
    "$DOCKER" network rm "$LAN" "$WAN" >/dev/null 2>&1 || true
  else
    echo "Retaining fixture: $LAN $WAN $ROUTER $ALLOW $DENY $THIRD $RUNNER" >&2
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$DOCKER" build -f "$ROOT/miniupnpd/tests/pcp_pmp/docker/Dockerfile" -t "$IMAGE" "$ROOT"
"$DOCKER" network create --driver bridge --ipv6 \
  --subnet 172.28.41.0/24 --gateway 172.28.41.254 \
  --subnet 2001:db8:1::/64 --gateway 2001:db8:1::ffff "$LAN" >/dev/null
"$DOCKER" network create --driver bridge --ipv6 \
  --opt com.docker.network.bridge.gateway_mode_ipv6=routed \
  --subnet 198.51.100.0/24 --gateway 198.51.100.254 \
  --subnet 2001:db8:9000::/64 --gateway 2001:db8:9000::ffff "$WAN" >/dev/null

# Router starts on its LAN. Connecting the WAN second gives entrypoint.sh the
# expected eth0/eth1 order; it renames the WAN link to wan0 before daemon start.
"$DOCKER" create --name "$ROUTER" --network "$LAN" --ip 172.28.41.1 \
  --ip6 2001:db8:1::1 --cap-add NET_ADMIN --cap-add NET_RAW \
  --volume "$RUN_DIR:/pcp-e2e-artifacts" \
  --env PCP_TEST_REAL_WAN=1 --sysctl net.ipv4.ip_forward=1 \
  --sysctl net.ipv6.conf.all.forwarding=1 "$IMAGE" >/dev/null
"$DOCKER" network connect --ip 198.51.100.1 --ip6 2001:db8:9000::1 "$WAN" "$ROUTER"
"$DOCKER" start "$ROUTER" >/dev/null
"$DOCKER" exec "$ROUTER" ip -6 address replace fd42:1::1/64 dev eth0 nodad

start_agent() {
  name=$1; lan4=$2; lan6=$3; wan4=$4; wan6=$5
  "$DOCKER" create --name "$name" --network "$LAN" --ip "$lan4" --ip6 "$lan6" \
    --cap-add NET_ADMIN --entrypoint python3 "$IMAGE" \
    /src/miniupnpd/tests/common/traffic_agent.py --host 0.0.0.0 --source4 "$wan4" \
    --source6 "$wan6" --route6 2001:db8:1::/64 --gateway6 2001:db8:9000::1 \
    --route6 fd42:1::/64 --gateway6 2001:db8:9000::1 --peer-port 42100 >/dev/null
  "$DOCKER" network connect --ip "$wan4" --ip6 "$wan6" "$WAN" "$name"
  "$DOCKER" start "$name" >/dev/null
}
start_agent "$ALLOW" 172.28.41.12 2001:db8:1::12 198.51.100.2 2001:db8:9000::2
start_agent "$DENY" 172.28.41.13 2001:db8:1::13 198.51.100.130 2001:db8:9000::130
start_agent "$THIRD" 172.28.41.14 2001:db8:1::14 198.51.100.200 2001:db8:9000::200

ready=0
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
  if "$DOCKER" exec "$ALLOW" python3 -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=1)' >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[ "$ready" = 1 ] || { echo "Traffic agent failed to start" >&2; exit 1; }
sleep 2
# Model a normal gateway's LAN-to-WAN policy. Inbound WAN traffic remains
# blocked unless PCP installs the corresponding mapping rule.
"$DOCKER" exec "$ROUTER" nft insert rule inet filter forward \
  iifname eth0 oifname wan0 accept
# Model ordinary outbound IPv4 NAT so PEER can adopt a mapping created by traffic.
"$DOCKER" exec "$ROUTER" nft add rule inet filter postrouting \
  meta nfproto ipv4 oifname wan0 masquerade
RESULTS_FILE=$RUN_DIR/results.tsv
: > "$RESULTS_FILE"
"$DOCKER" logs --timestamps "$ROUTER" > "$RUN_DIR/daemon-start.log" 2>&1 || true
echo "Daemon startup log: $RUN_DIR/daemon-start.log"

snapshot_daemon_state() {
  state_file=$1
  {
    echo "Snapshot time: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    "$DOCKER" exec "$ROUTER" sh -c '
      echo "=== miniupnpd process ==="
      cat /proc/1/status
      tr "\\000" " " </proc/1/cmdline
      echo
      echo "=== miniupnpd configuration ==="
      cat /etc/miniupnpd.conf
      echo "=== interface addresses ==="
      ip -details address
      echo "=== IPv4 routes ==="
      ip -4 route show table all
      echo "=== IPv6 routes ==="
      ip -6 route show table all
      echo "=== nftables ruleset ==="
      nft -a list ruleset
      echo "=== daemon state files ==="
      for state_file in /var/lib/miniupnpd/*; do
        [ -f "$state_file" ] || continue
        echo "--- $state_file ---"
        cat "$state_file"
      done
      echo "=== lease files ==="
      for lease_file in /var/lib/miniupnpd/*.leases /var/lib/miniupnpd/*.leases6 /var/log/upnp.leases; do
        [ -f "$lease_file" ] || continue
        echo "--- $lease_file ---"
        cat "$lease_file"
      done
    '
  } >"$state_file" 2>&1 || true
}

set_nat66() {
  enabled=$1
  nat66_handle=$(
    "$DOCKER" exec "$ROUTER" nft -a list chain inet filter postrouting 2>/dev/null |
      awk -v marker="pcp-e2e-nat66-$SUFFIX" 'index($0, marker) {
        for (i = 1; i < NF; i++) if ($i == "handle") print $(i + 1)
      }'
  )
  if [ "$enabled" = 1 ]; then
    [ -n "$nat66_handle" ] && return 0
    "$DOCKER" exec "$ROUTER" nft add rule inet filter postrouting \
      meta nfproto ipv6 oifname wan0 masquerade comment "pcp-e2e-nat66-$SUFFIX"
  elif [ -n "$nat66_handle" ]; then
    "$DOCKER" exec "$ROUTER" nft delete rule inet filter postrouting handle "$nat66_handle"
    if "$DOCKER" exec "$ROUTER" nft -a list chain inet filter postrouting 2>/dev/null |
      grep -Fq "pcp-e2e-nat66-$SUFFIX"; then
      echo "failed to remove section NAT66 rule" >&2
      return 1
    fi
  fi
}

start_packet_capture() {
  pcap_file=$1
  capture_pidfile=/tmp/pcp-e2e-capture-$SUFFIX.pid
  "$DOCKER" exec "$ROUTER" sh -c '
    pcap_file=$1
    pidfile=$2
    rm -f "$pcap_file" "$pcap_file.log"
    tcpdump -U -n -i any -s 0 -w "$pcap_file" >"$pcap_file.log" 2>&1 &
    echo $! >"$pidfile"
  ' sh "$pcap_file" "$capture_pidfile"
  capture_ready=0
  for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$DOCKER" exec "$ROUTER" sh -c \
      'pid=$(cat "$1" 2>/dev/null) && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ -s "$2" ]' \
      sh "$capture_pidfile" "$pcap_file" >/dev/null 2>&1; then
      capture_ready=1
      break
    fi
    sleep 0.1
  done
  [ "$capture_ready" = 1 ] || {
    echo "tcpdump failed to start; see $pcap_file.log" >&2
    return 1
  }
}

stop_packet_capture() {
  pcap_file=$1
  capture_pidfile=/tmp/pcp-e2e-capture-$SUFFIX.pid
  "$DOCKER" exec "$ROUTER" sh -c '
    pidfile=$1
    pcap_file=$2
    pid=$(cat "$pidfile" 2>/dev/null) || exit 0
    kill -INT "$pid" 2>/dev/null || true
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
      grep -q "packets captured" "$pcap_file.log" 2>/dev/null && break
      sleep 0.1
    done
    rm -f "$pidfile"
  ' sh "$capture_pidfile" "$pcap_file" >/dev/null 2>&1 || true
}

run_family() {
  family=$1
  if [ "$family" = ipv4 ]; then
    lanip=172.28.41.2
    router=172.28.41.1
    external=198.51.100.1
    allow_url=http://172.28.41.12:18080
    deny_url=http://172.28.41.13:18080
    third_url=http://172.28.41.14:18080
    allow_ip=198.51.100.2; deny_ip=198.51.100.130; third_ip=198.51.100.200
    ip_flag=--ip
  else
    lanip=2001:db8:1::2
    router=2001:db8:1::1
    external=2001:db8:9000::1
    # The control channel uses LAN IPv4; each agent still binds test traffic
    # to its independently assigned IPv6 WAN address.
    allow_url=http://172.28.41.12:18080
    deny_url=http://172.28.41.13:18080
    third_url=http://172.28.41.14:18080
    allow_ip=2001:db8:9000::2; deny_ip=2001:db8:9000::130; third_ip=2001:db8:9000::200
    ip_flag=--ip6
  fi

  family_dir=$RUN_DIR/$family
  mkdir -p "$family_dir"
  case_rows=$(e2e_case_rows "$family")
  active_section=
  active_section_nat66=0
  while IFS="$(printf '\t')" read -r section_name case_name section_nat66; do
    [ -n "$case_name" ] || continue
    e2e_case_selected "$case_name" || continue
    if [ "$section_name" != "$active_section" ]; then
      if [ "$active_section_nat66" = 1 ]; then
        set_nat66 0
      fi
      active_section=$section_name
      active_section_nat66=0
      if [ "$section_nat66" = true ]; then
        set_nat66 1
        active_section_nat66=1
      fi
    fi
    case_client=$lanip
    case_router=$router
    if [ "$family" = ipv6 ] && [ "$section_nat66" = true ]; then
    case_client=fd42:1::2
    case_router=fd42:1::1
      runner_ula=add
    else
      runner_ula=skip
    fi
    case_start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    EXECUTED=$((EXECUTED + 1))
    case_dir=$family_dir/$section_name/$case_name
    mkdir -p "$case_dir"
    snapshot_daemon_state "$case_dir/daemon-before.txt"
    start_packet_capture "/pcp-e2e-artifacts/$family/$section_name/$case_name/packet-capture.pcap"
    e2e_start_runner
    e2e_wait_runner
    stop_packet_capture "/pcp-e2e-artifacts/$family/$section_name/$case_name/packet-capture.pcap"
    snapshot_daemon_state "$case_dir/daemon-after.txt"
    case_artifacts="$family/$section_name/$case_name/packet-capture.pcap; $family/$section_name/$case_name/packet-capture.pcap.log; $family/$section_name/$case_name/daemon-before.txt; $family/$section_name/$case_name/daemon-expect-*.txt; $family/$section_name/$case_name/daemon-after.txt; $family/$section_name/$case_name/runner.log; $family/$section_name/$case_name/daemon.log"
    e2e_record_result "$case_artifacts"
    "$DOCKER" logs --timestamps --since "$case_start" "$ROUTER" \
      > "$case_dir/daemon.log" 2>&1 || true
  done <<EOF
$case_rows
EOF
  if [ "$active_section_nat66" = 1 ]; then
    set_nat66 0
  fi
}

run_family ipv4
run_family ipv6
"$DOCKER" logs --timestamps "$ROUTER" > "$RUN_DIR/daemon-final.log" 2>&1 || true
e2e_write_status "PCP E2E Status" "Address families: IPv4 and IPv6" \
  "${RUN_DIR#"$ROOT"/} (daemon startup/final logs and per-case captures/snapshots)"
echo "Daemon logs: $RUN_DIR (startup, per-case snapshots, final)"
exit "$STATUS"
