---
name: computer
description: How the host reads a user's machine — the map of installed apps with the deepest way to drive each, the windows open now, and an app's accessibility tree — and plans computer work from it. Observe first; on a machine that granted root commands, run an app's CLI with machine_exec from the root thread.
allowed-tools: machine_map, machine_exec, mgmt_run, propose_mgmt_grant, get_network, turn_type
---

# Computer SOP

Read the map before planning any work on a user's computer ("open Fusion 360
and build me a flowerpot"): it is how you know what is installed.

## machine_map — guidance

`op='read'` (default) returns the kept map for one server or all: per app its
name, version, publisher, path, and `ways_in` — the BOX'S REPORT of the
interfaces that app offers, deepest first: a documented scripting door
('script:python (Fusion 360 API …)'), then 'cli', then 'gui'. It is a map, not
a set of tools you hold. TODAY you cannot press a control or take a
screenshot. On a machine whose bastion granted you root commands you CAN run a
command yourself, an app's CLI or script included: `machine_exec(machine,
command)`, from your root thread (open it with `operate(tool='turn_type',
args={op:'root', request:…})`), in a turn that has read nothing untrusted (skill network). On
any other machine you can LOOK: `op='tree'` reads one
app's accessibility tree (server + pid from the windows list): its windows as
trees of controls with role, the app's own name for each, id, value,
enabled/focused, and screen bounds. That is how you SEE an app with no
scripting door — exact text, never a screenshot. Use ways_in and the tree to
PLAN and to tell the user which door the work would take; without root
commands on that machine, never claim you can drive one yet.

`windows` is what is open right now with the owning process and focus. Each
report carries `scanned_at`; apps and windows are paged (total + truncated say
when a list is cut — pass limit to see more apps). A tree is capped at limit
nodes (default 500, max 800) AND trimmed to the reply budget, shallowest
controls kept; truncated + trimmed_to_fit say so. A control you expected but
do not see may be below the cut: asking again with a smaller limit is not the
answer — the tree is breadth-first, so what is missing is deep; tell the user
what you can see and what you cannot.

`op='refresh'` asks the box for a fresh map through the management lane: it
needs a signed grant naming computer:inventory / computer:windows (and
computer:tree for op='tree') on that server (draft one with
`propose_mgmt_grant` in the user's words; a deny in any grant wins). Each kind
comes back ok / denied (the user's grant said no — relay their words) /
ungranted (draft a grant) / too-old (the bastion predates these ops — say so:
it needs the update) / unreachable / unreadable; a tree can also be blind (the box
could not read it: no accessibility binding, permission not granted, the app
exposes nothing) or bad-request; held means the user suspended or
quarantined that box. A report with supported=false and a reason is a box
that CANNOT look (no display, no wmctrl, macOS Accessibility not granted) —
relay the reason; never read it as "nothing installed" or "no windows open".


## Parameter notes (full text of shortened argument descriptions)

- **machine_map** `limit`: read: max apps per server (default 200, max 1000). tree: max nodes (default 500, max 800; also trimmed to the reply budget).
