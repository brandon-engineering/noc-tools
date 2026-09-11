# Building a two-layer network monitoring pipeline

## The problem

In a NOC, raw device syslog is noisy and context-free. A
`%LINEPROTO-5-UPDOWN` line tells you an interface changed state — it
doesn't tell you which customer, which uplink, whether redundancy
caught it, or what to do next. The common fix is to bake that context
into the alert when it fires. That ages badly: by the time a technician
opens the ticket the state may have changed, and the "suggested
action" was a guess made seconds after the event, not a reading of what
the device is doing right now.

## The design

Split the job into two layers that are deliberately kept apart:

```
                 ┌─────────────────────────────────────────┐
  network        │  rsyslog  →  /var/log/network-devices.log │
  devices  ─────▶│                    │                      │
 (syslog)        │                    ▼                      │
                 │   Layer 1:  netalert.py  (systemd, always on)
                 │     • tail -F the log, regex-match LINK / │
                 │       LINEPROTO UP/DOWN events            │
                 │     • enrich from a device/interface map  │
                 │       (which link is a WAN uplink, which  │
                 │       is VRRP-protected, etc.)            │
                 │     • post to Discord  (every event)      │
                 │     • post to Grafana IRM  (core devices  │
                 │       only — ack / escalate workflow)     │
                 └──────────────────┬──────────────────────┘
                                    │  alert names a device + interface
                                    ▼
                 ┌─────────────────────────────────────────┐
                 │   Layer 2:  noc_check.py                  │
                 │   (CLI on a jump host, run on demand)     │
                 │     • SSH to that device *now* (netmiko)  │
                 │     • show interface / show logging /     │
                 │       transceiver / show vrrp brief       │
                 │     • neighbour ping test over a          │
                 │       dedicated static route             │
                 │     • parse it all into plain status +    │
                 │       description + next step, with the   │
                 │       carrier / circuit ID for that link │
                 └─────────────────────────────────────────┘
```

- **Layer 1 (`netalert.py`)** stays fast and simple. Its only job is
  to notice an event, attach the context that never changes (this
  interface is EdgeR1's internet uplink; this one is VRRP-protected),
  and get a readable message in front of a human immediately. Anything
  not in the enrichment map still alerts — with a generic message —
  so nothing fails silently.
- **Layer 2 (`noc_check.py`)** runs from a jump host / central
  controller. A technician gives it the device and interface the alert
  named; it SSHes to that device, runs the relevant show commands
  against that interface, and turns the raw output into a plain
  reading — current status and description, how long the interface has
  held that state (computed from the device's own log buffer), optical
  Tx/Rx levels, recent flap history, VRRP master/standby in plain
  language, and a neighbour-sourced ping that proves whether the WAN
  interface is actually passing traffic — ending in a plain-language
  next step. If the link is a known WAN circuit it prints the carrier
  and circuit ID to reference on the escalation. It's written for
  someone who can't, or shouldn't have to, read raw IOS output; what it
  checks and how it maps interfaces to circuits and next steps lives in
  dictionaries at the top of the file.

## Why keep them separate

Fast alerting and accurate investigation have opposite requirements.
Alerting must never block or lag, so it can't go make SSH connections.
Investigation must be accurate, so it can't rely on a snapshot taken
minutes ago. Splitting them lets each do its job well, and it means an
alerting outage never takes down the ability to investigate, and vice
versa.

**Update:** Layer 1 is no longer just `netalert.py`. A second,
independent alerting mechanism (`snmp_poll.py`) now exists alongside
it — same reasoning about staying separate from investigation applies
to it too, but it solves a different problem than the syslog listener
does. See "What shipped since" below.

## How it runs

`netalert.py` runs as a `systemd` service, restarts on failure, and
waits for the syslog file to exist before tailing it (a real bug from
early on: on a freshly rebooted collector the service started before
rsyslog had created the file, and `tail` exited and never recovered).
Device IPs come from the existing Ansible inventory rather than a
second device list to maintain — one source of truth. Secrets (webhook
URLs) come from environment variables, never the code.

**Stack:** Python (`netmiko`, `requests`, `pyyaml`), rsyslog, Discord
webhooks, Grafana IRM, Ansible inventory, `systemd`.

## What it demonstrates

Alert coverage across every core and edge device; faster, more
consistent first-response triage; and a design decision — event-time
context vs. live investigation — made deliberately rather than by
default. The investigation output also doubles as a clean starting
point for the root-cause write-up.

## What shipped since — SNMP polling as a second alerting mechanism

The pipeline was originally **entirely event-driven**: it only reacted
to syslog messages, which means it only saw things a device bothered
to log and successfully deliver. That has a real, structural blind
spot beyond "slow degradation with no event" — it also means a device
that goes fully unreachable, or that fails to *deliver* an event it
tried to log (a syslog transport misconfiguration, a routing change,
the device itself going dark), generates no alert at all. There's
nothing for an event-driven listener to react to.

`snmp_poll.py` closes that specific gap: instead of waiting for a
device to say something, it asks — polling every device's reachability
and every interface's operational state on a timer, independent of
whatever the syslog pipeline is or isn't doing. It runs on a different
host than the syslog collector on purpose (a box outside the network
being watched, where practical), so the thing doing the alerting isn't
also the thing that might go dark. Same alert destinations as
`netalert.py` (Discord + Grafana IRM), same message conventions, and
it hands `noc_check.py` the same kind of ready-to-run investigation
command.

This shipped as the **first, narrower slice** of the original SNMP
idea below — reachability and up/down state, not yet the deeper
metrics. Two real bugs surfaced building and testing it against a live
fleet, each a useful lesson in its own right: a syslog transport
config that silently defaulted to the wrong TCP port (nothing failed
loudly — it just quietly never delivered anything, caught only by
deliberately testing the alerting path rather than trusting a clean
deploy), and a difference in message wording that determined whether
Grafana's own alert-matching logic would auto-resolve an alert or leave
it open forever after the underlying problem cleared.

## Still ahead — the deeper SNMP-monitoring vision

The original goal remains only partly built. `snmp_poll.py` today
answers "reachable?" and "up or down?" — it doesn't yet poll interface
error/discard counters, optical Tx/Rx power, utilization, or device
health (CPU/memory), and there's no time-series backend, so "was this
interface always this noisy, or did it start last Tuesday?" still
isn't answerable. That's the harder, more valuable half of the
original idea: catching a problem that's degrading for hours *before*
it crosses a hard up/down line and becomes an outage — the class of
failure that neither the syslog listener nor the current SNMP poller
can see yet.

## A note on how this was built

I'm a network engineer, not a software developer. The workflow design,
the alert logic, the enrichment model, and the operations are mine —
the decision to split alerting from investigation, what context
matters for which interface, how to prove a WAN link is really down.
The Python implementation was AI-assisted. The value is that it runs
in production and does a real job.
