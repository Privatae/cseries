# Privatae C-Series

C-Series is an open-source AI agent that runs on your own machine. We call
each running instance a Host: one AI with its own memory, its own PostgreSQL
database, its own apps and its own rules, used from a browser. It runs
sandboxed in a small Linux virtual machine, in containers: the Host, its
database, and the apps that need a container of their own. A separate
program, the bastion, runs on the machine itself as a system service and is
paired with the Host.

The person brings the models: a key from OpenRouter, Anthropic, OpenAI or
another provider, or a local model server. C-Series does not sell or proxy
model usage.

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

The bastion is a separate Go program with its own repository, licensed
GPL-3.0-or-later.

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

## What you need

- Windows 10 (build 19041 or later) or Windows 11; macOS 13.5 or later; or
  Linux with `/dev/kvm`. An x64 or Arm64 machine with hardware
  virtualisation turned on.
- Node.js 18 or later, for `npx`.
- About 10 GB of free disk. The virtual machine gets half the machine's
  memory, so 8 GB or more works best.
- A browser that sends `*.localhost` names to your own machine: Chrome, Edge
  or Firefox.

## Install

```
npx cseries
```

It checks the machine, downloads the bastion, sets up a Linux virtual machine
named `cseries` (in WSL on Windows, in Lima on macOS and Linux), pulls the
published Host images into it and starts the Host. It then installs the
bastion as this machine's system service and pairs it with the Host, in one
administrator prompt, and opens the setup page with a one-time setup code.
The virtual machine starts again each time you log in.

On Windows without WSL, it first asks for administrator rights to install
WSL and for a restart, then carries on by itself after the restart. On Linux
without QEMU, or without access to `/dev/kvm`, it asks for your sudo password
once to install QEMU from the distribution's packages or to add you to the
`/dev/kvm` group. Run it as yourself, not as root.

The virtual machine mounts none of your folders. The Host publishes its ports
(8800 for the dashboard, 8810 and 8811 for the bastion) on 127.0.0.1 only.

## First sign-in

Open <http://home.cseries.localhost:8800/setup> (or <http://127.0.0.1:8800/setup>),
enter the setup code, and choose the owner's email and a password of at least
12 characters. Until the owner account exists, the code changes every time the
Host restarts.

Then give the Host a model. The Getting Started app walks you through it: add
a key from a model provider (OpenRouter, Anthropic, OpenAI and others) in
Keys, then apply the recommended setup in the Orchestrator.

## Day to day

| To | Run |
|---|---|
| Open the dashboard | `npx cseries open` |
| See whether it is running | `npx cseries status` |
| Update to the latest release | `npx cseries@latest update` |
| Stop / start | `npx cseries stop`, `npx cseries start` |
| Read the logs | `npx cseries logs` (`host` or `postgres`) |
| Stop it and stop it starting at login, keeping its data | `npx cseries uninstall` |
| Delete the Host and its data | `npx cseries uninstall --delete-data` (permanent) |

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
   redirect to its sign-in page, or 401, so nothing but the sign-in pages
   and the OAuth returns reaches it without the owner's session. Another
   proxy must send the request's method and URI in `X-Forwarded-Method` and
   `X-Forwarded-Uri` and hand a redirect back to the browser as it is;
   nginx's `auth_request` does neither by itself, and treats a redirect as
   an error.
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
address that machine reaches the Host's ports 8810 and 8811 at; the Network
app then shows the pair command for it. Where a server's firewall keeps 8810
shut, `CSERIES_DOOR_HELPER` names the unix socket of a helper on that server
that opens it to the owner's address for the 15-minute pairing window, after
a fresh passkey check. `CSERIES_SETUP_CODE_FILE` names a file holding the
setup code, for a Host someone else installs.

## The Privatae network

A Host works without a Privatae account. With a network membership, it can
join the Privatae network: open Settings, then Network, and enter the one-time
code from <https://privatae.ai/account/network>. A member Host connects to the
network outbound only.

## Licence

GNU Affero General Public License, version 3 only (AGPL-3.0-only); see
[LICENSE](LICENSE). Copyright Privatae LLC. If you run a modified version for
other people over a network, the licence requires you to offer them its
source. Third-party components keep their own licences; see [NOTICE](NOTICE)
and [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
