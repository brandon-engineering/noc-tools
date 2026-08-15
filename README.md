# NOC Tools

Lightweight tooling for network alerting and on-demand interface
investigation on Cisco IOS devices, built around a two-layer NOC
workflow:

1. **Fast, automatic alerting** (`netalert.py`) - watches syslog for
   interface state changes and posts minimal, unambiguous alerts to
   Discord the moment something changes.
2. **On-demand investigation** (`noc_check.py`) - a CLI tool that
   connects to any device, pulls live interface state, and suggests
   a plain-language next step based on what's actually happening
   right now (not a guess baked in at alert time).

Splitting these into two layers keeps alerts fast and keeps
investigation advice accurate, since it's generated from current
live state rather than a static assumption made when the alert
first fired.

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

   For a persistent/production setup via systemd, add it to the
   service file instead:
   ```ini
   [Service]
   Environment="DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/..."
   ExecStart=/usr/bin/python3 /path/to/netalert.py
   ```

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
