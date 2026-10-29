# C-Series roadmap

What is available, what is being built, and what comes after. Each release
updates the status of every item here ([docs/RELEASING.md](docs/RELEASING.md)).
Other documents in this repository say whether each paid offering is on sale,
and link here for the rest.

Last updated 2026-10-29. An estimate is our guess, counted from that date.
It is not a promised date, and it is changed here when it moves.

- **Available**: in a published release, or on sale.
- **Waitlist open**: not on sale yet; [privatae.ai/cloud](https://privatae.ai/cloud)
  takes the waitlist.
- **Built**: finished, and opens with the offering it belongs to.
- **In progress**: being built now.
- **Planned**: decided, not started.

## Goals

- A Host on your own machine that can be reached from anywhere, can reach
  other Hosts and people, and can have work done while the machine is off,
  with its memory, conversations and keys staying on the machine.
- You hold your keys: to your models, your disk, your backups and your
  messages.
- An open-source Host that anyone can run, free.

## The free Host

- **Install with one command.** Available. `npx cseries` sets up a Linux
  virtual machine (WSL on Windows, Lima on macOS and Linux), starts the Host
  in it and pairs the bastion with it.
- **The bastion.** Available. A separate program on your machine; the Host
  reaches your files and programs only through it, and it checks each
  request there.
- **Folder search.** Available. The bastion agent indexes a folder you
  connect, on your machine, and the Host searches it through the bastion.
- **Memory and skills.** Available. The Host keeps its memory in its own
  database, writes notes you can read and correct, and writes skills for
  jobs worth repeating, keeping every version.
- **The App Store.** Available, free for every Host. Its catalog is public
  and signed, and no membership is needed.
- **Coding agents.** Available. Claude Code, Codex, Cursor and others run in
  containers beside the Host, on your own key or sign-in.
- **Chat apps.** Available. Telegram, Discord and Slack, through connector
  apps from the App Store.
- **MCP servers.** Available. The Host uses tools from MCP servers, including
  servers that ask you to sign in.
- **Outside agents.** Available. Claude Code and other agents connect to the
  Host over MCP. Anything new comes to you as a link to an approval page on
  the Host, which you answer with your passkey or your authenticator's code.
- **Backups to storage you choose.** Available. Once you make a recovery key,
  the Host backs itself up, encrypted to that key, to a folder in its own
  storage on the machine or to an S3 bucket you name.

## Cloud machines (C-Series Cloud)

A Host on a virtual machine of its own, on a server Privatae runs, rented from
OVHcloud in Virginia (US East). Sizes and prices are in the
[README](README.md#c-series-cloud-waitlist-open). Nothing can be bought yet:
[privatae.ai/cloud](https://privatae.ai/cloud) takes the waitlist.

- **Virtual machines.** Waitlist open. Three sizes, billed monthly or weekly,
  for customers in the United States.
- **Setup with one button and one passkey.** Built. You set up the machine
  from your machines page, with no command to run on it.
- **Your disk key.** Built. The Host's data is on an encrypted volume that,
  once you set up the machine, only your passkey or your recovery key opens.
  We hold no copy of either.
- **An access log.** Built. Your machines page lists what Privatae did on
  your machine after handing it to you, and whether a program, an AI agent
  or a person did each thing, apart from the two cases
  [privatae.ai/cloud](https://privatae.ai/cloud) lists.
- **The Host's own backups.** Built. Once you give it a backup recovery key,
  the Host takes its own encrypted backups and keeps them on the machine,
  unless you choose other storage.
- **Network membership included.** Built. Each machine comes with the
  network membership for as long as it runs.
- **Bare metal.** Waitlist open. A whole server in Virginia (US East) or
  Oregon (US West), ordered by email at info@privatae.ai, not online.

## Network membership

$9 a month when it opens, for a Host on your own machine. It is not on sale
yet. The network will keep your Host's backups and messages sealed to keys
it does not have, and a cloud worker will keep no memory of its own. What
you post on the community board is not sealed: other members read it.

- **Joining and buying a membership.** In progress; estimate about 1 week.
  Joining is already in the Host (Settings, Network, a one-time code from
  your account page). Buying a membership online is being built; until it is
  done, a membership cannot be bought.
- **Encrypted backups off your machine.** In progress; estimate: when
  membership opens. Built in the Host and in the network's code; the network
  does not run it yet. The Host encrypts each backup to your recovery key
  before it leaves the machine, and the network will store it.
- **Community board.** Built; it opens with membership. A board where
  members read, post and vote, from the Community app.
- **Messages between Hosts, and between people.** In progress; estimate about
  3 weeks. Your Host will message another Host on the network, and you
  another person. A message will wait on the network sealed to the receiving
  Host's key, so the network cannot read it.
- **Git for members.** Planned; estimate about 3 to 4 weeks. Private git
  repositories on the network, which you reach through your own Host.
- **Cloud workers.** Planned; estimate about 3 to 5 weeks. A worker on the
  network that runs for your Host while your machine is off. It will keep no
  memory of its own, and what it stores for your Host will be encrypted to
  your Host's key.
- **Your Host from anywhere.** Built; it opens with membership. Reach your
  own Host from a phone or a browser anywhere, through the network, while it
  keeps running on your machine. The connection stays encrypted from the
  browser to a door on your machine, which holds the only key that opens it;
  the network's relay passes it on without one.

## Later

Intended, not scheduled, with no estimate yet.

- **Backups of a Cloud machine's disk.** Today we take none; the Host's own
  encrypted backups are the copy.
- **Hosting members' websites and apps** on the network.
