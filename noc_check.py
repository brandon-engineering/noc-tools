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
# - EdgeR1/EdgeR2 Gi0/0 (WAN uplink down) -> checks EdgeR1/EdgeR2's
#   own VRRP (their Gi0/2 backbone relationship) to confirm which
#   EdgeRouter is currently handling traffic.
# - R1/R2 Gi0/0 (PTP link to EdgeRouter down) -> checks R1/R2's own
#   VRRP (their VLAN 10/20/30/99 relationship) to confirm which
#   inner router is currently handling traffic.
VRRP_CHECK_ON_DOWN = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/0"): "WAN uplink down",
    ("MemberA", "EdgeR2", "GigabitEthernet0/0"): "WAN uplink down",
    ("MemberA", "R1", "GigabitEthernet0/0"): "PTP link to EdgeR1 down",
    ("MemberA", "R2", "GigabitEthernet0/0"): "PTP link to EdgeR2 down",
}

# How many recent up/down events to show from the device's own local
# log buffer.
RECENT_HISTORY_COUNT = 10


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


def parse_last_state_change(raw_interface_output: str) -> str:
    """Pull the 'line protocol ... last flapped' / uptime-in-current-
    state duration out of the raw show interface output, if present.
    IOS doesn't always include this by default (depends on platform/
    image), so this returns a plain message rather than failing if
    it's not there."""
    match = re.search(
        r"Line protocol.*?last (?:input|flapped).*", raw_interface_output, re.IGNORECASE
    )
    if match:
        return match.group(0).strip()

    match = re.search(r"Last input (\S+), output (\S+)", raw_interface_output)
    if match:
        return f"Last input {match.group(1)}, output {match.group(2)} (time since last traffic, not necessarily since last state change)"

    return "Could not determine time in current state from this platform's output."


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

    print(f"\nConnecting to {hostname} ({host_ip}) [{site}]...")

    try:
        conn = connect(host_ip, username, password)
    except NetmikoAuthenticationException:
        print(f"❌ Authentication failed connecting to {hostname}.")
        sys.exit(1)
    except NetmikoTimeoutException:
        print(f"❌ Could not reach {hostname} ({host_ip}) - connection timed out.")
        print("   This itself may be meaningful - the device or its path may be down.")
        sys.exit(1)
    except Exception as exc:
        print(f"❌ Unexpected error connecting to {hostname}: {exc}")
        sys.exit(1)

    raw_interface_output = run_command(conn, f"show interface {interface}")
    state = parse_state(raw_interface_output)

    print(f"\nSite:      {site}")
    print(f"Device:    {hostname}")
    print(f"Interface: {interface}")
    print(f"Status:    {state['status']}")
    print(f"Protocol:  {state['protocol']}")

    # Recent flap history from the device's own local log buffer -
    # shown early, before the suggested next step, so a tech sees the
    # flap pattern before reading the recommendation.
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

    # Time in current state
    print("\n--- Time in current state ---")
    print(f"   {parse_last_state_change(raw_interface_output)}")

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

    conn.disconnect()

    print("\n--- Raw show interface output ---")
    print(raw_interface_output)


if __name__ == "__main__":
    main()