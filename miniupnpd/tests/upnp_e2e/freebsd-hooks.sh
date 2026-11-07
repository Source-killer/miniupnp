# The suite hooks of upnp_e2e/run-freebsd.sh: pcp_e2e/run-freebsd.sh sources this file after its own
# functions, so these replace them. Uses the variables of that script.
UPNP_DIR=$ROOT/miniupnpd/tests/upnp_e2e
UPNP_BUILD=igd2
E2E_UPNP=    # the driver sets it for the pcp_e2e runner
DIR=$UPNP_DIR
E2E_RUNNER=run_upnp_e2e_cases.py
E2E_RUNNER_ARGS="--build $UPNP_BUILD"
E2E_PLATFORM=freebsd-$UPNP_BUILD

# The third column of the rows is restart here, nat66 in the driver: the restart cases go to a file.
pcp_case_rows() {
  "$DOCKER" run --rm --volume "$ROOT/miniupnpd/tests:/pcp-tests:ro" --entrypoint python3 "$IMAGE" \
    "/pcp-tests/upnp_e2e/$E2E_RUNNER" --list-cases $E2E_RUNNER_ARGS --family "$1"
}
e2e_case_rows() {
  pcp_case_rows "$1" | awk -F '	' -v OFS='	' -v list="$RUN_DIR/restart-cases" \
    '$3 == "true" { print $2 >> list } { print $1, $2, "false" }'
}

# snapshot_daemon_state FILE: as in the driver, and the restart of a restart case after its first request
snapshot_daemon_state() {
  {
    echo "Snapshot time: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    vm sh miniupnpd/tests/pcp_e2e/freebsd/pf-router.sh snapshot
  } >"$1" 2>&1 || true
  case "$1" in
    *daemon-expect-1.txt)
      if grep -qx "$case_name" "$RUN_DIR/restart-cases" 2>/dev/null; then
        echo "restarting the daemon (after request 1 of $case_name)"
        vm sh miniupnpd/tests/pcp_e2e/freebsd/pf-router.sh restart >/dev/null
      fi ;;
  esac
}
