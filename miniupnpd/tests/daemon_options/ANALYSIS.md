# Configuration option coverage

Which `miniupnpd.conf` options the test suites set, and whether a case checks what an option does.
I built this from a scan of the tree on 2026-11-03 (branch `dev/daemon-options-tests`):

- the `optionids[]` table of `miniupnpd/options.c`: 51 names, counted over every `#ifdef`;
- the `allow` / `deny` permission lines, which `readoptionsfile()` parses apart from the table;
- every place that writes a configuration: `common/docker/entrypoint.sh`, the profiles of
  `pcp_wire/run-docker.sh`, `pcp_wire/freebsd/setup.sh` and `lease-suite.sh`,
  `pcp_e2e/freebsd/pf-router.sh`, and the `config:` lines of `daemon-options-cases.yaml`.

`run_daemon_options_cases.py --self-test` prints the current count of this suite as an INFO line
(`lib_test.option_coverage_line`). The line never fails the self-test.

Some names are synonyms:

- `enable_natpmp` and `enable_pcp_pmp` are one option (`UPNPENABLENATPMP`);
- `port` and `http_port` are one option (`UPNPPORT`).

`pcp_min_lifetime_override` (the `short-lifetime` profile of `pcp_wire`) exists only on
`feat/pcp-peer-filter`. It is not in this branch's `options.c`, so this branch's daemon ignores
that profile line.

## Options the tests set

Column key:

- **daemon_options**: cases that run with different values of the option;
- **pcp_wire profiles**: a daemon restarted with another value for some cases;
- **fixture default**: set to one value and never varied.

| Option | Behaviour tested? | daemon_options | pcp_wire profiles | fixture default (set, never varied) |
|---|---|---|---|---|
| `enable_pcp_pmp` | yes: off gives no PCP/NAT-PMP reply, on answers | `pcp-pmp-off-*`, `all-on-*`, `upnp-off-*` | — | `yes`: entrypoint.sh, freebsd/setup.sh, pf-router.sh |
| `enable_natpmp` | yes (synonym of `enable_pcp_pmp`) | `natpmp-off-*` | — | — |
| `enable_upnp` | yes: off means no HTTP listener, on answers SOAP | `upnp-off-*`, `*-upnp-answers`, `wan-upnp-no-reply` | — | `no`: entrypoint.sh, freebsd/setup.sh; `yes`: pf-router.sh (pcp_e2e UPnP section) |
| `http_port` (`port`) | only as the port the UPnP cases use (5000) | set in every UPnP case config | — | `5000`: pf-router.sh |
| `min_lifetime` | yes: the exact grant under 300 and the 120 s floor | `min-lifetime-*` | `min-lifetime-only` (Docker, FreeBSD lease-suite) | — |
| `max_lifetime` | yes: the exact grant under 300 and 900, the 86400 s ceiling, NAT-PMP not capped | `max-lifetime-*` | `7200` in the FreeBSD lease-suite daemon (a fixture choice, no case about it) | `600`: entrypoint.sh, setup.sh, pf-router.sh |
| `lease_file` | yes, on, off and path: `lease-file-*` | — | — | entrypoint.sh, pf-router.sh, freebsd run-suite.sh / lease-suite.sh |
| `listening_ip` | yes: `listening-ip-by-address`, the `wan-*` cases | `wan-*` (with the default) | — | `eth0` (Docker); two lines, `epair0a` and `epair1a`, in freebsd/setup.sh; `vtnet1` in pf-router.sh |
| `ext_ifname` | yes: `ext-ifname-address-reported`, the reported address follows it | — | — | `wan0`, `epair2a`, `vtnet2` |
| `ext_allow_private_ipv4` | yes: `private-address-*` | — | — | `yes` in every fixture |
| `secure_mode` | yes: UPnP for another LAN address; no effect on PCP THIRD_PARTY | `secure-*`, `thirdparty-on-secure-*` | — | `yes` in every fixture |
| `ipv6_listening_ip` | yes: `ipv6-listening-ip` | — | — | `PCP_TEST_IPV6_LISTENING_IP=1` in pcp_wire/fixtures/docker.sh |
| `pcp_allow_thirdparty` | partly: the reply on and off, WAN side silent; no traffic to the other host (needs a second LAN host) | `thirdparty-*` | — | — |
| `allow` / `deny` lines | yes: port, client address and no-match rules, for PCP, NAT-PMP and UPnP | `perm-*` | — | `allow 1024-65535 0.0.0.0/0 1024-65535`, `deny 0-65535 0.0.0.0/0 0-65535` in every fixture |

## Options no test sets

`run-freebsd.sh` runs the corpus on pf, also the pf names (`anchor`, `queue`, `tag`, `quickrules`),
against a tree with the `feat/pcp-peer-filter` daemon, built with `PF_ENABLE_FILTER_RULES`. The
`platform: linux` cases stay out: nftables, the Linux uptime, the minissdpd stand-in, `lease_file6`.
`port` counts as untested in the self-test line, because the cases use its synonym `http_port`.
`set_learn_dscp` lines (`PCP_SADSCP` builds) are parsed apart from the table and are not counted.

The other options are in the sections of `daemon-options-cases.yaml` after the permission rules:
daemon start, external address, listening addresses, UPnP description, timers, lease files,
nftables names and third party traffic.

## Findings

- **The IPv6 PCP socket answers with PCP/NAT-PMP off.**
  - With `enable_pcp_pmp=no` or `enable_natpmp=no`, a PCP ANNOUNCE over IPv6 still gets a reply.
  - `miniupnpd.c` calls `OpenAndConfPCPv6Socket()` without checking `ENABLENATPMPMASK`.
  - Cases `pcp-pmp-off-pcp-silent-ipv6` and `natpmp-off-pcp-silent-ipv6` FAIL until the daemon is
    fixed. IPv4 is silent, as expected.
- **A later line can't undo an earlier one.** Each of these flags can only switch one way:
  - `enable_upnp` and `secure_mode` only switch off;
  - `enable_natpmp` / `enable_pcp_pmp` only switch on.

  So `enable_upnp=yes` after `enable_upnp=no` keeps UPnP off, and `enable_pcp_pmp=no` after
  `enable_pcp_pmp=yes` keeps PCP on. The suite uses `PCP_TEST_CONF_REPLACE=1` in
  `common/docker/entrypoint.sh` to replace the default line instead (README.md).
- **`max_lifetime` and `min_lifetime` do not apply to NAT-PMP.** A NAT-PMP request for 3600 s
  under `max_lifetime=300` is granted 3600 s. `miniupnpd.conf` lists both options under PCP, so
  the case records this as expected.
- **`secure_mode` does not apply to PCP.** With `pcp_allow_thirdparty=yes`, a client can map for
  another address with `secure_mode=yes`. `miniupnpd.conf` describes it as a UPnP option.
- **`allow` / `deny` lines do not apply to IPv6.** The lines hold IPv4 addresses and `pcpserver.c`
  checks them for IPv4-mapped addresses only, so a PCP MAP over IPv6 outside the allowed ports
  succeeds. `perm-pcp-ipv6-outside-refused` FAILs on this.
- **No quotas and no static mappings.** miniupnpd has no option for either, so the plan's limits
  and static scenarios have nothing to vary. `USER_EX_QUOTA` and `EXCESSIVE_REMOTE_PEERS` are
  defined, and the daemon never returns them from a limit of its own.
- **`clean_ruleset_interval` does not remove an idle mapping on nftables.**
  `clean-ruleset-removes-idle` FAILs. In `get_upnp_rules_state_list()` the `PCP_PEER` block sets
  `i=0` before it counts the peer rules, so the redirects are not counted, the list is thrown away
  as below the threshold, and nothing is ever checked. Builds with `--pcp-peer` are affected.
  `clean-ruleset-threshold-keeps` passes only because of this. The check also runs only when the
  main loop wakes up, so the cases send requests while they wait.
- **`ipv6_disable=yes` stops an HTTPS build on FreeBSD.** With `V6SOCKETS_ARE_V6ONLY` the daemon
  opens a second IPv4 HTTPS socket on the port the first one holds, and exits. The HTTP code has
  the `IPV6DISABLEDMASK` guard, the HTTPS code does not. `ipv6-disable-silent` and
  `listening-ip-by-address` FAIL on FreeBSD for this.
- **`lease_file6` holds the IPv6 mappings of UPnP and PCP.** `lease-file6-path` checks that a PCP
  IPv6 MAP is written to it; the case runs on Linux only until it is checked on FreeBSD.
- **`packet_log` has no effect.** `miniupnpd.c` sets `LOGPACKETSMASK` and nothing reads it. That
  is a missing feature; the case only checks that the option is accepted.
- **An `--https` build needs its certificate files at start,** also without `https_port`.
- **minissdpd is used only as a fallback.** The daemon submits its services only when it cannot
  open the SSDP socket, so the fixture holds port 1900 for that case.
- **`enable_natpmp` and `enable_pcp_pmp` are one option.** There is no switch for PCP or NAT-PMP
  alone.

## The FreeBSD column

`freebsd/run-suite.sh` builds with `PF_ENABLE_FILTER_RULES`, for the pf-queue, pf-quickrules and stun cases
(the STUN check needs filter rules). `pf/obsdrdr.c` of feat/pcp-peer-filter and feat/lease-table does not
compile with it (`use of undeclared identifier 'rule'`). The freebsd column comes from
fix/pf-filter-rules-and-mx, which fixes that on top of feat/lease-table.
