# TODO

## noc_check.py — features

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
