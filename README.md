# NOC Tools

A network monitoring workflow for Cisco IOS environments, running in
production as `systemd` services. Alerting and investigation are kept
as separate layers, and alerting itself has **two independent
mechanisms you can run one of, or both**:

- **Layer 1a — `netalert.py` (event-driven, syslog push):** watches
  centralised syslog, recognises interface up/down events the instant
  a device logs one, enriches them with fixed context (which link is
  a WAN uplink, which is VRRP-protected), and posts to Discord (every
  event) and Grafana IRM (core devices, for the ack/escalate
  workflow).
- **Layer 1b — `snmp_poll.py` (timer-driven, SNMP pull):** doesn't
  wait to be told anything — polls every device's reachability and
  every interface's operational state on a timer instead. Same alert
  destinations and message style as `netalert.py`. Structurally
  catches what event-driven alerting can't: a device that goes fully
  unreachable, or fails to deliver syslog at all, never generates an
  event for `netalert.py` to react to, but a poller notices on its
  next cycle regardless.
- **Layer 2 — `noc_check.py`:** an on-demand CLI that runs from a jump
  host / central controller. Given the device and interface an alert
  named, it SSHes to that device, runs the relevant show commands
  against that interface, and parses the raw output into a plain
  reading — interface status, description, time-in-state, optical
  levels, recent flap history, VRRP role, a neighbour-sourced
  reachability ping, the carrier/circuit ID for known WAN links — and a
  plain-language next step. Written for a NOC technician, or anyone who
  can't (or shouldn't have to) interpret raw IOS output. What it checks
  and how it maps interfaces to circuits and next steps is tunable in
  dictionaries at the top of `noc_check.py`. Both alerting layers hand
  it a ready-to-run command as part of their alert.

## Why two alerting mechanisms, and why investigation stays separate

Push and pull each have a blind spot the other doesn't. Event-driven
alerting (`netalert.py`) is instant but depends on the device
successfully logging and delivering the event — if a device can't
reach its syslog target (a misconfigured port, a routing change, the
device itself going dark), there's nothing to react to and no alert
fires at all. Timer-driven polling (`snmp_poll.py`) doesn't depend on
the device volunteering anything — it asks directly — but it can only
notice a change on its next cycle, not the instant it happens. Running
both means each covers the other's gap; running either alone is still
a complete, working pipeline on its own.

Investigation is kept separate from *both* of them for a different
reason: fast alerting can't block on SSH, and accurate investigation
can't trust a snapshot from whenever the alert fired minutes ago.
Keeping `noc_check.py` as its own on-demand layer means an outage in
alerting never takes out the ability to investigate, and investigation
advice is always generated from the device's live state at the moment
a technician actually asks.

**Design rationale and the deeper SNMP-monitoring roadmap:
[`WRITEUP.md`](WRITEUP.md).**

---

## Requirements

- Python 3.8+
- A Cisco IOS device configured to send syslog to a host running
  `netalert.py` (only if using that layer)
- SNMP read access (`snmp-server community <string> RO` or SNMPv3)
  on any device `snmp_poll.py` will watch, and the `net-snmp` package
  installed on the host running it (`snmpget`/`snmpwalk` - `sudo apt
  install snmp` / `sudo pacman -S net-snmp` / equivalent) (only if
  using that layer)
- SSH access to devices you want to investigate with `noc_check.py`,
  and a per-site Ansible inventory (`inventory/<site>.yml`) it can read
  addresses from — this same inventory is also what `snmp_poll.py`
  reads its device list from, so one file serves both

Install Python dependencies:

```bash
pip install netmiko pyyaml requests --break-system-packages
```

(Drop `--break-system-packages` if you're using a virtual environment
instead. `snmp_poll.py` doesn't need `netmiko` - it shells out to
`snmpget`/`snmpwalk` rather than making an SSH connection.)

---

## netalert.py

Watches a local syslog file for interface up/down events and posts an
alert to a Discord webhook. Includes an optional `DEVICE_INTERFACE_MAP`
for friendly, specific messages on interfaces that matter most (WAN
uplinks, core links) - anything not in the map still gets a clean,
generic alert, so nothing silently fails to notify.

### Setup

1. Point your devices' syslog at the host running this script:
   ```
   logging host <this-host-ip>
   logging trap informational
   logging origin-id hostname
   ```
   `logging origin-id hostname` is required - without it, some IOS
   platforms omit the hostname from syslog messages entirely, and the
   alert will show a sequence number instead of a real device name.

2. Set your Discord webhook as an environment variable - **do not**
   hardcode it in the script or commit it to Git:
   ```bash
   export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
   ```

   For a persistent/production setup via systemd, use the included
   `netalert.service` template:
   ```bash
   sudo cp netalert.service /etc/systemd/system/netalert.service
   sudo nano /etc/systemd/system/netalert.service
   # Set REPLACE_WITH_YOUR_REAL_WEBHOOK_URL, REPLACE_WITH_SERVICE_USER,
   # and the ExecStart path to wherever you cloned this repo.
   sudo systemctl daemon-reload
   sudo systemctl enable --now netalert.service
   ```
   Never edit and commit your real webhook into `netalert.service`
   itself - it stays local to the machine running the service, same
   as the environment-variable approach above.

3. Edit `LOGFILE` at the top of the script to match where your syslog
   messages actually land (e.g. `/var/log/network-devices.log`).

4. Edit `DEVICE_INTERFACE_MAP` to add friendly messages for the
   interfaces that matter most in your environment. Anything not
   listed still alerts, just with a generic message.

### Run

```bash
python3 netalert.py
```

Runs continuously, tailing the log file and posting to Discord as
events occur. Intended to run as a systemd service for production use.

**Full line-by-line walkthrough (no prior Python experience assumed):
[`netalert_explained.md`](netalert_explained.md).**

---

## snmp_poll.py

Doesn't wait for a device to log anything — polls every device's
reachability and every interface's operational state on a timer
(`IF-MIB::ifOperStatus` via SNMP), and posts an alert on any change.
Same alert destinations, same message style, and the same
`DEVICE_INTERFACE_MAP` concept as `netalert.py` (kept as a separate
copy in this file rather than shared — see the walkthrough for why).
Deliberately meant to run somewhere **outside** the network it's
watching where practical — if the thing doing the alerting is also the
thing that goes dark, it can't tell you it went dark.

### Setup

1. Make sure every device you want to watch actually has SNMP read
   access configured:
   ```
   snmp-server community public RO
   ```
   (Use a non-default community string, or SNMPv3, if this will ever
   be reachable from anywhere less trusted than a lab behind a VPN.)
   It's easy for this step to look done when it isn't — a device can
   have an SNMP agent that responds to *some* things (traps, if
   configured) while still having no read community at all, which
   looks identical to a tooling problem until you check the device's
   actual config directly.

2. Confirm `net-snmp` is installed on the host that will run this
   script (`which snmpget snmpwalk`) — install it if not
   (`sudo apt install snmp`, `sudo pacman -S net-snmp`, or your
   distro's equivalent).

3. Set your Discord webhook (required) and Grafana IRM webhook
   (optional) as environment variables — same rule as `netalert.py`:
   **do not** hardcode either one in the script or commit it to Git:
   ```bash
   export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
   export IRM_WEBHOOK_URL="https://<region>.grafana.net/oncall/integrations/v1/webhook/..."
   ```
   `IRM_WEBHOOK_URL` must be the Grafana IRM/OnCall **integration's**
   inbound webhook URL — an endpoint Grafana gives you to paste into
   other systems, found in Grafana Cloud under Alerts & IRM → IRM →
   Integrations → the integration's "how to connect" details. It is
   **not** a Discord webhook URL for some other channel — those are
   opposite-direction endpoints and posting to one will never reach
   Grafana. If alerts reach Discord but never Grafana, test the IRM
   URL directly first, independent of this script or any Python code
   at all:
   ```bash
   curl -sv -X POST -H "Content-Type: application/json" \
     -d '{"message":"test","alert_key":"curl-test"}' \
     "<your IRM integration url>"
   ```
   A `2xx` response confirms the URL itself is valid; a
   `403 Integration key was not found` means the URL is wrong,
   incomplete (these can be long and easy to truncate when copying),
   or the integration was deleted/regenerated.

4. Optional environment variables:
   ```bash
   export SNMP_COMMUNITY="public"      # default shown
   export SNMP_POLL_INTERVAL="60"      # seconds between polls, default shown
   ```

5. Edit `DEVICE_INTERFACE_MAP` at the top of the script, same as
   `netalert.py`'s copy — anything not listed still alerts, just with
   a generic message.

For a persistent/production setup via systemd, use the included
`snmp_poll.service` template the same way as `netalert.service`:
```bash
sudo cp snmp_poll.service /etc/systemd/system/snmp_poll.service
sudo nano /etc/systemd/system/snmp_poll.service
# Set the real webhook URL(s), service user, working directory, and
# the ExecStart path to wherever you cloned this repo.
sudo systemctl daemon-reload
sudo systemctl enable --now snmp_poll.service
```

### Run

```bash
python3 snmp_poll.py [site]
```

Must be run from the same directory containing `inventory/` (or with
that as an ancestor directory) — it reads `inventory/<site>.yml`,
defaulting to a site named `MemberA` if none is given. Runs
continuously, polling on the configured interval and posting alerts as
state changes are detected. Intended to run as a systemd service for
production use, same as `netalert.py`.

**Full line-by-line walkthrough (no prior Python experience assumed):
[`snmp_poll_explained.md`](snmp_poll_explained.md).**

---

## noc_check.py

On-demand investigation tool, run from a jump host or central
controller. Give it the site, device, and interface an alert named; it
SSHes in, gathers live state for that interface, and turns the raw
output into a plain-language report a NOC technician can act on without
reading IOS directly.

For each run it reports:

- **Interface status and description** — current status / line
  protocol, parsed and labelled.
- **Recent flap history** — up/down events for that interface pulled
  from the device's own log buffer, most recent first.
- **Time in current state** — how long it has actually been up or down,
  computed from that history (and it flags the case where live state
  and the newest log entry disagree).
- **Optical levels** — Tx / Rx power for fibre interfaces; recognises
  copper and "no transceiver present" cleanly.
- **Redundancy status** — for interfaces flagged as VRRP-relevant (edge
  and core uplinks), runs `show vrrp brief` and says in plain language
  which router is currently active.
- **WAN reachability** — for known WAN interfaces, has a neighbour
  router ping the down interface over a dedicated static route, to
  prove whether it is really passing traffic or just misreporting.
- **A suggested next step**, plus the carrier / circuit ID to quote if
  the link is a known WAN circuit.

The raw `show` output is printed at the end as well, so nothing is
hidden from someone who wants to dig further.

### Scaling across sites

Device addresses come from **per-site Ansible inventory files** —
`inventory/<site>.yml` — not a list kept inside the script. One managed
site is one inventory file; adding a site is dropping in its inventory.
The `<site>` argument picks the file, and each client's device list
stays isolated from every other client's.

Interface-specific behaviour is configured in dictionaries at the top
of `noc_check.py`, all keyed by `(site, hostname, interface)`:

```python
WAN_CIRCUIT_MAP = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/0"): "WAN link to <Carrier> CID <circuit-id>",
}
VRRP_CHECK_ON_DOWN = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/0"): "WAN uplink down",
}
```

### Run

```bash
python3 noc_check.py <site> <hostname> <interface>
```

On the jump host it's wrapped in a one-word alias:

```bash
alias noccheck='python3 /opt/noc-tools/noc_check.py'
```

```bash
noccheck MemberA EdgeR1 GigabitEthernet0/0
```

Run it with no arguments to be prompted for site / device / interface
interactively. You're prompted for SSH credentials either way.

### What a run looks like

Representative output — a WAN interface found down/down, addresses
redacted:

```text
$ noccheck MemberA EdgeR1 GigabitEthernet0/0
Username: noc
Password:

Connecting to EdgeR1 (<redacted>) [MemberA]...

Site:      MemberA
Device:    EdgeR1
Interface: GigabitEthernet0/0
Status:    down
Protocol:  down

--- Recent interface history (this device's local log) ---
   Sep  8 13:42:11  DOWN
   Sep  8 09:15:03  UP
   Sep  7 22:50:47  DOWN
   Sep  7 22:49:31  UP

🔴 Interface is physically down (down/down).
   WAN link to <Carrier> CID <circuit-id>
   Contact carrier and reference the circuit ID above. Confirm
   cabling and local hardware first if accessible on-site.

--- Time in current state ---
   Interface has been DOWN for 2h 18m (since Sep  8 13:42:11).

--- Transceiver status ---
   Tx Power: -2.14 dBm | Rx Power: -40.00 dBm

--- Redundancy status (WAN uplink down) ---
   EdgeR1 is currently in STANDBY for all 1 redundancy group(s) checked -
   the other router is handling traffic instead. This appears to be
   working as intended.

--- Raw VRRP output ---
   [ show vrrp brief ]

--- WAN interface reachability check (via dedicated static route) ---
   EdgeR2 (sourced from its GigabitEthernet0/2) pinging WAN interface
   <redacted> via the dedicated static route: 0% success (0/5 replies) -
   the WAN interface itself is NOT reachable over the backbone either.
   Consistent with a genuine physical/link-layer failure, not just a
   reporting issue.

--- Raw show interface output ---
   [ show interface GigabitEthernet0/0 ]
```

### States it recognizes

| Status | Protocol | Meaning |
|---|---|---|
| up | up | Healthy |
| administratively down | down | Manually shut - confirm intent before changing |
| down | down | Physically down - check cabling/remote end/carrier |
| up | down | Link present, protocol not establishing - check encapsulation/duplex |

---

## Roadmap

- **Basic SNMP polling — done (`snmp_poll.py`).** Reachability and
  interface up/down state, on a timer, into the same alert path as
  `netalert.py`.
- **Deeper SNMP monitoring — still ahead.** `snmp_poll.py` today only
  asks "reachable?" and "up or down?" It doesn't yet poll interface
  error/discard counters, optical Tx/Rx power, utilization, or device
  health (CPU/memory), and there's no time-series backend yet — no way
  to ask "was this interface always this noisy, or did it start last
  Tuesday?" That's the harder, more valuable half of the original
  SNMP-polling idea: catching *slow degradation* that never crosses a
  hard up/down line and so never generates any event at all, on either
  the syslog or the reachability side. See [`WRITEUP.md`](WRITEUP.md)
  for the full design thinking.

## Built with AI assistance

The workflow design, the alert logic, and the operations are mine; the
Python implementation was AI-assisted. It runs in production and does a
real job.

---

## Security notes

- Never commit real webhook URLs, passwords, or credentials to this
  repo. `netalert.py` and `snmp_poll.py` both read every webhook
  (`DISCORD_WEBHOOK_URL`, `IRM_WEBHOOK_URL`) from environment
  variables for exactly this reason — never hardcode either one, in
  the script or in a filled-in copy of the `.service` templates.
- `noc_check.py` prompts for credentials interactively rather than
  storing them anywhere.
- `snmp_poll.py`'s `SNMP_COMMUNITY` defaults to the well-known string
  `"public"`. Fine behind a VPN in a lab; change it (or move to
  SNMPv3) before pointing this at anything less trusted.
- If you fork or adapt this for your own environment, double check
  `git diff` before committing to make sure nothing environment-specific
  or sensitive slipped in.

---

## Contributing

Pull requests welcome - particularly around:
- Additional interface state detection (error counters, duplex
  mismatches, etc.)
- A web front-end over `noc_check.py`'s existing logic
- Support for additional device platforms beyond Cisco IOS