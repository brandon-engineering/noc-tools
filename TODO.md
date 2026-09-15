# TODO

## noc_check.py — features

- [ ] **OSPF/BGP adjacency checks for internal links.** EdgeR1/EdgeR2's
      backbone links (Gi0/1 to DSW1/DSW2, Gi0/2 to each other) and
      their eBGP links to R1/R2 (Gi0/3), plus DSW1/DSW2's uplinks to
      the edge routers (Gi0/0), have no interface-specific logic yet -
      they get only the generic status/log/transceiver output. Add an
      OSPF-neighbor-style check (parallel to `VRRP_CHECK_ON_DOWN`) once
      BGP/OSPF monitoring is actually wanted; not needed yet since NOC
      alerting today is escalate-to-engineer once a link is correctly
      labeled, not deep protocol diagnosis. Requested 2026-09-14.

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
