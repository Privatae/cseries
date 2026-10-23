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

Every other inbound connection is dropped by `etc/cseries-cloud/nftables/door.nft`, which is
also where new connections are metered per source address, per /64 for IPv6
(20 a second after a burst of 100), with one shared limit for sources the
meter has no room left to track (100 a second after a burst of 200). There is no SSH after the
box is sealed. While the box's vault is locked, 8810 and 8811 are shut too, and
443 also answers the unlock page (`unlock-<name>.<address_id>.<zone>`, below). IPv4 8810/8811 reach the container by DNAT to its fixed address
(the quadlet's own publish holds 127.0.0.1 on those ports); IPv6 8810/8811 go
through `cseries-enroll6`/`cseries-lane6`, v6-only relays.

Outbound, the Host and database containers and the bastion reach the internet
and no private range or metadata address, and of the box itself only its DNS
resolver. The local runtime's containers are more limited (below). The
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

## The vault: the customer holds the disk key

Everything on the box that is the customer's sits in one LUKS2 volume, the
vault, that only the customer's keys open (`cseries_vault.py` has the
details, `cseries-vault` and `cseries-unlock-agent` run it).

**Inside the vault**, bound from it at their usual paths:
`/var/lib/containers` (every image, layer and named volume, so the Host's and
the database's data), `/var/lib/cseries`, `/var/lib/cseries-runtime`,
`/var/lib/cseries-build`, `/var/spool/squid`, the box bastion's state
(`/root/.config/privatae-bastion`), the journal (`/var/log/journal`, which
holds the Host's container logs) and crash dumps (`/var/lib/systemd/coredump`).
While the vault is locked journald keeps the journal in memory and
systemd-coredump stores nothing (`/etc/systemd/*.conf.d/95-cseries-vault.conf`);
at unlock a runtime drop-in under `/run` sends both into the vault. The
access log's mirror and the unlock record are in the vault too.

**Outside it**, because a locked box needs them and none of them is the
customer's: the OS, the overlay, Caddy and the TLS key, the acme-dns
credential, the heartbeat key, the release keys, the firewall, the unlock
page, `/var/lib/cseries-cloud` (the updater's state and the release overlays)
and the door key. The door key is replaced at every unlock, so a copy read
from the disk while the box was locked opens nothing.

**The volume** is `/var/lib/cseries-vault.img`, the root filesystem's size
less 10 GiB, opened through a loop device as `/dev/mapper/cseries-vault` and
mounted at `/var/lib/cseries-vault`. Every keyslot holds 256 random bits (or
one derived from them), so each uses PBKDF2 at its minimum cost.

**Keys** (each keyslot named by a LUKS2 token):
- `cseries-bootstrap`, before the claim only: its key is on the disk
  (`/etc/cseries-cloud/vault-bootstrap.key`, root 0600), so until the customer
  claims the box it opens at boot by itself.
- `cseries-passkey`, one per unlock passkey. The unlock page asks the passkey
  for its WebAuthn PRF output over the vault's salt and sends the box only
  `hex(HKDF-SHA256(prf, info="cseries-vault-v1|<machine_id>|<credential_id>"))`.
  The passkey's relying party is `unlock-<name>.<address_id>.<zone>`, a name
  the Host never serves.
- `cseries-recovery`, the recovery key: 32 random bytes, shown once as 13
  groups of four base32 characters and a QR code.

**Boot.** `cseries-vault-boot.service` opens the vault with the bootstrap key
before the claim, and after it records the vault locked. Every customer
service (the Host, the database, the runtime, squid, the bastion, the
updater) requires `cseries-vault.service`, which succeeds only while the
vault is open, or on a box with no vault. While locked, the firewall keeps
8810 and 8811 shut (`lanes_open`), Caddy answers the Host's names with a
"locked" page that links to the unlock page, and unattended upgrades never
reboot the box (the owner's Restart button in the Host does).

**Unlock.** At `https://unlock-<name>.<address_id>.<zone>/` the owner unlocks
with a passkey, or the recovery key where the browser or passkey has no PRF.
`cseries-unlock-agent` opens the vault, records the unlock in it (with the
attempts kept outside while it was locked: on the plain disk only the time,
the outcome and the source's /24, or /48 for IPv6, at most the newest 1000
once the file passes 512 KiB), compares the access log with the
vault's copy, replaces the door key, opens 8810 and 8811 and starts
`cseries-unlocked.target`. Five tries a minute per source. Nothing else opens
the vault: no key is cached, sealed to a TPM or held by Privatae.

**Claim.** `/setup` refuses to create the owner until the vault's keys are set
up. On the unlock page the owner types the setup code, makes the unlock
passkey, and saves and types back the recovery key. Then `cseries-vault claim`
runs in a unit of its own, in this order:
1. it adds the recovery key and the passkey under the volume key the vault
   has, authorised by the bootstrap key;
2. it kills the bootstrap slot and overwrites and deletes its file;
3. it re-encrypts the vault to a new volume key with the owner's keys.

So the bootstrap key never wraps the new volume key: when the re-encryption
starts, no keyslot and no file on the disk holds it. The re-encryption is
given every keyslot's passphrase (one line each on its standard input, no
`--key-slot`), which keeps both of the owner's keys; given one `--key-slot`
it would keep only that one. It rewrites every sector of the vault. Changing
keyslots never changes the volume key, so without it a copy of the disk taken
before the claim (a provider backup, a rescue boot) would read everything
written later.

A reboot in the middle of the re-encryption leaves the box locked, as after
any reboot once the bootstrap key is gone. Either of the owner's keys opens
it, and that unlock starts the claim's unit again, which resumes the
re-encryption with the key the owner used; both keys work afterwards. A
reboot in the moment between steps 2 and 3 (two cryptsetup runs apart)
leaves the owner's keys under the old volume key with no re-encryption
begun: the next unlock re-encrypts with the one key it was given, which
keeps only that key, and the unlock page asks the owner to make the other
again. A reboot before step 2 leaves the bootstrap key in place, and the
owner sets up the keys again. The re-encryption's process killed without a
reboot leaves the device suspended until one, which is why nothing stops
that unit.

**What it does not stop**, stated plainly:
- Root on the server below can read the memory of a running, unlocked
  machine, including the key in use.
- Someone with the disk could change the unencrypted part so the next unlock
  hands them the key. That needs a restart the owner did not ask for, which
  the owner sees as a locked machine and in its access log.
- The owner who loses every passkey and the recovery key loses the data. No
  one can recover it.

A box built before vaults (bootstrap.json `"vault": false`, or one updated
from an earlier release) reports its vault as `absent` and runs as before.

## The access log

The platform records every action Privatae takes on a machine, and every
provider operation on it that Privatae did not start, as a hash chain, and
hands the rows to the box in the heartbeat's answer. The box keeps them in
`/var/lib/cseries-cloud/access-log.jsonl` only while each extends the chain
(`cseries_accesslog.py`). A row removed or changed later is a mismatch the
box keeps and reports, and the Host's `/machine` page shows the box's own
copy. The Host reads the box's state, the log and the unlock record from
`/run/cseries-door` (`cseries_machine.publish`), which it can read and cannot
write.

## Files

- `bootstrap.sh`: the provisioner's one command (`bootstrap.sh`, then
  `bootstrap.sh seal`). It installs the Debian packages, fetches the overlay
  (verified against the release keys) or takes one already on the machine, and
  hands over to `cseries-cloud-bootstrap`.
- `overlay/`: every file the box installs under `/`. A name ending in `.in` is
  a template; `@NAME@` in it is filled at install time.
- `build-overlay.sh`: the overlay tarball, the same bytes from the same tree.
  It adds the parts of the WSL guest a cloud box runs unchanged (both quadlets,
  `cseries.network`, the containers.conf drop-in, `cseries-guest`), the local
  runtime from `vm/runtime`, and both seccomp profiles.
- `cloud-release.json`: the release manifest the release workflow fills and
  the maintainer signs (`docs/RELEASING.md`).
- `overlay/usr/local/lib/cseries-cloud/cseries_vault.py`, `cseries_accesslog.py`,
  `cseries_machine.py`: the vault, the access log's copy, and what the box's
  services share about both (the heartbeat's fields, the files the Host reads,
  what follows an unlock). `overlay/usr/local/sbin/cseries-vault` and
  `cseries-unlock-agent` run them; `overlay/usr/share/cseries-cloud/unlock/` is
  the unlock page.
- `test/`: the end-to-end tests in a VM: `run-vm-test.sh` for the whole box,
  `run-vault-test.sh` for the vault on a small disk (the claim rewrites every
  sector of the vault, so the VM's disk file grows by the vault's size).

## bootstrap.json

`/etc/cseries-cloud/bootstrap.json`, written by the provisioner before it runs
`bootstrap.sh`:

```json
{
  "name": "orchard",
  "address_id": "k7x2m9",
  "machine_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "acme_dns": {"api_base": "https://acme.example.test",
               "credential": {"username": "...", "password": "...", "fulldomain": "...",
                              "subdomain": "...", "allowfrom": ["203.0.113.40/32"]}},
  "reboot_window": "04:00",
  "setup_code": "0a1b2c-3d4e5f-6a7b8c"
}
```

`name`, `address_id` and `machine_id` (a UUID) are required. `address_id` is
the machine's own 6 characters from `abcdefghijkmnpqrstuvwxyz23456789` (no `l`,
`o`, `0` or `1`), a letter first and at least one digit, which the provisioner
generates once and never reuses; a bootstrap.json without one is refused. The
Host answers at `<name>.<address_id>.<zone>` (`orchard.k7x2m9.cloud.privatae.ai`
here), which is its `CSERIES_URL` and the name the bastion pairs at, and each
isolated app at `apps-<key>-<name>.<address_id>.<zone>`. Both sit in the
machine's zone, `<address_id>.<zone>`: the wildcard certificate is for
`*.<address_id>.<zone>`, and the acme-dns credential is for
`_acme-challenge.<address_id>.<zone>`. The name alone need not be unique.
Optional: `setup_code`,
`zone`, `heartbeat_url`, `release_base`, `reboot_window` (HH:MM UTC),
`acme_email`, `acme_server`; `vault` (default true: false builds a box without a vault, as before vaults);
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
`https://<name>.<address_id>.<zone>:8810` inside the window, holds its lane on 8811,
and a later one is refused once the window has closed. Sign-in and the door
run again after the reboot, with the platform blocked, and after the seal.
The local runtime is checked too: the runtime images pinned, apps and a
coding engine started and reached, the browser loading a page through the
Host's egress gate, and a stale runtime image removed. Everything goes under
`/var/tmp/cloud-vm` (`VM_WORK`, `TEST_WORK`, `VM_IMAGE_DIR`); `--keep` leaves
the VM running. `vm/cloud/test/vm.sh` starts, reaches and stops the VM on its
own (`vm.sh up`, `vm.sh ssh`, `vm.sh reboot`, `vm.sh down`).

The vault's own test, on a 16G VM with the Host and its database only:

```sh
HOST_IMAGE=cseries:cloud-box PG_IMAGE=cseries-postgres:cloud-box \
BASTION_BIN=/path/to/privatae-bastion-linux-amd64 vm/cloud/test/run-vault-test.sh
```

It bootstraps a box, checks what is inside and outside the vault, claims it
through the unlock page with a passkey's passphrase computed as the page does
(the browser's PRF half is held to the same function by
`scripts/test_cloud_box_unlock_page.py`), cuts the claim off with a hard
reset half way through its re-encryption (the box then waits locked, the
bootstrap key gone) and unlocks it with the passkey, which resumes the
re-encryption, looks for an
invented marker in the VM's disk file, opens a copy of the header taken before
the claim to show it reads nothing written after, takes the contract's rows
from a stand-in heartbeat receiver and refuses one that does not chain,
reboots into the locked state (443 only, the Host down, the journal in
memory, nothing unlocking it by itself), unlocks with the passkey and with
the recovery key, restarts from the Host's Restart, refuses the seal while
swap is on, and checks the ports after the seal, locked and unlocked.
