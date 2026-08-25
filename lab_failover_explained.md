# How lab_failover.py Works — A Line-by-Line Walkthrough

This document explains every part of `lab_failover.py`, written for
someone who has never programmed in Python before. It assumes no prior
coding knowledge - every Python concept is explained the first time it
appears. If you haven't read `netalert_explained.md` yet, some basic
concepts (imports, functions, variables, if/else) are explained there
in more depth - this document builds on those without re-explaining
every single one from scratch.

---

## The big picture, before we look at any code

This script solves one problem: the laptop needs a working path into
the lab network, but that path runs through one of two edge routers
(EdgeR1 or EdgeR2), and either one could go down at any time. Rather
than making you manually change routing commands during an outage,
this script runs forever in the background, continuously checking both
paths, and automatically switches the laptop's routes to whichever one
is currently healthy.

---

## Part 1: The file header (lines 1-15)

```python
#!/usr/bin/env python3
"""
lab_failover.py
...
"""
```

The very first line, starting with `#!`, is called a **shebang** - on
Linux, it tells the operating system "run this file using the program
found at this path" (in this case, Python 3). This line only matters
if you try to run the file directly as a program (like
`./lab_failover.py`); it's ignored when Python itself reads the file.

The text between the two sets of three quotation marks (`"""`) is a
**docstring** - a special kind of comment placed right at the top of a
file (or function) to describe what it does. Python treats anything
between matching sets of `"""` as one long block of text, even across
multiple lines, which is why this comment doesn't need a `#` on every
single line the way a normal comment would.

---

## Part 2: Imports (lines 17-19)

```python
import subprocess
import time
import logging
```

Same idea as in `netalert.py` - each of these brings in a "toolbox" of
pre-built functionality.

- **`subprocess`** - lets this script run other programs, like `ping`
  and `ip route`.
- **`time`** - lets the script pause/wait.
- **`logging`** - Python's built-in system for writing organized,
  timestamped log messages to a file - much more structured than just
  printing plain text, since it automatically adds timestamps and
  severity levels (info, warning, error, critical) to every message.

---

## Part 3: Configuration (lines 21-45)

```python
EDGE_R1_OUTSIDE_IP = "<edgeR1-outside-ip>"
EDGE_R2_OUTSIDE_IP = "<edgeR2-outside-ip>"

R1_PTP_IP = "<r1-to-edgeR1-ptp-ip>"
R2_PTP_IP = "<r2-to-edgeR2-ptp-ip>"
```

These are just variables (named containers holding a value) storing
the real IP addresses this script needs to know about. The
`<placeholder-text>` versions shown here are meant to be replaced with
your actual IP addresses before running the script for real.

```python
ROUTE_DESTINATIONS = [
    "10.0.0.0/8",
    "1.1.1.1/32",
    "2.2.2.2/32",
    "3.3.3.3/32",
    "4.4.4.4/32",
]
```

The `[ ]` square brackets here create a **list** - an ordered
collection of items (unlike the "set" we saw in `netalert.py`, a list
can contain duplicates and always remembers the order items were added
in). This list holds every network destination the laptop needs a
route to - the whole lab supernet, plus each router's individual
loopback address.

```python
CHECK_INTERVAL_SECONDS = 5
FAILURES_BEFORE_SWITCH = 3
SUCCESSES_BEFORE_SWITCH_BACK = 3

LOG_FILE = "/var/log/lab-failover.log"
```

More plain configuration values - how often to check (every 5 seconds),
how many consecutive failures/successes are needed before actually
switching paths (3 each way - this exists specifically to avoid
"flapping," switching back and forth rapidly just because of one
random dropped packet), and where to write the log file.

---

## Part 4: Setting up logging (lines 49-53)

```python
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
```

This configures the `logging` toolbox we imported earlier - a
one-time setup that tells it: write log messages to `LOG_FILE`, include
anything rated "INFO" severity or higher (INFO, WARNING, ERROR,
CRITICAL - but not the more verbose "DEBUG" level), and format each
line as timestamp, then severity level, then the actual message text.
Once this is configured, every `logging.info(...)`,
`logging.warning(...)`, etc. call later in the script automatically
follows this format without needing to repeat these settings.

---

## Part 5: The ping function (lines 56-67)

```python
def ping(ip: str, timeout_seconds: int = 2) -> bool:
    """Return True if a single ping to ip succeeds."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout_seconds), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception as exc:
        logging.error(f"Ping to {ip} raised an exception: {exc}")
        return False
```

This function takes an IP address and returns either `True` (ping
succeeded) or `False` (it didn't).

`subprocess.run([...])` is similar to `subprocess.Popen` from
`netalert.py`, but simpler - `.run()` waits for the other program to
completely finish before continuing, then hands back its result,
whereas `.Popen()` (used in `netalert.py`) keeps running alongside your
script and streams output continuously. Here, we want to run one single
ping and wait for the definitive answer before moving on, which is
exactly what `.run()` is built for.

The list `["ping", "-c", "1", "-W", str(timeout_seconds), ip]` is the
actual command being run, broken into separate pieces exactly as you'd
type it in a terminal: `ping -c 1 -W 2 <ip>` (send 1 packet, wait up to
2 seconds for a reply). `str(timeout_seconds)` converts the number `2`
into the text `"2"`, since command-line arguments always need to be
text, not numbers.

`stdout=subprocess.DEVNULL` and `stderr=subprocess.DEVNULL` tell Python
"throw away any text this command would normally print - we only care
about whether it succeeded or failed, not what it printed."

Every real program, when it finishes running, reports back a **return
code** - by long-standing convention, `0` means "success," and any
other number means "something went wrong." `result.returncode == 0`
checks for that success code specifically, and the function returns
`True` or `False` based on it.

The `try` / `except` wrapper (same concept as in `netalert.py`) catches
any unexpected problem running the command at all (not just "ping
failed to reach the host," but something more fundamental going wrong),
logs it, and safely returns `False` rather than crashing the whole
script.

---

## Part 6: The generic command runner (lines 70-77)

```python
def run(cmd: list) -> None:
    """Run a shell command, logging failures."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logging.warning(f"Command failed: {' '.join(cmd)} - {result.stderr.strip()}")
    except Exception as exc:
        logging.error(f"Command raised an exception: {' '.join(cmd)} - {exc}")
```

This is a general-purpose helper for running *any* command (used later
for things like `ip route add`), not just ping specifically.

`(cmd: list)` means this function expects to receive a list (like
`["ip", "route", "add", ...]`) as its input.

`capture_output=True` tells Python to capture both the normal output
and any error messages the command produces, so we can inspect them
afterward - `result.stderr` holds whatever error text the command
printed, if any.

`' '.join(cmd)` takes our list of separate command pieces and joins
them back together into one readable string, with a single space
between each piece - purely for writing a clear log message. `' '` here
is literally a single space character being used as the "glue" between
list items.

Unlike the `ping` function, this one doesn't return `True`/`False` at
all (`-> None` means "this function doesn't hand back any particular
value") - it's used purely for its side effect (actually running the
command), with logging as a safety net if something goes wrong.

---

## Part 7: Applying routes (lines 80-86)

```python
def set_routes(via_ip: str) -> None:
    """Point all lab route destinations at via_ip, removing any
    existing route for each destination first."""
    for dest in ROUTE_DESTINATIONS:
        run(["sudo", "ip", "route", "del", dest])
    for dest in ROUTE_DESTINATIONS:
        run(["sudo", "ip", "route", "add", dest, "via", via_ip])
```

This function takes one IP address (`via_ip` - whichever edge router
we've decided is currently healthy) and updates every single
destination in our `ROUTE_DESTINATIONS` list to route through it.

Two separate `for` loops: the first goes through every destination and
deletes any existing route for it (using our `run` helper from Part 6
to actually execute `sudo ip route del <destination>`), and the second
goes through the same list again, this time adding a fresh route for
each destination, pointed at `via_ip`.

Doing delete-everything-first, then add-everything-fresh (rather than
trying to update each one in place) keeps the logic simple and
guarantees a clean, consistent result every time this function runs,
regardless of whatever routes might have existed beforehand.

---

## Part 8: Keeping the test routes alive (lines 89-95)

```python
def ensure_ptp_test_routes() -> None:
    """Ensure the two PTP test routes exist so the script always has
    something to test reachability over, even right after a fresh
    start (service restart, reboot, or the laptop waking from sleep
    and losing any manually-added routes)."""
    run(["sudo", "ip", "route", "replace", f"{R1_PTP_IP}/32", "via", EDGE_R1_OUTSIDE_IP])
    run(["sudo", "ip", "route", "replace", f"{R2_PTP_IP}/32", "via", EDGE_R2_OUTSIDE_IP])
```

This is a small but important safety function. In order for the `ping`
function to even attempt reaching `R1_PTP_IP`/`R2_PTP_IP` at all, the
laptop needs a route to those specific addresses in the first place -
without one, the ping would simply fail with "no route to host," which
would look identical to "the path is genuinely down," even when it
isn't.

`ip route replace` (rather than `ip route add`) is used deliberately
here - `replace` either creates the route if it's missing, or silently
updates it if it already exists, without ever throwing an error either
way. This makes it safe to run repeatedly, over and over, every single
check cycle, which is exactly what happens (see Part 9) - guarding
against these specific routes ever silently disappearing (which was
observed happening after the laptop went to sleep and reconnected to
the network).

---

## Part 9: The main function (lines 98-163)

```python
def main():
    current_path = None  # "edgeR1", "edgeR2", or None (unknown/down)
    edgeR1_fail_count = 0
    edgeR2_fail_count = 0
    edgeR1_success_count = 0
```

This is the function that actually drives the whole program. It starts
by setting up four **tracking variables** that will change over time as
the script runs: `current_path` remembers which edge router the laptop
is currently routed through (starting as `None`, meaning "we don't know
yet"), and the three counters track consecutive successes/failures for
each path.

```python
    logging.info("lab_failover starting up")

    while True:
```

`while True:` creates an intentionally **infinite loop** - since `True`
is always true, this loop never naturally ends on its own. This is
exactly what we want for a background service meant to run forever,
continuously monitoring, until something external (like `systemctl
stop`) forcibly ends the program.

```python
        ensure_ptp_test_routes()

        edgeR1_ok = ping(R1_PTP_IP)
        edgeR2_ok = ping(R2_PTP_IP)
```

Every single time through the loop: first make sure our test routes
still exist (Part 8), then actually ping both paths, storing each
result (`True` or `False`) in its own variable.

```python
        if edgeR1_ok:
            edgeR1_fail_count = 0
            edgeR1_success_count += 1
        else:
            edgeR1_fail_count += 1
            edgeR1_success_count = 0
```

This updates our counters based on what just happened. `+= 1` is a
shorthand for "add 1 to whatever this variable currently holds" (the
long way of writing the same thing would be
`edgeR1_success_count = edgeR1_success_count + 1`). So: if EdgeR1's
ping just succeeded, reset the failure counter to zero and increase the
success counter by one; if it failed, do the exact opposite - the
failure counter goes up, and the success counter resets to zero. This
is exactly how we track "how many times *in a row*" something has
happened - any single opposite result immediately breaks the streak.

```python
        if edgeR2_ok:
            edgeR2_fail_count = 0
        else:
            edgeR2_fail_count += 1
```

The same idea for EdgeR2, though notice this one only tracks a failure
counter, not a success counter - that's because EdgeR2 is only ever
used as the *backup* path in this design; we don't need to track "how
many successes before switching back to EdgeR2," only "how many
failures before we know it can't be relied on right now."

```python
        if edgeR1_ok and current_path != "edgeR1" and edgeR1_success_count < SUCCESSES_BEFORE_SWITCH_BACK:
            pass
```

`!=` means "does not equal." This first condition checks: "EdgeR1 is
currently working, we're not already using it, but we haven't seen
enough consecutive successes yet to trust it." `pass` is a special
Python keyword meaning "do absolutely nothing here" - it's used because
Python's `if` blocks aren't allowed to be completely empty, so `pass`
is a placeholder that satisfies that rule while genuinely doing
nothing. In plain English: "EdgeR1 looks like it might be back, but
let's wait a bit longer before trusting it and switching back."

```python
        elif edgeR1_ok and current_path != "edgeR1" and edgeR1_success_count >= SUCCESSES_BEFORE_SWITCH_BACK:
            logging.info(...)
            set_routes(EDGE_R1_OUTSIDE_IP)
            current_path = "edgeR1"
```

`elif` means "else, if" - only checked when the first `if` above it was
false. `>=` means "greater than or equal to." This condition: "EdgeR1
is working, we're not currently using it, AND we've now seen *enough*
consecutive successes" - the moment this becomes true, we actually
switch: log what happened, call `set_routes` from Part 7 to update the
real routing table, and update `current_path` to remember we're now on
EdgeR1.

```python
        elif current_path == "edgeR1" and edgeR1_fail_count >= FAILURES_BEFORE_SWITCH:
            if edgeR2_ok:
                logging.warning(...)
                set_routes(EDGE_R2_OUTSIDE_IP)
                current_path = "edgeR2"
            else:
                logging.critical("Both EdgeR1 and EdgeR2 paths are down - no route available!")
                current_path = None
```

This branch handles the opposite situation: we're currently on EdgeR1,
and it's now failed enough times in a row to no longer be trusted. Here
there's an `if`/`else` **nested inside** this `elif` block - a smaller
decision made within the larger one. If EdgeR2 happens to be working
right now, switch over to it. If EdgeR2 is *also* down, there's nothing
useful to do - log the most severe kind of message ("critical"), and
set `current_path` back to `None`, honestly representing "we don't
currently have a working path at all," rather than pretending we're
still successfully using a broken one.

```python
        elif current_path is None:
            if edgeR1_ok:
                logging.info("Establishing initial route via EdgeR1")
                set_routes(EDGE_R1_OUTSIDE_IP)
                current_path = "edgeR1"
            elif edgeR2_ok:
                logging.info("Establishing initial route via EdgeR2 (EdgeR1 unavailable)")
                set_routes(EDGE_R2_OUTSIDE_IP)
                current_path = "edgeR2"
```

`is None` is the correct, idiomatic way in Python to check "does this
variable hold nothing/no value" (rather than using `== None`, which
works but isn't the conventional style). This branch handles the very
first time the script runs (or any time we've fallen back to "no known
working path" from the branch above) - if EdgeR1 works, use it; if not,
but EdgeR2 works, use that instead. If neither works, this whole block
is simply skipped and we try again next cycle.

```python
        elif current_path == "edgeR2" and edgeR2_fail_count >= FAILURES_BEFORE_SWITCH:
            logging.critical(...)
            current_path = None
```

The last branch: we're currently on EdgeR2 (our backup path) and it's
now failed too many times in a row. Since EdgeR2 was already our
fallback, there's nowhere else to fail over *to* - just log a critical
warning and reset to "no known path," letting the branch above
(`current_path is None`) pick things back up on the next cycle,
whenever either path recovers.

```python
        time.sleep(CHECK_INTERVAL_SECONDS)
```

At the very end of each pass through the loop, pause for 5 seconds
before starting the whole process over again from the top.

```python
if __name__ == "__main__":
    main()
```

This is a very common Python pattern. `__name__` is a special,
automatically-set variable - it equals `"__main__"` only when this
specific file is the one you directly ran (as opposed to being
imported by some *other* Python file as a toolbox). This check means
"only actually call and run `main()` if this file was run directly" -
a habit that makes code more reusable, though for this particular
script, it's mostly just standard practice rather than something with
a visible effect, since nothing else currently imports this file.

---

## Summary: the full journey of one check cycle

1. The infinite `while True:` loop begins another pass.
2. `ensure_ptp_test_routes()` guarantees the two PTP test routes exist,
   no matter what happened during sleep/wake or a restart.
3. Both paths get pinged; results and streak counters update.
4. Based on the current state and those streaks, exactly one of five
   possible situations is identified: stay put and wait for more
   confirmation, switch back to EdgeR1, fail over to EdgeR2 (or declare
   both down), establish an initial path from a cold start, or declare
   the backup path also dead.
5. If a switch was warranted, `set_routes()` deletes and rebuilds every
   route in `ROUTE_DESTINATIONS`, pointed at the new gateway.
6. The loop waits 5 seconds, then starts over - forever.