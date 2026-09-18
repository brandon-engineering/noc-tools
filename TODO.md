# TODO

## noc_check.py — features

- [x] **Auto-check the far end of a backbone link — DONE 2026-09-18.**
      Any interface defined in `OSPF_NEIGHBOR_CHECK_ON_DOWN` or
      `BGP_NEIGHBOR_CHECK_ON_DOWN` now prints a "Both sides of this
      link" section regardless of state, reverse-looking-up the peer's
      own interface name (`find_peer_interface`) and connecting to it
      with the same already-entered credentials
      (`check_peer_interface_status`) to show its Status/Protocol
      alongside this device's. Requested 2026-09-15.

- [x] **OSPF/BGP adjacency checks for the remaining internal links —
      DONE 2026-09-18.** `snmp_poll.py`'s old `LINK_PAIR_MAP`
      (interface-state-inferred protocol health) replaced entirely by
      `PROTOCOL_CHECKS` - direct SNMP polling of real BGP4-MIB
      `bgpPeerState` / OSPF-MIB `ospfNbrState`, covering the
      EdgeR1-EdgeR2 iBGP session and all three OSPF adjacencies
      (DSW1-DSW2 backbone, EdgeR1-DSW1 uplink, EdgeR2-DSW2 uplink).
      Found and fixed a real gap along the way: the old interface-pair
      logic only alerted cleanly when both ends transitioned in the
      same poll cycle, so a single-sided `shutdown` fell through to a
      generic interface message instead of "iBGP session down"/"OSPF
      neighbor down". Real protocol-state polling fixes this
      structurally - either side alone reporting non-Established/non-
      Full is real signal, no longer requires both sides to agree.

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
