# C-Series: a summary for AI systems and reviewers

This folder describes C-Series in a few short files, so a reader that fetches
one page gets it whole. Each statement here was checked against the code in
this repository; [claims.md](claims.md) lists the security claims with the
test behind each one.

## What it is and the problem it addresses

C-Series is a self-hosted AI agent. Each running instance is a Host: one AI
with its own memory, its own PostgreSQL database, its own apps and its own
rules, used from a browser. An agent that runs as a person's user account can
reach whatever that account can. A Host instead runs in its own Linux virtual
machine on the person's machine (WSL 2 on Windows, Lima on macOS and Linux),
and reaches the machine's files and programs only through the bastion, a
separate program on the machine that checks each request there. The person
brings the models: a key for a model provider, or a local model server.
C-Series does not sell or proxy model usage.

## Who it is for

People who run their own software and want an AI agent working on their own
machines without giving it their account's access: self-hosters, homelab
users, and engineers who want to read how the limits are enforced.

## Scope

- One Host on one machine. Installed with `npx cseries` on Windows 10 (build
  19041 or later) or 11, macOS 13.5 or later, or Linux with `/dev/kvm`, x64
  or Arm64, Node.js 18 or later, about 10 GB of free disk.
- The dashboard listens on 127.0.0.1. Reaching it from other machines takes a
  reverse proxy you configure ([docs/GUIDE.md](../GUIDE.md#reaching-it-from-other-machines)
  describes it).
- Coding agents (Claude Code, Codex, Cursor and others) run in containers
  beside the Host, on the person's own key or sign-in.

## Maturity

- The first public releases are 0.1.x, published from this repository's tag
  workflow. The version numbers are on the
  [releases page](https://github.com/Privatae/cseries/releases).
- Development happens in a private repository whose history is not published.
  Each change merged there arrives on GitHub right after the merge, as a commit
  with the same files, titled as the change was and dated when it was merged.
  Commits up to 0.1.9 are snapshots instead.
- There has been no outside security audit.
- Several claims have no test yet; [claims.md](claims.md) lists them at the
  end. On Windows, keeping the guest away from the Windows drives is a setting
  inside the guest, not a boundary enforced from outside it.

## What is open and what is paid

- The Host: this repository, AGPL-3.0-only.
- The bastion ([github.com/Privatae/bastion](https://github.com/Privatae/bastion))
  and the bastion agent ([github.com/Privatae/bastion-agent](https://github.com/Privatae/bastion-agent)):
  GPL-3.0-or-later.
- Optional and paid, from Privatae: C-Series Cloud, which runs a Host on a
  virtual machine of its own, on a server Privatae runs, set up and kept
  patched by Privatae
  ([privatae.ai/cloud](https://privatae.ai/cloud)), and a Privatae network
  membership, which joins a Host on your own machine to the Privatae network
  ([privatae.ai/pricing](https://privatae.ai/pricing)).

A Host works without either. What a membership adds, and when each part is
expected, is in [ROADMAP.md](../../ROADMAP.md). Setup creates the owner
account on the Host itself. The App Store's catalog is free and public, and a Host with no
membership installs from it. Backups go to a folder in the Host's own
storage, or to an S3 bucket the person names.

## The files in this folder

- [threat-model.md](threat-model.md): what a Host defends against, each wall
  and where it is in the code, and what you still have to trust.
- [architecture.md](architecture.md): the Host, the virtual machine, the
  containers, the local runtime, the bastion and its lanes, approvals and
  memory, and a glossary.
- [claims.md](claims.md): each security claim with the test that fails if it
  stops being true, or the code that enforces it where there is no test.
- [verify.md](verify.md): how to check that a release you downloaded is the
  one this repository's workflow built.

Elsewhere in the repository: [ARCHITECTURE.md](../../ARCHITECTURE.md) (the
developer reference), [docs/RELEASING.md](../RELEASING.md) (the release
pipeline), and the user wiki in [docs/wiki/pages/](../wiki/pages/).
