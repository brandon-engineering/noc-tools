# TODO

## noc_check.py — features

- [ ] **Auto-check the far end of a backbone link.** When a link in
      `OSPF_NEIGHBOR_CHECK_ON_DOWN` (DSW1/DSW2 Gi0/3) or
      `BGP_NEIGHBOR_CHECK_ON_DOWN` (EdgeR1/EdgeR2 Gi0/2) comes back
      down/down, also connect to the `peer` device and check its
      corresponding interface state directly - same idea as
      `NEIGHBOR_PING_CHECK` for WAN links, but checking the actual
      other end of the same physical link instead of a third device.
      Lets a single check (e.g. `noc_check.py MemberA DSW1
      GigabitEthernet0/3`) tell a tech whether this is a local port
      failure or the whole link is down on both sides, without a
      second manual check. Requested 2026-09-15.

- [ ] **OSPF/BGP adjacency checks for the remaining internal links.**
      EdgeR1/EdgeR2's backbone link to DSW1/DSW2 (Gi0/1) and their eBGP
      links to R1/R2 (Gi0/3), plus DSW1/DSW2's uplinks to the edge
      routers (Gi0/0), still have no interface-specific logic. DSW1/
      DSW2's Gi0/3 (OSPF, `OSPF_NEIGHBOR_CHECK_ON_DOWN`) and EdgeR1/
      EdgeR2's Gi0/2 (iBGP, `BGP_NEIGHBOR_CHECK_ON_DOWN`, plus the
      matching `LINK_PAIR_MAP` entry in netalert.py/snmp_poll.py for
      one consolidated alert) are both done as of 2026-09-15. Extend
      the same pattern to the rest once BGP/OSPF monitoring is wanted
      more broadly; not needed yet since NOC alerting today is
      escalate-to-engineer once a link is correctly labeled, not deep
      protocol diagnosis. Requested 2026-09-14.

- [ ] **Speed and duplex in the plain-text output.** Parse speed
      (e.g. `1000Mb/s`, `10Gb/s`, `Auto`) and duplex (`Full`, `Half`,
      `Auto`) out of the `show interface` text this tool already
      collects, and print them in the human-readable summary — near the
      Status / Protocol lines, or in a short `--- Interface details ---`
      block. Nice-to-have: call out a likely mismatch (half-duplex on a
      gig link, or a speed that negotiated lower than expected).
      Requested 2026-09-08.

## Housekeeping

- [ ] `<Carrier>` / `<circuit-id>` in `README.md` / `noc_check.py` are
      left as generic placeholders — fine as-is; revisit only if a
      concrete example reads better.

Repo made public 2026-09-09. `lab_failover_explained.md` was removed
first (it explained a script not in the repo and carried lab
addressing). Screenshots (Discord, Grafana IRM) were considered and
dropped — the inline CLI example carries the tool.
