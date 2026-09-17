import subprocess
import requests
import re
import os
import time
from typing import Optional, Tuple

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
IRM_WEBHOOK_URL = os.environ.get("IRM_WEBHOOK_URL")
LOGFILE = "/var/log/network-devices.log"

if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL environment variable not set")

# Hosts that should also fire an alert into Grafana IRM (in addition to
# Discord), for practicing the acknowledge/escalate workflow. Currently
# scoped to just the edge routers - expand this set as needed.
IRM_ALERT_HOSTS = {"EdgeR1", "EdgeR2", "R1", "R2", "DSW1", "DSW2", "ASW1", "ASW2", "NUS1"}

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
    ("R1", "GigabitEthernet0/0"): "R1 uplink to EdgeR1 is affected - verify WAN path.",
    ("R2", "GigabitEthernet0/0"): "R2 uplink to EdgeR2 is affected - verify WAN path.",
    ("R1", "GigabitEthernet0/1"): "R1 link to DSW1 is affected - VRRP should be handling failover, verify R2 is active.",
    ("R2", "GigabitEthernet0/1"): "R2 link to DSW2 is affected - VRRP should be handling failover, verify R1 is active.",
}

# Interface pairs that are actually two ends of the SAME physical
# link, not two independent events - a real link failure (as opposed
# to a one-sided port fault) makes both ends log a state change within
# moments of each other, which would otherwise become two separate
# alerts (or with snmp_poll.py also running against the same devices,
# four) for one underlying failure. Each entry maps one side to the
# other, a canonical link_id shared by both sides (so both keys
# de-duplicate into the same alert regardless of which side's syslog
# line arrives first), a `label` naming the protocol relationship this
# link is actually monitored by (used as both the message prefix and
# the Grafana title - see build_link_message), and the ready-to-run
# noc_check.py command (either device works - the check queries both
# ends' state either way).
LINK_PAIR_MAP = {
    ("DSW1", "GigabitEthernet0/3"): {
        "peer_host": "DSW2",
        "peer_interface": "GigabitEthernet0/3",
        "link_id": "DSW1-DSW2-backbone",
        "label": "OSPF neighbor",
        "detail": "DSW1 <-> DSW2 adjacency down - check backbone link Gi0/3.",
        "noc_check_command": "noccheck MemberA DSW1 GigabitEthernet0/3",
    },
    ("DSW2", "GigabitEthernet0/3"): {
        "peer_host": "DSW1",
        "peer_interface": "GigabitEthernet0/3",
        "link_id": "DSW1-DSW2-backbone",
        "label": "OSPF neighbor",
        "detail": "DSW1 <-> DSW2 adjacency down - check backbone link Gi0/3.",
        "noc_check_command": "noccheck MemberA DSW1 GigabitEthernet0/3",
    },
    ("EdgeR1", "GigabitEthernet0/2"): {
        "peer_host": "EdgeR2",
        "peer_interface": "GigabitEthernet0/2",
        "link_id": "EdgeR1-EdgeR2-backbone",
        "label": "iBGP session",
        "detail": "EdgeR1 <-> EdgeR2 iBGP session down - check backbone link Gi0/2.",
        "noc_check_command": "noccheck MemberA EdgeR1 GigabitEthernet0/2",
    },
    ("EdgeR2", "GigabitEthernet0/2"): {
        "peer_host": "EdgeR1",
        "peer_interface": "GigabitEthernet0/2",
        "link_id": "EdgeR1-EdgeR2-backbone",
        "label": "iBGP session",
        "detail": "EdgeR1 <-> EdgeR2 iBGP session down - check backbone link Gi0/2.",
        "noc_check_command": "noccheck MemberA EdgeR1 GigabitEthernet0/2",
    },
    ("EdgeR1", "GigabitEthernet0/1"): {
        "peer_host": "DSW1",
        "peer_interface": "GigabitEthernet0/0",
        "link_id": "EdgeR1-DSW1-uplink",
        "label": "OSPF neighbor",
        "detail": "EdgeR1 <-> DSW1 adjacency down - check uplink Gi0/1 (EdgeR1) / Gi0/0 (DSW1).",
        "noc_check_command": "noccheck MemberA EdgeR1 GigabitEthernet0/1",
    },
    ("DSW1", "GigabitEthernet0/0"): {
        "peer_host": "EdgeR1",
        "peer_interface": "GigabitEthernet0/1",
        "link_id": "EdgeR1-DSW1-uplink",
        "label": "OSPF neighbor",
        "detail": "EdgeR1 <-> DSW1 adjacency down - check uplink Gi0/1 (EdgeR1) / Gi0/0 (DSW1).",
        "noc_check_command": "noccheck MemberA EdgeR1 GigabitEthernet0/1",
    },
    ("EdgeR2", "GigabitEthernet0/1"): {
        "peer_host": "DSW2",
        "peer_interface": "GigabitEthernet0/0",
        "link_id": "EdgeR2-DSW2-uplink",
        "label": "OSPF neighbor",
        "detail": "EdgeR2 <-> DSW2 adjacency down - check uplink Gi0/1 (EdgeR2) / Gi0/0 (DSW2).",
        "noc_check_command": "noccheck MemberA EdgeR2 GigabitEthernet0/1",
    },
    ("DSW2", "GigabitEthernet0/0"): {
        "peer_host": "EdgeR2",
        "peer_interface": "GigabitEthernet0/1",
        "link_id": "EdgeR2-DSW2-uplink",
        "label": "OSPF neighbor",
        "detail": "EdgeR2 <-> DSW2 adjacency down - check uplink Gi0/1 (EdgeR2) / Gi0/0 (DSW2).",
        "noc_check_command": "noccheck MemberA EdgeR2 GigabitEthernet0/1",
    },
}

# How long to suppress a second alert for the same link_id + state -
# long enough to absorb the peer device's matching syslog line
# arriving a few seconds later, short enough not to swallow a genuine
# second failure minutes later.
LINK_SUPPRESS_WINDOW_SECONDS = 15
_recent_link_alerts = {}  # link_id -> (state, timestamp)


def already_alerted_for_link(link_id: str, state: str) -> bool:
    """True if this exact link_id + state was already alerted on
    within the suppression window - folds the peer device's matching
    event for the same physical failure into a no-op instead of a
    duplicate alert. Records this call as the latest alert otherwise,
    so the next call (from either side) starts a fresh window."""
    last = _recent_link_alerts.get(link_id)
    if last and last[0] == state and (time.time() - last[1]) < LINK_SUPPRESS_WINDOW_SECONDS:
        return True
    _recent_link_alerts[link_id] = (state, time.time())
    return False


def build_link_message(pair: dict, state: str) -> str:
    """Single consolidated message for a link defined in
    LINK_PAIR_MAP, representing both physical ends as one logical
    event instead of two per-device interface events."""
    icon = "🔴" if state == "down" else "🟢"
    if state == "down":
        return f"{icon} {pair['label']} down: {pair['detail']}"
    return f"{icon} {pair['label']} RECOVERED: {pair['detail']} (previously flagged as down)."


def build_message(line: str) -> Tuple[str, Optional[str]]:
    """Return a friendly, enriched Discord message if we recognize the
    device/interface; otherwise fall back to the original raw format.
    Also returns the parsed hostname (or None if unparsed), so the
    caller can decide whether this event should also go to IRM."""
    match = PARSE_PATTERN.search(line)
    if not match:
        return f"⚠️ Interface event: {line.strip()}", None

    host = match.group("host")
    interface = match.group("interface")
    state = match.group("state").lower()

    friendly = DEVICE_INTERFACE_MAP.get((host, interface))

    state_icon = "🔴" if state == "down" else "🟢"

    if friendly and state == "down":
        return f"{state_icon} {friendly}\nDevice: {host} | Interface: {interface} | State: DOWN", host
    elif friendly and state == "up":
        return f"{state_icon} {host} {interface} has RECOVERED (previously flagged as down).\nDevice: {host} | Interface: {interface} | State: UP", host
    elif state == "down":
        return f"{state_icon} Interface event: {host} {interface} changed state to DOWN", host
    else:
        # Deliberately says "RECOVERED", not just "changed state to UP" -
        # Grafana OnCall's default webhook template appears to key off
        # that word (or similar) to decide whether an incoming post with
        # a matching alert_key should auto-resolve the existing alert
        # group. Confirmed live 2026-09-11 via snmp_poll.py hitting the
        # identical branch: a mapped interface's "RECOVERED" wording
        # auto-resolved in Grafana, this un-mapped fallback's plain
        # "changed state to UP" did not. Every interface needs this
        # wording, not just the ones in DEVICE_INTERFACE_MAP.
        return f"{state_icon} {host} {interface} has RECOVERED (previously flagged as down).\nDevice: {host} | Interface: {interface} | State: UP", host


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
        match = PARSE_PATTERN.search(line)
        pair = LINK_PAIR_MAP.get((match.group("host"), match.group("interface"))) if match else None

        if pair:
            state = match.group("state").lower()
            if already_alerted_for_link(pair["link_id"], state):
                continue  # peer device's matching event for this same link - already alerted
            message = build_link_message(pair, state)
            requests.post(WEBHOOK_URL, json={"content": message})
            if IRM_WEBHOOK_URL:
                try:
                    requests.post(
                        IRM_WEBHOOK_URL,
                        json={
                            "title": f"{pair['label']} DOWN" if state == "down" else f"{pair['label']} RECOVERED",
                            "message": message,
                            "alert_key": f"link-{pair['link_id']}",
                            "noc_check_command": pair["noc_check_command"],
                        },
                        timeout=5,
                    )
                except requests.exceptions.RequestException:
                    # Don't let an IRM delivery failure interrupt Discord
                    # alerting, which is the primary/already-proven channel.
                    pass
            continue

        message, host = build_message(line)
        requests.post(WEBHOOK_URL, json={"content": message})

        if IRM_WEBHOOK_URL and host in IRM_ALERT_HOSTS:
            try:
                interface = match.group("interface") if match else "unknown"
                alert_key = f"{host}-{interface}"
                noc_check_command = f"noccheck MemberA {host} {interface}"
                requests.post(
                    IRM_WEBHOOK_URL,
                    json={
                        "message": message,
                        "alert_key": alert_key,
                        "noc_check_command": noc_check_command,
                    },
                    timeout=5,
                )
            except requests.exceptions.RequestException:
                # Don't let an IRM delivery failure interrupt Discord
                # alerting, which is the primary/already-proven channel.
                pass