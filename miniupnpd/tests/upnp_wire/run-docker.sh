#!/bin/sh
# The UPnP wire suite on Linux (Docker): builds miniupnpd (igd1: the default build, igd2: --igd2, and each with --strict), starts
# it in UPnP mode (PCP_TEST_ENABLE_UPNP=yes) on a LAN network, and runs run_upnp_wire_cases.py in a LAN client
# container for IPv4 and IPv6. The results go into status.md (columns linux-igd1, linux-igd2); the raw
# data, the logs and the daemon's state files of each run into .tmp/tests/upnp_wire/ of the repository.
#
#   run-docker.sh [igd1|igd2|all] [--case NAME]...
#
# A case that needs a daemon restart asks this script for it (router_call restart). UPNP_WIRE_SLOT=N picks a
# slot of e2e_address_plan for the LAN; by default the first free one, so runs go side by side.
# UPNP_WIRE_KEEP=1 keeps the containers; PCP_TEST_SELF_TEST=0 skips the self-tests.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../../.." && pwd)
DIR=$ROOT/miniupnpd/tests/upnp_wire
DOCKER=${DOCKER:-docker}
KEEP=${UPNP_WIRE_KEEP:-0}
DATA_DIR=$ROOT/.tmp/tests/upnp_wire
STATUS_FILE=${UPNP_WIRE_STATUS_FILE:-$DIR/status.md}
STATUS_DATA_FILE=${UPNP_WIRE_STATUS_DATA_FILE:-$DATA_DIR/status-data.json}
RUN_ID=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=$DATA_DIR/runs/$RUN_ID
STATUS=0

. "$ROOT/miniupnpd/tests/common/e2e_lib.sh"
e2e_timestamp_output "$@"

BUILDS=
args=$#
while [ "$args" -gt 0 ]; do
  argument=$1; shift; args=$((args - 1))
  case "$argument" in
    igd1|igd2|igd1-strict|igd2-strict) BUILDS="$BUILDS $argument" ;;
    all) BUILDS="igd1 igd2 igd1-strict igd2-strict" ;;
    *) set -- "$@" "$argument" ;;
  esac
done
e2e_parse_args "$@"
CASE_ARGS=
for selected in $SELECTED_CASES; do CASE_ARGS="$CASE_ARGS --case $selected"; done
[ -n "$BUILDS" ] || BUILDS="igd1 igd2 igd1-strict igd2-strict"
command -v "$DOCKER" >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 2; }
e2e_self_test
mkdir -p "$RUN_DIR"

reserve_slot e2e_address_plan "${UPNP_WIRE_SLOT:-}"
echo "Fixture slot $SLOT: LAN $LAN4.0/24 ${LAN6}/64"
LAN=upnp-wire-lan-$SLOT
ROUTER=upnp-wire-router-$SLOT
RUNNER=upnp-wire-runner-$SLOT
UUID=$(sed -n 's/^uuid: //p' "$DIR/upnp-wire-cases.yaml")

cleanup() {
  result=$?
  trap - EXIT INT TERM
  if [ "$KEEP" != 1 ]; then
    "$DOCKER" rm -f "$RUNNER" "$ROUTER" >/dev/null 2>&1 || true
    "$DOCKER" network rm "$LAN" >/dev/null 2>&1 || true
  else
    echo "Keeping $ROUTER $RUNNER $LAN" >&2
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$DOCKER" network create --driver bridge --ipv6 \
  --subnet "$LAN4.0/24" --gateway "$LAN4.254" \
  --subnet "${LAN6}/64" --gateway "${LAN6}ffff" "$LAN" >/dev/null

# router_call COMMAND: restart, the same container, so the lease file stays
router_call() {
  case $1 in
    restart)
      echo "restarting the daemon for the runner" >&2
      "$DOCKER" restart "$ROUTER" >/dev/null
      e2e_wait_upnp "$ROUTER" "$LAN4.1" && echo restarted ;;
    *) echo "unknown router command: $1" ;;
  esac
}

run_family() {
  build=$1 family=$2 out=$3
  if [ "$family" = ipv4 ]; then router=$LAN4.1 client=$LAN4.2 second=$LAN4.3
  else router=${LAN6}1 client=${LAN6}2 second=${LAN6}3; fi
  mkdir -p "$out/calls"
  chmod 777 "$out/calls"
  # shellcheck disable=SC2086 # CASE_ARGS and STRICT_ARG are lists of options
  "$DOCKER" run -d --name "$RUNNER" --network "$LAN" --ip "$LAN4.2" --ip6 "${LAN6}2" --cap-add NET_ADMIN \
    --volume "$ROOT/miniupnpd/tests:/pcp-tests:ro" \
    --volume "$out:/out" --volume "$out/state:/state:ro" --entrypoint python3 "$IMAGE" \
    /pcp-tests/upnp_wire/run_upnp_wire_cases.py --build "${build%-strict}" $STRICT_ARG --family "$family" --router "$router" \
    --client-address "$client" --second-client "$second" --interface eth0 --router-call-dir /out/calls \
    --lan4 "$LAN4" --lan6 "$LAN6" \
    --lease-file /state/miniupnpd.leases --results "/out/results-$family.tsv" $CASE_ARGS >/dev/null
  e2e_serve_runner "$out/calls"
  e2e_collect_runner "$out/runner-$family.log"
  grep -E '^(PASS|XFAIL|FAIL|SKIP|Summary)' "$out/runner-$family.log" || true
  [ "$runner_exit" = 0 ] || STATUS=1
  if [ -f "$out/results-$family.tsv" ]; then
    branch=$(git -C "$ROOT" branch --show-current 2>/dev/null || echo unknown)
    python3 "$DIR/run_upnp_wire_cases.py" --write-status "$out/results-$family.tsv" --platform "linux-$build" \
      --status-file "$STATUS_FILE" --status-data-file "$STATUS_DATA_FILE" \
      --run-info "branch=$branch" --run-info "router=Linux nftables" \
      --run-info "artifacts=${out#"$ROOT"/}" || STATUS=1
  else
    echo "The runner wrote no results ($build $family); see $out/runner-$family.log" >&2
    STATUS=1
  fi
}

e2e_tools_image
for build in $BUILDS; do
  flags= STRICT_ARG=
  case "$build" in igd2*) flags=--igd2 ;; esac
  case "$build" in *-strict) flags="$flags --strict"; STRICT_ARG=--strict ;; esac
  e2e_build_daemon "$flags"
  out=$RUN_DIR/$build
  mkdir -p "$out/state"
  chmod 777 "$out" "$out/state"
  "$DOCKER" run -d --name "$ROUTER" --network "$LAN" --ip "$LAN4.1" --ip6 "${LAN6}1" \
    --cap-add NET_ADMIN --cap-add NET_RAW --env PCP_TEST_ENABLE_UPNP=yes --env "PCP_TEST_UPNP_UUID=$UUID" \
    --volume "$out/state:/var/lib/miniupnpd" --volume "$DAEMON_DIR:/opt/miniupnpd:ro" "$IMAGE" >/dev/null
  e2e_wait_upnp "$ROUTER" "$LAN4.1"
  echo "=== $build ==="
  for family in ipv4 ipv6; do
    run_family "$build" "$family" "$out"
  done
  "$DOCKER" logs --timestamps "$ROUTER" > "$out/daemon.log" 2>&1 || true
  "$DOCKER" rm -f "$ROUTER" >/dev/null
done
echo "Status report: $STATUS_FILE (raw data: $STATUS_DATA_FILE; logs: $RUN_DIR)"
e2e_result "$STATUS"
exit "$STATUS"
