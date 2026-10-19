---
title: The bastion
summary: The bastion is a small open-source program you run on your own machine to join it to your Host's private network. It dials out to your Host, and your Host can do only what you allowed on that machine, checked by the machine itself.
section: network
order: 10
questions:
  - What is the Privatae bastion?
  - How do I install the bastion on my computer?
  - Does the bastion need root or admin rights?
  - How do I connect my computer to my Host?
  - Does the bastion open a port on my machine?
  - Can the bastion reach other Hosts or the platform?
  - What does anchoring a machine mean?
  - Is the bastion open source?
---

The bastion is the program you run on your own machine to join it to your Host's private network. It is one file. It dials out to your Host, and whatever your Host may do on that machine is checked on the machine itself before it happens. Its source is public, under the GNU GPL.

## How do I install it?

Open the Network app, go to Bastions and choose **Set up a bastion**. The card shows the install line for your system:

- macOS or Linux: `curl -fsSL https://privatae.ai/bastion/install.sh | sh`
- Windows (PowerShell): `irm https://privatae.ai/bastion/install.ps1 | iex`

Run as yourself, the installer puts the program in your own folders and needs no root or administrator rights. It checks the download against the published SHA-256 before putting it in place, and if the check fails nothing is installed. The bastion runs without root as well: its WireGuard tunnel runs inside the program, so there is no network device to install.

On a Linux server you can run the installer with `sudo` instead. It then also sets up a service that keeps the bastion running across restarts, and tells you to pair with `sudo -H` in front of the pair command.

## How do I pair a machine with my Host?

The same card gives you a pairing token. It works once and expires after 15 minutes. On the machine, run:

```
privatae-bastion pair <token>
privatae-bastion run
```

During pairing the bastion makes its own keys and sends your Host only their public halves. It gets back a certificate and your Host's address. The private keys never leave the machine.

A bastion normally serves one Host. A machine that several people's Hosts should use, such as a shared server, is set up as a networked bastion with an anchor, described below.

## Does it open a port on my machine?

It dials out. Your machine starts the tunnel, and the lanes your Host uses to work on the machine are connections the bastion opens back to your Host. Inside the tunnel your Host can open a connection to only a few things, each checked against the pairing when it arrives: the machine's model servers, a redaction service if one runs there, and servers your Host built there. Your git traffic to the platform's git server goes the other way, through the tunnel and your Host. The bastion's own console and control ports listen only on the machine itself, where nothing else on your network can reach them.

## What can the tunnel reach?

One address: your Host's. The tunnel is set up so its only allowed destination is your Host's single tunnel address, and a pairing that offers a wider range is refused. Even with valid keys it reaches nothing beyond your Host.

## What can my Host do on the machine?

Only what its pairing allows. The local console (`privatae-bastion ui`) lists, in plain words, the kinds of access a Host can hold on the machine, among them:

- read your files, in the folders you granted
- read the machine's state, and the windows and controls on its screen
- change which models sit on its disk
- run work on it
- send traffic out through its internet connection
- keep a server your Host built running on it
- run programs your Host wrote, after a dry run in a sandbox
- use the model servers already running there

What your Host may do arrives as signed grants that the bastion verifies itself, on every operation. It pins the key it verifies with the first time, and refuses a different key later. In the local console you can switch any of these off for a Host, and that switch is checked first: it can only take access away. On a machine anchored to a Host, the console also shows the few actions that Host can direct there: updating the bastion, stopping a program, and placing a copy of its database.

## What is an anchored machine?

In networked mode one Host is the machine's anchor. Every Host that pairs after that waits with nothing running for it, no tunnel and no lanes, until the anchor's owner approves it and chooses what it may do. Switching to networked mode and claiming the anchor need root, or an administrator on Windows. A bastion installed as networked runs as the machine's own system service, on Linux, macOS and Windows alike: it starts when the machine boots, before anyone signs in, and comes back if it stops. An existing install switched to networked mode keeps running the way it did; install it again as networked to make it a service. The anchor moves to another Host only with the current anchor's signed consent, or after 14 days of silence from it.

On an anchored machine, anything that changes the machine, such as removing a model or running a program your Host wrote, runs only on an approval you give for that one request, used once, apart from standing work you set up there and taking access away. You give it with the second factor enrolled on that machine or from your approval device, a machine of yours set up to sign approvals. A Host whose changes are refused five times in ten minutes has its changes held on that machine for a while. An update to a newer, signed bastion release needs no approval.

## Can my Host run commands as root on the machine?

Only on a machine it anchors, and only after you grant it there. On the machine, `sudo -H privatae-bastion root-exec grant --for 30d` asks for the code from the authenticator enrolled on it, then lets the anchor run commands as root one at a time, with no approval for each, until the time you chose, at most a year. Nothing your Host sends can create or extend that grant. Every command and its output are recorded on the machine, and `privatae-bastion root-exec revoke` takes the grant back, stopping a command already running.

## How do approvals on the machine work?

`privatae-bastion stepup-enroll` puts an authenticator on the machine. From then on, a new folder grant, a plan to reorganize a folder or a model download waits for your approval on that machine, with a code your Host never sees. A folder grant can also be approved from your approval device. Without an enrolled factor, model downloads are refused outright.

## Can I check what I am running?

The bastion is licensed GPL-3.0-or-later. The bastion updates itself only to a release whose manifest is signed with a release key built into it, and it checks the new binary's SHA-256 against that manifest before swapping it in.

What the bastion lets your Host reach is covered in [your private network](wiki:your-private-network), [connected folders](wiki:connected-folders), [local models](wiki:local-models) and [your machines](wiki:your-machines).
