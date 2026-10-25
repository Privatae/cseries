# Privatae C-Series

C-Series is a free, open-source AI agent that runs on your own machine. Each
running instance is a Host: one AI with its own memory in its own PostgreSQL
database, its own apps and its own rules, used from a browser. It runs in its
own Linux virtual machine on Windows, macOS or Linux, and reaches your files
and programs only through the bastion, a separate program on the machine that
checks every request there. You bring the models: a key from a model
provider, or a model server on another computer on your network.

Privatae, which builds C-Series, also offers two optional paid services,
neither on sale yet: C-Series Cloud, a Host on a virtual machine of its own
on a server we run, whose waitlist is open, and a network membership for a
Host on your own machine. [ROADMAP.md](ROADMAP.md) is the plan: what each
part does today, what is being built, and our estimate of when it arrives.

## Install

```
npx cseries
```

- Windows 10 (build 19041 or later) or Windows 11; macOS 13.5 or later; or
  Linux with `/dev/kvm`. An x64 or Arm64 machine with hardware
  virtualisation turned on.
- Node.js 18 or later, for `npx`.
- About 10 GB of free disk. The virtual machine gets half the machine's
  memory, so 8 GB or more works best.
- A browser that sends `*.localhost` names to your own machine: Chrome, Edge
  or Firefox.

It checks the machine, downloads the bastion, sets up a Linux virtual machine
named `cseries` (in WSL on Windows, in Lima on macOS and Linux), pulls the
published Host images into it and starts the Host. It then installs the
bastion as this machine's system service and pairs it with the Host, in one
administrator prompt, and opens the setup page with a one-time setup code.
[docs/GUIDE.md](docs/GUIDE.md) covers the first sign-in, what the installer
asks for on a machine without WSL or QEMU, and reaching the Host from other
machines.

| To | Run |
|---|---|
| Open the dashboard | `npx cseries open` |
| See whether it is running | `npx cseries status` |
| Update to the latest release | `npx cseries@latest update` |
| Stop / start | `npx cseries stop`, `npx cseries start` |
| Read the logs | `npx cseries logs` (`host` or `postgres`) |
| Stop it and stop it starting at login, keeping its data | `npx cseries uninstall` |
| Delete the Host and its data | `npx cseries uninstall --delete-data` (permanent) |

## What is free

- **The Host**: this repository, under AGPL-3.0-only. Setup creates the owner
  account on the Host itself, with no Privatae account.
- **The bastion** ([github.com/Privatae/bastion](https://github.com/Privatae/bastion))
  and **the bastion agent** ([github.com/Privatae/bastion-agent](https://github.com/Privatae/bastion-agent)),
  under GPL-3.0-or-later. The bastion agent indexes a folder you connect, on
  your machine, and the Host searches it through the bastion.
- **The App Store.** Its catalog is public and signed, and any Host installs
  from it with no membership.
- **Backups to storage you choose.** Once you make a recovery key in the
  Backups app, the Host backs itself up, encrypted to that key, to a folder
  in its own storage on this machine or to an S3 bucket you name.

You pay your model provider for what you use. C-Series does not sell or proxy
model usage.

## C-Series Cloud: waitlist open

Nothing can be bought yet. [privatae.ai/cloud](https://privatae.ai/cloud)
takes the waitlist and sends one email when it opens.

A Host on a virtual machine of its own, on a server Privatae runs, rented from
OVHcloud in Virginia (US East). We install C-Series on it and hold its network
security posture: what may reach it, what it may reach, and how it stays
patched.

- **Your disk key.** The Host's data is on an encrypted volume that, once you
  set up the machine, only your passkey or your recovery key opens. We hold
  no copy of either. After a restart the machine waits for you to unlock it.
- **One button to set up.** You set up the machine from your machines page
  with one button and one passkey.
- **An access log.** Your machines page lists what Privatae did on your
  machine after handing it to you. Each of our actions says whether a
  program, an AI agent or a person did it;
  [privatae.ai/cloud](https://privatae.ai/cloud) lists the two cases where it
  does not.
- **Backups.** We take no backups of the machine's disk. The Host backs
  itself up, encrypted, once you give it a backup recovery key.

The sizes, and the prices it will open at:

| Size | Processor | Memory | Storage | Billed monthly | Or billed weekly |
|---|---|---|---|---|---|
| C4-8 | 4 vCPU | 8 GB | 75 GB NVMe | $59 a month | $17 a week |
| C6-12 | 6 vCPU | 12 GB | 100 GB NVMe | $99 a month | $29 a week |
| C8-24 | 8 vCPU | 24 GB | 200 GB NVMe | $169 a month | $49 a week |

US dollars, before tax. For customers in the United States, for now. Bare
metal, a whole server in Virginia (US East) or Oregon (US West), is ordered
by email at info@privatae.ai, not online. Billing terms, and the full list
of what we can and cannot reach on your machine, are on
[privatae.ai/cloud](https://privatae.ai/cloud).

## Network membership: not on sale yet

$9 a month when it opens, for a Host on your own machine. A member Host
connects out to the Privatae network over one connection it keeps open, and
nothing the network sends can run on it.

The network is for what one machine cannot do by itself: messages between
Hosts and between people, private git, workers that run while your machine is
off, encrypted backups kept off the machine, a community board, and reaching
your own Host from a phone or a browser anywhere. The Host already has the
step that joins it to the network; buying a membership is being built, and
most of the rest is not built yet. [ROADMAP.md](ROADMAP.md) gives each
feature's status and estimate.

## How it is built

A Host is built on the assumption that its model can be wrong, can be steered
by content it reads (a web page, a file, a message), or can act when nobody
asked it to. So its limits are checks that run in code whether or not the
model cooperates. The Host runs in a virtual machine that mounts none of your
folders, in containers with no Linux capabilities and a read-only root
filesystem. It reaches your files and programs only through the bastion,
which checks each request on the machine itself. An agent the Host dispatches
starts with no capabilities and cannot run code. A rule that widens what the
Host may reach on your network waits for a code from your authenticator. Each
limit that can stop work (spend, counts of turns, time) has a coded default
that only you change. [docs/ai/threat-model.md](docs/ai/threat-model.md) lists
each of these, where it is in the code, and what you still have to trust. On
Windows, keeping the virtual machine away from your drives and programs is a
WSL setting inside it, which holds only while the virtual machine is not
compromised as root.

The same rules apply to what Privatae offers:

- **Your Host's memory, conversations and keys stay where it runs.** They
  are kept in the Host, on your own machine or on your Cloud machine's
  encrypted disk. Model requests go from the Host to the provider you chose,
  under your key, through no service of ours.
- **You hold the keys.** Your model providers' keys stay in the Host. A Cloud
  machine's disk opens only with your passkey or your recovery key, and
  backups are encrypted to the backup recovery key you make. We hold no copy
  of any of them.
- **The network will keep backups and messages sealed.** A backup is
  encrypted before it leaves the Host, and a message will wait on the
  network sealed to the receiving Host's key. A cloud worker will keep no
  memory of its own. What you post on the community board is not sealed:
  other members read it.
- **The Host is free.** Anyone can run it and read its source. We charge for
  Cloud machines and for the network membership, once each is on sale.

## More

- [ROADMAP.md](ROADMAP.md): what is available, in progress and planned, with
  estimates.
- [docs/GUIDE.md](docs/GUIDE.md): the first sign-in, chat apps, memory,
  reaching the Host from other machines, building from source, and where to
  look in the code.
- [docs/ai/](docs/ai/README.md): for AI systems and reviewers: scope,
  maturity, the threat model, each security claim with its test, and how to
  verify a release.
- [ARCHITECTURE.md](ARCHITECTURE.md): how the Host is put together.
- [privatae.ai/cseries](https://privatae.ai/cseries),
  [privatae.ai/cloud](https://privatae.ai/cloud) and
  [privatae.ai/pricing](https://privatae.ai/pricing).

## How this repository is published

Development happens in a private repository, and its history is not
published. Each change merged there arrives here right after the merge, as a
commit with the same files, titled as the change was and dated when it was
merged. Commits up to 0.1.9 are snapshots instead. Each release is tagged
`vX.Y.Z` and published by
[`.github/workflows/release.yml`](.github/workflows/release.yml): the images
pushed by digest with provenance attestations, the guest images with their
sha256, and the npm package with provenance.
[docs/RELEASING.md](docs/RELEASING.md) describes the pipeline, and
[docs/ai/verify.md](docs/ai/verify.md) how to check a release you downloaded.

Issues and pull requests are welcome on GitHub. An accepted change is carried
into the development repository and arrives here as its own commit.
Report a security problem by email to security@privatae.ai, not in a public
issue; [SECURITY.md](SECURITY.md) says what to send and what is in scope.

## Licence

GNU Affero General Public License, version 3 only (AGPL-3.0-only); see
[LICENSE](LICENSE). Copyright Privatae LLC. If you run a modified version for
other people over a network, the licence requires you to offer them its
source. Third-party components keep their own licences; see [NOTICE](NOTICE)
and [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
