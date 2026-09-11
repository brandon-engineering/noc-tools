# How snmp_poll.py Works — A Line-by-Line Walkthrough

This document explains every part of `snmp_poll.py`, written for
someone who has never programmed in Python before. It assumes no prior
coding knowledge - every Python concept is explained the first time it
appears. If you've already read `netalert_explained.md`, some of this
will feel familiar (both scripts share several ideas), but this
document stands alone.

---

## The big picture, before we look at any code

`netalert.py` (see its own walkthrough) is **event-driven**: it reacts
to syslog messages devices choose to send. That works well, but it has
a structural blind spot - it can only see events a device actually
manages to log and deliver. If a device loses its syslog connection
entirely, or goes fully unreachable, `netalert.py` sees nothing at
all, because there's nothing to react to.

`snmp_poll.py` takes the opposite approach: **it asks, on a timer,
instead of waiting to be told.** Every interval (60 seconds by
default), it asks every device in the inventory two things over SNMP:
"are you even there?" and "what state is each of your interfaces in?"
This is called **polling**. The tradeoff: it can't react the instant
something happens (it only finds out on the next poll), but it can
detect the one thing event-driven alerting structurally can't - a
device that's gone completely dark, with nothing to say for itself.

The script does this, forever, until stopped:

1. Read the list of devices to watch from an Ansible inventory file
2. On a timer, ask each device "are you reachable at all?" (SNMP)
3. If reachable, also ask "what's the up/down state of every one of
   your interfaces?" (SNMP)
4. Compare each answer to what it was on the *previous* poll
5. If anything changed, send an alert to Discord (always) and Grafana
   IRM (if configured) - using the same message style and the same
   two destinations as `netalert.py`

---

## Part 1: Imports (lines 1-9, after the docstring)

```python
import os
import re
import subprocess
import sys
import time

import requests
import yaml
```

Same idea as `netalert.py`'s imports - each one brings in a "toolbox"
of pre-built functionality:

- **`os`** - reading environment variables, checking file paths.
- **`re`** - regular expressions, for pulling structured data out of
  text (used here to parse `snmpwalk`'s output).
- **`subprocess`** - runs other programs from inside Python. This
  script uses it to run the command-line tools `snmpget` and
  `snmpwalk`, rather than using a Python SNMP library directly -
  simpler, and it's the same "shell out to a well-known tool" approach
  `netalert.py` uses for `tail`.
- **`sys`** - lets the script read arguments typed on the command line
  (used here for an optional site name).
- **`time`** - lets the script pause between polls.
- **`requests`** - sends the actual alerts to Discord/Grafana over
  HTTP, same as `netalert.py`.
- **`yaml`** - reads YAML files (the Ansible inventory is written in
  YAML) and turns them into regular Python data (dictionaries and
  lists) the script can work with.

---

## Part 2: Configuration (lines after imports)

```python
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
IRM_WEBHOOK_URL = os.environ.get("IRM_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL environment variable not set")

INVENTORY_DIR = "inventory"
SNMP_COMMUNITY = os.environ.get("SNMP_COMMUNITY", "public")
POLL_INTERVAL = int(os.environ.get("SNMP_POLL_INTERVAL", "60"))
SNMP_TIMEOUT = 2
```

Same pattern as `netalert.py`: secrets and settings come from
**environment variables** (configuration set outside the script
itself), never hardcoded, so the script is safe to publish without
leaking anything. A few new ones specific to this script:

- `os.environ.get("SNMP_COMMUNITY", "public")` - the second argument
  to `.get()` is a **default value**. This reads the `SNMP_COMMUNITY`
  environment variable if it's set, and falls back to the plain text
  `"public"` if it isn't. (SNMP v2's "community string" is like a
  shared password - `"public"` is a very common default, fine for a
  home lab behind a VPN, not something to use on anything
  internet-facing.)
- `int(os.environ.get("SNMP_POLL_INTERVAL", "60"))` - same idea, but
  wrapped in `int(...)`, which converts text into a whole number,
  since environment variables are always plain text even when they
  represent a number.
- `INVENTORY_DIR = "inventory"` - a relative path (not starting with
  `/`), meaning "look for a folder named `inventory` inside whatever
  folder you're currently running this script from." This only works
  correctly if the script is run from the right starting folder - see
  the Usage note at the end.

```python
SYSUPTIME_OID = "1.3.6.1.2.1.1.3.0"
IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
OPER_STATUS_UP = "up"
```

An **OID** (Object Identifier) is SNMP's way of naming a specific
piece of information on a device - a long dotted number that means the
same thing on every SNMP-compliant device in the world, regardless of
vendor. Think of it like a universal part number instead of a
brand-specific one.

- `1.3.6.1.2.1.1.3.0` (`sysUpTime`) - "how long has this device been
  running since its last reboot." We don't actually care about the
  *value* here - we only care whether the device answers at all. A
  real answer means "reachable"; no answer means "not reachable."
- `1.3.6.1.2.1.2.2.1.2` (`ifDescr`) - the human-readable name of an
  interface (like `GigabitEthernet0/0`).
- `1.3.6.1.2.1.2.2.1.8` (`ifOperStatus`) - whether that interface is
  currently operationally up or down.

Both `ifDescr` and `ifOperStatus` are **table** OIDs, not single
values - a device with 8 interfaces has 8 separate answers for each,
one per interface, distinguished by an extra number (the *index*)
tacked onto the end of the OID. More on this in Part 5.

`OPER_STATUS_UP = "up"` looks trivial, but it's actually documenting a
real gotcha this script's author ran into: the command-line tool used
to query these OIDs (`snmpget`/`snmpwalk`) can be told to return
either the raw number SNMP uses internally (`1` for up, `2` for down,
etc.) or a human-readable translation (`"up"`, `"down"`) based on the
device's own MIB (a kind of dictionary SNMP devices publish, mapping
numbers to meanings). This script deliberately requests the
translated, readable version - confirmed by testing against a real
device before assuming either way - so the code compares against the
word `"up"`, not the number `1`.

---

## Part 3: The friendly-message lookup table

```python
DEVICE_INTERFACE_MAP = {
    ("EdgeR1", "GigabitEthernet0/0"): "EdgeR1 internet uplink is affected - verify ISP/WAN path.",
    ...
}
```

Identical idea to `netalert.py`'s table of the same name (see that
script's walkthrough, Part 6, for the full explanation of dictionaries
and tuples) - a lookup table mapping specific (device, interface)
pairs to a friendly, specific description.

This table is **intentionally duplicated** between the two scripts,
not shared. `netalert.py` isn't written in a way that would let this
script safely import pieces from it - as soon as you `import` a Python
file, every top-level line in it runs immediately, and `netalert.py`'s
top-level code checks for a webhook URL, waits for a log file to
exist, and then loops forever tailing it. Importing it would trigger
all of that instantly, which isn't what this script wants. Keeping a
second copy is simpler than restructuring a working, already-deployed
script - the tradeoff is that if the topology changes, both copies
need updating by hand.

---

## Part 4: Reading the device list from Ansible's inventory

```python
def load_all_hosts(site: str) -> dict:
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
```

This function's job: given a site name like `"MemberA"`, find every
device listed in that site's Ansible inventory file and return a
simple mapping of `{device name: IP address}`.

- `os.path.join(INVENTORY_DIR, f"{site}.yml")` - builds a file path by
  joining two pieces together correctly (handling the `/` between them
  for you), producing something like `"inventory/MemberA.yml"`. The
  `f"{site}.yml"` is an **f-string** (explained in the `netalert.py`
  walkthrough, Part 7) - it inserts the `site` value into the text.
- `if not os.path.isfile(...)`: `raise SystemExit(...)` - a safety
  check: if that file genuinely doesn't exist, stop immediately with a
  clear error rather than failing confusingly later.
- `with open(inventory_path) as f:` - opens the file for reading.
  `with ... as ...` is Python's way of saying "open this, do some work
  with it, and automatically close it again afterward, even if
  something goes wrong partway through" - you don't have to remember
  to close it yourself.
- `yaml.safe_load(f)` - reads the whole YAML file and converts it into
  regular Python data: nested dictionaries and lists. `safe_load`
  (versus a plain `load`) refuses to run any code that might be
  embedded in the YAML file itself - just in case, since this file
  could in principle come from somewhere untrusted. Ansible inventory
  files are naturally nested (a top-level group containing child
  groups, each containing hosts, each host having its own settings),
  so the result is a dictionary containing more dictionaries containing
  more dictionaries.
- `hosts = {}` - creates an empty dictionary to collect results into
  as we go.

The tricky part is the nested `def walk(node):` function - a function
**defined inside another function**. This is called a **recursive**
function: it calls *itself* to handle each layer of nesting, however
deep the inventory file happens to be structured, without needing to
know in advance how many layers there are.

- `if isinstance(node, dict):` - `isinstance(x, dict)` checks whether
  `x` is actually a dictionary (as opposed to a list, a string, or
  something else). Since a YAML file can nest dictionaries inside
  lists inside dictionaries in all sorts of ways, this check guards
  against trying to treat something as a dictionary when it isn't one.
- `for key, value in node.items():` - a loop that goes through every
  key/value pair in the current dictionary, one at a time.
- `if isinstance(value, dict) and "ansible_host" in value:` - checks
  two things: is this value itself a dictionary, AND does it contain
  a key named `"ansible_host"`? In Ansible inventory files, a specific
  host's settings look like
  `EdgeR1: { ansible_host: "10.20.0.1", ... }` - so a dictionary that
  has `ansible_host` inside it is, by definition, a single device's
  entry, not a group of devices.
- `hosts[key] = value["ansible_host"]` - if we found a device entry,
  record it: `key` is the device's name (like `"EdgeR1"`), and
  `value["ansible_host"]` is its IP address.
- `else: walk(value)` - if this particular value *wasn't* a device
  entry, it must be a deeper layer of grouping (a child group
  containing more hosts, or more groups) - so we call `walk` again on
  it, going one level deeper. This is the "recursive" part: the
  function keeps calling itself, one layer at a time, until it's
  visited the entire structure, however it's organized.
- `walk(inv)` - kicks the whole process off, starting from the very
  top of the file.
- `return hosts` - hands back the finished `{name: ip}` dictionary once
  the entire file has been walked.

The benefit of doing it this way, rather than assuming a fixed
structure (like "hosts are always exactly two levels deep"), is that
it works correctly no matter how the inventory file's groups are
organized - which matters here, since this same inventory also gets
used by Ansible playbooks that organize devices into several different
overlapping groups (by role, by whether they're currently powered on,
etc.).

---

## Part 5: Asking a device "are you there?" (single-value SNMP)

```python
def snmp_get(ip: str, oid: str) -> str:
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
```

This function runs the command-line tool `snmpget` once, to fetch a
single OID's value from a single device, and returns either the
answer (as text) or `None` ("nothing"/"failed") if anything went
wrong.

- `subprocess.run([...])` - similar to `subprocess.Popen` in
  `netalert.py`, but `run` is for a command you want to execute once
  and wait for, rather than a long-running process you keep reading
  from. The list of strings is the command and its arguments, exactly
  as you'd type them at a terminal, just split into separate pieces:
  - `"-v2c"` - use SNMP protocol version 2c.
  - `"-c", SNMP_COMMUNITY` - the community string (our "password").
  - `"-t", str(SNMP_TIMEOUT)` - how many seconds to wait for a
    response before giving up (converted to text with `str(...)`,
    since command-line arguments are always text).
  - `"-r", "1"` - retry once if the first attempt times out.
  - `"-Ovq"` - output formatting flags: value-only, quick/plain
    format, with no extra labels cluttering the result.
  - `ip, oid` - which device to ask, and which OID to ask about.
- `capture_output=True, text=True` - "capture whatever this command
  prints, and give it to me as readable text" (same idea as
  `netalert.py`'s `stdout=subprocess.PIPE, text=True`).
- `timeout=SNMP_TIMEOUT + 3` - a *second*, independent safety net:
  even though `snmpget` has its own `-t` timeout, this tells Python
  "if the whole command somehow takes longer than this, forcibly stop
  waiting" - a couple of extra seconds of headroom beyond `snmpget`'s
  own timeout, in case it doesn't behave exactly as expected.
- `try: ... except subprocess.TimeoutExpired: return None` - if that
  outer safety-net timeout is what actually fires, treat it exactly
  like any other failure: return `None`.
- `if result.returncode != 0: return None` - every command run on
  Linux reports back a number when it finishes; `0` conventionally
  means "success," anything else means "something went wrong." If
  `snmpget` itself reported failure, treat this as unreachable.
- The last few lines double-check the *content* of a successful-looking
  response, because `snmpget` can sometimes exit with code `0` but
  still print an error message as its "answer" (like "No Such Object"
  when a community string is wrong, or a plain "Timeout" message) -
  this catches those cases too, rather than trusting the exit code
  alone.

---

## Part 6: Asking a device about *all* its interfaces (table SNMP)

```python
def snmp_walk_table(ip: str, base_oid: str) -> dict:
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
```

`snmpget` fetches one value. `snmpwalk` fetches an entire **table** at
once - every interface's value for a given column, in one command.
This function runs `snmpwalk` and turns its output into a Python
dictionary.

A real `snmpwalk` result (with the `-Oqn` formatting flags used here)
looks like this:

```
.1.3.6.1.2.1.2.2.1.2.1 GigabitEthernet0/0
.1.3.6.1.2.1.2.2.1.2.2 GigabitEthernet0/1
.1.3.6.1.2.1.2.2.1.2.3 GigabitEthernet0/2
```

Each line is one interface's answer. The OID itself
(`1.3.6.1.2.1.2.2.1.2`) is the same for every line - what's different
is the extra number tacked onto the very end (`.1`, `.2`, `.3`), which
is the **index**: SNMP's way of saying "this particular row of the
table." The text after the OID is that row's actual value.

- `for line in result.stdout.splitlines():` - `splitlines()` breaks
  one big block of text into a list of individual lines, and the `for`
  loop goes through them one at a time.
- `re.match(r"^\.?[\d.]+\.(\d+)\s+(.+)$", line.strip())` - a regular
  expression (see `netalert.py`'s walkthrough, Part 5, for a fuller
  introduction to regex) that pulls the index number and the value
  apart from each other:
  - `^\.?` - the line might start with a literal `.` (it does, in the
    example above) - `?` makes it optional.
  - `[\d.]+` - one or more digits or dots (the bulk of the OID).
  - `\.(\d+)` - one final `.` followed by one or more digits, and
    *this* part is captured (the parentheses) as the index - this
    works because the very last number after the very last dot is
    always the table index, regardless of how long the rest of the OID
    is.
  - `\s+` - one or more spaces separating the OID from the value.
  - `(.+)$` - "capture everything else, all the way to the end of the
    line" - this is the actual value.
- `if match:` - only proceed if the line actually matched this
  pattern (guards against blank lines or unexpected output).
- `index, value = match.groups()` - unpacks the two captured pieces
  into two separate variables.
- `table[index] = value.strip('"')` - stores the value in our
  dictionary, keyed by index. `.strip('"')` removes surrounding quote
  characters if the value happens to have them (some SNMP values come
  back quoted).

Using the index as the dictionary key (rather than just trusting that
two separate `snmpwalk` calls will return their rows in the exact same
order) matters: it means we can safely combine two different tables by
matching up their indexes explicitly, rather than hoping position `3`
in one list lines up with position `3` in another.

---

## Part 7: Combining two tables into one useful answer

```python
def poll_interfaces(ip: str) -> dict:
    names = snmp_walk_table(ip, IF_DESCR_OID)
    statuses = snmp_walk_table(ip, IF_OPER_STATUS_OID)
    return {
        names[idx]: ("up" if statuses.get(idx) == OPER_STATUS_UP else "down")
        for idx in names
        if idx in statuses
    }
```

This function asks for both tables (names and operational statuses)
and merges them into the single answer the rest of the script actually
wants: `{interface name: "up" or "down"}`.

The `{...}` with a `for` inside it is called a **dictionary
comprehension** - a compact way of building a new dictionary by
transforming an existing collection, instead of writing out a full
`for` loop with explicit `.append()`-style steps. Read it like this,
right to left, then reassembled:

- `for idx in names` - go through every index we found in the names
  table.
- `if idx in statuses` - but only keep going if that same index also
  exists in the statuses table (guards against a mismatch, e.g. if one
  walk came back incomplete).
- `names[idx]` - use this interface's actual name as the new
  dictionary's key.
- `"up" if statuses.get(idx) == OPER_STATUS_UP else "down"` - a
  conditional expression (seen before in `netalert.py`'s walkthrough):
  if this interface's status exactly equals our `"up"` constant, the
  value is `"up"`; otherwise (down, or any other unexpected value like
  "testing" or "unknown"), treat it as `"down"` for alerting purposes.

The end result, for a device with 6 interfaces, looks like:

```python
{'GigabitEthernet0/0': 'up', 'GigabitEthernet0/1': 'up', 'Loopback0': 'up', ...}
```

---

## Part 8: Building the alert message

```python
def build_interface_message(host: str, interface: str, state: str) -> str:
    friendly = DEVICE_INTERFACE_MAP.get((host, interface))
    icon = "🔴" if state == "down" else "🟢"
    if friendly and state == "down":
        return f"{icon} {friendly}\nDevice: {host} | Interface: {interface} | State: DOWN"
    if friendly and state == "up":
        return (f"{icon} {host} {interface} has RECOVERED (previously flagged as down).\n"
                 f"Device: {host} | Interface: {interface} | State: UP")
    if state == "down":
        return f"{icon} Interface event: {host} {interface} changed state to DOWN"
    return (f"{icon} {host} {interface} has RECOVERED (previously flagged as down).\n"
             f"Device: {host} | Interface: {interface} | State: UP")
```

Very similar logic to `netalert.py`'s `build_message` function - look
up a friendly description if one exists, pick an emoji, and build the
right sentence for down/up/mapped/unmapped. One detail worth calling
out specifically, because it came from a real bug found by testing
this exact code against real devices:

**Every "came back up" message says "has RECOVERED," never just
"changed state to UP" - even for interfaces with no entry in
`DEVICE_INTERFACE_MAP`.** This wasn't the original design - the first
version had a single fallback branch shared by both the down and up
cases, so an un-mapped interface's recovery message just said "changed
state to UP." Testing against Grafana IRM (the incident-management
tool this script can also alert into) showed that a mapped interface's
alert would automatically resolve itself in Grafana once the recovery
message arrived, but an un-mapped interface's would not - even though
both used the same grouping key. Comparing the two messages side by
side, the only meaningful difference was that one said "RECOVERED" and
the other didn't. Grafana's own alert-matching logic appears to use
that word (or similar language) to decide whether an incoming message
represents a resolution, not just a new update - so the fallback
branch was rewritten to always use that wording on the way back up,
regardless of whether the interface has a custom description.

---

## Part 9: Sending the alert

```python
def send_alert(message: str, alert_key: str, noc_check_command: str = None) -> None:
    requests.post(WEBHOOK_URL, json={"content": message})
    if IRM_WEBHOOK_URL:
        payload = {"message": message, "alert_key": alert_key}
        if noc_check_command:
            payload["noc_check_command"] = noc_check_command
        try:
            requests.post(IRM_WEBHOOK_URL, json=payload, timeout=5)
        except requests.exceptions.RequestException:
            pass
```

Same two-destination pattern as `netalert.py`: Discord always gets the
message; Grafana IRM gets it too, but only if configured, and wrapped
in `try`/`except` so a Grafana problem can never interrupt the
Discord side (explained in full in `netalert.py`'s walkthrough, Part
9).

- `noc_check_command: str = None` - a **default parameter value**.
  This means callers of `send_alert` don't have to supply this
  argument at all; if they don't, it automatically becomes `None`.
  This is used for the device-level "entirely unreachable" alerts,
  which don't have one specific interface to hand off for
  investigation, unlike interface-level alerts (see Part 10).
- `if noc_check_command: payload["noc_check_command"] = ...` - only add
  this field to the outgoing message if one was actually provided.

---

## Part 10: The main loop

```python
def main() -> None:
    site = sys.argv[1] if len(sys.argv) > 1 else "MemberA"
    hosts = load_all_hosts(site)
    ...
    device_up = {host: None for host in hosts}
    iface_state = {}

    while True:
        for host, ip in hosts.items():
            reachable = snmp_get(ip, SYSUPTIME_OID) is not None

            if device_up[host] is not None and reachable != device_up[host]:
                ...
                send_alert(f"{icon} {host} {verb}.", f"{host}-snmp-device")
            device_up[host] = reachable

            if not reachable:
                continue

            for interface, state in poll_interfaces(ip).items():
                key = (host, interface)
                if iface_state.get(key) is not None and state != iface_state[key]:
                    send_alert(
                        build_interface_message(host, interface, state),
                        f"{host}-{interface}",
                        noc_check_command=f"noccheck {site} {host} {interface}",
                    )
                iface_state[key] = state

        time.sleep(POLL_INTERVAL)
```

This is where everything comes together, run once, then repeated
forever.

- `sys.argv[1] if len(sys.argv) > 1 else "MemberA"` - `sys.argv` is the
  list of everything typed on the command line when the script was
  started (`sys.argv[0]` is always the script's own name). This reads
  "if a second thing was typed (an actual site name), use that;
  otherwise, default to `"MemberA"`."
- `device_up = {host: None for host in hosts}` - another dictionary
  comprehension (see Part 7), building a starting dictionary where
  every host's remembered state is `None` ("we don't know yet").
  Starting at `None` rather than `True`/`False` matters: it means the
  very first poll of any device never triggers an alert (there's
  nothing to compare against yet), only a genuine *change* on a later
  poll does.
- `iface_state = {}` - starts empty; entries get added the first time
  each interface is ever seen.
- `while True:` - a loop with no condition that's ever false - this
  runs forever, until the program is stopped from outside (like a
  `systemd` service being stopped, or Ctrl+C).
- `for host, ip in hosts.items():` - go through every device, one at a
  time, each time through the outer loop.
- `reachable = snmp_get(ip, SYSUPTIME_OID) is not None` - ask "are you
  there?" `is not None` converts the result into a plain `True`/`False`:
  `True` if we got a real answer back, `False` if `snmp_get` returned
  `None` (any kind of failure).
- `if device_up[host] is not None and reachable != device_up[host]:` -
  "if we actually know this device's *previous* state (not the very
  first poll), AND that state is different from what we just found" -
  only then does this represent a genuine change worth alerting on.
- `device_up[host] = reachable` - regardless of whether we alerted,
  always update the remembered state to whatever we just found, ready
  for the next comparison.
- `if not reachable: continue` - `continue` immediately skips the rest
  of this loop's current pass and moves on to the next device. There's
  no point asking an unreachable device about its interfaces.
- The inner `for interface, state in poll_interfaces(ip).items():`
  loop does the exact same "compare to last time, alert if different,
  remember the new state" pattern, but per-interface instead of
  per-device, using a **tuple** `(host, interface)` as the dictionary
  key so that, say, `("DSW1", "GigabitEthernet0/0")` and
  `("DSW2", "GigabitEthernet0/0")` are correctly tracked as two
  completely separate things, even though they share an interface
  name.
- `time.sleep(POLL_INTERVAL)` - once every device has been checked,
  pause for the configured interval before starting the whole process
  over again.

```python
if __name__ == "__main__":
    main()
```

This is a very common Python idiom. `__name__` is a special
automatically-set value; it equals `"__main__"` only when this file is
run directly (like `python3 snmp_poll.py`), and something else if this
file were ever imported into another script instead. In practice here
it just means "actually run the `main()` function when this script is
executed" - written this way mostly as a widely recognized convention,
rather than out of strict necessity for a script this size.

---

## Summary: the full journey of one poll cycle

1. The script wakes up (either just started, or finished its last
   `time.sleep`).
2. For each device in the inventory: ask "are you there?" over SNMP.
3. If that answer differs from last time, send an alert (device fully
   up or fully down).
4. If the device answered at all, also ask "what state is every one of
   your interfaces in?"
5. For each interface, if its state differs from last time, send an
   alert - with the same "RECOVERED" wording on the way back up
   regardless of whether that interface has a custom friendly
   description, so Grafana IRM can auto-resolve consistently.
6. Remember every current state, for comparison next time.
7. Sleep for the configured interval, then start again from step 2.

---

## Usage

```bash
python3 snmp_poll.py [site]
```

Must be run from the same folder that contains the `inventory/`
directory (or a parent-relative setup matching `noc_check.py`'s own
convention) - it reads `inventory/<site>.yml`, defaulting to
`MemberA` if no site is given on the command line. See the main
`README.md` for environment variable setup and the included
`snmp_poll.service` template for running this as a persistent
background service.
