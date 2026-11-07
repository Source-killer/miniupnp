# UPnP end-to-end Test Status

One column per platform; a run updates only its own platform's column (raw results: `.tmp/tests/upnp_e2e/status-data.json`).

## Platforms

- **freebsd-igd2**: **21 PASS**, **0 XFAIL**, **4 FAIL**, **0 SKIP** (updated 2026-11-04T16:03:50+01:00; ipv4: branch `dev/upnp-test-harness`, router `router=FreeBSD 14.5 pf`, artifacts `miniupnpd/tests/upnp_e2e/../../../.tmp/tests/upnp_e2e/freebsd/freebsd-2026-11-04T14-58-03Z-slot2`, cases `17`; ipv6: branch `dev/upnp-test-harness`, router `router=FreeBSD 14.5 pf`, artifacts `miniupnpd/tests/upnp_e2e/../../../.tmp/tests/upnp_e2e/freebsd/freebsd-2026-11-04T14-58-03Z-slot2`, cases `8`)
- **linux-igd1**: **14 PASS**, **0 XFAIL**, **2 FAIL**, **0 SKIP** (updated 2026-11-05T16:19:44+01:00; ipv4: branch `dev/upnp-test-harness`, selection `all`, artifacts `.tmp/tests/upnp_e2e/runs/2026-11-05T15-18-43Z/igd1`, router `Linux nftables`, cases `14`; ipv6: branch `dev/upnp-test-harness`, selection `all`, artifacts `.tmp/tests/upnp_e2e/runs/2026-11-05T15-18-43Z/igd1`, router `Linux nftables`, cases `2`)
- **linux-igd2**: **22 PASS**, **0 XFAIL**, **6 FAIL**, **0 SKIP** (updated 2026-11-05T16:21:37+01:00; ipv4: branch `dev/upnp-test-harness`, selection `all`, artifacts `.tmp/tests/upnp_e2e/runs/2026-11-05T15-18-43Z/igd2`, router `Linux nftables`, cases `19`; ipv6: branch `dev/upnp-test-harness`, selection `all`, artifacts `.tmp/tests/upnp_e2e/runs/2026-11-05T15-18-43Z/igd2`, router `Linux nftables`, cases `9`)

## Results by section

### PortMapping

IPv4 port mappings made with AddPortMapping / AddAnyPortMapping (WANIPC1 §2.4.16, WANIPC2 §2.5.16-§2.5.19), checked with traffic from the WAN peers.

| Case | Protocol | Family | freebsd-igd2 | linux-igd1 | linux-igd2 | Result / note |
|---|---|---|---|---|---|---|
| upnp-e2e-udp-lifecycle | upnp | ipv4 | PASS | PASS | PASS | A UDP mapping forwards UDP (and not TCP); after DeletePortMapping it forwards nothing. |
| upnp-e2e-tcp-lifecycle | upnp | ipv4 | PASS | PASS | PASS | A TCP mapping forwards TCP from every peer; after DeletePortMapping it forwards nothing. |
| upnp-e2e-port-translation | upnp | ipv4 | PASS | PASS | PASS | External port 45010 is translated to internal port 45011. |
| upnp-e2e-lease-expiry | upnp | ipv4 | PASS | PASS | PASS | A 4-second lease forwards traffic, and the daemon removes the mapping when it ends. |
| upnp-e2e-remote-host | upnp | ipv4 | PASS | PASS | PASS | A mapping for NewRemoteHost allowed-peer forwards the allowed peer's traffic and not the denied peer's. |
| upnp-e2e-disabled-mapping | upnp | ipv4 | FAIL | FAIL | FAIL | freebsd-igd2: udp 198.18.2.1:45021: expected blocked; got {'delivered': True}; linux-igd1: udp 6.18.1.1:45021: expected blocked; got {'delivered': True}; linux-igd2: udp 6.18.1.1:45021: expected blocked; got {'delivered': True} |
| upnp-e2e-delete-range | upnp | ipv4 | PASS | — | PASS | DeletePortMappingRange removes both mappings of the range, and both ports stop forwarding. |
| upnp-e2e-addany-free-port | upnp | ipv4 | PASS | — | PASS | AddAnyPortMapping on a free port reserves it, and it forwards traffic. |
| upnp-e2e-lease-zero-igd2 | upnp | ipv4 | PASS | — | PASS | NewLeaseDuration 0 on WANIPConnection:2 (taken as 604800 seconds) forwards traffic. |

### Pinholes

IPv6 pinholes made with WANIPv6FirewallControl (6FC1 §2.6.3-§2.6.5, IGDv2) for this client's GUA, checked with traffic from the WAN peers.

| Case | Protocol | Family | freebsd-igd2 | linux-igd1 | linux-igd2 | Result / note |
|---|---|---|---|---|---|---|
| upnp-e2e-pinhole-wildcard | upnp | ipv6 | PASS | — | PASS | A pinhole with a wildcard RemoteHost lets every peer's UDP in. |
| upnp-e2e-pinhole-remote-host | upnp | ipv6 | PASS | — | PASS | A pinhole for RemoteHost allowed-peer lets the allowed peer in and not the denied peer. |
| upnp-e2e-pinhole-tcp | upnp | ipv6 | PASS | — | PASS | A TCP pinhole lets TCP in. |
| upnp-e2e-pinhole-delete | upnp | ipv6 | PASS | — | PASS | After DeletePinhole the pinhole lets nothing in. |
| upnp-e2e-pinhole-update | upnp | ipv6 | FAIL | — | PASS | freebsd-igd2: UPnP response expected SUCCESS, got UPNP_501 |
| upnp-e2e-pinhole-expiry | upnp | ipv6 | PASS | — | PASS | A 4-second pinhole lets traffic in, and lets nothing in once it expired. |

### PCP interplay

UPnP and PCP mappings of one daemon, which no spec covers in one box; a port one mechanism holds is not given to the other, and their rules work side by side.

| Case | Protocol | Family | freebsd-igd2 | linux-igd1 | linux-igd2 | Result / note |
|---|---|---|---|---|---|---|
| upnp-then-pcp-same-port | pcp+upnp | ipv4 | PASS | PASS | PASS | After a UPnP mapping of 45200, a PCP MAP suggesting 45200 gets another external port; both mappings forward traffic. |
| pcp-then-upnp-same-port | pcp+upnp | ipv4 | FAIL | FAIL | FAIL | freebsd-igd2: UPnP response expected UPNP_729, got SUCCESS; linux-igd1: UPnP response expected UPNP_718, got SUCCESS; linux-igd2: UPnP response expected UPNP_729, got SUCCESS |
| pcp-then-upnp-addany | pcp+upnp | ipv4 | FAIL | — | FAIL | freebsd-igd2: udp 198.18.2.1:45220: expected delivered; got {'delivered': False, 'error': '[Errno 111] Connection refused'}; linux-igd2: udp 6.18.1.1:45220: expected delivered; got {'delivered': False, 'error': '[Errno 111] Connection refused'} |
| pcp-then-upnp-range-keeps-pcp | pcp+upnp | ipv4 | — | — | FAIL | linux-igd2: udp 6.18.1.1:45230: expected delivered; got {'delivered': False, 'error': '[Errno 111] Connection refused'} |
| pcp-only-range-refused | pcp+upnp | ipv4 | — | — | FAIL | linux-igd2: UPnP response expected UPNP_606, got SUCCESS |
| pcp-pinhole-then-upnp-addpinhole-refused | pcp+upnp | ipv6 | — | — | FAIL | linux-igd2: UPnP response expected UPNP_606, got SUCCESS |
| upnp-and-pcp-different-ports | pcp+upnp | ipv4 | PASS | PASS | PASS | A UPnP mapping and a PCP mapping on different ports both forward, and deleting the UPnP one leaves the PCP one. |

### Persistence

A mapping restored from the lease_file after a daemon restart still forwards traffic.

| Case | Protocol | Family | freebsd-igd2 | linux-igd1 | linux-igd2 | Result / note |
|---|---|---|---|---|---|---|
| upnp-e2e-mapping-survives-restart | upnp | ipv4 | PASS | PASS | PASS | A UDP mapping (lease 3600) forwards traffic after the daemon restarted. |
| upnp-e2e-permanent-mapping-survives-restart | upnp | ipv4 | — | PASS | — | A permanent TCP mapping (lease 0, IGDv1) forwards traffic after the daemon restarted. |

### WAN exposure

The daemon's UPnP side is for the LAN only (IGD2 §2.3.2); a WAN peer sending to the router's WAN address gets no SSDP answer, no HTTP connection and so no SOAP control.

| Case | Protocol | Family | freebsd-igd2 | linux-igd1 | linux-igd2 | Result / note |
|---|---|---|---|---|---|---|
| wan-ssdp-msearch-unanswered | upnp | ipv4 | PASS | PASS | PASS | A unicast M-SEARCH from a WAN peer to the router's WAN address, port 1900, gets no answer. |
| wan-ssdp-msearch-unanswered | upnp | ipv6 | PASS | PASS | PASS | A unicast M-SEARCH from a WAN peer to the router's WAN address, port 1900, gets no answer. |
| wan-http-not-reachable | upnp | ipv4 | PASS | PASS | PASS | A WAN peer cannot connect to the router's WAN address, port 5000 (description and SOAP). |
| wan-http-not-reachable | upnp | ipv6 | PASS | PASS | PASS | A WAN peer cannot connect to the router's WAN address, port 5000 (description and SOAP). |
| wan-soap-not-reachable | upnp | ipv4 | PASS | PASS | PASS | A SOAP request (GetExternalIPAddress) from a WAN peer to port 5000 gets no answer. |
