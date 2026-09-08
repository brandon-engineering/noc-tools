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

<!-- TODO before publishing: add screenshots to docs/ and reference
them here — a real Discord alert, a noc_check.py run against a down
interface, and the Grafana IRM incident view. -->

---

## Requirements

- Python 3.8+
- A Cisco IOS device configured to send syslog to a host running
  `netalert.py`
- SSH access to devices you want to investigate with `noc_check.py`

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

On-demand investigation tool. Connects to a specific device, runs
`show interface <interface>`, and prints the live status/protocol
state along with a suggested next step.

Reads device IPs from an Ansible inventory file (`inventory/inventory.yml`,
relative to wherever you run the script from) rather than maintaining
a separate device list - point it at your existing Ansible inventory.

### Setup

Edit `WAN_CIRCUIT_MAP` to add real carrier/circuit info for known
WAN-facing interfaces, so a down/down result tells you exactly which
circuit to reference when escalating:

```python
WAN_CIRCUIT_MAP = {
    ("EdgeR1", "GigabitEthernet0/0"): "WAN link to <Carrier> CID <circuit-id>",
}
```

### Run

```bash
python3 noc_check.py <hostname> <interface>
```

Example:
```bash
python3 noc_check.py EdgeR1 GigabitEthernet0/0
```

You'll be prompted for SSH credentials. Output includes the raw
`show interface` text as well as the parsed summary and suggestion, so
nothing is hidden if you want to dig further manually.

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

The workflow design, alert logic, and operations are the author's; the
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