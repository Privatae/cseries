---
title: Your private network
summary: Your Host and the machines you pair with it form one private WireGuard network. You run it from the Network app, where rules in three lanes decide who may reach in, what may run on your machines and what your Host may reach. A rule change that widens access waits for your authenticator code.
section: network
order: 20
questions:
  - What is my Host's private network?
  - How do my machines connect to a Host that has no public port?
  - How do I control what my Host can reach on the internet?
  - Can I make my Host's traffic leave from my own internet connection?
  - Is my network default-deny?
  - Can my Host change its own network rules?
---

Your Host and the machines you pair with it form one private network. Each machine joins by running a [bastion](wiki:the-bastion), and its tunnel reaches your Host and nothing else. You run the network from the Network app: rules in three lanes decide who may reach in, what may run on your machines and what your Host may reach, and a rule change that widens access waits for your authenticator code. Adding a machine does not: anyone holding a pairing token can pair one, and your Host can make a token itself.

## How do my machines reach a Host with no public port?

Your Host's side of every tunnel ends inside its own pod, in a small network component called the Hub, which holds your Host's WireGuard key. A machine at home sits behind a router and your Host has no port open to the internet, so neither can dial the other. They meet at a relay: a public service that forwards sealed WireGuard packets between the two ends. The relay holds no key and cannot read the tunnel, and each pairing gets its own relay address. We could read it, because our software makes the Hub's key and keeps it in the cluster we run; [what the platform can see](wiki:what-the-platform-can-see) says so in full.

When you revoke a machine, its credential is revoked at once, and the Hub drops its tunnel on its next check, which runs every 15 seconds.

## What is in the Network app?

Map (drawn logically, or as the physical path from your machine through the relay to your Host), Bastions, NAC, Agents, Bastion agents, Servers and Peers. The [Network app](wiki:network-app) page walks through each view; the rules below live under NAC.

## What are the three lanes?

- **Ingress**, who may reach in: the machines you have paired are admitted until a rule says otherwise.
- **Management**, what may be run on your machines: denied. Nothing runs without a grant, and a grant made here lasts at most 30 days. Running commands as root is the one exception to where the grant lives: you give it at the machine itself, as [the bastion](wiki:the-bastion) page describes.
- **Egress**, what may leave your Host: open, and yours to close. Egress rules apply to your coding engines and the Browser, and to some of your Host's own calls. Your apps, your Host's model requests and its reading of web pages leave through the node's exit and are not checked against them.

## Who approves a change?

You do. Your Host can draft rules, and you can add egress rules yourself. A change that widens access waits for a six-digit code from your authenticator: an allow rule, a management grant that allows something, removing a deny or a quarantine, or letting a suspended or quarantined machine back in. A change that tightens access needs no code, and your Host can make it itself. A code works once, and with no authenticator enrolled, nothing that widens access can be approved.

Once approved, a rule's terms are signed with your Host's own key, and what is enforced is the signed terms, so a rule edited afterwards is not trusted. On ingress, a rule that fails that check shuts your paired machines out until you remove it. On egress, where the default is open, a rule that fails the check is skipped. The authenticator's secret and the signing key are kept in your Host's own database, so they guard against a request from outside your Host, not against code running inside it or against us.

## What do egress rules do?

An egress rule names a destination (a site, or a domain and everything under it), says allow or deny, and names who it applies to: everything, your Host itself, or one coding harness. Rules are checked in the order the Network app lists them, the first match decides, and a request that matches nothing is allowed. To allow only a list of sites, add the allows and then a final rule that denies everything.

When your Host connects to an MCP server on the internet, it drafts an allow rule for that server and contacts nothing until you approve it. That approval is given only in the Network app, where the server and the key it uses are shown beside the code box, and removing the rule disconnects the server.

The rules are checked by a gate inside your Host's pod. It records that it refused a request, never where the request was going.

## Can traffic leave from my own connection?

Yes. An egress rule can also name the way out: the usual exit (for your coding engines and the Browser, the platform's; for your Host's own calls, a connected machine of yours if there is one, otherwise the platform's), the platform's exit, or one of your machines by name. Traffic pinned to one of your machines leaves through that machine's internet connection, under its address, or not at all: if the machine is offline, the request fails and says so rather than going out another way. That is the answer for a site that refuses datacentre addresses, such as a bank. A rule that pins traffic to a machine can only be approved in the Network app, on the card that names the machine. Your Host's connections to the platform's own git server always take the platform's path.

Traffic that does not go out through one of your machines leaves through the exit of the node your Host runs on. That exit keeps no log of destinations and strips the headers that would say where a request came from. It carries web traffic only, and over IPv4 its address is shared with the other Hosts on that node.

## Is my network default-deny?

Parts of it. Management is: no grant, nothing runs. So is traffic pinned to one of your machines, which never falls back to another route. Ingress and egress start open. For strict ingress, have your Host draft a catch-all quarantine rule below your other rules; for strict egress on the traffic egress rules cover, add a final deny-all rule below your allows. On each machine the bastion refuses anything it cannot verify.

## What can I do about one machine?

Suspend it to pause management and build work there while keeping its grants. Quarantine it to cut its tunnel completely; releasing it brings it back without pairing again. Both are free; resuming and releasing ask for your code. Revoking removes the machine.
