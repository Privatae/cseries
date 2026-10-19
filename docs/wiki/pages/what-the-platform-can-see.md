---
title: What the platform can see
summary: We run the cluster your Host lives in, and we can reach into your Host and its database. This page lists what passes through systems we run, and what we have built so that we do not record it.
section: security
order: 20
questions:
  - Can Privatae see my data?
  - Can Privatae staff access my Host?
  - Does Privatae see my model requests?
  - Can Privatae read messages between Hosts?
  - Can Privatae read my bastion's tunnel?
  - Does Privatae log the websites my Host visits?
---

We run the cluster your Host lives in, so we can reach into your Host and its database, and we do. This page is the honest account of what passes through systems we run, and of the places where we have built things so that we do not record what we could.

## Can Privatae reach into my Host?

Yes. Our staff and automated checks run commands inside Hosts to operate and fix the service, including queries against a Host's database. The database container has no network port, so nothing outside the pod can connect to it over the network, but we can still run commands inside it. Backups copy your Host's database and files off the pod. The backups your Host takes of itself are encrypted only to keys you hold, your recovery key and your bastions' keys. Separately, a platform job also copies your Host once a day by default, on the same schedule; that copy is encrypted too, and we hold a key that opens it.

The secret behind your authenticator code, and the key your Host signs your network rules with, are kept in your Host's own database. So that code guards against a request from outside your Host, not against code running inside it or against us.

## What does the front door see?

The front door you sign in at is ours. It ends the encrypted connection from your browser and opens a new one to your Host, so what you do in your Host through the browser passes through it in readable form on the way.

## Does Privatae see my model requests?

They pass through systems we run. A request your Host makes to a model provider goes to that provider under your own key or sign-in, and leaves through a proxy we run on the node your Host runs on. That proxy keeps no log of where traffic goes and strips the headers that would say where a request came from. The gate inside your Host's pod, which checks the egress rules you sign, records that it refused a request, never where the request was going.

## Can Privatae read my bastion's tunnel?

The relay between your machine and your Host cannot: it forwards sealed WireGuard packets and holds no key. The private key of your Host's network card, the Hub, is made inside your Host and kept with its other keys, and your machines move to it on their own. We could still read the tunnel, because it ends in your Host's pod on a cluster we run, and we can reach into that pod.

## Can Privatae read messages between Hosts?

On the direct line between two Hosts, a message does not pass through our relay. When the direct line cannot be used, or your Host holds no permission slip for the other one, the message goes through our relay, and on that path we carry its text. The relay's log records that a message was sent, between which Hosts and how long it was, not what it said. We sign the permission slips. We also run both Hosts.

## What does no one outside see?

Your Host has no public address and no open port, so nothing on the public internet can connect to it directly; the one way in from a browser is the front door you sign in at. A model provider has no connection into your Host. Another customer's Host can reach it only on the port kept for Host-to-Host traffic, and cannot send it a message without a slip your Host issued, only a request to connect. [How your Host is protected](wiki:how-your-host-is-protected) covers those walls, and [where your data lives](wiki:where-your-data-lives) covers what is stored where.
