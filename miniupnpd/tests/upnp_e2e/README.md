# upnp_e2e: the UPnP IGD end-to-end suite

The cases in `upnp-e2e-cases.yaml` make port mappings and IPv6 pinholes with SOAP and check them with real
traffic from WAN peers through the router: the lifecycle of UDP and TCP mappings, port translation, lease
expiry, NewRemoteHost and NewEnabled, AddAnyPortMapping and DeletePortMappingRange (IGDv2), pinholes with a
wildcard and a specific RemoteHost, UpdatePinhole, DeletePinhole and pinhole expiry (IGDv2, IPv6), UPnP and
PCP mappings of one daemon, a mapping across a daemon restart, and what a WAN peer reaches of
the UPnP side (SSDP, HTTP, SOAP on the router's WAN address).

The protocol details (faults, descriptions, eventing) are the subject of [../upnp_wire](../upnp_wire);
the UPnP section of [../pcp_e2e](../pcp_e2e) stays as the check of the firewall rule code UPnP shares with
PCP.

## Running

    sh miniupnpd/tests/upnp_e2e/run-docker.sh
    UPNP_E2E_BUILDS=igd2 sh miniupnpd/tests/upnp_e2e/run-docker.sh --case upnp-e2e-pinhole-remote-host

The topology is that of `pcp_e2e/run-docker.sh` (and its addresses, so the two cannot run at the same
time): a LAN (172.28.41.0/24, 2001:db8:1::/64) with the runner, the router (miniupnpd in UPnP mode,
`PCP_TEST_ENABLE_UPNP=yes`, with PCP on), a WAN (of a fixture slot: `UPNP_E2E_SLOT`) with three traffic agents
(allowed-peer, denied-peer, third-peer). The driver compiles the `igd1` and `igd2` daemons in the
shared tools image (`e2e_build_daemon`) and runs each case of each build and address family in its own runner container.
The results go into [status.md](status.md), one column per platform and build (`linux-igd1`,
`linux-igd2`); the raw results and the logs (runner, daemon, ruleset and lease file after each request)
into `.tmp/tests/upnp_e2e/` of the repository.

The request and traffic keys of the corpus are those of the pcp_e2e corpus (`lib_test.CaseRun`), with the
UPnP request kinds listed at the top of the corpus. A case with `requires: [restart]` has the driver
restart the router container after its first request (the runner waits for the answer of its router call). An
`exposure:` case sends a payload from a WAN peer to the router's WAN address and passes when the traffic
agent's probe ends with an error (no answer, no connection).

`python3 run_upnp_e2e_cases.py --self-test` checks the corpus of each build with `validate_corpus` and
the status report; it is part of `common/run_self_tests.sh`.

The driver defines its own `e2e_case_rows`, `e2e_start_runner` and status step, as the ones of
`common/e2e_lib.sh` name the pcp_e2e runner; they go away when that file takes the suite directory and
the runner as variables (with `pcp_e2e/run-freebsd.sh`, which a FreeBSD driver of this suite will wrap).
