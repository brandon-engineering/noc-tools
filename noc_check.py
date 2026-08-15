#!/usr/bin/env python3
"""
noc_check.py

On-demand NOC investigation tool. Connects to a specific device,
runs `show interface <interface>`, parses the CURRENT live state,
and prints a plain-language suggested next step based on what's
actually happening right now - not a guess baked in at alert time.

Usage:
    python3 noc_check.py <hostname> <interface>

Example:
    python3 noc_check.py EdgeR1 GigabitEthernet0/0

Reads device connection details (IP, device_type) from the Ansible
inventory (inventory.yml) so there's a single source of truth for
host addressing - no duplicated IP list to maintain here.
"""

import sys
import re
import getpass
import yaml
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

INVENTORY_PATH = "inventory/inventory.yml"

# Circuit/carrier info for known WAN-facing interfaces. Add entries
# here as real circuits are identified, keyed by (hostname, interface).
# Used to make the down/down suggestion specific and actionable -
# exactly which carrier/circuit to reference when escalating.
WAN_CIRCUIT_MAP = {
    ("EdgeR1", "GigabitEthernet0/0"): "WAN link to ATT CID -test.203203.ATT",
    ("EdgeR2", "GigabitEthernet0/0"): "WAN link to ATT CID -test.203203.ATT",
}


def load_inventory_host(hostname: str) -> str:
    """Look up a host's ansible_host IP from inventory.yml.
    Searches all groups for a matching host key."""
    with open(INVENTORY_PATH) as f:
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


def get_interface_output(host_ip: str, username: str, password: str, interface: str) -> str:
    """SSH to the device and return the raw output of show interface."""
    device = {
        "device_type": "cisco_ios",
        "host": host_ip,
        "username": username,
        "password": password,
    }
    conn = ConnectHandler(**device)
    output = conn.send_command(f"show interface {interface}")
    conn.disconnect()
    return output


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


def suggest_next_step(state: dict, hostname: str, interface: str) -> str:
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
        circuit = WAN_CIRCUIT_MAP.get((hostname, interface))
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
    if len(sys.argv) != 3:
        print("Usage: python3 noc_check.py <hostname> <interface>")
        print("Example: python3 noc_check.py EdgeR1 GigabitEthernet0/0")
        sys.exit(1)

    hostname = sys.argv[1]
    interface = sys.argv[2]

    host_ip = load_inventory_host(hostname)
    if not host_ip:
        print(f"❌ Could not find '{hostname}' in {INVENTORY_PATH}")
        sys.exit(1)

    username = input("Username: ")
    password = getpass.getpass("Password: ")

    print(f"\nConnecting to {hostname} ({host_ip})...")

    try:
        raw_output = get_interface_output(host_ip, username, password, interface)
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

    state = parse_state(raw_output)

    print(f"\nInterface: {interface}")
    print(f"Status:    {state['status']}")
    print(f"Protocol:  {state['protocol']}\n")
    print(suggest_next_step(state, hostname, interface))
    print("\n--- Raw output ---")
    print(raw_output)


if __name__ == "__main__":
    main()