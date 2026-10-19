# Privatae C-Series

C-Series is the consumer series of Privatae Hosts. A Host is one AI that runs
on your own machine, with its own memory, database and apps, and you use it in
your browser. It runs sandboxed in a small Linux virtual machine on your
machine, as two containers: the Host and its PostgreSQL database. The Privatae
bastion runs beside it on the machine itself.

## What you need

- Windows 10 (build 19041 or later) or Windows 11, with hardware
  virtualisation turned on. macOS and Linux are coming next.
- Node.js 18 or later, for `npx`.
- About 10 GB of free disk.
- A browser that sends `*.localhost` names to your own machine: Chrome, Edge
  or Firefox.

## Install

```
npx cseries
```

It checks the machine, installs the bastion for your user, sets up a Linux
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
