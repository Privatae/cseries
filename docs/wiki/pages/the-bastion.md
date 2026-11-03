---
title: The bastion
summary: The bastion is a small open-source program that runs on your own machine, outside the Host's virtual machine, and decides what the Host may touch there. It connects to your Host, and your Host can do only what you allowed, checked by the bastion itself.
section: network
order: 10
questions:
  - What is the bastion?
  - How do I install the bastion?
  - Does the bastion need root or admin rights?
  - How do I pair the bastion with my Host?
  - Does the bastion open a port on my machine?
  - How do I add a server on the internet to a Host at home?
  - What does anchoring a machine mean?
  - Is the bastion open source?
---

The bastion is the program that runs on your own machine, outside the Host's virtual machine, and decides what the Host may touch there. It is one file. Whatever your Host may do on the machine is checked by the bastion itself before it happens. Its source is public, under the GNU GPL (GPL-3.0-or-later), and it is a separate program from the Host, which is AGPL-3.0-only.

## How do I install it?

`npx cseries` installs it for you. It sets the bastion up as the machine's system service with one administrator prompt, then pairs it with the Host. Because it is a system service it starts when the machine boots and comes back if it stops. Installing it as a system service needs administrator rights once; the Host itself does not need them.

To install the bastion on its own, open the Network app, go to Bastions and choose **Set up a bastion**. The card shows the install line for your system:

- macOS or Linux: `curl -fsSL https://privatae.ai/bastion/install.sh | sh`
- Windows (PowerShell): `irm https://privatae.ai/bastion/install.ps1 | iex`

## How do I pair it with my Host?

The installer pairs the bastion for you. To pair by hand, the Bastions card in the Network app gives you a pairing token. It works once and expires after 15 minutes, and the card shows the `privatae-bastion pair` command to run on the machine.

When the bastion pairs with your Host over the network, as it does with a C-Series Cloud machine or with a Host you run as the control plane for your other machines, **Set up a bastion** shows one line for each kind of machine instead. Paste it on the machine you want as a bastion. It installs the bastion, or uses the one already there, pairs it with your Host and starts it, and the bastion then starts again when the machine does. The line carries a pairing code: the token, the address to pair with, and the fingerprint of your Host's certificate authority. The bastion trusts that authority and no other, so there is no certificate file to save. If the machine's bastion already serves another Host, it asks on that machine before adding this one. The steps the line replaces are still on the card, under **Do it step by step**.

On a C-Series Cloud machine, once you confirm with your passkey, your Host's pairing port opens for 15 minutes, and only to the address your browser came from. It does not open when you reach your Host through remote access. So run the line from the same network, with any VPN on or off just as it was when you opened the card. To pair a machine on another network, choose **Pairing a machine on another network?** beside **Set up a bastion** and give that machine's public address: the port then opens to it instead. The same choice is on the card, for when you notice only once it is open. There it ends the card's line before giving a new one, and it uses one more of the hour's pairing openings.

## How do I add a server my Host can't be reached from?

A Host on your own computer sits behind your home router, so a server on the internet can't connect to it, and nothing on your home network needs opening for it. Your Host connects to the server instead. On the server, run `privatae-bastion pair --open` as an administrator. It shows a code that works once, for 15 minutes, and the port it listens on, 8812 unless you chose another. Then in the Network app, under Bastions, choose **Add a server** and give it the server's address and the code. You can also just tell your Host the address and the code in chat: it asks you on a card over the chat, and only your **Connect** pairs it. Either way your Host shows you what the server said: that it is connected, that another Host in charge of the server has to let it in first, or why it refused.

The code never crosses the network. Your Host proves it holds the code first, and checks that the server holds it too before it signs anything for the server, so a wrong address or a stranger at it gets nothing. From then on your Host keeps a few connections open to the server, and opens them again when either side restarts. The server needs TCP 8812 open to the internet. Where the server's firewall is ufw, firewalld, Windows Firewall or the macOS firewall, the bastion opens that port there while it is in use and closes it after; with any other firewall, and in a cloud provider's firewall, you allow it yourself, and the bastion's status says so.

During pairing the bastion makes its own keys and sends your Host only their public halves. It gets back a certificate signed by a certificate authority the Host made for itself, and trusts that authority alone. The private keys never leave the machine.

## Does it open a port on my machine?

It connects out to your Host. The Host publishes a pairing port and a lane port on your machine's own loopback address, or on its public address for a Host on a server of its own, such as a C-Series Cloud machine, and the bastion dials them. A Host you run as the control plane for your other machines publishes them on the network address you set for it, so those machines can reach them; the C-Series source repository's guide describes that setup. The bastion's own console and control ports listen only on the machine itself. The one exception is a server your Host connects to, as above: while that is in use, its bastion listens on TCP 8812 and keeps only connections from the Hosts paired with it there.

## What can the connection reach?

Only what the Host publishes for the bastion: the pairing door and the lane. The lane accepts only a client certificate this Host issued at pairing and has not revoked, and a request on it can reach only the bastion's own paths on the Host.

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

The bastion reports which models its machine's model servers serve, and your Host reaches those servers through the bastion's connection, by asking for the server, never by an address; the bastion reaches only a server on its own computer. [Local models](wiki:local-models) has the details.

What your Host may do arrives as signed grants that the bastion verifies itself, on every operation. It pins the key it verifies with the first time, and refuses a different key later. In the local console you can switch any of these off for a Host, and that switch is checked first: it can only take access away. On a machine anchored to a Host, the console also shows the few actions that Host can direct there: updating the bastion, stopping a program, and placing a copy of its database.

## What is an anchored machine?

In networked mode one Host is the machine's anchor. Every Host that pairs after that waits with no lane open for it until the anchor's owner approves it and chooses what it may do. The anchor Host asks you itself, with a card over your chat: "Studio wants to use your machine. Let it in?", what it may do ticked in plain words, **Let it in** with a tap of your passkey or a code from your authenticator, or **Never**. If you leave it, it asks again once a day. Switching to networked mode and claiming the anchor need root, or an administrator on Windows. A bastion installed as networked runs as the machine's own system service, on Linux, macOS and Windows alike: it starts when the machine boots, before anyone signs in, and comes back if it stops. The anchor moves to another Host only with the current anchor's signed consent, or after 14 days of silence from it.

On an anchored machine, a change such as removing a model runs only on your approval, for that one request or for repeats of one action you allowed (up to 30 days, never as root), apart from standing work you set up there and taking access away. You give it with the second factor enrolled on that machine or from your approval device, a machine of yours set up to sign approvals. A Host whose changes are refused five times in ten minutes has its changes held on that machine for a while. An update to a newer, signed bastion release needs no approval.

## Can my Host run commands as root on the machine?

Only on a machine it anchors, or on a machine whose bastion runs as root with no anchor (the bastion's local mode), and only after you grant it there. On the machine, `sudo -H privatae-bastion root-exec grant --for 30d` asks for the code from the authenticator enrolled on it, then lets that Host run commands as root one at a time, with no approval for each, until the time you chose, at most 30 days. Nothing your Host sends can create or extend that grant. Every command and its output are recorded on the machine, and `privatae-bastion root-exec revoke` takes the grant back, stopping a command already running.

## How do approvals on the machine work?

`privatae-bastion stepup-enroll` puts an authenticator on the machine. From then on, a new folder grant or a plan to reorganize a folder waits for your approval, given on that machine with a code your Host never sees, or from your approval device. A model download always needs such an approval: with no factor enrolled on the machine and no approval device, it is refused outright.

## Can I check what I am running?

The bastion is licensed GPL-3.0-or-later. The bastion updates itself only to a release whose manifest is signed with a release key built into it, and it checks the new binary's SHA-256 against that manifest before swapping it in.

What the bastion lets your Host reach is covered in [network rules](wiki:your-private-network), [connected folders](wiki:connected-folders), [local models](wiki:local-models) and [your machines](wiki:your-machines).
