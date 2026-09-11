#!/usr/bin/env python3
"""
snmp_poll.py

Pull-based interface-state poller for the CML fleet, meant to run on
a box outside the lab's own dependency chain (the laptop, not NUS1) -
if both edge routers' WAN uplinks go down, NUS1 has no path out to
alert anyone, but the laptop (reached over WireGuard into the home
network, a separate path) still can.

Scoped to match netalert.py's own behavior, just pull-based instead of
syslog-push-based: watches EVERY interface on every device in the
inventory via SNMP (IF-MIB ifOperStatus), and alerts on any state
transition - same message style, same alert channels
(DISCORD_WEBHOOK_URL / IRM_WEBHOOK_URL), same DEVICE_INTERFACE_MAP
friendly-name lookup. That map is DUPLICATED here rather than
imported, since netalert.py isn't written as an importable module -
it has side effects (webhook check, tail subprocess, infinite loop) at
import time. Keep the two maps in sync by hand if the topology
changes; netalert.py's copy is the source of truth for interface
descriptions.

Layered on top of interface polling: a separate device-level
reachability check (sysUpTime), so a device that's completely
unreachable over SNMP still gets flagged even before/without any
per-interface data. This is what catches the class of failure
netalert.py structurally can't - a device that goes fully dark, or a
syslog-delivery failure like RCA-2026-09-11-001, where the device
never manages to push a message in the first place. Interface polling
is skipped for a cycle where the device itself didn't respond (no
point walking a table from something that isn't there); it resumes
normally once the device is reachable again.

Usage:
    python3 snmp_poll.py [site]

Run from ~/ansible (so the relative inventory/<site>.yml path
resolves) - same convention as noc_check.py.
"""

import os
import re
import subprocess
import sys
import time

import requests
import yaml

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
IRM_WEBHOOK_URL = os.environ.get("IRM_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL environment variable not set")

INVENTORY_DIR = "inventory"
SNMP_COMMUNITY = os.environ.get("SNMP_COMMUNITY", "public")
POLL_INTERVAL = int(os.environ.get("SNMP_POLL_INTERVAL", "60"))
SNMP_TIMEOUT = 2

SYSUPTIME_OID = "1.3.6.1.2.1.1.3.0"
IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
# ifOperStatus comes back as net-snmp's MIB-translated enum label, not
# the raw integer, under -Oqn (confirmed against a live device: "up"/
# "down", not "1"/"2"). "up" = up; everything else (down, unknown,
# dormant, notPresent, lowerLayerDown, ...) is "not up" for alerting.
OPER_STATUS_UP = "up"

# Duplicated from netalert.py's DEVICE_INTERFACE_MAP - see this file's
# docstring for why it isn't a shared import.
DEVICE_INTERFACE_MAP = {
    ("EdgeR1", "GigabitEthernet0/0"): "EdgeR1 internet uplink is affected - verify ISP/WAN path.",
    ("EdgeR2", "GigabitEthernet0/0"): "EdgeR2 internet uplink is affected - verify ISP/WAN path.",
    ("EdgeR1", "GigabitEthernet0/2"): "EdgeR1-EdgeR2 backbone link is affected - VRRP failover in use, verify EdgeR2.",
    ("EdgeR2", "GigabitEthernet0/2"): "EdgeR1-EdgeR2 backbone link is affected - VRRP failover in use, verify EdgeR1.",
}


def load_all_hosts(site: str) -> dict:
    """Return {hostname: ansible_host} for every host defined anywhere
    in inventory/<site>.yml, regardless of which group it's under.
    Same file location/lookup convention as noc_check.py."""
    inventory_path = os.path.join(INVENTORY_DIR, f"{site}.yml")
    if not os.path.isfile(inventory_path):
        raise SystemExit(f"no such inventory file: {inventory_path}")

    with open(inventory_path) as f:
        inv = yaml.safe_load(f)

    hosts = {}

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, dict) and "ansible_host" in value:
                    hosts[key] = value["ansible_host"]
                else:
                    walk(value)

    walk(inv)
    return hosts


def snmp_get(ip: str, oid: str) -> str:
    """Single SNMP GET. Returns the value, or None if the device
    didn't respond (timeout, or a semantic no-such-object error -
    both mean "can't trust this device right now")."""
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", SNMP_COMMUNITY,
             "-t", str(SNMP_TIMEOUT), "-r", "1", "-Ovq", ip, oid],
            capture_output=True, text=True, timeout=SNMP_TIMEOUT + 3,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output or "No Such" in output or "Timeout" in output:
        return None
    return output


def snmp_walk_table(ip: str, base_oid: str) -> dict:
    """Return {index: value} for a single ifTable column, keyed by the
    numeric index parsed off each returned OID - not by array
    position, since two separate walks aren't guaranteed to stay
    aligned positionally if a row is momentarily missing."""
    try:
        result = subprocess.run(
            ["snmpwalk", "-v2c", "-c", SNMP_COMMUNITY,
             "-t", str(SNMP_TIMEOUT), "-r", "1", "-Oqn", ip, base_oid],
            capture_output=True, text=True, timeout=SNMP_TIMEOUT + 5,
        )
    except subprocess.TimeoutExpired:
        return {}
    if result.returncode != 0:
        return {}

    table = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^\.?[\d.]+\.(\d+)\s+(.+)$", line.strip())
        if match:
            index, value = match.groups()
            table[index] = value.strip('"')
    return table


def poll_interfaces(ip: str) -> dict:
    """Return {ifDescr: 'up'|'down'} for every interface on a device.
    Empty dict if the walk came back empty (device unreachable, or has
    no interfaces to report - either way, nothing to alert on here)."""
    names = snmp_walk_table(ip, IF_DESCR_OID)
    statuses = snmp_walk_table(ip, IF_OPER_STATUS_OID)
    return {
        names[idx]: ("up" if statuses.get(idx) == OPER_STATUS_UP else "down")
        for idx in names
        if idx in statuses
    }


def build_interface_message(host: str, interface: str, state: str) -> str:
    friendly = DEVICE_INTERFACE_MAP.get((host, interface))
    icon = "🔴" if state == "down" else "🟢"
    if friendly and state == "down":
        return f"{icon} {friendly}\nDevice: {host} | Interface: {interface} | State: DOWN"
    if friendly and state == "up":
        return (f"{icon} {host} {interface} has RECOVERED (previously flagged as down).\n"
                 f"Device: {host} | Interface: {interface} | State: UP")
    return f"{icon} Interface event: {host} {interface} changed state to {state.upper()}"


def send_alert(message: str, alert_key: str) -> None:
    requests.post(WEBHOOK_URL, json={"content": message})
    if IRM_WEBHOOK_URL:
        try:
            requests.post(
                IRM_WEBHOOK_URL,
                json={"message": message, "alert_key": alert_key},
                timeout=5,
            )
        except requests.exceptions.RequestException:
            # Don't let an IRM delivery failure interrupt Discord
            # alerting, which is the primary/already-proven channel.
            pass


def main() -> None:
    site = sys.argv[1] if len(sys.argv) > 1 else "MemberA"
    hosts = load_all_hosts(site)
    if not hosts:
        raise SystemExit(f"no hosts with ansible_host found in inventory/{site}.yml")

    print(f"snmp_poll: watching {len(hosts)} host(s) in '{site}' every {POLL_INTERVAL}s: "
          f"{', '.join(sorted(hosts))}", flush=True)

    device_up = {host: None for host in hosts}     # device-level reachability
    iface_state = {}                                # {(host, interface): "up"|"down"}

    while True:
        for host, ip in hosts.items():
            reachable = snmp_get(ip, SYSUPTIME_OID) is not None

            if device_up[host] is not None and reachable != device_up[host]:
                state = "up" if reachable else "down"
                icon = "🟢" if reachable else "🔴"
                verb = "is responding to SNMP again (previously unreachable)" if reachable \
                    else "stopped responding to SNMP entirely — check reachability"
                send_alert(f"{icon} {host} {verb}.", f"{host}-snmp-device")
            device_up[host] = reachable

            if not reachable:
                continue  # nothing to walk from a device that's not answering

            for interface, state in poll_interfaces(ip).items():
                key = (host, interface)
                if iface_state.get(key) is not None and state != iface_state[key]:
                    send_alert(build_interface_message(host, interface, state), f"{host}-{interface}")
                iface_state[key] = state

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
