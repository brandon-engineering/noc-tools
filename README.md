# NOC Tools

A two-layer network monitoring workflow for Cisco IOS environments,
running in production as a `systemd` service.

- **Layer 1 — `netalert.py`:** watches centralised syslog, recognises
  interface up/down events, enriches them with fixed context (which
  link is a WAN uplink, which is VRRP-protected), and posts to Discord
  (every event) and Grafana IRM (core devices, for the ack/escalate
  workflow).
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
  dictionaries at the top of `noc_check.py`.

## Why two layers

Fast alerting and accurate investigation have opposite requirements.
Alerting can't block on SSH; investigation can't trust a snapshot from
minutes ago. Keeping them separate lets each do its job, and an outage
in one never takes out the other. Investigation advice is generated
from the device's live state at the moment a technician asks — not
baked in when the alert fired.

**Design rationale and the SNMP-polling roadmap:
[`WRITEUP.md`](WRITEUP.md).**

---

## Requirements

- Python 3.8+
- A Cisco IOS device configured to send syslog to a host running
  `netalert.py`
- SSH access to devices you want to investigate with `noc_check.py`,
  and a per-site Ansible inventory (`inventory/<site>.yml`) it can read
  addresses from

Install dependencies:

```bash
pip install netmiko pyyaml requests --break-system-packages
```

(Drop `--break-system-packages` if you're using a virtual environment
instead.)

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

- **SNMP polling** alongside the syslog listener — poll interface
  errors/discards, optical power, utilisation, and device health on an
  interval, feed threshold crossings into the same alert path, and land
  the time series in a backend. Catches slow degradation that never
  generates a syslog event. See [`WRITEUP.md`](WRITEUP.md).

## Built with AI assistance

The workflow design, the alert logic, and the operations are mine; the
Python implementation was AI-assisted. It runs in production and does a
real job.

---

## Security notes

- Never commit real webhook URLs, passwords, or credentials to this
  repo. `netalert.py` reads its webhook from an environment variable
  for exactly this reason.
- `noc_check.py` prompts for credentials interactively rather than
  storing them anywhere.
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