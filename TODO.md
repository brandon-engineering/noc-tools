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

## Before making the repo public

- [ ] Decide on the `<Carrier>` / `<circuit-id>` placeholders — fill
      with a lab-safe example or leave generic.

Screenshots (Discord alert, Grafana IRM) were considered and dropped —
the inline CLI example carries the tool well enough. The "What a run
looks like" block stays as a labelled representative example.
