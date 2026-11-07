#!/bin/sh
# FreeBSD driver of the daemon_options suite. The whole run is inside a throwaway VM: this script
# boots it, copies the tree in, runs freebsd/run-suite.sh and copies the results out, with the
# machinery of pcp_wire/run-freebsd.sh. The `platform: linux` cases stay out (nftables,
# the Linux uptime, the minissdpd stand-in, lease_file6).
#
#   miniupnpd/tests/daemon_options/run-freebsd.sh [--case NAME ...]
#
# Fills the freebsd column of status.md. The tree has to hold a daemon that builds with pf PCP PEER:
# fix/pf-filter-rules-and-mx. DAEMON_OPTIONS_DAEMON names it in the report. Environment as for
# pcp_wire/run-freebsd.sh; PCP_TEST_FREEBSD_SSH_PORT defaults to 2245 here.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../../.." && pwd)
DIR=$ROOT/miniupnpd/tests/daemon_options
. "$ROOT/miniupnpd/tests/common/e2e_lib.sh"
e2e_parse_args "$@"
cases=$SELECTED_CASES
daemon=${DAEMON_OPTIONS_DAEMON:-$(git -C "$ROOT" branch --show-current 2>/dev/null || echo unknown)}

export PCP_TEST_FREEBSD_SSH_PORT=${PCP_TEST_FREEBSD_SSH_PORT:-2245}
export PCP_TEST_OUT_DIR=${PCP_TEST_OUT_DIR:-$ROOT/.tmp/ci/out/freebsd/daemon_options}
export PCP_TEST_RESULTS_DIR=$DIR
export PCP_TEST_DATA_DIR=$ROOT/.tmp/tests/daemon_options
export PCP_TEST_VM_COMMAND="env DAEMON_OPTIONS_DAEMON='$daemon' DAEMON_OPTIONS_CASES='$cases' sh miniupnpd/tests/daemon_options/freebsd/run-suite.sh /tmp/pcp-results"
exec sh "$ROOT/miniupnpd/tests/pcp_wire/run-freebsd.sh"
