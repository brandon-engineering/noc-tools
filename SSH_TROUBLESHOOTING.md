# SSH Troubleshooting SOP (Lab Environment)

Applies to any SSH access problem against a MemberA device — whether
you hit it through `noc_check.py`, an Ansible playbook
(`ansible_connection: network_cli` in `inventory/MemberA.yml`), or a
plain manual `ssh`. All three go through the same vty lines and the
same SSH server on the box, so the same checklist covers all of them.

**The error you get back tells you which half of the problem you're
in — start there, don't skip to a fix.**

## 1. Connection *times out* (no response at all)

Means nothing answered at the IP layer. This is a path/reachability
problem, not the SSH service itself.

- Can you ping the target's loopback? If not, that's routing/interface,
  not SSH — check the interface state, and if it's an edge router's
  outside link, whether VRRP has actually failed over
  (`show vrrp brief`).
- If ping works but SSH still times out, something mid-path (an ACL,
  a wedged process) may be dropping the SSH attempt specifically —
  worth a console check (see §2) even though the symptom is "timeout"
  not "refused."
- **Known false alarm:** right after intentionally downing an
  interface (e.g. testing VRRP failover on an edge router's WAN link),
  a single SSH attempt can land inside the few-second convergence
  window (VRRP hold timer, ARP refresh) and time out even though the
  network heals moments later. `noc_check.py` makes one connection
  attempt with no retry, so this shows up there as "PAGE NETWORK
  ENGINEERING" even when nothing is actually wrong. **Retry once,
  a few seconds later, before escalating.**

## 2. Connection *refused*

Means something at the target *did* answer and actively rejected the
attempt — the device is reachable, the problem is local to that box.
**Stop retrying SSH and get on the CML console instead** (out-of-band,
bypasses the SSH problem entirely). Work through these in order:

1. **`show ip ssh`** — confirms the SSH server is actually running.
   If it reports SSH not enabled / no key pair, that's the whole
   problem.
   Fix: `crypto key generate rsa modulus 2048` (no vty/login change
   needed).
   *Real incident, 2026-09-14: EdgeR2 refused SSH fleet-wide — to
   `noc_check.py`, to an Ansible playbook, and to a manual `ssh` —
   because its RSA host key had never been (re)generated, even though
   `show running-config | section line vty` was byte-for-byte
   identical to EdgeR1, which worked fine. Identical vty/line config
   does **not** imply identical SSH server state — the host key isn't
   part of that comparison.*
2. **`show users` / `show line vty 0 4`** — checks for exhausted vty
   lines (all 5 default lines pinned with stale sessions). If found,
   `clear line vty <n>` on the stale ones. If this turns out to be the
   cause, also flag it: something (a script, a playbook) opened a
   session and never cleanly disconnected — worth fixing at the
   source, not just clearing lines each time.
3. **`show run | section line vty`**, compared against a known-working
   peer device — look for a missing `login`, `login local`, or
   `transport input ssh`.
4. **`show run | include aaa`** — if `aaa new-model` is enabled
   (it is, fleet-wide, per RCA 2026-09-09-002), then plain
   `login local` on the vty line is **invalid input**, not a typo —
   AAA has taken over authentication for that line. You need
   `login authentication <list-name>` referencing the right method
   list instead. Check `show run | section aaa authentication` for
   the list name before touching the line config.

## 3. Connects, then authentication is rejected

SSH itself is fine; the account/credentials aren't.

- `show run | section username` — confirm the account exists on *this*
  device with the expected privilege level (don't assume a fleet-wide
  account is actually present everywhere).
- Double check you're not fighting the `login local` vs. AAA mismatch
  from §2.4 — a method-list problem can also surface as an auth
  rejection rather than "invalid input," depending on how it's set up.

## 4. After any host key regeneration

Your laptop will refuse to reconnect (or throw a host-key-mismatch
warning) once the target's key changes. Clear the stale entry before
retrying:

```
ssh-keygen -R <ip-or-hostname>
```

Run this for **every** address/alias you use to reach that device
(loopback, outside IP, any SSH-config hostname alias) —
`ssh-keygen -R` only clears the exact value you give it.

## Quick reference

| Symptom | Likely cause | First diagnostic |
|---|---|---|
| Timeout | Path/interface/routing, or convergence race | `ping` the loopback; check interface & VRRP state |
| Refused | SSH server itself down | `show ip ssh` |
| Refused, previously worked | RSA host key missing/never generated | `show ip ssh`, `show crypto key mypubkey rsa` |
| Refused, box has been up a while | VTY lines exhausted by stale sessions | `show users` |
| Refused, "invalid input" on `login local` | `aaa new-model` governs this line | `show run \| include aaa` |
| Connects, then rejected | Bad/missing local account | `show run \| section username` |
