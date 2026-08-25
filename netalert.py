import subprocess
import requests
import re
import os
import time

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
LOGFILE = "/var/log/network-devices.log"

if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL environment variable not set")

PATTERN = re.compile(r"%LINK-3-UPDOWN|%LINEPROTO-5-UPDOWN|%LINK-5-CHANGED")

# Pulls hostname, interface name, and up/down state out of a raw syslog line.
# Matches lines like:
#   ... ASW1: *Aug 12 04:47:18 EDT: %LINEPROTO-5-UPDOWN: Line protocol on
#   Interface GigabitEthernet0/3, changed state to up
# The [\*\.]? matches either a leading asterisk (clock NTP-synchronized)
# or a period (clock not yet synchronized) - IOS uses both depending on
# sync state, and a fresh/rebuilt device may log with a period before
# NTP stabilizes. Only matching asterisk caused those lines to fail
# parsing and fall through to the raw/unformatted message.
PARSE_PATTERN = re.compile(
    r"(?P<host>[A-Za-z0-9_\-]+): [\*\.]?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r" +\d+ [\d:]+ \S+: %(?:LINK-3-UPDOWN|LINEPROTO-5-UPDOWN|LINK-5-CHANGED):"
    r".*?Interface (?P<interface>[\w/.]+),? ?(?:changed state to |is )?(?P<state>up|down)",
    re.IGNORECASE,
)

# Friendly-name lookup table for interfaces that matter most.
# Updated to reflect the current topology: EdgeR1/EdgeR2 (direct to
# unmanaged switch/Excon, no ISP-A/ISP-B or BGP), R1/R2 uplinks to
# their respective EdgeRouter, and R1/R2's VRRP-enabled distribution
# links (now trunked across VLAN 10/20/30/99, not a single link to
# one switch).
DEVICE_INTERFACE_MAP = {
    ("EdgeR1", "GigabitEthernet0/0"): "EdgeR1 internet uplink is affected - verify ISP/WAN path.",
    ("EdgeR2", "GigabitEthernet0/0"): "EdgeR2 internet uplink is affected - verify ISP/WAN path.",
    ("EdgeR1", "GigabitEthernet0/2"): "EdgeR1-EdgeR2 backbone link is affected - VRRP failover in use, verify EdgeR2.",
    ("EdgeR2", "GigabitEthernet0/2"): "EdgeR1-EdgeR2 backbone link is affected - VRRP failover in use, verify EdgeR1.",
    ("R1", "GigabitEthernet0/0"): "R1 uplink to EdgeR1 is affected - verify WAN path.",
    ("R2", "GigabitEthernet0/0"): "R2 uplink to EdgeR2 is affected - verify WAN path.",
    ("R1", "GigabitEthernet0/1"): "R1 link to DSW1 is affected - VRRP should be handling failover, verify R2 is active.",
    ("R2", "GigabitEthernet0/1"): "R2 link to DSW2 is affected - VRRP should be handling failover, verify R1 is active.",
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
        return f"{state_icon} Interface event: {host} {interface} changed state to {state.upper()}"


def wait_for_logfile(path: str, check_interval_seconds: int = 5) -> None:
    """Block until the log file exists. rsyslog only creates this file
    once it receives its first matching message - on a freshly booted
    or rebuilt NUS1, this service can start before that's happened.
    Without this wait, the tail -F subprocess launched below fails
    immediately at startup and never recovers, even after the file
    appears later - this was a real, recurring issue after lab/host
    restarts."""
    while not os.path.exists(path):
        time.sleep(check_interval_seconds)


wait_for_logfile(LOGFILE)

proc = subprocess.Popen(["tail", "-F", LOGFILE], stdout=subprocess.PIPE, text=True)
for line in proc.stdout:
    if PATTERN.search(line):
        message = build_message(line)
        requests.post(WEBHOOK_URL, json={"content": message})
