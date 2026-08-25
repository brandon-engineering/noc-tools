# How netalert.py Works — A Line-by-Line Walkthrough

This document explains every part of `netalert.py`, written for someone
who has never programmed in Python before. It assumes no prior coding
knowledge - every Python concept is explained the first time it appears.

---

## The big picture, before we look at any code

`netalert.py` does one job, over and over, forever, until you stop it:

1. Watch a log file for new lines
2. When a new line looks like an interface going up or down, figure out
   which device and interface it's about
3. Turn that into a friendly, readable message
4. Send that message to Discord (always) and to Grafana IRM (only for
   certain devices)

Everything in the file exists to support those four steps. Let's go
through it top to bottom.

---

## Part 1: Imports (lines 1-6)

```python
import subprocess
import requests
import re
import os
import time
from typing import Optional, Tuple
```

In Python, code that does useful things (like sending a web request, or
running another program) usually isn't built into the language itself -
it lives in separate "modules" that you have to explicitly bring into
your script with the word `import`. Think of each `import` line as
saying "I'm going to need this toolbox, please make it available."

- **`subprocess`** - lets Python start and control *other* programs on
  the computer. We use this later to run the Linux `tail` command from
  inside our Python script.
- **`requests`** - lets Python send data over the internet (specifically,
  HTTP requests - the same kind of request your web browser makes when
  you visit a website). This is how we actually post messages to
  Discord and Grafana.
- **`re`** - short for "regular expressions." This is a mini-language
  for searching text for patterns, rather than exact matches. We use it
  to recognize syslog lines even though the exact wording varies
  slightly from message to message.
- **`os`** - lets Python interact with the operating system itself -
  checking if a file exists, reading environment variables, etc.
- **`time`** - lets Python pause/wait for a specified number of seconds.
- **`from typing import Optional, Tuple`** - this one is a bit different.
  `Optional` and `Tuple` aren't things the code *runs* - they're just
  labels used to describe, in writing, what kind of data a function
  will return. This is purely for humans reading the code (and for
  some code-checking tools) - Python itself would still work fine
  without this line.

---

## Part 2: Reading configuration (lines 8-13)

```python
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
IRM_WEBHOOK_URL = os.environ.get("IRM_WEBHOOK_URL")
LOGFILE = "/var/log/network-devices.log"

if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL environment variable not set")
```

In Python, `WEBHOOK_URL = something` is called an **assignment** - it
creates a named container (a "variable") and puts a value inside it.
From this point forward in the file, anywhere you see `WEBHOOK_URL`,
Python mentally substitutes in whatever value got stored here.

- `os.environ.get("DISCORD_WEBHOOK_URL")` - this reads an **environment
  variable**, a piece of configuration set *outside* the Python script
  itself (in this case, in the systemd service file that starts
  `netalert.py`). This is a deliberate security choice: the real
  Discord webhook URL (which is essentially a secret password for
  posting to your channel) never appears written down inside the
  script itself, so it's safe to share the script publicly (like on
  GitHub) without leaking that secret.
- `LOGFILE = "/var/log/network-devices.log"` - this is a plain string
  (text) telling the script exactly which file to watch.
- The `if not WEBHOOK_URL:` block is a **safety check**. `if` means
  "only do the next part when this condition is true." `not WEBHOOK_URL`
  is true when `WEBHOOK_URL` is empty/missing. So in plain English:
  "if we never actually got a Discord webhook URL, stop the whole
  program right now with an error message" - rather than limping along
  and failing confusingly later when it tries to actually use a missing
  URL.

---

## Part 3: Which devices also go to Grafana IRM (lines 15-18)

```python
IRM_ALERT_HOSTS = {"EdgeR1", "EdgeR2"}
```

The `{ }` curly braces here create a **set** - a collection of items
with no particular order, where each item can only appear once. This
line says: "here is the list of device names that should *also* get
sent to Grafana IRM, on top of always going to Discord." Later in the
script, we'll check "is this device's name inside this set?" to decide
whether to send the extra Grafana message.

---

## Part 4: Recognizing an interface event (line 20)

```python
PATTERN = re.compile(r"%LINK-3-UPDOWN|%LINEPROTO-5-UPDOWN|%LINK-5-CHANGED")
```

This is our first real use of `re` (regular expressions). `re.compile(...)`
takes a text pattern and turns it into a reusable "pattern object" that
Python can efficiently check other text against later.

The pattern itself, `%LINK-3-UPDOWN|%LINEPROTO-5-UPDOWN|%LINK-5-CHANGED`,
reads as: "match any line containing one of these three exact pieces of
text." The `|` symbol means "or." Cisco routers and switches use these
three specific codes in their log messages whenever an interface
changes state - so this pattern is our first, quick filter: "is this
even a line we care about at all?"

The `r` right before the quotation marks (`r"..."`) tells Python "treat
this text completely literally, don't try to interpret any special
character sequences inside it." This matters a lot for regular
expressions, which use characters like backslashes in their own special
way - the `r` prevents Python's normal text rules from interfering.

---

## Part 5: Pulling out the details (lines 22-36)

```python
PARSE_PATTERN = re.compile(
    r"(?P<host>[A-Za-z0-9_\-]+): [\*\.]?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r" +\d+ [\d:]+ \S+: %(?:LINK-3-UPDOWN|LINEPROTO-5-UPDOWN|LINK-5-CHANGED):"
    r".*?Interface (?P<interface>[\w/.]+),? ?(?:changed state to |is )?(?P<state>up|down)",
    re.IGNORECASE,
)
```

This is a second, much more detailed pattern. While `PATTERN` above just
answers "is this line interesting at all," `PARSE_PATTERN` actually
reaches *into* the line and extracts three specific pieces of
information: which device sent it, which interface it's about, and
whether that interface went up or down.

A real log line looks something like this:

```
ASW1: *Aug 12 04:47:18 EDT: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet0/3, changed state to up
```

Here's how the pattern maps onto that real text:

- `(?P<host>[A-Za-z0-9_\-]+)` - this captures the device's hostname
  (`ASW1` in the example). The `(?P<host>...)` part gives this captured
  piece a name ("host") so we can refer to it later by name instead of
  position. `[A-Za-z0-9_\-]+` means "one or more letters, numbers,
  underscores, or hyphens" - basically, any normal-looking device name.
- `[\*\.]?` - matches either a `*` or a `.` character, and the `?`
  means "this is optional, zero or one of it." Cisco devices put a `*`
  before the date/time if their clock is synced to a real time source
  (NTP), or a `.` if it isn't synced yet. We accept either, since a
  freshly rebuilt device might briefly log with a `.` before its clock
  catches up.
- `(?:Jan|Feb|Mar|...)` - matches any three-letter month abbreviation.
  The `(?:...)` (versus `(?P<name>...)`) means "group these options
  together, but don't bother capturing/naming the result" - we don't
  actually need to know *which* month it was for our purposes, we just
  need the pattern to correctly skip past the date.
- `\d+` - one or more digits (the day of the month).
- `[\d:]+` - one or more digits or colons (the time, like `04:47:18`).
- `\S+` - one or more non-space characters (the timezone abbreviation,
  like `EDT`).
- `%(?:LINK-3-UPDOWN|LINEPROTO-5-UPDOWN|LINK-5-CHANGED):` - the same
  three event codes from before, confirming this really is one of the
  interface-change message types.
- `.*?Interface ` - `.` means "any character," `*` means "zero or more
  of the previous thing," and `?` right after makes it "as few as
  possible" (this avoids accidentally grabbing too much text). Combined,
  this means "skip over whatever text comes next, until you find the
  word 'Interface '."
- `(?P<interface>[\w/.]+)` - captures the interface name itself (like
  `GigabitEthernet0/3`), naming this capture "interface."
- `,? ?(?:changed state to |is )?` - this handles the fact that Cisco
  devices phrase this part slightly differently depending on which
  message type it is (`changed state to up` vs. `is up`). Each `?`
  makes the thing right before it optional, so this pattern flexibly
  matches either phrasing, or something in between.
- `(?P<state>up|down)` - captures whether it says "up" or "down,"
  naming this capture "state."
- `re.IGNORECASE` - a setting telling the pattern to ignore whether
  letters are upper or lower case when matching.

The reason this pattern has three named captures (`host`, `interface`,
`state`) is so that later code can ask "what did you find for `host`?"
by name, rather than having to count position numbers - much easier to
read and much less error-prone.

---

## Part 6: The friendly-message lookup table (lines 38-53)

```python
DEVICE_INTERFACE_MAP = {
    ("EdgeR1", "GigabitEthernet0/0"): "EdgeR1 internet uplink is affected - verify ISP/WAN path.",
    ...
}
```

This creates a **dictionary** - a collection where each item has two
parts: a "key" (something to look up) and a "value" (what you get back
for that key). Here, each key is a **tuple** (a small, fixed-size,
ordered group of values - in this case, exactly two: a device name and
an interface name, written in parentheses like `("EdgeR1", "GigabitEthernet0/0")`),
and each value is the friendly explanation text you want shown for that
specific device/interface combination.

Later in the code, we'll do something like
`DEVICE_INTERFACE_MAP.get((host, interface))` - "look up this exact
device-and-interface pair in the table, and give me back the matching
sentence, or nothing if it's not in the table at all."

---

## Part 7: The function that builds the message (lines 56-78)

```python
def build_message(line: str) -> Tuple[str, Optional[str]]:
```

`def` starts a **function definition** - a named, reusable block of
code that you can "call" (run) elsewhere in the script whenever you
need it, rather than writing the same steps out repeatedly. Think of a
function like a small machine: you feed it some input, it does work
internally, and it hands back a result.

- `build_message` is the name we're giving this function.
- `(line: str)` means this function expects to receive one piece of
  input, which we'll refer to as `line` inside the function, and it's
  expected to be a string (`str` = text).
- `-> Tuple[str, Optional[str]]` describes what the function will hand
  back: a tuple containing exactly two things - a string (the message
  text), and *either* a string *or* nothing at all (`Optional[str]`
  means "a string, or possibly `None`" - Python's special way of
  representing "nothing here"). This whole `->` part is just
  documentation for humans (and some tools) - again, Python itself
  doesn't require it to actually run correctly.

```python
    match = PARSE_PATTERN.search(line)
    if not match:
        return f"Interface event: {line.strip()}", None
```

`PARSE_PATTERN.search(line)` actually runs our detailed pattern against
the specific line of text we were given. If the line doesn't match the
pattern at all, `match` will be `None` ("nothing found"). `if not match:`
catches that case: "if we genuinely couldn't parse this line at all,
just return the raw original text as a fallback message, with `None`
as the second part of the tuple (since we don't have a hostname to
report)."

`f"Interface event: {line.strip()}"` is an **f-string** - a way of
building text that has other values inserted into it. Anything inside
`{ }` gets evaluated and inserted into the surrounding text.
`line.strip()` removes any extra blank space/newline characters from
the beginning and end of the original line before inserting it.

```python
    host = match.group("host")
    interface = match.group("interface")
    state = match.group("state").lower()
```

If we get past the check above, `match` genuinely found our pattern in
the text. `match.group("host")` retrieves the specific piece of text
that was captured under the name "host" earlier - same for
`"interface"` and `"state"`. `.lower()` converts the state text to all
lowercase, so "Up" and "UP" and "up" are all treated identically.

```python
    friendly = DEVICE_INTERFACE_MAP.get((host, interface))
```

This looks up our friendly-message dictionary from Part 6, using the
device and interface we just extracted. If there's a match, `friendly`
holds that sentence; if not, `friendly` becomes `None`.

```python
    state_icon = "[red]" if state == "down" else "[green]"
```

(The real code uses colored circle emoji here.) This is a compact way
of writing an if/else in a single line, called a **conditional
expression**. Read right to left: "if `state` equals 'down', use the
red circle emoji; otherwise, use the green one."

```python
    if friendly and state == "down":
        return f"{state_icon} {friendly}...", host
    elif friendly and state == "up":
        return f"{state_icon} {host} {interface} has RECOVERED...", host
    else:
        return f"{state_icon} Interface event: {host} {interface} changed state to {state.upper()}", host
```

This is the actual decision logic. `if ... elif ... else` means "check
the first condition; if that's not true, check the next one; if none
of them are true, do this last option instead."

- `if friendly and state == "down":` - "if we found a friendly message
  AND the interface just went down" -> build the down-alert message.
- `elif friendly and state == "up":` - "if we found a friendly message
  AND the interface just came back up" -> build the recovery message.
- `else:` - "otherwise (no friendly message exists for this
  device/interface at all)" -> fall back to a generic, still-readable
  message.

`\n` inside a string means "start a new line" (a line break). Each
`return` statement ends the function immediately and hands back exactly
what follows - in every case here, that's the message text *and* the
`host` value, packaged together as a tuple (that's what the comma
between them does - it creates a tuple with two items).

---

## Part 8: Waiting for the log file to exist (lines 81-90)

```python
def wait_for_logfile(path: str, check_interval_seconds: int = 5) -> None:
    while not os.path.exists(path):
        time.sleep(check_interval_seconds)
```

Another function, this one taking two inputs: `path` (the file to
check) and `check_interval_seconds`, which has a **default value** of
`5` - meaning if you call this function without specifying that second
value, it automatically uses 5.

`while ... :` is a **loop** - unlike `if`, which checks a condition
once, `while` keeps repeating its indented block for as long as the
condition stays true. `os.path.exists(path)` checks whether a file
genuinely exists on disk right now, returning `True` or `False`.
`not os.path.exists(path)` flips that - `True` when the file does
*not* exist.

So the whole loop means: "as long as this file doesn't exist yet,
wait 5 seconds, then check again" - repeating forever until the file
finally shows up, at which point the loop naturally ends and the
function returns.

This exists because, on a freshly rebuilt system, this script might
start running before the log file has been created (rsyslog only
creates it once it receives its very first message) - without this
wait, the script would crash trying to watch a file that isn't there
yet.

---

## Part 9: The main loop - where everything actually happens (lines 93-119)

```python
wait_for_logfile(LOGFILE)
```

This actually **calls** the function from Part 8, using our real
`LOGFILE` value. Execution pauses here until the file exists.

```python
proc = subprocess.Popen(["tail", "-F", LOGFILE], stdout=subprocess.PIPE, text=True)
```

This is where `subprocess` (from Part 1) gets used. `subprocess.Popen(...)`
starts a completely separate program running alongside our Python
script - in this case, the Linux command `tail -F <logfile>`, which
continuously watches a file and outputs any new lines as they're added
(the `-F` flag specifically means "keep watching even if the file gets
rotated/recreated," which matters for long-running log files).

`stdout=subprocess.PIPE` tells Python "capture whatever this other
program prints out, so I can read it myself, rather than just letting
it print to the screen." `text=True` says "give me that output as
readable text, not raw computer bytes."

```python
for line in proc.stdout:
```

This is another kind of loop - a **for loop**, which repeats its
indented block once for every item in a collection. Here, `proc.stdout`
behaves like an endless stream of new lines from the `tail` command -
so this loop runs forever, executing its body once every single time
a new line appears in the log file.

```python
    if PATTERN.search(line):
```

For every new line, we first do our quick check from Part 4: does this
line even contain one of our three interesting event codes at all? If
not, the `if` block is skipped entirely and we go straight to waiting
for the next line - this is a fast filter so we don't waste effort
running the much more detailed pattern on every single line in the log,
most of which are irrelevant.

```python
        message, host = build_message(line)
```

If the line passed the quick check, we call our `build_message`
function from Part 7. Since that function returns a tuple of two
values, this line **unpacks** them into two separate variables at once
- `message` gets the first item, `host` gets the second.

```python
        requests.post(WEBHOOK_URL, json={"content": message})
```

This is where `requests` (from Part 1) finally gets used - sending our
message to Discord. `requests.post(...)` sends an HTTP POST request (a
web request that submits data) to the given URL. `json={"content": message}`
specifies the data being sent, formatted the exact way Discord's
webhook system expects to receive it: a small dictionary with one key,
`"content"`, holding our message text.

This line runs for *every* recognized event, regardless of which
device it's about - Discord always gets notified.

```python
        if IRM_WEBHOOK_URL and host in IRM_ALERT_HOSTS:
```

This checks two things at once, joined by `and` (meaning both parts
must be true): first, that an IRM webhook URL was actually configured
at all (if it's missing, `IRM_WEBHOOK_URL` would be empty/`None`,
which Python treats as `False`); second, `host in IRM_ALERT_HOSTS`
checks whether the device name we parsed is inside the set we defined
back in Part 3. Only if both are true does the code inside this block
run at all.

```python
            try:
                ...
            except requests.exceptions.RequestException:
                pass
```

`try` / `except` is Python's way of saying "attempt this code, and if
something goes wrong (an error/exception occurs) partway through,
don't crash the whole program - instead, do this other thing instead."
Here, if sending to Grafana fails for any reason (network issue, IRM
being temporarily down, etc.), `except requests.exceptions.RequestException:`
catches that specific kind of error, and `pass` means "do nothing in
response, just quietly move on." The comment right above explains why:
we don't want a Grafana IRM problem to ever interrupt or crash the
Discord alerting, which is the reliable, already-proven channel.

```python
                match = PARSE_PATTERN.search(line)
                interface = match.group("interface") if match else "unknown"
                alert_key = f"{host}-{interface}"
                noc_check_command = f"noccheck MemberA {host} {interface}"
```

Inside the `try` block, we re-run the detailed pattern one more time
(since `build_message` didn't hand back the interface name directly,
only the host) to get the interface name too. The
`match.group("interface") if match else "unknown"` line is another
conditional expression like we saw in Part 7: "if we got a match,
use the interface name; otherwise, fall back to the text 'unknown'
rather than crashing."

We then build two more pieces of text using f-strings: `alert_key`
(a unique identifier combining device and interface, like
`"EdgeR2-GigabitEthernet0/0"`, used by Grafana IRM to know that a
"down" event and a later "up" event for the *same* interface belong
together as one incident, not two separate ones), and
`noc_check_command` (the exact, ready-to-copy-and-run command for
investigating this specific device and interface with your
`noc_check.py` tool).

```python
                requests.post(
                    IRM_WEBHOOK_URL,
                    json={
                        "message": message,
                        "alert_key": alert_key,
                        "noc_check_command": noc_check_command,
                    },
                    timeout=5,
                )
```

Finally, another `requests.post`, this time to Grafana IRM's webhook
URL, sending a dictionary with three pieces of information this time
(the message, the grouping key, and the investigation command).
`timeout=5` tells `requests` "don't wait more than 5 seconds for a
response - if it's taking longer than that, give up," which prevents
the whole script from freezing indefinitely if Grafana IRM is slow or
unreachable.

---

## Summary: the full journey of one event

1. A router logs an interface going down.
2. That log line travels over the network (via syslog, TCP) to NUS1.
3. rsyslog writes it into `/var/log/network-devices.log`.
4. The `tail -F` subprocess (started by our script) notices the new
   line and hands it to our `for` loop.
5. `PATTERN.search()` confirms it's an interesting event.
6. `build_message()` runs `PARSE_PATTERN` to extract the host,
   interface, and state, looks up a friendly explanation if one
   exists, and builds a readable message.
7. That message is posted to Discord immediately.
8. If the device is EdgeR1 or EdgeR2, a second message - with extra
   detail for grouping and investigation - is posted to Grafana IRM
   too, wrapped in error-handling so a Grafana problem can never break
   Discord alerting.
9. The loop goes back to waiting for the next line, forever.