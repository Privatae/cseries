# Using C-Series

The parts of running a Host that the [README](../README.md) leaves out. What
is available and what is coming is in [ROADMAP.md](../ROADMAP.md).

## What the installer asks for

On Windows without WSL, it first asks for administrator rights to install
WSL and for a restart, then carries on by itself after the restart.

On Linux, C-Series is installed for the whole machine, as the bastion is:
run `npx cseries` as yourself and it asks for your sudo password once, for
the whole run. The Host's virtual machine runs as a system account of its
own, `cseries`, started at boot by the system service `cseries.service`,
and everything of it is in `/var/lib/cseries`. No account's home holds any
of it, so removing an account does not remove the Host. QEMU, when it is
missing, comes from the distribution's packages in the same run.
`npx cseries --just-me` installs it for your account only instead: the
virtual machine then lives in your home and starts at your login, and sudo
is asked only to install QEMU or to add you to the `/dev/kvm` group. An
install made before whole-machine installs keeps working as it is, and
`npx cseries` offers it once to move it to the whole machine. On a y the
Host's virtual machine and all its data (its database, apps, pairings and
keys) move to `/var/lib/cseries`, still the same Host, so the bastion stays
paired and anchored to it; the copy in your home is removed only once the
moved Host runs, and a move that does not finish is undone. On a no it is
never asked again; `npx cseries --whole-machine` moves it later.

`npx cseries uninstall --delete-data` removes the Host and its data, the
bastion it installed with its pairings, keys and audit log (also when that
bastion is anchored to this Host; `--keep-bastion` keeps it), and its
folders and system account. On Windows that is the bastion's service, its
program in `Program Files\Privatae`, its state under the system profile, the
tray's sign-in entry and its own firewall rules, through the same single
administrator prompt the install uses. It names the QEMU packages it
installed (Linux), or WSL and the lines it added to your `.wslconfig`
(Windows), and removes them only when you type y. A bastion it did not
install stays; it names the command that removes it
(`sudo -H privatae-bastion uninstall --delete-data`, or on Windows the same
from an Administrator terminal).

The virtual machine mounts none of your folders. The Host publishes its ports
(8800 for the dashboard, 8810 and 8811 for the bastion) on 127.0.0.1 only. A
firewall inside the virtual machine stops every container in it from opening
a connection to your machine; other computers on your network and the
internet stay reachable.

## First sign-in

Open <http://home.cseries.localhost:8800/setup> (or <http://127.0.0.1:8800/setup>),
enter the setup code, and choose the owner's email and a password of at least
12 characters. Until the owner account exists, the code changes every time the
Host restarts.

Then give the Host a model. The Getting Started app walks you through it: add
a key from a model provider (OpenRouter, Anthropic, OpenAI and others) in
Keys, then apply the recommended setup in the Orchestrator.

## Words you will meet

- **Host**: one running C-Series AI, with its own memory, database, apps and rules, used from a browser.
- **Bastion**: a separate program that runs on your machine as a system service, outside the virtual machine; the Host reaches the machine's files and programs only through it.
- **Bastion agent**: a separate program the bastion runs on the machine for a folder you connected; it indexes the folder there, and the Host searches it through the bastion.
- **Anchor**: the Host a bastion answers to on its machine; `npx cseries` anchors the bastion it installs to the Host it installs.
- **Grant**: a permission for something specific, such as the capabilities a Host gives an agent for one task, or a folder or machine you let the Host use.
- **Approval card**: a request the Host cannot decide by itself; it waits over the chat and in Circles, under Waiting on you, until you answer.
- **The person's limits**: every limit that can stop work you asked for, each with a coded default that only you change (Settings, Limits).
- **`world`**: the Host's own database schema, where it designs tables for data it keeps, under a role that can write nowhere else.
- **Privatae network**: an optional paid membership; a Host joins with a one-time code and connects outbound only.
- **C-Series Cloud**: an optional paid service that runs a Host on a virtual machine of its own, on a server Privatae runs.

## Chat apps

Besides the browser, a Host can be reached from Telegram, Discord and Slack,
through connector apps in its App Store. Each connector links one of the
person's own accounts, after a pairing they approve: a message to their bot
starts a turn in one of their threads, and the reply goes back the same way.
Every connector connects out (Telegram by long polling, Slack through Socket
Mode, Discord through a gateway connection the Host holds): the Host opens
the connection, never the chat service. The bot token stays in the Host's Keys; the
connector's container never holds it. A message from chat approves nothing:
approval cards still wait for the person.

## Memory and learning

A Host remembers in its own PostgreSQL database, and learns in its own turns:

- **The transcript.** Every message and reply is stored as it happens.
- **Notes.** In its own turns, the Host writes notes about what it learns:
  the person's preferences, rules they teach it, notes about people, the
  goal of a conversation, cited facts and research. A note can carry a
  topic; a newer note on the same topic becomes the live one, and the older
  one is kept on record. The person can read and correct notes in the
  Memory app.
- **Recall.** Matching preferences and notes about people ride each turn.
  One `recall` tool searches the notes, documents, projects, code, skills
  and the Host's own tables together.
- **Its own app data.** When it needs to keep structured data, such as a
  list it maintains or records it tracks over time, the Host designs the
  tables itself in its own schema, `world`, and works with them through the
  `data` tool, without building an app. Every table carries a line saying
  what it is for, which is how `recall` finds it later. Before a change that
  could remove or reshape data, the schema is backed up. It runs under a
  database role that can write nowhere else.
- **Skills it writes.** When a kind of job is worth repeating, the Host
  writes a skill: the steps, the tools, how to check the result and what to
  do when it fails. Each skill is checked against a standard in code before
  it is saved. Every version is kept, and the person can restore one in the
  Skills app. A skill written after reading the web is marked untrusted
  until the person saves it. A message that matches a skill gets a line
  pointing to it in that turn.
- **Looking back.** Nothing reads past conversations in the background. If
  the person asks for it ("review our work every day"), the Host sets up a
  daily review that revises its skills, corrects memory and closes its own
  finished tickets. It runs only after the person approves it, within their
  limits.

## Why it opens on a name

C-Series is opened on a name, not an address. Each app runs on a name of its
own beside the dashboard (`apps-<key>-home.cseries.localhost`), so one app's
page can never read another's.

## Reaching it from other machines

The dashboard listens on 127.0.0.1 only and serves plain HTTP. To use it from
other machines, put it behind a reverse proxy that terminates TLS rather than
publishing the port on a network address:

1. Give it a name with a wildcard DNS record for its zone, for example
   `home.cseries.lan` and `*.cseries.lan`.
2. Set `CSERIES_URL` to that address, for example `https://home.cseries.lan`.
3. Set `CSERIES_TRUSTED_PROXIES` to the proxy's address. A wrong-password
   lockout then applies to the visitor rather than to everyone behind the
   proxy, and the proxy's `X-Forwarded-Host` and `X-Forwarded-Proto` are
   believed. Nobody else's are.
4. Have the proxy ask the Host about each request before passing it on, at
   `GET /__host/door` (Caddy's `forward_auth`). The Host answers 200, a
   redirect to its sign-in page, or 401, so nothing reaches it without the
   owner's session but the sign-in pages, the OAuth returns, and `/mcp` and
   the OAuth paths an outside agent calls itself; `/mcp` answers nothing
   without a token the owner approved. Another proxy must send the
   request's method and URI in `X-Forwarded-Method` and `X-Forwarded-Uri`
   and hand a redirect back to the browser as it is; nginx's `auth_request`
   does neither by itself, and treats a redirect as an error.
5. Give the proxy and the Host one door key: a random value of 32 to 512
   characters (letters, digits and `._~+/=-`) in a file only the two of
   them read, for example from `openssl rand -hex 32`. Point
   `CSERIES_DOOR_SECRET_FILE` at the file, and have the proxy send the key in
   an `X-Cseries-Door` header on its question to `/__host/door` and on
   nothing else (in Caddy, `header_up X-Cseries-Door {$CSERIES_DOOR_SECRET}`
   inside the `forward_auth` block). The Host answers only a question that
   carries it, since the proxy also passes the browser's own requests on;
   without the file every request is refused. A new key in the file is used
   without a restart.

On an https address the session cookie is a `__Host-` cookie, and the owner
signs in with a passkey: after setup and after a password sign-in the Host
walks the owner through adding one and shows ten recovery codes, each good
once. From then on the password no longer signs in, and adding the first
passkey signs out every other session. The `/passkey` page removes a lost
passkey (never the last one) and signs out every other session, each after
a fresh check with a passkey or a recovery code.

To pair a bastion on another machine, set `CSERIES_LANE_HOST` to the name or
address that machine reaches the Host's ports 8810 and 8811 at, and publish
those ports there (next section); the Network app then shows the pair
command for it. Where a server's firewall keeps 8810
shut, `CSERIES_DOOR_HELPER` names the unix socket of a helper on that server
that opens it to the owner's address for the 15-minute pairing window, after
a fresh passkey check. `CSERIES_SETUP_CODE_FILE` names a file holding the
setup code, for a Host someone else installs.

## A control plane for your machines

A Host needs no model to look after your machines. Without a model key it
still pairs bastions, and its Network app and Oversight show what each
machine runs: its AI agents, the tool servers they use, its posture and its
audit. Chat says no model is set up until you add one.
<!-- pairing: server/network/bastion_enroll.py add_tcp_pairing; agents: server/network/nac_agents.py ingest; findings: brain/mastercode/runtime/oversight.py -->

Run it on one Linux machine on your network. Beside `compose.yaml`, put the
two settings in a file named `.env`:

```sh
# The name or address your other machines reach this one at.
CSERIES_LANE_HOST=192.168.1.20
# The address of this machine to publish the pairing ports 8810 and 8811 on.
CSERIES_LANE_BIND=192.168.1.20
```

then start it as above with `docker compose up -d --build`. When
`CSERIES_LANE_HOST` is an address, `CSERIES_LANE_BIND` is usually the same
one; when it is a name, set `CSERIES_LANE_BIND` to the address that name
points at. Let your other machines reach ports 8810 and 8811 through this
machine's firewall. The dashboard stays on 127.0.0.1; open it from another
computer as the section above says.
<!-- compose.yaml host.ports and CSERIES_LANE_HOST; server/network/local_bastion.py lane_host -->

The pairing ports are safe to publish on your network. Port 8810 answers one
request, the redeem of a pairing token that is single-use and expires in 15
minutes. Port 8811 answers only a client certificate this Host issued at a
redeem and has not taken back.
<!-- server/network/local_bastion.py module docstring (:8810, :8811); server/network/bastion_enroll.py PAIRING_TTL_SECONDS -->

To pair a machine, open the Network app and set up a bastion. Its card shows
one line for macOS and Linux and one for Windows; run the one for that
machine in a terminal there. The line installs the bastion, pairs it with
this Host trusting this Host's certificate and no other, and starts it.
<!-- server/network/bastion_enroll.py one_line, add_tcp_pairing -->

What the bastion lets this Host do depends on how it was installed (the
bastion's README, "Your own computer or a shared machine"):

- **Your own computer.** Run as yourself, the line installs the bastion for
  the whole machine through `sudo` (or as administrator on Windows) and takes
  this Host as the machine's one Host. That is what Oversight's checks need,
  including the ones that read what only root can.
  <!-- privatae-bastion deploy/install.sh elevate_whole_machine; client-go/anchor.go capsAllowIn -->
- **Just your account.** Installed with `--just-me`, or where `sudo` is
  missing, the bastion runs as you: it sees your own agents, and checks that
  need root are refused.
  <!-- privatae-bastion deploy/install.sh --just-me -->
- **A shared machine.** Installed with `--shared`, or as root, one Host is in
  charge of the machine and every write waits for your approval at the
  machine. The first Host paired as root is put in charge; another one is put
  in charge at that machine with `sudo -H privatae-bastion claim-anchor`.
  <!-- privatae-bastion client-go/anchor.go; README "Your own computer or a shared machine" -->

To pass an option through the card's line on macOS or Linux, end it with
`sh -s -- --shared` (or `--just-me`) instead of `sh`.
<!-- privatae-bastion deploy/install.sh option parsing -->

## The Privatae network

With a network membership, a Host can join the Privatae network: open
Settings, then Network, and enter the one-time code from
<https://privatae.ai/account/network>. A member Host connects to the network
outbound only.

Membership is not on sale yet; [ROADMAP.md](../ROADMAP.md) says what it
adds and when.

## Building from source

The images are built from this repository's `Dockerfile.linux` and
`Dockerfile.postgres`; `compose.yaml` runs them on any machine with Docker:

```sh
git clone https://github.com/Privatae/cseries.git
cd cseries
docker compose up -d --build
docker compose logs host | grep "SETUP CODE"
```

This runs the Host and its database only. Apps that need a container of
their own start through the local runtime, which the installer sets up in
the virtual machine.

## Running the tests

```sh
# Installer unit tests
cd packages/cseries && npm test

# A single gate or test script (most run without a database)
python3 scripts/test_public_source_clean.py

# The full smoke run against a built image
docker build -f Dockerfile.postgres -t cseries-postgres:dev .
docker build -f Dockerfile.linux -t cseries:dev .
IMAGE=cseries:dev PG_IMAGE=cseries-postgres:dev bash scripts/smoke_test.sh
```

## For reviewers: where to look

| Area | Where | What it shows |
|---|---|---|
| The turn | `brain/mastercode/runtime/session.py`, `turn_types.py`, `root_turn.py` | How one message becomes model calls, tool calls and a reply, and which turn types exist |
| Tools and the capability gate | `brain/mastercode/runtime/tools.py`, `brain/mastercode/ceigas_capabilities.py`, `runtime/turn_type_approval.py` | What the model may call, and which calls need the person's approval first |
| The person's limits | `brain/mastercode/runtime/user_limits.py`, `server/routes/limits.py`, `runtime/scheduled_approval.py` | Each limit that can stop work has a coded default that only the person changes (Settings, Limits); a recurring scheduled task that runs a model is set only after the person approves it |
| Memory | `brain/mastercode/memory_engine.py`, `memory_slots.py`, `memory_retrieval.py`, `runtime/close_turn.py`; `docs/HOST-MEMORY-AND-PROMPT.md` | Every writer and reader of the Host's memory, topics where the newest note is live, and what `recall` searches |
| Skills the Host writes | `brain/mastercode/runtime/host_skills.py`, `runtime/skill_standard.py`, `default-skills/housekeeping.md` | The standard a skill must pass, versions, the untrusted mark, and the daily review the person can turn on |
| The Host's own app data | `brain/mastercode/world_db.py`, `migrations/v233_host_world.sql` | The `data` tool: the Host keeps structured data in tables it designs in its own schema, without building an app, under a database role that can write only there |
| Safety floor | `brain/mastercode/runtime/crisis.py` | The crisis floor: a deterministic phrase check plus an embedding-based check on the incoming message |
| HTTP API | `server/` (FastAPI), `server/route_table.py` | The dashboard API; the route table is read through FastAPI's public route API so contract checks survive framework upgrades |
| Apps | `system-apps/` (each app with an `app.yaml` manifest) | Chat (Circles), Workbench, Orchestrator, Keys, Settings, connectors, creative engines |
| Local runtime | `vm/runtime/`, `server/applications/local_runtime.py` | The service in the virtual machine that starts, stops and limits the containers apps and coding agents run in, on a socket only the Host can use |
| Coding agents | `sidecars/harness-*` | Container images that run Claude Code, Codex, Cursor and other coding agents under the Host's control |
| Sandboxing | `seccomp-*.json`, `sidecars/app-egress/`, `server/connectors/_picture_child.py` | Seccomp profiles, the egress gate every app's traffic goes through, and a renderer child process locked down with seccomp and Landlock |
| Installer | `packages/cseries/` (Node, tests in `packages/cseries/test`) | `npx cseries`: preflight, the virtual machine (WSL on Windows, Lima on macOS and Linux), the bastion as a system service installed with one administrator prompt, pairing and approvals |
| Release pipeline | `.github/workflows/release.yml` | Multi-arch images, vulnerability scan gate, npm publish with provenance |
| Personal-data and term gates | `scripts/test_public_source_clean.py`, `scripts/release_term_check.py` | Checks that keep real people's data and private names out of public source |

The bastion is a separate Go program with its own security model and release
notes in its repository. `npx cseries` installs it only after checking its
release manifest's signature against the bastion's pinned release keys, and
the downloaded binary's sha256 against that manifest
(`packages/cseries/src/bastion.js`).
