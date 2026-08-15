import subprocess
import requests
import re
import os

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL environment variable not set")

LOGFILE = "/var/log/network-devices.log"

PATTERN = re.compile(r"%LINK-3-UPDOWN|%LINEPROTO-5-UPDOWN|%LINK-5-CHANGED")

# Pulls hostname, interface name, and up/down state out of a raw syslog line.
# Matches lines like:
#   ... ASW1: *Aug 12 04:47:18 EDT: %LINEPROTO-5-UPDOWN: Line protocol on
#   Interface GigabitEthernet0/3, changed state to up
PARSE_PATTERN = re.compile(
    r"(?P<host>[A-Za-z0-9_\-]+): \*?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r" +\d+ [\d:]+ \S+: %(?:LINK-3-UPDOWN|LINEPROTO-5-UPDOWN|LINK-5-CHANGED):"
    r".*?Interface (?P<interface>[\w/.]+),? ?(?:changed state to |is )?(?P<state>up|down)",
    re.IGNORECASE,
)

# Friendly-name lookup table for interfaces that matter most.
# Key: (hostname, interface) - both matched case-sensitively as they
# appear in the device's own syslog output.
# Add new entries here as you identify more interfaces worth calling out.
DEVICE_INTERFACE_MAP = {
    ("EdgeRouter", "eth0"): "Link to upstream provider is DOWN - contact carrier.",
    ("R1", "GigabitEthernet0/0"): "R1 uplink to EdgeRouter is affected - verify WAN path.",
    ("R2", "GigabitEthernet0/0"): "R2 uplink to EdgeRouter is affected - verify WAN path.",
    ("R1", "GigabitEthernet0/1"): "R1 link to DSW1 is affected - VRRP should be handling failover, verify R2 is active.",
    ("R2", "GigabitEthernet0/1"): "R2 link to DSW2 is affected - VRRP should be handling failover, verify R1 is active.",
    ("DSW1", "GigabitEthernet0/0"): "DSW1 uplink to R1 is affected - core routing path impacted.",
    ("DSW2", "GigabitEthernet0/0"): "DSW2 uplink to R2 is affected - core routing path impacted.",
}


def build_message(line: str) -> str:
    """Return a friendly, enriched Discord message if we recognize the
    device/interface; otherwise fall back to the original raw format."""
    match = PARSE_PATTERN.search(line)
    if not match:
        return f"⚠️ Interface event: {line.strip()}"

    host = match.group("host")
    interface = match.group("interface")
    state = match.group("state").lower()

    friendly = DEVICE_INTERFACE_MAP.get((host, interface))

    state_icon = "🔴" if state == "down" else "🟢"

    if friendly and state == "down":
        return f"{state_icon} {friendly}\nDevice: {host} | Interface: {interface} | State: DOWN"
    elif friendly and state == "up":
        return f"{state_icon} {host} {interface} has RECOVERED (previously flagged as down).\nDevice: {host} | Interface: {interface} | State: UP"
    else:
        # Known pattern, unmapped interface - still cleaner than raw, but
        # generic since we don't have a friendly description for it yet.
        return f"{state_icon} Interface event: {host} {interface} changed state to {state.upper()}"


proc = subprocess.Popen(["tail", "-F", LOGFILE], stdout=subprocess.PIPE, text=True)
for line in proc.stdout:
    if PATTERN.search(line):
        message = build_message(line)
        requests.post(WEBHOOK_URL, json={"content": message})