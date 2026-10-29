---
title: Network rules for your Host
summary: The machines you pair with your Host are governed by rules in three lanes that decide who may reach in, what may run on your machines and what your Host may reach. You run them from the Network app, and a rule change that widens access waits for your authenticator code.
section: network
order: 20
questions:
  - How do I control what my Host can reach on the internet?
  - Can I make my Host's traffic leave from my own internet connection?
  - Is my network default-deny?
  - Can my Host change its own network rules?
  - How do I cut off a machine I no longer trust?
---

Each machine joins by running a [bastion](wiki:the-bastion). You run the rules from the Network app: three lanes decide who may reach in, what may run on your machines and what your Host may reach, and a rule change that widens access waits for a code from your authenticator. Adding a machine does not: anyone holding a pairing token can pair one, and your Host can make a token itself.

## How does the bastion reach the Host?

With a Host installed by `npx cseries`, the bastion runs on the same machine as the Host. A Host on its own has no tunnel and no relay. It has two ports: one where a bastion pairs and one where it connects in over mutual TLS, under a certificate authority the Host made for itself. Pairing uses a one-time token that expires after 15 minutes.

- **On your own computer,** both ports are on the machine's own loopback address, so nothing on another machine can reach them.
- **On a C-Series Cloud machine,** both are on its public address. Anyone can reach the port a bastion connects in on, but it lets in only a machine holding a certificate your Host gave it when it paired, and none your Host has revoked. The pairing port stays shut. Each time you set up a bastion and confirm with your passkey, it opens for 15 minutes to one address: the one your browser came from, or the one you give for a machine on another network.

When you revoke a machine, its credential is revoked at once and its connection is refused from then on.

## What is in the Network app?

Map, Bastions, NAC, Agents, Bastion agents, Servers and Peers. The [Network app](wiki:network-app) page walks through each view; the rules below live under NAC.

## What are the three lanes?

- **Ingress**, who may reach in: the machines you have paired are admitted until a rule says otherwise.
- **Management**, what may be run on your machines: denied. Nothing runs without a grant, and a grant made here lasts at most 30 days. Running commands as root is the one exception to where the grant lives: you give it at the machine itself, as [the bastion](wiki:the-bastion) page describes.
- **Egress**, what may leave your Host: open, and yours to close. Egress rules apply to your coding engines, the Browser and your apps, and to some of your Host's own calls. Your Host's model requests and its reading of web pages are not checked against them.

## Who approves a change?

You do. Your Host can draft rules, and you can add egress rules yourself. A change that widens access waits for a six-digit code from your authenticator: an allow rule, a management grant that allows something, removing a deny or a quarantine, or letting a suspended or quarantined machine back in. A change that tightens access needs no code, and your Host can make it itself. A code works once, and with no authenticator enrolled, nothing that widens access can be approved.

Once approved, a rule's terms are signed with your Host's own key, and a rule is enforced only from its signed terms, in the order of its signed priority, so a rule edited afterwards is not trusted. On ingress, your Host refuses every paired machine while any approved rule fails that check: its signed terms were changed, it carries no signature, its conditions are not in a form your Host can read, or your Host cannot read its signing key to check it. A condition written as one value where a list of values belongs, such as the class "server", is read as that value. A changed, unsigned or unreadable rule keeps your machines out until you remove it, and removing it asks for your code. A key that cannot be read is tried again each time your Host reloads its rules, and your machines are let back in on their own once it reads. Each time your Host starts refusing them, it leaves one notice on the Support card in Circles saying why. Your Host also refuses a paired machine it cannot decide for: when checking its rules for that machine fails with an error, or when the rules did not load as your Host started. In the second case the Ingress lane in the Network app says so, your Host leaves a notice on the Support card in Circles, and it tries again at its next start. On egress, where the default is open, a rule that fails the check is skipped. The authenticator's secret and the signing key are kept in your Host's own database, so they guard against a request from outside your Host, not against code running inside it.

## What do egress rules do?

An egress rule names a destination (a site, or a domain and everything under it), says allow or deny, and names who it applies to: everything, your Host itself, or one coding harness. Rules are checked in the order the Network app lists them, the first match decides, and a request that matches nothing is allowed. To allow only a list of sites, add the allows and then a final rule that denies everything.

When your Host connects to an MCP server on the internet, it drafts an allow rule for that server and contacts nothing until you approve it. That approval is given only in the Network app, where the server and the key it uses are shown beside the code box, and removing the rule disconnects the server.

The rules are checked by a gate inside your Host. It records that it refused a request, never where the request was going.

## Can traffic leave from my own connection?

Yes. An egress rule can also name the way out: the usual exit, or one of your machines by name. Traffic pinned to one of your machines leaves through that machine's internet connection, under its address, or not at all: if the machine is offline, the request fails and says so rather than going out another way. A rule that pins traffic to a machine can only be approved in the Network app, on the card that names the machine.

## Is my network default-deny?

Parts of it. Management is: no grant, nothing runs. So is traffic pinned to one of your machines, which never falls back to another route. Ingress and egress start open. For strict ingress, have your Host draft a catch-all quarantine rule below your other rules; for strict egress on the traffic egress rules cover, add a final deny-all rule below your allows. On each machine the bastion refuses anything it cannot verify.

## What can I do about one machine?

Suspend it to pause management and build work there while keeping its grants. Quarantine it to cut its connection completely; releasing it brings it back without pairing again. Resuming and releasing ask for your code. Revoking removes the machine.
