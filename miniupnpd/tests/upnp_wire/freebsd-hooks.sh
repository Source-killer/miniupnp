# The suite hooks of upnp_wire/run-freebsd.sh: pcp_e2e/run-freebsd.sh sources this file after its own
# functions, so these replace them. Uses the variables of that script. The whole corpus of one family
# runs in one LAN container; the cases of the lease file SKIP (the file is in the VM).
UPNP_DIR=$ROOT/miniupnpd/tests/upnp_wire

router_call() {
  case $1 in
    restart)
      echo "restarting the daemon for the runner" >&2
      vm sh miniupnpd/tests/pcp_e2e/freebsd/pf-router.sh restart >/dev/null && echo restarted ;;
    *) echo "unknown router command: $1" ;;
  esac
}

run_family() {
  family=$1
  if [ "$family" = ipv4 ]; then router=$LAN4.1 client=$LAN4.2 second=$LAN4.3
  else router=${LAN6}1 client=${LAN6}2 second=${LAN6}3; fi
  out=$RUN_DIR/$family
  mkdir -p "$out/calls"
  chmod 777 "$out" "$out/calls"
  cases=
  for name in $SELECTED_CASES; do cases="$cases --case $name"; done
  # shellcheck disable=SC2086 # cases is a list of options
  "$DOCKER" run -d --name "$RUNNER" --network "$LAN" --ip "$LAN4.2" --ip6 "${LAN6}2" --cap-add NET_ADMIN \
    --volume "$ROOT/miniupnpd/tests:/pcp-tests:ro" --volume "$out:/out" \
    --entrypoint python3 "$IMAGE" /pcp-tests/upnp_wire/run_upnp_wire_cases.py --build igd2 --family "$family" \
    --router "$router" --client-address "$client" --second-client "$second" \
    --interface eth0 --router-call-dir /out/calls --lan4 "$LAN4" --lan6 "$LAN6" \
    --results "/out/results-$family.tsv" $cases >/dev/null
  e2e_serve_runner "$out/calls"
  e2e_collect_runner "$out/runner.log"
  grep -E '^(PASS|XFAIL|FAIL|SKIP|Summary)' "$out/runner.log" || true
  [ "$runner_exit" = 0 ] || STATUS=1
  $SSH "$PRIV cat /tmp/pcp-e2e/miniupnpd.log" >"$out/daemon.log" 2>&1 || true
  if [ -f "$out/results-$family.tsv" ]; then
    EXECUTED=$((EXECUTED + 1))
    python3 "$UPNP_DIR/run_upnp_wire_cases.py" --write-status "$out/results-$family.tsv" --platform freebsd-igd2 --status-file "$STATUS_FILE" \
      ${UPNP_WIRE_STATUS_DATA_FILE:+--status-data-file "$UPNP_WIRE_STATUS_DATA_FILE"} \
      --run-info "router=FreeBSD $VERSION pf" --run-info "artifacts=${out#"$ROOT"/}" || STATUS=1
  else
    echo "The runner wrote no results ($family); see $out/runner.log" >&2
    STATUS=1
  fi
}

e2e_write_status() { [ "$EXECUTED" -gt 0 ] || { echo "No results." >&2; STATUS=1; }; }
