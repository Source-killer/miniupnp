# Shared steps of the PCP end-to-end drivers (pcp_e2e/run-docker.sh, run-freebsd.sh).
# Source this file after setting ROOT, DIR, DOCKER, IMAGE, RUN_DIR, RUNNER, LAN and
# STATUS_FILE.  The helpers read and set the drivers' variables on purpose: the
# drivers differ in how they start the daemon, take snapshots and capture packets,
# not in how a case is run and reported.

# e2e_parse_args "$@": --case NAME (repeatable) selects cases into SELECTED_CASES.
e2e_parse_args() {
  SELECTED_CASES=
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --case)
        [ "$#" -ge 2 ] || { echo "--case requires a name" >&2; exit 2; }
        case "$2" in *[!A-Za-z0-9_.-]*|'') echo "invalid case name: $2" >&2; exit 2 ;; esac
        SELECTED_CASES="$SELECTED_CASES $2"
        shift 2
        ;;
      *) echo "unsupported option: $1 (only --case NAME is accepted)" >&2; exit 2 ;;
    esac
  done
}

# e2e_case_selected NAME: true when no --case was given or NAME was selected.
e2e_case_selected() {
  [ -z "$SELECTED_CASES" ] && return 0
  case " $SELECTED_CASES " in *" $1 "*) return 0 ;; esac
  return 1
}

# e2e_case_rows FAMILY: "section<TAB>case<TAB>nat66" for every case that runs on FAMILY.
e2e_case_rows() {
  "$DOCKER" run --rm --volume "$DIR:/pcp-e2e:ro" \
    --volume "$ROOT/miniupnpd/tests/common:/pcp-common:ro" --entrypoint python3 "$IMAGE" \
    /pcp-e2e/run_pcp_e2e_cases.py --list-cases --family "$1"
}

# e2e_start_runner: start the detached runner container for one case.  Uses family,
# section_name, case_name, case_client, case_router, runner_ula, ip_flag, lanip,
# external, the *_url and *_ip peer variables.
e2e_start_runner() {
  "$DOCKER" run -d --name "$RUNNER" --network "$LAN" "$ip_flag" "$lanip" \
    --cap-add NET_ADMIN --entrypoint sh \
    --volume "$DIR:/pcp-e2e:ro" --volume "$ROOT/miniupnpd/tests/common:/pcp-common:ro" \
    --volume "$RUN_DIR:/pcp-e2e-artifacts" \
    "$IMAGE" -c 'if [ "$1" = add ]; then ip -6 address replace fd42:1::2/64 dev eth0 nodad; fi; shift; exec python3 "$@"' \
    pcp-e2e-runner "$runner_ula" /pcp-e2e/run_pcp_e2e_cases.py \
    --route-through-router --family "$family" --case "$case_name" \
    --router "$case_router" --client-address "$case_client" --external-address "$external" \
    --agent "allowed-peer=$allow_url" --agent "denied-peer=$deny_url" \
    --agent "third-peer=$third_url" \
    --peer-address "allowed-peer=$allow_ip" --peer-address "denied-peer=$deny_ip" \
    --peer-address "third-peer=$third_ip" \
    --snapshot-gate-prefix "/pcp-e2e-artifacts/$family/$section_name/$case_name/daemon-expect" >/dev/null
}

# e2e_wait_runner: while the runner container runs, take a daemon snapshot each time
# it drops a daemon-expect-N.ready marker, then collect its result into
# case_result / case_note (and STATUS=1 on failure).  Needs snapshot_daemon_state.
e2e_wait_runner() {
  while [ "$("$DOCKER" inspect -f '{{.State.Running}}' "$RUNNER" 2>/dev/null || echo false)" = true ]; do
    marker_found=0
    for marker in "$case_dir/daemon-expect-"*.ready; do
      [ -f "$marker" ] || continue
      request_number=${marker##*-}
      request_number=${request_number%.ready}
      snapshot_daemon_state "$case_dir/daemon-expect-$request_number.txt"
      rm -f "${marker:?}"
      marker_found=1
    done
    [ "$marker_found" = 1 ] || sleep 0.05
  done
  runner_exit=$("$DOCKER" wait "$RUNNER")
  "$DOCKER" logs "$RUNNER" > "$case_dir/runner.log" 2>&1 || true
  "$DOCKER" rm "$RUNNER" >/dev/null
  sed '/^Summary: /d' "$case_dir/runner.log"
  if [ "$runner_exit" = 0 ]; then
    case_result=PASS
    case_note="all configured assertions passed"
  else
    STATUS=1
    case_result=FAIL
    case_note=$(sed -n 's/^FAIL [^:]*: //p' "$case_dir/runner.log" | head -n 1)
  fi
}

# e2e_record_result ARTIFACTS: append this case's row to RESULTS_FILE.
e2e_record_result() {
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$family" "$section_name" "$case_name" "$case_result" "$case_note" "$1" >> "$RESULTS_FILE"
}

# e2e_write_status TITLE EXTRA_RUN_LINE ARTIFACTS_NOTE: write STATUS_FILE and print the
# summary.  EXTRA_RUN_LINE (may be empty) is added under "## Run" after the branch.
e2e_write_status() {
  if [ "$EXECUTED" -eq 0 ]; then
    echo "No E2E cases matched the requested selection." >&2
    STATUS=1
  fi
  PASS_TOTAL=$(awk -F '\t' '$4 == "PASS" { n++ } END { print n+0 }' "$RESULTS_FILE")
  FAIL_TOTAL=$(awk -F '\t' '$4 == "FAIL" { n++ } END { print n+0 }' "$RESULTS_FILE")
  RESULT_TOTAL=$(awk -F '\t' 'NF >= 4 { n++ } END { print n+0 }' "$RESULTS_FILE")
  SCENARIO_SELECTION=${SELECTED_CASES# }
  [ -n "$SCENARIO_SELECTION" ] || SCENARIO_SELECTION=all
  mkdir -p "$(dirname -- "$STATUS_FILE")"
  {
    printf '# %s\n\nUpdated: %s\n\n## Run\n\n- Branch: %s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "$(git -C "$ROOT" branch --show-current 2>/dev/null || echo unknown)"
    [ -z "$2" ] || printf -- '- %s\n' "$2"
    printf -- '- Scenario selection: %s\n' "$SCENARIO_SELECTION"
    printf -- '- Totals: **%s PASS**, **%s FAIL** (%s family runs)\n' "$PASS_TOTAL" "$FAIL_TOTAL" "$RESULT_TOTAL"
    printf -- '- Artifacts: %s\n\n## Results\n' "$3"
    for family in ipv4 ipv6; do
      printf '\n### %s\n' "$family"
      for section in MAP FILTER PEER NAT PMP; do
        printf '\n#### %s\n\n| Case | Status | Result / note |\n|---|---|---|\n' "$section"
        awk -F '\t' -v family="$family" -v section="$section" '$1==family && $2==section { note=$5; gsub(/\|/, "\\|", note); printf "| %s | %s | %s |\n", $3, $4, note }' "$RESULTS_FILE"
      done
    done
  } > "$STATUS_FILE"
  echo "Status report: $STATUS_FILE"
  echo "Summary: $PASS_TOTAL passed, $FAIL_TOTAL failed ($RESULT_TOTAL family runs)"
}
