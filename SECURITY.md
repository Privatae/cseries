# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Email **security@privatae.ai** with:

- the version (Settings shows it, as does the release line of
  `npx cseries status`) and your machine's operating system,
- what you found and how to reproduce it,
- what an attacker could do with it.

You will get an acknowledgement within three business days. We will keep you
informed as the report is confirmed and fixed, and credit you in the release
notes if you wish.

## What is in scope

- This repository: the Host, its containers and images, the installer under
  `packages/cseries/`, and the virtual machine images under `vm/`.
- Anything that lets an app, a coding engine, a web page or message the Host
  reads, another user or program on the same machine, or a machine on the
  network do more than the owner allows: reaching the dashboard or its API
  without the owner's session outside the routes the security model lists,
  reading another app's data, getting past an approval or the capability gate,
  leaving the virtual machine or a container, or putting a stored secret into a
  model's prompt.

The security model, each claim with the test that enforces it, and how to
verify a release you downloaded are in [docs/ai/](docs/ai/README.md):
[threat-model.md](docs/ai/threat-model.md), [claims.md](docs/ai/claims.md) and
[verify.md](docs/ai/verify.md).

## What is out of scope

- The bastion and the bastion agent. They have their own policies:
  [Privatae/bastion](https://github.com/Privatae/bastion/blob/main/SECURITY.md)
  and [Privatae/bastion-agent](https://github.com/Privatae/bastion-agent/blob/main/SECURITY.md).
  Reports about them sent to the address above reach the same people.
- Privatae's own services (C-Series Cloud, the Privatae network and its App
  Store catalog). Report those to the same address.
- The models and model providers a Host is set up to use.
- Findings that need control of the machine's administrator account already.

## Supported versions

Only the current release receives fixes. A Host on your own machine is updated
with `npx cseries@latest update`; a C-Series Cloud Host is kept patched by Privatae.
