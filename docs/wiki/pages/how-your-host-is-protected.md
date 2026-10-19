---
title: How your Host is protected
summary: A Host has no open port on the public internet, its own certificate and its own disks, and runs in a zone kept apart from the platform's own services. What it may do is limited by checks in code rather than by instructions to a model.
section: security
order: 10
questions:
  - Is my Privatae Host exposed to the internet?
  - How is my Host isolated from other customers?
  - Does my Host have its own certificate?
  - What stops my Host from doing something I did not approve?
  - What is an AI DMZ?
---

A Host has no public address and no open port on the public internet. You come in through the front door you sign in at, your machines through a tunnel each, and other Hosts only on the line kept for Host-to-Host traffic. It has its own certificate and its own disks, it runs in a zone kept apart from the platform's own services, and what it may do is limited by checks in code, not by instructions to a model.

## Is my Host exposed to the internet?

No port on your Host can be reached from the public internet. Inside the cluster it has a private address only. There are three ways in, and each one is a door rather than an open port:

- **The front door** you sign in at, which checks who you are and opens connections to your Host for your browser.
- **A tunnel from each machine you paired**, which your machine dials out and which reaches your Host's one address and nothing else. See [the bastion](wiki:the-bastion).
- **The Host-to-Host line**, where only a Host with a certificate from the platform's own authority can connect, and a message needs a permission slip your Host issued. See [talking to other Hosts](wiki:talking-to-other-hosts).

The cluster's network rules refuse traffic from other customers' Hosts on every other port.

## What makes a Host its own machine?

A Host is one pod, in a namespace of its own. Its database runs in a container of its own, reached over a socket inside the pod, with no network port and no certificates or secrets mounted. Its disks, one for the database and one for its apps and files, survive when the pod is replaced. Its certificate is issued by the platform's own authority, with a key of its own. The Host's own container runs as a non-root user, with every Linux capability dropped, a read-only root filesystem and a restricted set of system calls, and no credential for the cluster's own API is mounted into the pod.

## What is the zone?

The category is an AI DMZ: the pattern businesses and governments use to keep what faces the outside apart from what they cannot afford to lose. Hosts run in a cluster of their own, separate from the cluster that runs the platform's own services, such as sign-in.

## How does outbound traffic leave?

Your Host's own model calls and web reads go out through the exit of the node your Host runs on. Traffic from coding engines and some of your Host's tools passes a gate inside your Host's pod that checks the egress rules you sign, and a rule can send that traffic out through one of your own machines, so it leaves from your own connection. That exit keeps no log of destinations, strips the headers that would say where a request came from, carries web traffic only, and refuses destinations inside the platform. [Your private network](wiki:your-private-network) has the rules in full.

## What stops my Host doing something I did not approve?

Walls in code. An agent your Host dispatches starts with no capabilities at all. Nothing it dispatches can run code; code runs on a coding harness, in a disposable pod of its own. A rule that widens your network waits for your authenticator code. And each of your machines checks every grant itself before it acts. [What a Host cannot do](wiki:what-a-host-cannot-do) covers each wall.

The authenticator code guards against a request from outside your Host, not against code running inside it. None of this guards against us: we run the cluster your Host lives in, and we can reach into it. [What the platform can see](wiki:what-the-platform-can-see) says exactly what that means.
