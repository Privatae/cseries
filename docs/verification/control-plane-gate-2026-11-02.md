# Control plane for your machines: gate run, 2026-11-02

What was run, on fresh machines, to check that a C-Series Host with no model
key works as the control plane for the bastions on a person's other machines.
Every result below is output captured during the run; nothing is from reading
code.

## The machines

Three fresh Ubuntu 24.04 cloud images under QEMU/KVM. Each has a NAT link to
the internet and a second link on one private network, 192.168.77.0/24, that
only these three share:

| Name  | LAN address   | Role |
|-------|---------------|------|
| m2-cp | 192.168.77.11 | the control plane: Docker, `compose.yaml` built from this tree; also a bastion |
| m2-b1 | 192.168.77.12 | a bastion, installed by an ordinary account with sudo (your own computer) |
| m2-b2 | 192.168.77.13 | a bastion, installed with `--shared` (a shared machine) |

The browser is headless Chromium on a fourth machine, reaching the control
plane's dashboard (127.0.0.1:8800 on m2-cp) through an SSH tunnel to the same
port, as a person at that machine would open it. Passkeys come from Chromium's
virtual authenticator; the owner's six-digit codes from an RFC 6238 generator
fed the secret the Network app showed at enrolment. Claude Code 2.1.312 runs on
that fourth machine against `http://127.0.0.1:8800/mcp` through the same tunnel.

The bastion on every machine is the published 0.60.0, installed by the line
the Network app's card shows.

The tree was built at `edd7e828`. Halfway through, after the run found the
draft grant not showing (below), the control plane was updated in place to
`57ab4b9d` (`docker compose build && docker compose up -d`, same volumes); the
steps from "Give the Host the job" on ran on that build.

## 1. Compose up with no provider key

```
$ cat .env
CSERIES_LANE_HOST=192.168.77.11
CSERIES_LANE_BIND=192.168.77.11
$ docker compose up -d
$ docker compose ps
host running Up 32 seconds (healthy)
postgres running Up 32 seconds (healthy)
$ ss -ltn (pairing and dashboard ports)
LISTEN 0      4096   192.168.77.11:8811       0.0.0.0:*
LISTEN 0      4096   192.168.77.11:8810       0.0.0.0:*
LISTEN 0      4096       127.0.0.1:8800       0.0.0.0:*
$ docker compose logs host | grep -ciE "traceback| error |exception"
0
```

The owner account was made at `/setup` with the code from the log. Chat, in
Circles, with no key anywhere:

```
YOU      Hello, what can you do?
C-SERIES I don't have a chat model configured yet. Open the Orchestrator and
         assign chat a provider and model — your own provider key (OpenRouter,
         Anthropic, …) or a local model.
```

**Result: pass.** No model, no error; the pairing ports are on the LAN
address and the dashboard stays on loopback.

## 2. Three bastions pair with the card's one line, over the LAN

Before pairing, test agents were started on two machines: on m2-b1 a `claude`
in a terminal, an `aider` started as a service and by cron, and a Claude Code
settings file declaring two tool servers (one with a key written in it, one
with an unpinned package); on m2-b2 a `codex` in a terminal. The programs are
`sleep` copied under those names.

For each machine: Network app, Bastions, Set up a bastion; the card's
macOS / Linux line run in a terminal on that machine. The pairing code in each
line names `https://192.168.77.11:8810/v1/local/redeem`.

m2-b1 (as an ordinary account, sudo available):

```
RUNNING — serving 1 host(s) on this machine.
paired with: home.cseries.localhost  [active]
  lanes:    192.168.77.11:8811 (a Host over TCP, name host.cseries.internal)
mode: local
This machine is paired. The bastion runs as the system service privatae-bastion, and starts at boot.
```

m2-b2 (the line ending `sh -s -- --shared`):

```
paired with: home.cseries.localhost  [active]
  lanes:    192.168.77.11:8811 (a Host over TCP, name host.cseries.internal)
mode: networked
anchor: home.cseries.localhost (owner "home", since 2026-11-02T05:48:18Z)
```

m2-cp (the control plane's own machine): `mode: local`, paired the same way.

The first try on m2-b1 ran the line with an empty code (the card had not
loaded in the test browser); the installer refused it and installed nothing:
`ERROR: PRIVATAE_PAIR_CODE is not a pairing code. ... Nothing was installed.`

**Result: pass** for three Linux machines, two ways of installing. macOS and
Windows were not run: no such machine was available.

## 3. Network app: machines, agents and tool servers, posture, audit

Bastions: 3/3 online. Agents (as the Network app shows them):

```
codex        UNREQUESTED  INTERACTIVE  ADMINISTRATOR   m2-b2 · pid 1185 · user tester
aider        UNREQUESTED  SERVICE      ADMINISTRATOR   m2-b1 · pid 1196 · user tester
claude-code  UNREQUESTED  INTERACTIVE  ADMINISTRATOR   m2-b1 · pid 1195 · user tester

TOOL SERVERS — WHAT YOUR AGENTS CAN REACH
m2-b2: read 0 settings files · m2-b1: read 1 settings file · m2-cp: read 0 settings files
notes   2 PROBLEMS   m2-b1 · claude-code · not running now
        set up in /home/tester/.claude.json
        a key is written in plain text in /home/tester/.claude.json
        runs notes-mcp-server with no fixed version, so whatever is published next is what runs
files   m2-b1 · claude-code · not running now
```

Posture and the management audit are in sections 5 and 6.

**Result: pass.**

## 4. Oversight

```
0 CRITICAL OPEN · 3 HIGH OPEN · 5 WARN OPEN · 0 NOTICE OPEN
HIGH  detection plane state: no-baseline        m2-cp · heartbeat
HIGH  detection plane state: no-baseline        m2-b1 · heartbeat
HIGH  detection plane state: no-baseline        m2-b2 · heartbeat
WARN  10 pending security update(s)             m2-cp · heartbeat
WARN  fail2ban is not active and tcp/22 is not source-restricted   m2-cp · heartbeat
...
```

Findings from all three machines, worst first, with no model.

**Result: pass.**

## 5. An outside agent: connect, read, contain, approve, audit

**Give the Host the job.** Management is denied by default and, until this
run, only the Host's model drafted a grant, so a Host with no model could never
stop anything (on a first run of these steps, `contain` returned "op 'contain' on 'm2-b1' not
permitted by this Host's own permissions"). The Network app's Management lane
now has "Give your Host a job on a machine". The owner enrolled an
authenticator, drafted "stop an AI agent ..." on m2-b1 and approved it:

```
--- after Draft ---
m2-b1 · ops [ contain ]   GRANT   DRAFT — AWAITING YOU
a draft · grant is a widen — needs your code
--- after Approve with the code ---
m2-b1 · ops [ contain ] · expires in 29d   GRANT   APPROVED
signed · expires in 29d · approved with step-up
```

**Connect.** The owner added a passkey (`/passkey`). In Claude Code, `/mcp`,
m2gate, Authenticate; the URL it printed opened the Host's page:

```
Connect an agent to your Host?
... What it calls itself: Claude Code (m2gate)
 Without asking you, let it list your machines; see what your Host knows about
 each: the AI agents running on it, the scheduled jobs and tool servers they use
 (never where a key is kept), ...
When you choose, your device asks for your passkey: ...
```

Connect, with the passkey; Claude Code: `Authentication successful. Connected to m2gate.`

**Read.** Claude Code called `list_machines`, `list_agents` (machine m2-b1)
and `tool_server_configs` (machine m2-b1). Excerpts of the raw results:

```
list_machines → {"machines":[{"name":"m2-b1","online":true,"platform":"linux","bastion_version":"0.60.0",...}, {"name":"m2-b2",...}, {"name":"m2-cp",...}]}
list_agents (m2-b1) → {"machine":"m2-b1","online":true,"agent_watch":"watching", "agents":[
  {"pid":1196,"kind":"agent","signature":"aider","origin":"service","owner_class":"local_user","owner_admin":"yes",
   "exe":"/usr/local/bin/aider","carries_delegation":false,...}, {"pid":1195,"signature":"claude-code","origin":"interactive",...}, ...]}
tool_server_configs (m2-b1) → {"scan":{"files_read":1,...},"clients":[{"client":"claude-code","servers":[
  {"name":"notes","package_pinned":"no","env_names":["NOTES_API_KEY"],"command":"npx -y notes-mcp-server",
   "problems":["a key is written in plain text in its settings file","runs notes-mcp-server with no fixed version, ..."]},
  {"name":"files","package_pinned":"yes",...}]}]}
```

The outside agent gets the problem, not where the settings file is, and never
the key's value.

**Contain.** `contain` (machine m2-b1, pid 1196). The first call ran nothing
and returned the person's page:

```
Running contain on m2-b1 needs the person's approval. Nothing ran. Ask them to open
http://home.cseries.localhost:8800/agent-access/ask/<id> on their Host, then call again with the same arguments.
```

The page, as the owner saw it:

```
An agent asks to make a change
A connected agent asks your Host to run contain on m2-b1.
With these arguments: 1196
... Its name: Claude Code (m2gate)   Its reason: an aider nobody asked for
Nothing has run. ...
Just this once | Always for this agent | No
```

Just this once, with the passkey: `Done. You allowed it once.` Claude Code
called again with the same arguments:

```
{"server":"m2-b1","op":"contain","exit":0,"stdout":"{\"contain\": \"queued\", \"pid\": 1196, \"delivered\": \"on the bastion's next check-in that can see its agents\"}","stderr":""}
```

**On the machine**, within a minute:

```
$ ps -p 1196
(pid 1196 is gone)
$ sudo privatae-bastion activity
Nov 2 06:06  Your host home.cseries.localhost stopped aider (pid 1196) running on this machine.
```

**On the Host:** Network, NAC, Management audit: `m2-b1 · contain · RAN · exit 0`.
Settings, Connected agents, Claude Code (m2gate):

```
CONTAIN ON M2-B1 — RAN (ONCE), EXIT 0
CONTAIN ON M2-B1 — ASKED THE PERSON
TOOL_SERVER_CONFIGS ON M2-B1 — OK
LIST_AGENTS ON M2-B1 — OK
LIST_MACHINES — OK
```

(The run showed this list as "CONTAIN CONTAIN ON M2-B1"; fixed after.)

**Result: pass.** Claude Code waited for a completion signal after the person
answered and needed "Retry now"; the Host does not send one (follow-up below).

## 6. Quarantine a machine: every lane down

Network, NAC, Posture, Quarantine on m2-b1. The Host's log:

```
06:08:18 harness:session lane CUT: bastion token 3 was quarantined while it was open   (x3)
06:08:18 mgmt lane CUT ... (x2) · egress lane CUT ... (x2)
06:08:18 lane port: closed 13 connection(s) of quarantined bastion token_id=3
06:08:18 harness:session / mgmt / harness:harness / egress lane REFUSED: bastion token 3 is quarantined
06:09:18 harness:session lane REFUSED: bastion token 3 is quarantined (7 more refused in the last minute)
06:09:18 mgmt lane REFUSED: ... (3 more refused in the last minute)
...
$ REFUSED lines in the last 2 min: 8
```

Posture: `m2-b1 QUARANTINED · REACH IN cut off (quarantined) · SEEN just now`:
its check-ins are still noted and nothing else it sends is acted on.

Release, with the owner's code: within a minute the Host logged no refusal,
m2-b1 held 13 lane connections again, and its card read `HEALTHY · admitted ·
SEEN just now`.

**Result: pass.**

## Found by this run, and fixed before it ended

1. A Host with no model could never stop an agent: management grants were
   drafted only by the model. The Network app's Management lane now drafts one
   from a job the person picks, approved with their code as before.
2. Drafting a grant made the draft but did not show it (a wrong state path
   stopped the reload). Found here, fixed, and tested in
   `scripts/test_network_grant_form.js`.
3. A quarantined bastion redialled every lane 30 to 80 times a second (measured
   on a first run): it waits about a second after a close and does not read the
   heartbeat's `quarantined`. The Host now holds a refused socket 30 s before
   closing it and logs one line per lane a minute; section 6 is the result. The
   bastion side (back off, and stop the lanes while held) is open work for the
   bastion.
4. Wording: a quarantined machine's card described a platform tunnel this Host
   does not have; Connected agents named the contain tool twice; the header
   showed the platform bridge as "control plane: unreachable" on a Host on its
   own (now shown to network members only, as "network").

## Not run, and still open

- macOS and Windows bastions: no such machine was available.
- Claude Code on another computer over the LAN: the dashboard is published on
  loopback only (GUIDE.md, "Reaching it from other machines" sets up the TLS
  proxy for that); this run used an SSH tunnel to the same port.
- After the person answers, Claude Code waits for a completion signal the Host
  does not send; "Retry now" calls again and runs it.
- `baseline_delta` gives the machine's last summary and the open findings, not
  the full delta, which only the bastion's own `baseline diff` has.
- Circles still shows a Support card on a Host that is not a network member.
