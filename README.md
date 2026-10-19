# Privatae C-Series

C-Series is an open-source AI agent that runs on your own machine. We call
each running instance a Host: one AI with its own memory, its own PostgreSQL
database, its own apps and its own rules, used from a browser. It runs
sandboxed in a small Linux virtual machine as two containers (the Host and its
database), and a separate program, the bastion, runs on the machine itself
and decides what the Host may touch there.

The person brings the models: a key from OpenRouter, Anthropic, OpenAI or
another provider, or a local model server. C-Series does not sell or proxy
model usage.

## Status

Pre-release. The code in this repository builds and runs from source (see
[Building from source](#building-from-source)). The one-command installer
(`npx cseries`) is written and tested for Windows but not yet published, and
the macOS and Linux driver is in progress. Work under way before the first
release:

- a local runtime, so apps that run in their own container (the Telegram,
  Discord and Slack connectors, the browser, the coding agents, the creative
  engines) start on the person's machine instead of asking a hosted platform;
- backups a standalone Host can write and restore by itself;
- memory and context defaults for long-running use;
- memory and CPU limits for the virtual machine.

This repository is published as a single snapshot commit. Development history
lives in a private forge.

## For reviewers: where to look

| Area | Where | What it shows |
|---|---|---|
| The turn | `brain/mastercode/runtime/session.py`, `turn_types.py`, `root_turn.py` | How one message becomes model calls, tool calls and a reply, and which turn types exist |
| Tools and the capability gate | `brain/mastercode/runtime/tools.py`, `brain/mastercode/ceigas_capabilities.py`, `runtime/turn_type_approval.py` | What the model may call, and which calls need the person's approval first |
| The Host's own data | `brain/mastercode/world_db.py` | The `data` tool: the Host designs and queries its own tables in a schema of its own, under a restricted database role |
| Safety floor | `brain/mastercode/runtime/crisis.py` | The crisis floor: a deterministic phrase check plus an embedding-based check on the incoming message |
| HTTP API | `server/` (FastAPI), `server/route_table.py` | The dashboard API; the route table is read through FastAPI's public route API so contract checks survive framework upgrades |
| Apps | `system-apps/` (47 apps, each with an `app.yaml` manifest) | Chat (Circles), Workbench, Orchestrator, Keys, Settings, connectors, creative engines |
| Coding agents | `sidecars/harness-*` | Container images that run Claude Code, Codex, Cursor and other coding agents under the Host's control |
| Sandboxing | `seccomp-*.json`, `sidecars/app-egress/`, `server/connectors/_picture_child.py` | Seccomp profiles, the egress gate every app's traffic goes through, and a renderer child process locked down with seccomp and Landlock |
| Installer | `packages/cseries/` (Node, tests in `packages/cseries/test`) | `npx cseries`: preflight, the WSL virtual machine, the bastion as a Windows service installed with one admin prompt, pairing and approvals |
| Release pipeline | `.github/workflows/release.yml` | Multi-arch images, vulnerability scan gate, npm publish with provenance |
| Personal-data and term gates | `scripts/test_public_source_clean.py`, `scripts/release_term_check.py` | Checks that keep real people's data and private names out of public source |

Scale: about 620,000 lines of Python, about 110,000 lines of TypeScript and
JavaScript (including the web apps), 232 database migrations, and 626 test
and gate scripts under `scripts/` plus the suites under `tests/` and
`packages/cseries/test`.

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

- Windows 10 (build 19041 or later) or Windows 11, with hardware
  virtualisation turned on. macOS and Linux are coming next.
- Node.js 18 or later, for `npx`.
- About 10 GB of free disk.
- A browser that sends `*.localhost` names to your own machine: Chrome, Edge
  or Firefox.

## Install (once published)

```
npx cseries
```

It checks the machine, installs the bastion as this machine's system service (one administrator prompt), sets up a Linux
virtual machine named `cseries` in WSL, pulls the published Host images into
it, starts the Host, pairs the bastion with it, and opens the setup page with
a one-time setup code. If WSL is not installed yet it asks for administrator
rights once and for a restart, then carries on after the restart by itself.

The virtual machine sees none of your folders. Only the Host's own ports
(8800 for the dashboard, 8810 and 8811 for the bastion) reach your machine,
on 127.0.0.1.

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
| Update to the latest release | `npx cseries update` |
| Stop / start | `npx cseries stop`, `npx cseries start` |
| Read the logs | `npx cseries logs` (`host` or `postgres`) |
| Remove it, keep the data | `npx cseries uninstall` |
| Remove it and its data | `npx cseries uninstall --delete-data` (permanent) |

## Building from source

The images are built from this repository's `Dockerfile.linux` and
`Dockerfile.postgres`; `compose.yaml` runs them on any machine with Docker:

```sh
git clone https://github.com/Privatae/cseries.git
cd cseries
docker compose up -d --build
docker compose logs host | grep "SETUP CODE"
```

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
   proxy, and the proxy's `X-Forwarded-Host` is believed.

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
