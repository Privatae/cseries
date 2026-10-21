# Threat model

## What a Host defends against

The model that drives a Host can be wrong, can be steered by content it reads
(a web page, a file, a chat message, a tool's result), or can act when nobody
asked it to. A Host is built so that none of these needs the model to behave:
each limit below is a check in code that runs whether or not the model
cooperates. [claims.md](claims.md) gives the test behind each one.

## The walls, and where each lives

- **The virtual machine.** `npx cseries` runs the Host in a Linux virtual
  machine: Lima on macOS and Linux (`packages/cseries/src/vm/lima.js`), WSL 2
  on Windows (`packages/cseries/src/vm/wsl.js`, `vm/wsl/`). The Lima template
  shares no folder and forwards only ports 8800, 8810 and 8811, from the
  guest's 127.0.0.1 to the machine's 127.0.0.1. The WSL guest turns off
  drive mounts and Windows programs in `vm/wsl/rootfs/etc/wsl.conf`.
- **The containers.** The Host and its database run as UID 1000 with a
  read-only root filesystem, every Linux capability dropped and no new
  privileges (`compose.yaml`, and the guest's units in
  `vm/wsl/rootfs/etc/containers/systemd/`). The Host runs under the seccomp
  profile `seccomp-personal.json`. The database has no network port and is
  reached over a Unix socket (`entrypoint-postgres.sh`). Apps, the browser
  and coding engines run in containers of their own that the local runtime
  in the guest starts (`vm/runtime/`).
- **The bastion.** The Host reaches the machine through the bastion, a
  separate program that runs as the machine's system service. It connects to
  the Host over mutual TLS, under a certificate authority the Host makes; the
  Host accepts only certificates it issued at pairing and has not revoked.
  Pairing is in `server/network/bastion_enroll.py`. What the bastion checks on each
  operation is in its own repository,
  [github.com/Privatae/bastion](https://github.com/Privatae/bastion).
- **The capability gate.** What the model may call is set by a capability
  gate in code (`brain/mastercode/ceigas_capabilities.py`,
  `brain/mastercode/runtime/tools.py`). An agent the Host dispatches starts
  with none, may be granted only reading, and cannot run code
  (`brain/mastercode/runtime/agent_grant.py`, `brain/mastercode/code_exec_guard.py`).
- **Approvals in code.** A network rule that widens what the Host may reach
  commits only with a code from the person's authenticator
  (`server/network/nac_stepup.py`). A recurring task that runs a model
  starts only after the person approves its card
  (`brain/mastercode/runtime/scheduled_approval.py`). Restoring a backup and
  revealing a saved key have no route the Host can call
  (`brain/mastercode/runtime/kernel_ops.py`).
- **The person's limits.** Each limit that can stop work (spend, counts of
  turns, repairs and wakes, time) has a coded default; only the person
  changes it, and the Host can only propose a change
  (`brain/mastercode/runtime/user_limits.py`, `server/routes/limits.py`).
- **The Host's own data.** What the Host builds for itself lives in the
  `world` schema, under a database role that can write nowhere else, reads
  no table holding secrets and cannot change the person's network rules or
  the tool log (`brain/mastercode/world_db.py`,
  `migrations/v233_host_world.sql`).

## What you still have to trust

- **The machine and its administrators.** Whoever has administrator rights
  on the machine controls the virtual machine and the Host in it.
- **The virtualisation software.** WSL 2 on Windows, Lima with Apple's
  virtualisation framework on macOS, Lima with QEMU and KVM on Linux. On
  Windows, keeping the guest away from the Windows drives and programs is a
  setting inside the guest, so it holds only while the guest is not
  compromised as root.
- **The virtual machine's network.** The guest has outbound network access,
  and no firewall in it stops the Host's container from connecting to the
  machine itself, such as to a service listening on the machine, wherever
  the virtualisation software's network carries that connection. The Host's
  own fetch tools refuse private and loopback addresses
  (`brain/mastercode/runtime/url_reader.py`), so that limit is in the Host's
  code, not in the virtual machine.
- **The model provider you choose.** Each model call carries the turn's
  context to that provider: the conversation, matching notes, and what the
  Host read for the turn. Calls go from the Host to the provider on your own
  key (`brain/mastercode/provider_gateway.py`), or to a local model server.
- **The bastion and its release keys.** `npx cseries` installs a bastion
  only when its release manifest is signed by one of two pinned Ed25519 keys
  and the binary matches the manifest's sha256
  (`packages/cseries/src/verify.js`, `packages/cseries/src/bastion.js`). The
  same two keys sign the App Store catalog and the C-Series Cloud release
  manifest. Whoever holds them can publish a release that Hosts and
  installers accept.
- **The release pipeline.** Images and the npm package are built by GitHub
  Actions from a tag (`.github/workflows/release.yml`). Attestations and npm
  provenance show which workflow run and commit produced an artifact; they
  do not show that the commit is safe. [verify.md](verify.md) has the checks.
- **Your own approvals.** An approval is as good as your reading of its card.
  The authenticator's secret is kept in the Host's own database
  (`server/network/nac_stepup.py`), so the code guards against requests from
  outside the Host, not against code already running inside it.
- **Coding engines and apps you turn on.** A coding engine runs a third
  party's tool in its own container, on your key or sign-in. An app from the
  App Store is installed only when its bundle is signed by a key in the
  Host's trust store; you trust that key's holder.

## Known gaps

- There has been no outside security audit.
- [claims.md](claims.md) ends with the claims that have no test yet.
- The dashboard serves plain HTTP on 127.0.0.1. Reaching it from another
  machine safely needs a reverse proxy that terminates TLS, set up as the
  main [README](../../README.md) describes.
