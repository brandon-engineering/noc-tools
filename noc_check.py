#!/usr/bin/env python3
"""
noc_check.py

On-demand NOC investigation tool. Connects to a specific device at a
specific client site, gathers live interface data - status/protocol,
optical transceiver levels, up/downtime duration, and recent flap
history from the device's own log buffer - and prints a plain-
language suggested next step based on what's actually happening right
now, not a guess baked in at alert time.

Usage:
    python3 noc_check.py <site> <device> <interface>

Example:
    python3 noc_check.py MemberA EdgeR1 GigabitEthernet0/0

Or run with no arguments to be prompted interactively.

Each client site has its own separate inventory file, at
inventory/<site>.yml - keeps each client's device list isolated from
every other client's, and avoids any risk of one client's config
affecting another's. <site> on the command line is just that
filename, without the .yml extension.
"""

import sys
import os
import re
import getpass
import yaml
from datetime import datetime
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

INVENTORY_DIR = "inventory"

# Circuit/carrier info for known WAN-facing interfaces. Add entries
# here as real circuits are identified, keyed by (site, hostname,
# interface). Used to make the down/down suggestion specific and
# actionable - exactly which carrier/circuit to reference when
# escalating.
WAN_CIRCUIT_MAP = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/0"): "WAN link to ATT CID -test.203203.ATT",
    ("MemberA", "EdgeR2", "GigabitEthernet0/0"): "WAN link to ATT CID -test.203203.ATT",
}

# Interfaces where a down/down result should also trigger a live
# "show vrrp brief" check on the SAME device, to report which router
# is currently master in plain language. Each entry maps to a short
# context note explaining what this specific link's failure means.
#
# Currently empty - no interface in this topology maps cleanly to a
# VRRP check yet:
# - EdgeR1/EdgeR2 Gi0/0 (outside) DOES run VRRP (vrrp_vip 10.10.70.14,
#   per inventory/MemberA.yml), but per design discussion, EdgeR1/
#   EdgeR2 -> unmanaged switch -> single Excon is a lab stand-in for
#   two SEPARATE ISP circuits, one per router - not a real redundant
#   pair. VRRP failover between them isn't a meaningful signal here,
#   so it's deliberately not monitored. Their Gi0/2 backbone link's
#   real redundancy signal is the iBGP session below instead -
#   BGP_NEIGHBOR_CHECK_ON_DOWN.
# - DSW1/DSW2 DO run real VRRP (area-1 VLANs 10/30/99), but on SVIs
#   reached via the trunk to the access switches, not on their Gi0/3
#   backbone link or Gi0/0 uplink - neither physical interface is a
#   valid trigger for it. That backbone link's own protocol
#   relationship is OSPF - see OSPF_NEIGHBOR_CHECK_ON_DOWN below.
#
# Mechanism kept (not deleted) since a real per-VLAN-SVI VRRP check
# for DSW1/DSW2 is a plausible near-term addition.
VRRP_CHECK_ON_DOWN = {}

# Interfaces where a down/down result should also trigger a live
# "show ip ospf neighbor" check on the SAME device, to report whether
# a specific peer is still an OSPF neighbor. Unlike VRRP, downing one
# of these links has a direct 1:1 effect: the two devices stop being
# OSPF neighbors over it, full stop - there's no "master/backup" state
# to report, just whether the adjacency in `peer_router_id` still
# shows up in the neighbor table.
#
# - DSW1/DSW2 Gi0/3 - the OSPF backbone point-to-point link between
#   them (10.20.1.6/10.20.1.7, per inventory/MemberA.yml). peer_router_id
#   is the peer's OSPF RID, which in this lab matches its loopback.
# - EdgeR1 Gi0/1 <-> DSW1 Gi0/0, and EdgeR2 Gi0/1 <-> DSW2 Gi0/0 - the
#   area0_links/uplink point-to-point pairs joining each edge router
#   to its distribution switch (10.20.1.0/10.20.1.1 and
#   10.20.1.2/10.20.1.3, per inventory/MemberA.yml).
OSPF_NEIGHBOR_CHECK_ON_DOWN = {
    ("MemberA", "DSW1", "GigabitEthernet0/3"): {"peer": "DSW2", "peer_router_id": "10.20.0.6"},
    ("MemberA", "DSW2", "GigabitEthernet0/3"): {"peer": "DSW1", "peer_router_id": "10.20.0.5"},
    ("MemberA", "EdgeR1", "GigabitEthernet0/1"): {"peer": "DSW1", "peer_router_id": "10.20.0.5"},
    ("MemberA", "DSW1", "GigabitEthernet0/0"): {"peer": "EdgeR1", "peer_router_id": "10.20.0.1"},
    ("MemberA", "EdgeR2", "GigabitEthernet0/1"): {"peer": "DSW2", "peer_router_id": "10.20.0.6"},
    ("MemberA", "DSW2", "GigabitEthernet0/0"): {"peer": "EdgeR2", "peer_router_id": "10.20.0.2"},
}

# Interfaces where a down/down result should also trigger a live
# "show ip bgp summary" check on the SAME device, to report whether a
# specific iBGP peer is still Established. Same 1:1-effect reasoning
# as OSPF_NEIGHBOR_CHECK_ON_DOWN above, just for BGP's session state
# instead of an OSPF adjacency.
#
# - EdgeR1/EdgeR2 Gi0/2 - the same physical backbone link OSPF area 0
#   already rides, but the chosen redundancy-check signal for this
#   pair is the iBGP session configured over it (bgp/edge_ibgp_backbone.yml,
#   added 2026-09-15), not OSPF - see the VRRP_CHECK_ON_DOWN comment
#   above for why VRRP was ruled out. peer_ip is the peer's Gi0/2
#   address (the iBGP session is peered directly over this link, not
#   the loopback - see ibgp: in inventory/MemberA.yml).
BGP_NEIGHBOR_CHECK_ON_DOWN = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/2"): {"peer": "EdgeR2", "peer_ip": "10.20.1.5"},
    ("MemberA", "EdgeR2", "GigabitEthernet0/2"): {"peer": "EdgeR1", "peer_ip": "10.20.1.4"},
}

# Maps a specific WAN-facing interface (site, hostname, interface) to
# a neighbor device that reaches it via a dedicated static route over
# the direct backbone link (Gi0/2) - NOT via OSPF (the outside subnet
# is deliberately never advertised into Area 0) and NOT via any
# shared outside segment (in this lab, EdgeR1/EdgeR2 happen to share
# unmanaged-switch-0 as a stand-in for two separate real ISP
# circuits, but that's a lab convenience, not something to rely on).
#
# Purpose: Cisco IOS doesn't expose optical light levels the way some
# other vendors do, so directly pinging the WAN interface's own IP
# from a neighbor is the practical substitute for verifying the
# interface is actually passing traffic at all - not just "is the
# router alive" (which a loopback ping would answer instead).
#
# Value is a dict: neighbor hostname, the backbone interface on that
# neighbor to source the ping from, and the target IP - the affected
# router's actual Gi0/0 (WAN) address, reachable only via the static
# route added by edge_outside_static_routes.yml.
NEIGHBOR_PING_CHECK = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/0"): {
        "neighbor": "EdgeR2",
        "source_interface": "GigabitEthernet0/2",
        "target_ip": "<edgeR1-gi0/0-ip>",
    },
    ("MemberA", "EdgeR2", "GigabitEthernet0/0"): {
        "neighbor": "EdgeR1",
        "source_interface": "GigabitEthernet0/2",
        "target_ip": "<edgeR2-gi0/0-ip>",
    },
}

# How many recent up/down events to show from the device's own local
# log buffer.
RECENT_HISTORY_COUNT = 10

# Local append-only record of every noc_check.py run - who ran it,
# against what, and when. Independent of Discord/Grafana, so it
# answers "when was this actually checked" even if an alert's timeline
# in Grafana gets resolved/edited later. Not committed - see
# .gitignore.
HISTORY_LOG = "noc_check_history.log"


def log_check_run(timestamp: str, site: str, hostname: str, interface: str, username: str) -> None:
    """Append one line recording this invocation. Best-effort - a
    logging failure (e.g. read-only filesystem) shouldn't block a
    technician from actually running the check."""
    try:
        with open(HISTORY_LOG, "a") as f:
            f.write(f"{timestamp}\t{username}\t{site}\t{hostname}\t{interface}\n")
    except OSError:
        pass


def list_available_sites() -> list:
    """Return the list of known sites, based on inventory files
    present in INVENTORY_DIR (inventory/<site>.yml)."""
    if not os.path.isdir(INVENTORY_DIR):
        return []
    return sorted(
        f[:-4] for f in os.listdir(INVENTORY_DIR)
        if f.endswith(".yml")
    )


def load_inventory_host(site: str, hostname: str) -> str:
    """Look up a host's ansible_host IP from a per-client inventory
    file (inventory/<site>.yml). Searches all groups within that
    file for a matching host key. Returns None if the site's
    inventory file doesn't exist or the host isn't found in it."""
    inventory_path = os.path.join(INVENTORY_DIR, f"{site}.yml")

    if not os.path.isfile(inventory_path):
        return None

    with open(inventory_path) as f:
        inv = yaml.safe_load(f)

    def search(node):
        if isinstance(node, dict):
            if hostname in node and isinstance(node[hostname], dict):
                host_vars = node[hostname]
                if "ansible_host" in host_vars:
                    return host_vars["ansible_host"]
            for value in node.values():
                result = search(value)
                if result:
                    return result
        return None

    return search(inv)


def connect(host_ip: str, username: str, password: str):
    """Open a single Netmiko connection to reuse across multiple
    show commands, rather than reconnecting for each one."""
    device = {
        "device_type": "cisco_ios",
        "host": host_ip,
        "username": username,
        "password": password,
    }
    return ConnectHandler(**device)


def run_command(conn, command: str) -> str:
    """Run a single show command on an already-open connection and
    return the raw text output. Returns an empty string and does not
    raise if the command itself isn't supported on this platform, so
    one unsupported command doesn't abort the whole check."""
    try:
        return conn.send_command(command)
    except Exception:
        return ""


def parse_state(raw_output: str) -> dict:
    """Extract interface status and line protocol state from raw
    'show interface' output."""
    match = re.search(
        r"is (?P<status>up|down|administratively down)"
        r",? line protocol is (?P<protocol>up|down)",
        raw_output,
        re.IGNORECASE,
    )
    if not match:
        return {"status": "unknown", "protocol": "unknown"}
    return {
        "status": match.group("status").lower(),
        "protocol": match.group("protocol").lower(),
    }


def calculate_duration_in_state(recent_events: list, current_state: str) -> str:
    """Calculate how long the interface has been in its current
    state, based on the most recent matching event timestamp pulled
    from the device's own log buffer. Falls back to a plain message
    if there's no usable history to calculate from (e.g. the buffer
    doesn't go back far enough, or the device was recently rebooted).

    Assumes the device's clock is accurate (NTP-synced) - this
    project's routers are, but this is worth knowing if the tool is
    ever pointed at a device with a wrong clock.
    """
    if not recent_events:
        return f"Interface is currently {current_state.upper()} - no recent state-change history available to calculate duration."

    # recent_events are "Mon DD HH:MM:SS  STATE", most recent first.
    # Find the most recent event and use its timestamp - if the
    # current live state doesn't match the most recent logged event
    # (e.g. a flap happened seconds ago and hasn't synced through
    # syslog yet), say so rather than reporting a misleading duration.
    most_recent = recent_events[0]
    match = re.match(r"(\w{3} \d{1,2} \d{2}:\d{2}:\d{2})\s+(UP|DOWN)", most_recent)
    if not match:
        return f"Interface is currently {current_state.upper()} - could not parse timing from device history."

    timestamp_str, logged_state = match.groups()
    if logged_state.lower() != current_state.lower():
        return (
            f"Interface is currently {current_state.upper()}, but the most recent "
            f"logged event ({most_recent.strip()}) doesn't match - state may have "
            f"just changed. Check current status above as the source of truth."
        )

    try:
        # Device logs don't include the year - assume current year,
        # which is correct for anything in the recent log buffer.
        event_time = datetime.strptime(f"{datetime.now().year} {timestamp_str}", "%Y %b %d %H:%M:%S")
        delta = datetime.now() - event_time
        if delta.total_seconds() < 0:
            # Clock skew or year rollover edge case
            return f"Interface has been {current_state.upper()} since {timestamp_str} (device time)."
        return f"Interface has been {current_state.upper()} for {format_timedelta(delta)} (since {timestamp_str})."
    except ValueError:
        return f"Interface is currently {current_state.upper()} - could not calculate exact duration."


def format_timedelta(delta) -> str:
    """Format a timedelta as a short, human-readable duration."""
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def parse_transceiver_summary(raw_transceiver_output: str) -> str:
    """Pull optical Tx/Rx power readings out of 'show interfaces
    transceiver detail' (or platform equivalent) output, if present.
    Returns a short summary, or a note if this device/interface
    doesn't support/report transceiver data (e.g. copper links)."""
    if not raw_transceiver_output.strip():
        return "No transceiver data returned (command may be unsupported on this platform)."

    if re.search(r"transceiver is not present|not applicable|N/A", raw_transceiver_output, re.IGNORECASE):
        return "No transceiver present on this interface (likely a copper/RJ45 link)."

    tx_match = re.search(r"Tx Power.*?(-?\d+\.\d+)\s*dBm", raw_transceiver_output, re.IGNORECASE)
    rx_match = re.search(r"Rx Power.*?(-?\d+\.\d+)\s*dBm", raw_transceiver_output, re.IGNORECASE)

    if tx_match or rx_match:
        tx = f"{tx_match.group(1)} dBm" if tx_match else "unknown"
        rx = f"{rx_match.group(1)} dBm" if rx_match else "unknown"
        return f"Tx Power: {tx} | Rx Power: {rx}"

    return "Transceiver data returned but light levels could not be parsed - see raw output below."


def parse_ping_success_rate(raw_ping_output: str) -> str:
    """Extract the success rate line from a Cisco IOS ping's output
    (e.g. 'Success rate is 100 percent (5/5)') and translate it into
    a plain-language reachability statement."""
    match = re.search(
        r"Success rate is (?P<percent>\d+) percent \((?P<received>\d+)/(?P<sent>\d+)\)",
        raw_ping_output,
    )
    if not match:
        return "Could not determine ping result from output - see raw output below."

    percent = int(match.group("percent"))
    received = match.group("received")
    sent = match.group("sent")

    if percent == 0:
        return (
            f"0% success ({received}/{sent} replies) - the WAN interface itself is "
            f"NOT reachable via the dedicated backbone static route either. "
            f"Consistent with a genuine physical/link-layer failure on this "
            f"interface, not just a reporting issue."
        )
    if percent == 100:
        return (
            f"100% success ({received}/{sent} replies) - the WAN interface IS "
            f"reachable via the dedicated backbone static route, despite showing "
            f"down/down. This is unusual and worth investigating further - "
            f"possible stale state on the device, an intermittent condition, or a "
            f"discrepancy between reported and actual interface state."
        )
    return (
        f"{percent}% success ({received}/{sent} replies) - partially reachable. "
        f"May indicate an intermittent or flapping condition on the interface "
        f"itself, rather than a hard failure."
    )


def run_neighbor_ping_check(site: str, neighbor_hostname: str, source_interface: str,
                             target_ip: str, username: str, password: str) -> str:
    """Connect to a neighboring device and have it ping the affected
    router's actual WAN (Gi0/0) interface IP directly, sourced from
    its own backbone interface, over a dedicated static route (see
    edge_outside_static_routes.yml) - NOT via OSPF and NOT via any
    shared outside segment. This is a practical substitute for
    optical light levels, which Cisco IOS doesn't expose the way some
    other vendors do, letting a tech verify the WAN interface itself
    is actually passing traffic rather than just checking if the
    router is alive in general."""
    neighbor_ip = load_inventory_host(site, neighbor_hostname)
    if not neighbor_ip:
        return f"Could not find neighbor '{neighbor_hostname}' in inventory/{site}.yml - skipping neighbor ping check."

    try:
        neighbor_conn = connect(neighbor_ip, username, password)
    except NetmikoAuthenticationException:
        return f"Authentication failed connecting to neighbor {neighbor_hostname} - skipping neighbor ping check."
    except NetmikoTimeoutException:
        return f"Could not reach neighbor {neighbor_hostname} ({neighbor_ip}) - neighbor itself may be down too."
    except Exception as exc:
        return f"Unexpected error connecting to neighbor {neighbor_hostname}: {exc}"

    raw_ping_output = run_command(neighbor_conn, f"ping {target_ip} source {source_interface}")
    neighbor_conn.disconnect()

    summary = parse_ping_success_rate(raw_ping_output)
    return f"{neighbor_hostname} (sourced from its {source_interface}) pinging WAN interface {target_ip} via dedicated static route: {summary}"


def parse_recent_history(raw_log_output: str, interface: str) -> list:
    """Filter the device's own log buffer down to up/down events for
    the specific interface being checked, most recent first, cleanly
    formatted as 'timestamp STATE' lines."""
    events = []
    for line in raw_log_output.splitlines():
        if interface not in line:
            continue
        match = re.search(
            r"(?P<month>\w{3})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2}).*?"
            r"changed state to (?P<state>up|down)",
            line,
            re.IGNORECASE,
        )
        if match:
            timestamp = f"{match.group('month')} {match.group('day')} {match.group('time')}"
            events.append(f"{timestamp}  {match.group('state').upper()}")

    # Most recent first - log buffers are typically oldest-first.
    events.reverse()
    return events[:RECENT_HISTORY_COUNT]


def describe_vrrp_status(raw_vrrp_output: str, hostname: str) -> str:
    """Translate 'show vrrp brief' output into a plain-language
    statement of which router is currently active - no VRRP
    terminology required to understand it. Summarizes across all
    VRRP groups on the device, since a router can be active for some
    VLANs and standby for others."""
    lines = raw_vrrp_output.splitlines()
    master_count = 0
    backup_count = 0
    for line in lines:
        if re.search(r"\bMaster\b", line, re.IGNORECASE):
            master_count += 1
        elif re.search(r"\bBackup\b", line, re.IGNORECASE):
            backup_count += 1

    if master_count == 0 and backup_count == 0:
        return "   Could not determine active/standby role from device output - review manually."

    if backup_count == 0:
        return (
            f"   {hostname} is currently ACTIVE for all {master_count} redundancy "
            f"group(s) checked - it is handling traffic normally right now."
        )
    if master_count == 0:
        return (
            f"   {hostname} is currently in STANDBY for all {backup_count} redundancy "
            f"group(s) checked - the other router is handling traffic instead. "
            f"This appears to be working as intended."
        )
    return (
        f"   {hostname} is ACTIVE for {master_count} and STANDBY for {backup_count} "
        f"redundancy group(s) - a mixed state. Review the raw output below to confirm "
        f"this is expected."
    )


def describe_ospf_neighbor_status(raw_ospf_output: str, peer_router_id: str, peer_hostname: str) -> str:
    """Translate 'show ip ospf neighbor' output into a plain-language
    statement of whether a specific peer is still an OSPF neighbor.
    IOS only lists neighbors it currently has an adjacency with, so a
    peer that's dropped off this link simply won't appear at all -
    there's no explicit 'down' state to look for, only absence."""
    match = re.search(rf"{re.escape(peer_router_id)}\s+\d+\s+(\S+)", raw_ospf_output)
    if match:
        return (
            f"   {peer_hostname} ({peer_router_id}) is still an OSPF neighbor, "
            f"state {match.group(1)} - adjacency is up, likely via an alternate "
            f"path. Confirm which interface it's reached over in the raw output "
            f"below."
        )
    return (
        f"   {peer_hostname} ({peer_router_id}) does not appear in the OSPF "
        f"neighbor table at all - adjacency over this link is down, consistent "
        f"with the interface state above."
    )


def describe_bgp_neighbor_status(raw_bgp_output: str, peer_ip: str, peer_hostname: str) -> str:
    """Translate 'show ip bgp summary' output into a plain-language
    statement of whether a specific BGP neighbor is Established.
    Unlike OSPF, a down BGP session still shows a row for the
    neighbor - the last column is either a received-prefix count
    (Established) or a state name (Idle/Active/Connect/OpenSent/...)
    when it isn't up."""
    match = re.search(rf"^{re.escape(peer_ip)}\s+.*?(\S+)\s*$", raw_bgp_output, re.MULTILINE)
    if not match:
        return (
            f"   {peer_hostname} ({peer_ip}) does not appear in the BGP table at "
            f"all - the neighbor statement may be missing, or the session has "
            f"never come up."
        )
    last_field = match.group(1)
    if last_field.isdigit():
        return (
            f"   {peer_hostname} ({peer_ip}) BGP session is Established "
            f"({last_field} prefix(es) received) - the iBGP session is up."
        )
    return (
        f"   {peer_hostname} ({peer_ip}) BGP session is down - state: "
        f"{last_field}, consistent with the interface state above."
    )


def suggest_next_step(state: dict, site: str, hostname: str, interface: str) -> str:
    """Return a plain-language suggested next step based on live
    interface state."""
    status = state["status"]
    protocol = state["protocol"]

    if status == "up" and protocol == "up":
        return "✅ Interface is healthy (up/up). No action needed."

    if status == "administratively down":
        return (
            "🔧 Interface has been manually shut down (administratively down).\n"
            "   Confirm with team before bringing it back up - may be intentional\n"
            "   (maintenance, decommission, security response, etc.)."
        )

    if status == "down" and protocol == "down":
        circuit = WAN_CIRCUIT_MAP.get((site, hostname, interface))
        if circuit:
            return (
                f"🔴 Interface is physically down (down/down).\n"
                f"   {circuit}\n"
                f"   Contact carrier and reference the circuit ID above. Confirm\n"
                f"   cabling and local hardware first if accessible on-site."
            )
        ospf_link = OSPF_NEIGHBOR_CHECK_ON_DOWN.get((site, hostname, interface))
        if ospf_link:
            return (
                f"🔴 OSPF adjacency to {ospf_link['peer']} is down (down/down).\n"
                f"   {hostname} and {ospf_link['peer']} are no longer OSPF\n"
                f"   neighbors over this backbone link - see live OSPF neighbor\n"
                f"   status below.\n"
                f"   Escalate to a Network Engineer."
            )
        bgp_link = BGP_NEIGHBOR_CHECK_ON_DOWN.get((site, hostname, interface))
        if bgp_link:
            return (
                f"🔴 iBGP session to {bgp_link['peer']} is down (down/down).\n"
                f"   {hostname} and {bgp_link['peer']} are no longer BGP peers\n"
                f"   over this backbone link - see live BGP session status below.\n"
                f"   Escalate to a Network Engineer."
            )
        return (
            "🔴 Interface is physically down (down/down).\n"
            "   Check cabling, remote end device, and upstream carrier/ISP status\n"
            "   if this is a WAN-facing link. Confirm the remote device is powered\n"
            "   and its corresponding interface is not itself shut down."
        )

    if status == "up" and protocol == "down":
        return (
            "🟡 Interface is up but line protocol is down.\n"
            "   Physical link is present but the connection isn't fully\n"
            "   establishing - check for encapsulation mismatch, duplex/speed\n"
            "   mismatch, or a keepalive/protocol issue with the remote end."
        )

    return (
        "❓ Unrecognized state - review the raw output below manually."
    )


def main():
    if len(sys.argv) == 4:
        site = sys.argv[1]
        hostname = sys.argv[2]
        interface = sys.argv[3]
    elif len(sys.argv) == 1:
        print("=== NOC Interface Check ===\n")
        available = list_available_sites()
        if available:
            print(f"Known sites: {', '.join(available)}")
        site = input("Site: ").strip()
        hostname = input("Device hostname (e.g. EdgeR1): ").strip()
        interface = input("Interface (e.g. GigabitEthernet0/0): ").strip()
    else:
        print("Usage: python3 noc_check.py <site> <device> <interface>")
        print("Example: python3 noc_check.py MemberA EdgeR1 GigabitEthernet0/0")
        print("(Or run with no arguments to be prompted interactively.)")
        sys.exit(1)

    host_ip = load_inventory_host(site, hostname)
    if not host_ip:
        available = list_available_sites()
        print(f"❌ Could not find '{hostname}' in inventory/{site}.yml")
        if available:
            print(f"   Known sites: {', '.join(available)}")
        sys.exit(1)

    username = input("Username: ")
    password = getpass.getpass("Password: ")

    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_check_run(run_timestamp, site, hostname, interface, username)

    print(f"\nConnecting to {hostname} ({host_ip}) [{site}]...")

    try:
        conn = connect(host_ip, username, password)
    except NetmikoAuthenticationException:
        print(f"❌ Authentication failed connecting to {hostname}.")
        sys.exit(1)
    except NetmikoTimeoutException:
        print(f"❌ Could not reach {hostname} ({host_ip}) - connection timed out.")
        print("   This itself may be meaningful - the device or its path may be down.")
        print()
        print("   ⚠️  Next step: PAGE NETWORK ENGINEERING.")
        print("   This tool cannot investigate further - it requires a live SSH")
        print("   session to the device itself, and that connection has failed.")
        sys.exit(1)
    except Exception as exc:
        print(f"❌ Unexpected error connecting to {hostname}: {exc}")
        sys.exit(1)

    try:
        raw_interface_output = run_command(conn, f"show interface {interface}")
        state = parse_state(raw_interface_output)

        print(f"\nSite:      {site}")
        print(f"Device:    {hostname}")
        print(f"Interface: {interface}")
        print(f"Checked:   {run_timestamp} (by {username})")
        print(f"Status:    {state['status']}")
        print(f"Protocol:  {state['protocol']}")

        # Recent flap history from the device's own local log buffer -
        # gathered and shown unconditionally (up/up included), so a tech
        # can rule out a false alert by seeing there's been no recent
        # flapping, not just when something is currently down.
        print("\n--- Recent interface history (this device's local log) ---")
        raw_log_output = run_command(conn, f"show logging | include {interface}")
        recent_events = parse_recent_history(raw_log_output, interface)
        if recent_events:
            for event in recent_events:
                print(f"   {event}")
        else:
            print("   No recent up/down events found in this device's local log buffer.")

        print()
        print(suggest_next_step(state, site, hostname, interface))

        # Real elapsed time in current state, calculated from the most
        # recent matching event above - not just raw IOS text, and shown
        # regardless of up or down.
        current_state_word = "up" if state["protocol"] == "up" and state["status"] == "up" else "down"
        print("\n--- Time in current state ---")
        print(f"   {calculate_duration_in_state(recent_events, current_state_word)}")

        # Transceiver / optical light levels
        print("\n--- Transceiver status ---")
        raw_transceiver_output = run_command(conn, f"show interfaces {interface} transceiver detail")
        print(f"   {parse_transceiver_summary(raw_transceiver_output)}")

        # VRRP / redundancy check, only when this specific interface is
        # known to matter for a redundancy relationship and is down
        is_down = state["status"] in ("down", "administratively down") or state["protocol"] == "down"
        context_note = VRRP_CHECK_ON_DOWN.get((site, hostname, interface))
        if is_down and context_note:
            print(f"\n--- Redundancy status ({context_note}) ---")
            try:
                vrrp_output = run_command(conn, "show vrrp brief")
                print(describe_vrrp_status(vrrp_output, hostname))
                print("\n--- Raw VRRP output ---")
                print(vrrp_output)
            except Exception as exc:
                print(f"   Could not check redundancy status: {exc}")

        # OSPF adjacency check, only when this specific interface is a
        # known OSPF backbone link between two devices and is down
        ospf_check = OSPF_NEIGHBOR_CHECK_ON_DOWN.get((site, hostname, interface))
        if is_down and ospf_check:
            print(f"\n--- OSPF adjacency status ({ospf_check['peer']} via {interface}) ---")
            try:
                ospf_output = run_command(conn, "show ip ospf neighbor")
                print(describe_ospf_neighbor_status(ospf_output, ospf_check["peer_router_id"], ospf_check["peer"]))
                print("\n--- Raw OSPF neighbor output ---")
                print(ospf_output)
            except Exception as exc:
                print(f"   Could not check OSPF adjacency status: {exc}")

        # BGP session check, only when this specific interface is a
        # known iBGP-bearing backbone link between two devices and is down
        bgp_check = BGP_NEIGHBOR_CHECK_ON_DOWN.get((site, hostname, interface))
        if is_down and bgp_check:
            print(f"\n--- BGP session status ({bgp_check['peer']} via {interface}) ---")
            try:
                bgp_output = run_command(conn, "show ip bgp summary")
                print(describe_bgp_neighbor_status(bgp_output, bgp_check["peer_ip"], bgp_check["peer"]))
                print("\n--- Raw BGP summary output ---")
                print(bgp_output)
            except Exception as exc:
                print(f"   Could not check BGP session status: {exc}")

        # Neighbor ping check - only when this specific interface is down
        # and a neighbor/target is configured for it. Tests real WAN
        # interface reachability via a dedicated static route (not OSPF,
        # not the shared lab switch). Uses the same credentials already
        # entered for the primary device.
        neighbor_check = NEIGHBOR_PING_CHECK.get((site, hostname, interface))
        if is_down and neighbor_check:
            print(f"\n--- WAN interface reachability check (via dedicated static route) ---")
            print(f"   {run_neighbor_ping_check(site, neighbor_check['neighbor'], neighbor_check['source_interface'], neighbor_check['target_ip'], username, password)}")
    finally:
        # Guarantees the vty line is freed even if something above
        # throws on unexpected device output - an unhandled crash here
        # used to leak the session until the router's own exec-timeout
        # cleared it.
        conn.disconnect()

    print("\n--- Raw show interface output ---")
    print(raw_interface_output)


if __name__ == "__main__":
    main()