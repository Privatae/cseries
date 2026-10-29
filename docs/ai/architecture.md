# Architecture

A short map of how a Host is put together. [ARCHITECTURE.md](../../ARCHITECTURE.md)
is the longer developer reference.

## The machine and the virtual machine

`npx cseries` (`packages/cseries/`) checks the machine, creates a Linux
virtual machine named `cseries` and starts the Host in it: WSL 2 on Windows,
Lima on macOS (Apple's virtualisation framework) and on Linux (QEMU with
KVM). The guest images are built by `vm/wsl/build-rootfs.sh` and
`vm/lima/build-image.sh` and published with each release. Only ports 8800
(the dashboard), 8810 (bastion pairing) and 8811 (the bastion lane) are
forwarded, to the machine's 127.0.0.1.

## The Host's containers

Inside the guest, two containers make the Host: the Host itself (the FastAPI
server in `server/` and the runtime in `brain/mastercode/`) and its
PostgreSQL database. Their settings are in the guest's units
(`vm/wsl/rootfs/etc/containers/systemd/`) and, for running from source, in
`compose.yaml`. The database is reached only over a Unix socket shared with
the Host; its data directory is never mounted into the Host.

## The local runtime

Apps that need a container of their own, the browser and the coding engines
run beside the Host, never inside it. The `cseries-runtime` service in the
guest starts, stops and limits those containers
(`vm/runtime/`, `server/applications/local_runtime.py`). It listens on the
Unix socket `/run/cseries-runtime/runtime.sock`, owned by root and the
`cseries-runtime` group, mode 0660. Its images are published with each
release and pinned by digest; a coding engine's image is built on the
machine from the harness source published with the release.

## The bastion and its lanes

The bastion is a separate program, installed by `npx cseries` as the
machine's system service with one administrator prompt
(`packages/cseries/src/bastion.js`). It dials the Host: a one-time pairing
token, valid for 15 minutes, on port 8810, then the lane on port 8811 over
mutual TLS under a certificate authority the Host makes for itself. The Host
accepts only certificates it issued at pairing and has not revoked, and a
revoke closes open lane connections. Through the bastion the Host can reach
folders the person connected, the machine's state and local model servers,
each as the bastion allows. The bastion's own repository describes each
operation and what it checks.

## Approvals

What the Host may not do alone waits for the person, in one of three ways:

- **A code from the person's authenticator.** Widening a network rule
  (`server/network/nac_stepup.py`), and revealing a saved key in the Keys app.
- **An approval card locked to the person.** For example a recurring task
  that runs a model (`brain/mastercode/runtime/scheduled_approval.py`), a
  change to one of the person's limits, or pairing a messaging account. The
  Host can file the card and cannot answer it.
- **An approval on the machine itself.** On a machine whose bastion is
  anchored to the Host, the bastion asks for its own approvals; its
  repository describes them.

## Outside agents

An agent such as Claude Code connects to the Host's `/mcp` by OAuth, with the
Host as the authorization server (`server/agent_access/`). It can list the
person's machines and ask for operations on them. `/mcp` and the OAuth paths
the agent calls itself reach their handlers from any address with no session;
`/mcp` answers nothing without a token, and an agent gets one only after the
signed-in owner approves it on the connect page. Anything the person has not
allowed comes back as a link to an approval page on the Host, answered with
the owner's passkey, or the authenticator's code as fallback.

## Memory

Every message and reply is stored as it happens. In its own turns the Host
writes notes: the person's preferences, rules they teach it, notes about
people, the goal of a conversation, cited facts and research. A note can
carry a topic, and a newer note on the topic becomes the live one while the
older is kept (`brain/mastercode/memory_slots.py`). The `recall` tool
searches notes, documents, projects, code, skills and the Host's own tables
together (`brain/mastercode/memory_retrieval.py`). Skills the Host writes
are checked against a standard and versioned
(`brain/mastercode/runtime/host_skills.py`, `runtime/skill_standard.py`).
[docs/HOST-MEMORY-AND-PROMPT.md](../HOST-MEMORY-AND-PROMPT.md) lists every
writer and reader.

## The Privatae network

Optional. A member Host holds an outbound connection to the network, which
carries only the operations listed in `NETWORK_OPS`
(`server/bridge/member_client.py`): App Store, community board, passport,
heartbeat, rendezvous, backup storage and remote access. A request the
network sends to the Host is answered as unsupported. While remote access is
on, the Host also keeps a tunnel out to each of the network's relays
(`server/network/remote_tunnel.py`); a relay carries a browser's
still-encrypted connection to a door in the guest, which ends TLS and asks
the Host about every request.

## Glossary

- **Host**: one running C-Series AI, with its own memory, PostgreSQL
  database, apps and rules, used from a browser.
- **Machine**: a computer; here, the one the Host runs on.
- **Bastion**: the separate program on the machine, outside the virtual
  machine, through which the Host reaches that machine. GPL-3.0-or-later,
  [github.com/Privatae/bastion](https://github.com/Privatae/bastion).
- **Bastion agent**: a separate program the bastion runs on the machine for
  a folder the person connected. It indexes the folder there, and the Host
  searches and reads it through the bastion. GPL-3.0-or-later,
  [github.com/Privatae/bastion-agent](https://github.com/Privatae/bastion-agent).
- **Anchor**: the Host a bastion answers to on its machine. `npx cseries`
  anchors the bastion it installs to the Host it installs.
- **Pairing**: the one-time exchange in which a bastion gets a certificate
  from the Host.
- **Lane**: the mutual-TLS connection a paired bastion holds to the Host.
- **Grant**: a permission for something specific. The Host grants a
  dispatched agent capabilities for one task; the person approves grants
  that let the Host use a folder or a machine within set limits.
- **Dispatched agent**: a worker the Host sends to read untrusted content,
  such as the web, and report back. It starts with no capabilities and
  cannot run code.
- **Coding engine** (harness): a coding tool such as Claude Code, Codex or
  Cursor, running in its own container beside the Host (`sidecars/harness-*`).
- **Approval card**: a request the Host cannot decide by itself, shown over
  the chat and in Circles under Waiting on you until the person answers.
- **Step-up code**: a code from the person's authenticator app, asked for
  before an action that widens what the Host can reach or reveals a secret.
- **The person's limits**: every limit that can stop work the person asked
  for, each with a coded default that only the person changes (Settings,
  Limits; `brain/mastercode/runtime/user_limits.py`).
- **`world`**: the Host's own database schema, where it designs tables for
  data it keeps, under a role that can write nowhere else.
- **Local runtime**: the service in the virtual machine that runs app,
  browser and coding-engine containers.
- **App Store**: the catalog of apps a Host can install. Its catalog is free,
  public and signed.
- **Privatae network**: an optional paid membership; a Host joins with a
  one-time code over one outbound connection.
- **C-Series Cloud**: an optional paid service that runs a Host on a server
  of its own ([privatae.ai/cloud](https://privatae.ai/cloud)).
