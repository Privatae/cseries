# C-Series Cloud box

A C-Series Host on a VPS or bare-metal machine with its own front door: Debian
13, the same Host and database containers as the local install, and in front
of them only what this directory adds. Nothing of a platform sits between the
internet and the Host.

## What the box exposes

| Port | Who answers | Who may connect |
|---|---|---|
| 443/tcp | Caddy, TLS 1.3 only, the box's wildcard certificate; every request goes through the Host's `GET /__host/door` (forward_auth, with the door key below) before it is proxied, and a request for `/__host/door` itself is answered 404 | anyone |
| 8811/tcp | the Host's bastion lane, mutual TLS against the Host's own CA | anyone; the Host completes a handshake only for certificates it issued |
| 8810/tcp | the Host's enroll port | only an address in the pairing window: the Host asks `cseries-door-helper` to open it for the requester's address, for 15 minutes |

Everything else is dropped by `etc/cseries-cloud/nftables/door.nft`, which is
also where new connections are metered per source (20 a second after a burst
of 100), with one shared limit for sources the meter has no room left to
track (100 a second after a burst of 200). There is no SSH after the
box is sealed. IPv4 8810/8811 reach the container by DNAT to its fixed address
(the quadlet's own publish holds 127.0.0.1 on those ports); IPv6 8810/8811 go
through `cseries-enroll6`/`cseries-lane6`, v6-only relays.

Outbound, the containers and the bastion reach the internet and no private
range or metadata address, and of the box itself only its DNS resolver (and,
for the coding engines, the plain proxy described below). The
bastion is paired with the Host over the unix sockets in `/run/privatae-host`,
with its `ops`, `dcn`, `services`, `observe`, `session`, `models` and
`directives` families switched off (`FAMILIES_OFF` in
`cseries-cloud-bootstrap`), and its coder mode off: the box's local runtime
starts every container instead.

## The local runtime

`cseries-runtime` (from `vm/runtime`, the same files the WSL guest installs)
starts the containers the Host asks for: its apps, the browser and the
coding engines. It listens on `/run/cseries-runtime/runtime.sock`, a
directory the Host container mounts with the `cseries-runtime` group, and
answers only uid 1000 inside the Host's own service.
Its containers are on `cseries-apps` (10.89.78.0/24, bridge `cseries-apps0`,
no default route), with the Host beside them at 10.89.78.2. door.nft lets
them reach the box only for DNS and, for the coding engines alone, the plain
proxy (`cseries-squid.service` on 10.89.78.1:3128); it
drops IPv6 from that bridge, routes nothing out of or into
that network, and its bridge table lets a frame between two of its ports
through only to or from the Host. The images are the release's, pinned by
digest in `/etc/cseries-runtime/images.json` (`cseries-guest configure`
writes it from the signed manifest); the coding engines are built on the box
from the release's pinned sources, as the unprivileged user `cseries-build`
(uid 199999) with rootless podman in a network namespace of its own, and
door.nft lets that user reach the plain proxy and nothing else. At each start
the runtime removes the containers it left, and the images and unpacked
sources an earlier release left: an image of a runtime family that
images.json no longer pins, and an engine built from other sources or the
way an earlier release built engines. Its containers log to the journal.

## The door key

Caddy asks the Host about every request at `GET /__host/door`, and the Host
answers only a question that carries the door key, once, in `X-Cseries-Door`.
Caddy sets that header on its forward_auth question (replacing any value a
client sent) and strips it from every request it passes on. Bootstrap makes
the key once (32 random bytes in hex) and keeps it on every later run; no
overlay file names it, so an update or a rollback never touches it, and the
seal leaves it alone. It lives in two files:

| File | Mode | Read by |
|---|---|---|
| `/etc/cseries-cloud/door-secret` | 0640 root:cseries | the Host, mounted read-only at `/run/cseries-cloud/door-secret` (`CSERIES_DOOR_SECRET_FILE`) |
| `/etc/cseries-cloud/door-secret.env` | 0600 root | systemd, as Caddy's `EnvironmentFile` (`CSERIES_DOOR_SECRET`, the Caddyfile's `{$CSERIES_DOOR_SECRET}`) |

systemd reads the env file before each start and each reload of Caddy, so
`systemctl reload caddy` takes a new key. Caddy runs without Debian's
`--environ`, which would print the key to the journal. Without the env file
Caddy does not start; with an empty key the Host refuses every question.

To rotate it, as root on the box:

```sh
/usr/local/sbin/cseries-cloud-bootstrap rotate-door-key
```

It writes a new key into `door-secret` in place, then `door-secret.env`, then
reloads Caddy; the Host notices the new key on its next question, without a
restart. From the first write to the reload the Host refuses Caddy's
questions, so a few requests in that moment are refused. Done by hand, the
same order holds, and `door-secret` must be rewritten in place (`truncate` and write, never a new file moved over it):
the Host's mount is of that one file and keeps its inode, so a renamed file
would never reach the Host.

## Files

- `bootstrap.sh`: the provisioner's one command (`bootstrap.sh`, then
  `bootstrap.sh seal`). It installs the Debian packages, fetches the overlay
  (verified against the release keys) or takes one already on the machine, and
  hands over to `cseries-cloud-bootstrap`.
- `overlay/`: every file the box installs under `/`. A name ending in `.in` is
  a template; `@NAME@` in it is filled at install time.
- `build-overlay.sh`: the overlay tarball, the same bytes from the same tree.
  It adds the parts of the WSL guest a cloud box runs unchanged (both quadlets,
  `cseries.network`, `cseries-guest`), the local runtime from `vm/runtime`,
  and both seccomp profiles.
- `cloud-release.json`: the release manifest the release workflow fills and
  the maintainer signs (`docs/RELEASING.md`).
- `test/`: the end-to-end test in a VM.

## bootstrap.json

`/etc/cseries-cloud/bootstrap.json`, written by the provisioner before it runs
`bootstrap.sh`:

```json
{
  "name": "orchard",
  "machine_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "acme_dns": {"api_base": "https://acme.example.test",
               "credential": {"username": "...", "password": "...", "fulldomain": "...",
                              "subdomain": "...", "allowfrom": ["203.0.113.40/32"]}},
  "reboot_window": "04:00",
  "setup_code": "0a1b2c-3d4e5f-6a7b8c"
}
```

`name` and `machine_id` (a UUID) are required. Optional: `setup_code`,
`zone`, `heartbeat_url`, `release_base`, `reboot_window` (HH:MM UTC),
`acme_email`, `acme_server`;
`acme_dns` (without it the box serves a self-signed certificate); `images`
(`{host:{ref,digest}, postgres:{ref,digest}}`, else the signed release's);
`image_archive` (`{path, host, postgres}`, images in an archive already on the
machine); `overlay_archive` and `bastion_binary` (files already on the
machine). `cseries-cloud-bootstrap` documents the one JSON line it prints.

The file holds the acme-dns password and the setup code and stays on the box
(a later run reads it again), so the bootstrap makes it root:root 0600 before
it reads it. Write it 0600 in the first place.

## The VM test

On a Linux machine with KVM, Docker, qemu, cloud-localds, nmap, openssl,
curl and python3 with cryptography, as root, with the images built from this
tree and a linux-amd64 privatae-bastion binary that has `switch`:

```sh
docker build -f Dockerfile.linux -t cseries:cloud-box .
docker build -f Dockerfile.postgres -t cseries-postgres:cloud-box .
docker build -f sidecars/app-runtime-python/Dockerfile -t cseries-app-runtime-python:cloud-box .
docker build -t cseries-app-helper:cloud-box sidecars/app-helper
docker build -t cseries-browser:cloud-box sidecars/browser
docker build -f sidecars/app-runtime-creative/Dockerfile -t cseries-app-runtime-creative:cloud-box .
BASTION_BIN=/path/to/privatae-bastion-linux-amd64 vm/cloud/test/run-vm-test.sh
```

It downloads the Debian 13 cloud image once (checked against Debian's
SHA512SUMS), boots a fresh VM, runs the bootstrap as a provisioner would, and
checks the door, the bastion, egress, the pairing window from outside, the
updater (refusals and a rollback), the heartbeat, a reboot, the box with every
Privatae address blocked, the seal, and the port scan after it. From outside,
through Caddy, it claims the Host with the setup code bootstrap mounted, signs
in with the password, follows an app origin's join, opens a WebSocket, sees
the door key's refusals and rotates the key; an outside bastion (a container
on Docker's bridge, the Host's name mapped to the VM) pairs at
`https://home.<name>.<zone>:8810` inside the window, holds its lane on 8811,
and a later one is refused once the window has closed. Sign-in and the door
run again after the reboot, with the platform blocked, and after the seal.
The local runtime is checked too: the runtime images pinned, apps and a
coding engine started and reached, the browser loading a page through the
Host's egress gate, and a stale runtime image removed. Everything goes under
`/var/tmp/cloud-vm` (`VM_WORK`, `TEST_WORK`, `VM_IMAGE_DIR`); `--keep` leaves
the VM running. `vm/cloud/test/vm.sh` starts, reaches and stops the VM on its
own (`vm.sh up`, `vm.sh ssh`, `vm.sh reboot`, `vm.sh down`).
