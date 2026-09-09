# How noc_check.py Works — A Walkthrough

This document explains `noc_check.py`, written for someone who has
never programmed in Python before. It assumes no prior coding
knowledge. If you haven't read `netalert_explained.md` yet, several
basic concepts (imports, functions, variables, if/else, try/except,
dictionaries, f-strings) are explained there in more depth - this
document moves a bit faster
over ideas already covered in those two, since this script is the
longest and most feature-rich of the three.

---

## The big picture, before we look at any code

This is an interactive tool a NOC technician runs by hand when an
alarm names a specific device and interface. It connects to that
device over SSH, asks it several questions (what's your interface
status? what are your optical light levels? what's your recent
history?), and prints a human-readable report ending in a suggested
next step - all based on the device's *current, live* state, not a
guess made back when the alert first fired.

---

## Part 1: The file header and imports (lines 1-34)

```python
#!/usr/bin/env python3
"""
noc_check.py
...
"""
```

The shebang (`#!/usr/bin/env python3`) is a note to the operating
system about how to run this file directly; the docstring below it is
a description of what the script does and how to use it.

```python
import sys
import os
import re
import getpass
import yaml
from datetime import datetime
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException
```

Some new toolboxes we haven't seen in the other two scripts:

- **`sys`** - lets Python read command-line arguments (the extra words
  you type after the script name, like `MemberA EdgeR1 GigabitEthernet0/0`)
  and exit the program with a specific status code.
- **`getpass`** - lets the script ask for a password without displaying
  it on screen as you type (unlike a normal `input()` prompt, which
  would show your password in plain text as you type it).
- **`yaml`** - lets Python read `.yml`/`.yaml` files (the same format
  your Ansible inventory files use) and turn them into Python data
  structures (dictionaries and lists) automatically.
- **`from datetime import datetime`** - brings in just one specific
  tool, `datetime`, from a larger toolbox called `datetime` (yes, the
  toolbox and the tool inside it share the same name, which is a
  little confusing but common in Python). This lets the script work
  with dates and times - calculating how long ago something happened,
  for example.
- **`from netmiko import ConnectHandler`** - Netmiko is a third-party
  library (not built into Python itself, but installed separately)
  specifically designed for connecting to network devices like routers
  and switches over SSH and running commands on them. `ConnectHandler`
  is the main tool it provides for actually opening a connection.
- **`from netmiko.exceptions import ...`** - brings in two specific
  *types* of errors Netmiko can raise, so our script can specifically
  detect and handle "authentication failed" versus "connection timed
  out" as two different situations, giving the user a more precise
  error message for each.

---

## Part 2: Configuration dictionaries (lines 36-68)

```python
INVENTORY_DIR = "inventory"
```

A plain variable holding the folder name where site inventory files
live.

```python
WAN_CIRCUIT_MAP = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/0"): "WAN link to ATT CID -test.203203.ATT",
    ...
}
```

A dictionary (explained in `netalert_explained.md`) where each key is
a three-part tuple - site, device, interface - and each value is the
specific carrier/circuit text to show when that exact interface is
found down. This lets a tech immediately know which carrier to call
and what circuit ID to reference, without hunting through separate
documentation.

```python
VRRP_CHECK_ON_DOWN = {
    ("MemberA", "EdgeR1", "GigabitEthernet0/0"): "WAN uplink down",
    ...
}
```

A second dictionary with the same three-part-tuple key style. This one
marks which specific interfaces are "redundancy-relevant" - when one
of these goes down, the script will do extra work later (checking VRRP
status) to tell the tech whether a backup device automatically took
over.

```python
RECENT_HISTORY_COUNT = 10
```

A simple number - how many recent up/down log events to display.

---

## Part 3: Finding sites and looking up device IPs (lines 71-107)

```python
def list_available_sites() -> list:
    if not os.path.isdir(INVENTORY_DIR):
        return []
    return sorted(
        f[:-4] for f in os.listdir(INVENTORY_DIR)
        if f.endswith(".yml")
    )
```

`os.path.isdir(...)` checks whether the `inventory` folder actually
exists at all; if not, return an empty list right away rather than
crashing trying to look inside a folder that isn't there.

`os.listdir(INVENTORY_DIR)` gets a list of every file/folder name
inside the inventory directory. The line
`f[:-4] for f in os.listdir(...) if f.endswith(".yml")` is a
**list comprehension** - a compact way of writing "build a new list by
taking each item (`f`), only keeping the ones that pass a test
(`if f.endswith(".yml")`, meaning the filename ends in `.yml`), and
transforming each surviving item somehow (`f[:-4]`, which chops off
the last 4 characters - removing the `.yml` extension so we're left
with just the site name)." `sorted(...)` wrapping the whole thing
alphabetizes the final result.

```python
def load_inventory_host(site: str, hostname: str) -> str:
    inventory_path = os.path.join(INVENTORY_DIR, f"{site}.yml")

    if not os.path.isfile(inventory_path):
        return None

    with open(inventory_path) as f:
        inv = yaml.safe_load(f)
```

`os.path.join(...)` safely combines a folder name and a filename into
one full path (handling the slash between them correctly, regardless
of operating system). `os.path.isfile(...)` checks whether that exact
file genuinely exists.

`with open(inventory_path) as f:` is Python's standard way of opening
a file. The `with` keyword guarantees the file gets properly closed
again afterward, even if something goes wrong while reading it -
you don't have to remember to close it yourself. `yaml.safe_load(f)`
reads the file's content and converts it from YAML text into a real
Python dictionary/list structure that the rest of the code can work
with directly.

```python
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
```

This defines a function *inside* another function - completely valid
in Python, and useful when a helper is only ever needed in one specific
place. `search` is a **recursive function** - a function that calls
*itself* - used here to dig through the inventory file's nested
structure (which can have groups containing groups containing hosts,
several layers deep) without needing to know in advance exactly how
many layers deep the structure goes.

`isinstance(node, dict)` checks "is this specific piece of data a
dictionary?" (as opposed to, say, a list or plain text). If the
hostname we're looking for is a key in this dictionary, and its value
is itself a dictionary containing `"ansible_host"`, we found what we
wanted - return that IP address immediately. Otherwise, loop through
every value in this dictionary and recursively call `search` on each
one, since the host we want might be buried inside one of them. If
nothing is ever found anywhere, the function naturally returns `None`.

---

## Part 4: Connecting and running commands (lines 110-130)

```python
def connect(host_ip: str, username: str, password: str):
    device = {
        "device_type": "cisco_ios",
        "host": host_ip,
        "username": username,
        "password": password,
    }
    return ConnectHandler(**device)
```

This builds a dictionary describing the connection Netmiko should make
(what kind of device it is, its address, and login credentials), then
calls `ConnectHandler(**device)`. The `**` before `device` is a Python
feature called **dictionary unpacking** - it takes a dictionary and
"spreads" its key-value pairs out as individual named arguments to the
function, as if you'd written
`ConnectHandler(device_type="cisco_ios", host=host_ip, ...)` directly.
This is a common, tidy way to pass a bunch of settings at once.

```python
def run_command(conn, command: str) -> str:
    try:
        return conn.send_command(command)
    except Exception:
        return ""
```

A small helper: given an already-open connection and a command to run
(like `"show interface GigabitEthernet0/0"`), attempt to run it and
return the text output. If anything goes wrong (the command isn't
supported on this particular device, for example), quietly return an
empty string instead of crashing - this lets the rest of the script
keep going and gather whatever *other* information it can, rather than
the whole tool failing just because one specific command didn't work.

---

## Part 5: Parsing interface status (lines 133-147)

```python
def parse_state(raw_output: str) -> dict:
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
```

Same regex-with-named-captures idea from `netalert_explained.md`,
applied here to a real `show interface` output line like
`GigabitEthernet0/0 is down, line protocol is down`. This function
returns a small dictionary with two keys, `status` and `protocol`,
holding the two states IOS reports separately (a real router can be
"administratively down" for status while its protocol is also down, or
various other combinations - this is genuinely useful diagnostic
information).

---

## Part 6: Calculating how long something has been in its current state (lines 150-211)

```python
def calculate_duration_in_state(recent_events: list, current_state: str) -> str:
```

This function does real date/time math to answer "how long has this
interface actually been up or down," based on the most recent matching
event found in the device's own log history (gathered by a different
function, covered in Part 7).

```python
    if not recent_events:
        return f"Interface is currently {current_state.upper()} - no recent state-change history available to calculate duration."
```

If we have no history at all to work with, say so honestly rather than
guessing.

```python
    most_recent = recent_events[0]
    match = re.match(r"(\w{3} \d{1,2} \d{2}:\d{2}:\d{2})\s+(UP|DOWN)", most_recent)
    if not match:
        return f"Interface is currently {current_state.upper()} - could not parse timing from device history."
```

`recent_events[0]` grabs the very first item in the list (Python lists
are numbered starting from 0, not 1 - so `[0]` means "the first one").
Since the history list is built most-recent-first (explained in Part
7), this gives us the latest event. We then run a regular expression
against it to pull out the timestamp text and the state
(UP/DOWN) as two separate captured groups this time, using unnamed
groups (just plain parentheses) instead of the named
`(?P<name>...)` style we've used elsewhere - both styles work, unnamed
groups are just retrieved by position instead of by name.

```python
    timestamp_str, logged_state = match.groups()
    if logged_state.lower() != current_state.lower():
        return (
            f"Interface is currently {current_state.upper()}, but the most recent "
            f"logged event ({most_recent.strip()}) doesn't match - state may have "
            f"just changed. Check current status above as the source of truth."
        )
```

`match.groups()` returns both captured pieces at once as a tuple, and
this line unpacks them into two separate variables in one step (the
same unpacking idea used in `netalert.py`'s
`message, host = build_message(line)`). This is a genuine sanity
check: if the *live* current state (passed in from elsewhere) doesn't
match what the *most recent logged event* says, something changed very
recently and hasn't fully propagated through the log yet - rather than
reporting a confusing or wrong duration, the function honestly flags
this mismatch instead.

```python
    try:
        event_time = datetime.strptime(f"{datetime.now().year} {timestamp_str}", "%Y %b %d %H:%M:%S")
        delta = datetime.now() - event_time
        if delta.total_seconds() < 0:
            return f"Interface has been {current_state.upper()} since {timestamp_str} (device time)."
        return f"Interface has been {current_state.upper()} for {format_timedelta(delta)} (since {timestamp_str})."
    except ValueError:
        return f"Interface is currently {current_state.upper()} - could not calculate exact duration."
```

`datetime.now()` gets the current date and time. `datetime.strptime(...)`
does the reverse of what you might expect - it *parses* a text string
into a real `datetime` value that Python can do math with, according to
a format pattern you specify (`"%Y %b %d %H:%M:%S"` means "4-digit
year, then abbreviated month name, then day, then hour:minute:second" -
matching exactly how we built the text right before it). Since device
logs don't include the year at all, we glue the *current* year onto the
front of the timestamp text before parsing, which is a reasonable
assumption for anything recent enough to still be in the log buffer.

Subtracting one `datetime` from another (`datetime.now() - event_time`)
gives you a **timedelta** - a value representing a *span* of time
(so many days, hours, minutes, seconds) rather than a specific moment.
If that span comes out negative (which shouldn't normally happen, but
could from clock differences or a year boundary edge case), we handle
that gracefully rather than showing a nonsensical negative duration.
Otherwise, we hand the timedelta to `format_timedelta` (right below)
to turn it into readable text.

The `try`/`except ValueError` wraps this in case the timestamp text
ever doesn't match the expected format exactly, which would otherwise
crash this specific calculation.

```python
def format_timedelta(delta) -> str:
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
```

This turns a timedelta into something like `"2d 3h 15m"`.
`delta.total_seconds()` converts the whole span into one plain number
of seconds. `divmod(a, b)` is a built-in Python function that does
division and gives you *both* the whole-number result and the
remainder at once - `divmod(total_seconds, 86400)` divides by the
number of seconds in a day, giving you "how many whole days" and
"how many seconds are left over after removing those days," in one
step. The same pattern repeats for hours (3600 seconds each) and
minutes (60 seconds each), progressively breaking the total down.

`parts = []` creates an empty list. Each `if days:` check (a number
other than zero is treated as "true" in Python, so `if days:` really
means "if days is not zero") appends a formatted piece of text to the
list, but only for units that are actually non-zero - so a duration of
"5 minutes" won't show "0d 0h 5m," just "5m." If the duration is under
a minute entirely (so `parts` is still empty after all three checks),
we fall back to showing seconds instead. `" ".join(parts)` glues
everything in the list together into one string, with a space between
each piece.

---

## Part 7: Reading transceiver light levels and recent history (lines 214-256)

```python
def parse_transceiver_summary(raw_transceiver_output: str) -> str:
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
```

This function handles three different possible situations gracefully,
in order: no data returned at all (command unsupported), a copper
(non-fiber) interface that explicitly says it has no transceiver, or a
genuine fiber interface with real optical power readings to extract.
The regex pattern `-?\d+\.\d+` matches an optional minus sign (since
optical power readings are commonly negative, measured in dBm),
followed by digits, a decimal point, and more digits - capturing
numbers like `-3.24`.

```python
def parse_recent_history(raw_log_output: str, interface: str) -> list:
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

    events.reverse()
    return events[:RECENT_HISTORY_COUNT]
```

`raw_log_output.splitlines()` breaks one long block of text into a
list of individual lines. `if interface not in line: continue` skips
any log line that doesn't even mention the interface we care about -
`continue` means "stop processing this particular line right now, and
jump straight to the next one in the loop," without running the rest
of the loop's code for this line.

For lines that do mention our interface, we run a pattern to pull out
the timestamp and state, build a clean formatted string, and
`.append(...)` it onto our growing `events` list.

`events.reverse()` flips the list's order in place (log files are
normally oldest-first, but we want most-recent-first for display).
`events[:RECENT_HISTORY_COUNT]` is **slicing** - taking just a portion
of a list. This particular slice means "everything from the beginning
up to (but not including) position `RECENT_HISTORY_COUNT`" - in other
words, just the first 10 items, discarding anything beyond that.

---

## Part 8: Translating VRRP status into plain language (lines 259-292)

```python
def describe_vrrp_status(raw_vrrp_output: str, hostname: str) -> str:
    lines = raw_vrrp_output.splitlines()
    master_count = 0
    backup_count = 0
    for line in lines:
        if re.search(r"\bMaster\b", line, re.IGNORECASE):
            master_count += 1
        elif re.search(r"\bBackup\b", line, re.IGNORECASE):
            backup_count += 1
```

This counts how many lines of `show vrrp brief` output mention
"Master" versus "Backup" - since a router can be VRRP master for some
VLANs and backup for others simultaneously, counting rather than just
checking for a single overall answer captures that nuance correctly.
`\b` in a regex pattern means a **word boundary** - it makes sure we
match the whole word "Master," not accidentally matching "Master" as
part of some longer unrelated word.

```python
    if master_count == 0 and backup_count == 0:
        return "   Could not determine active/standby role from device output - review manually."

    if backup_count == 0:
        return (...)
    if master_count == 0:
        return (...)
    return (...)
```

Four distinct possible outcomes, each with its own plain-language
message: nothing recognized at all, master for everything, backup for
everything, or a genuine mixed state - the function picks whichever
description accurately matches the counts.

---

## Part 9: Deciding what to actually tell the technician (lines 295-337)

```python
def suggest_next_step(state: dict, site: str, hostname: str, interface: str) -> str:
    status = state["status"]
    protocol = state["protocol"]

    if status == "up" and protocol == "up":
        return "Interface is healthy (up/up). No action needed."
```

This function is the heart of the tool's usefulness - turning raw
technical state into an actionable recommendation. Each `if` branch
below handles one specific combination of status and protocol.

```python
    if status == "down" and protocol == "down":
        circuit = WAN_CIRCUIT_MAP.get((site, hostname, interface))
        if circuit:
            return (
                f"Interface is physically down (down/down).\n"
                f"   {circuit}\n"
                f"   Contact carrier and reference the circuit ID above. ..."
            )
        return (
            "Interface is physically down (down/down).\n"
            "   Check cabling, remote end device, ..."
        )
```

This is where `WAN_CIRCUIT_MAP` from Part 2 gets used - looking up
whether this exact site/device/interface combination has known carrier
information. `if circuit:` checks whether the lookup found something
(a non-empty result is treated as true) - if so, the message includes
the specific circuit details; if not, it falls back to generic
troubleshooting advice.

---

## Part 10: The main function - tying everything together (lines 340-443)

```python
def main():
    if len(sys.argv) == 4:
        site = sys.argv[1]
        hostname = sys.argv[2]
        interface = sys.argv[3]
    elif len(sys.argv) == 1:
        ...
        site = input("Site: ").strip()
        hostname = input("Device hostname (e.g. EdgeR1): ").strip()
        interface = input("Interface (e.g. GigabitEthernet0/0): ").strip()
    else:
        print("Usage: ...")
        sys.exit(1)
```

`sys.argv` is a list containing everything typed on the command line -
`sys.argv[0]` is always the script's own name, and anything after that
is the actual arguments the user provided. `len(sys.argv)` counts how
many items are in that list. If exactly 4 items exist (script name
plus site, device, interface), use them directly. If only 1 item exists
(just the script name, meaning no extra arguments were given at all),
switch to interactive mode and use `input(...)` to ask the questions
one at a time - `.strip()` removes any accidental extra spaces the
user might type. Any other number of arguments means the user typed
something unexpected, so print usage instructions and
`sys.exit(1)` - stopping the program immediately with an exit code of
1 (the standard convention: 0 means success, any non-zero value
signals "this program ended due to an error").

```python
    host_ip = load_inventory_host(site, hostname)
    if not host_ip:
        available = list_available_sites()
        print(f"Could not find '{hostname}' in inventory/{site}.yml")
        if available:
            print(f"   Known sites: {', '.join(available)}")
        sys.exit(1)
```

Uses our function from Part 3 to look up the device's real IP address.
If nothing was found, print a clear error (including a helpful list of
sites that *do* exist, if any) and stop.

```python
    username = input("Username: ")
    password = getpass.getpass("Password: ")
```

Prompts for credentials - `getpass.getpass(...)` specifically hides
the password as it's typed, unlike a normal `input(...)` call.

```python
    try:
        conn = connect(host_ip, username, password)
    except NetmikoAuthenticationException:
        print(f"Authentication failed connecting to {hostname}.")
        sys.exit(1)
    except NetmikoTimeoutException:
        print(f"Could not reach {hostname} ({host_ip}) - connection timed out.")
        print("   This itself may be meaningful - the device or its path may be down.")
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error connecting to {hostname}: {exc}")
        sys.exit(1)
```

Attempts to actually open the SSH connection using our `connect`
function from Part 4. Notice there are **three** separate `except`
blocks here, each catching a different, more specific type of problem
before a final, general catch-all - this lets the script give a
precise, useful message depending on exactly what went wrong (wrong
password, versus device unreachable entirely, versus something else
unexpected), rather than one generic "something broke" message for
every possible failure.

```python
    raw_interface_output = run_command(conn, f"show interface {interface}")
    state = parse_state(raw_interface_output)
```

Runs the actual `show interface` command using our helper from Part 4,
then parses the result using our function from Part 5.

The rest of `main()` (roughly line 388 onward) is a straightforward
sequence: print the basic status, gather and display recent history,
print the suggested next step, calculate and print time-in-state,
gather and print transceiver info, and - only if the interface is both
down *and* listed in `VRRP_CHECK_ON_DOWN` - run an extra VRRP check and
print that too. Each of these sections follows the same basic pattern:
run a command with `run_command`, process the result with one of our
helper functions, and `print(...)` the result to the screen.

```python
    conn.disconnect()

    print("\n--- Raw show interface output ---")
    print(raw_interface_output)
```

Once every check is done, `conn.disconnect()` properly closes the SSH
connection (good practice - don't leave connections open longer than
necessary), and finally the complete raw, unmodified `show interface`
output is printed at the very end, so nothing is ever hidden from a
technician who wants to look at the original data themselves.

```python
if __name__ == "__main__":
    main()
```

A standard Python pattern - only run `main()` if this file was
executed directly, not when it's imported by another file.

---

## Summary: the full journey of one investigation

1. You run `noccheck MemberA R1 GigabitEthernet0/0` (or the equivalent
   interactive prompts).
2. The script figures out R1's real IP address from
   `inventory/MemberA.yml`.
3. It asks for your username and password, and opens an SSH connection.
4. It runs `show interface GigabitEthernet0/0` and parses the raw
   status/protocol out of the response.
5. It runs a show-logging command filtered to that interface, and
   builds a clean, most-recent-first history of recent up/down events.
6. Using that history, it calculates a real "how long has this been in
   its current state" duration.
7. It runs a transceiver command and extracts optical power levels, if
   applicable.
8. Based on the current status/protocol, it decides on and prints a
   specific, plain-language suggested next step - including a real
   carrier/circuit ID if one is on file for this exact interface.
9. If the interface is down *and* it's one of the specific interfaces
   marked as VRRP-relevant, it runs one more command and translates
   the redundancy status into plain language.
10. It closes the connection and prints the full raw output at the
    end, so nothing is hidden from you.