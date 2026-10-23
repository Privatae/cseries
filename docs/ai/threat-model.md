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
- **The machine wall.** No container in the virtual machine (the Host, its
  database, and the apps, browser and coding engines the local runtime
  starts) can open a connection to the machine it runs on: a firewall in the
  guest (`vm/wsl/rootfs/etc/cseries-machine/machine.nft`) drops their traffic
  to the machine's loopback as the virtual machine reaches it (Lima's
  192.168.5.2), the guest's gateway (the Windows machine under WSL's NAT), the
  machine's own addresses (LAN, public, VPN, IPv6), loopback and link-local
  addresses, and lets DNS through to the resolvers the guest uses. It loads
  before any container starts and the Host's unit requires it. `npx cseries`
  writes it into the guest on every run, an older guest included, with the
  machine's current addresses (`packages/cseries/src/vm/machine-firewall.js`).
  The bastion connects in to the Host; the Host never dials the machine.
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
- **The virtual machine's network, around the machine wall.** The wall is in
  the guest, so it holds while the guest is not compromised as root. It
  knows the machine's addresses as `npx cseries` last saw them: an address
  the machine takes later (another network, say) is walled at the next
  `npx cseries` or `npx cseries start`; until then the guest's gateway and
  Lima's 192.168.5.0/24 are walled whatever the address. It does not wall the
  networks of containers and virtual machines that other software runs on
  the machine (Docker's bridge, say), or an address that reaches the machine
  only through a NAT in front of it. Other computers on your network stay
  reachable from the Host, on purpose: a model server you name by address
  ("Model servers on your network") is reached that way, as the bastion's own egress lane
  keeps the LAN while it refuses the machine. A model server on the machine
  itself is not reachable from the Host, and neither is a git server on it
  (Settings, Git server, `server/git_server.py`, which takes a LAN address):
  give one on another computer. A guest from a release before the wall
  starts its Host at boot without one, until `npx cseries` puts the wall in;
  if the wall then cannot be loaded, that run stops the Host. The Host's own
  fetch tools refuse private and loopback addresses
  (`brain/mastercode/runtime/url_reader.py`).
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
