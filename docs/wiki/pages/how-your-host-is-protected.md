---
title: How your Host is protected
summary: A Host runs sandboxed in a virtual machine on your machine, publishes its ports on your own machine only, runs with no extra privileges, and is limited by checks in code rather than by instructions to a model. The virtual machine mounts none of your folders; the bastion, a separate program the installer sets up, is how the Host reaches your machine.
section: security
order: 10
questions:
  - Is my Host exposed to the internet?
  - How is a Host isolated from the rest of my machine?
  - What stops my Host from doing something I did not approve?
  - How does the bastion connect to the Host?
  - Who can sign in to my Host?
---

A Host runs in a Linux virtual machine on your machine and publishes its ports on your machine only. Its containers run with no extra privileges, and what it may do is limited by checks in code, not by instructions to a model. The bastion, a separate program the installer sets up on your machine, is how the Host reaches anything there.

## Is my Host exposed to the internet?

Not by default. The dashboard and the two bastion ports are published on 127.0.0.1 inside the virtual machine and forwarded only to 127.0.0.1 on your machine, so only programs on your own machine can connect to them. The installer's virtual machine mounts none of your folders. The database container has no network port at all: it listens on a Unix socket shared with the Host. To reach the dashboard from other machines you put a reverse proxy that ends TLS in front of it, as the README describes; that is your choice and your proxy's configuration.

## What makes a Host contained?

Both containers run as an ordinary user with every Linux capability dropped and a read-only root filesystem, and they cannot gain privileges. The Host's container also runs under a restricted set of system calls. What the Host keeps goes to named volumes, its /tmp is in memory, and the database's data directory is never mounted into the Host.

## Who can sign in?

The owner. The first time, you enter the setup code the Host prints at start and choose an email and a password of at least 12 characters. Until that account exists the code changes every time the Host restarts. Things that matter more than a click, such as revealing a saved key or widening the Host's reach, wait for a code from your authenticator. The code guards against a request from outside the Host, not against code already running inside it.

## How does the bastion connect?

The bastion pairs with a one-time token and then connects to the Host over mutual TLS under a certificate authority the Host makes for itself. The Host accepts only certificates it issued at pairing and has not revoked, and drops the open connections of a bastion it revokes. [The bastion](wiki:the-bastion) covers what it checks on every operation.

## What stops my Host doing something I did not approve?

Walls in code. An agent your Host dispatches starts with no capabilities at all. Nothing it dispatches can run code; code runs on a coding engine, in a container of its own. A rule that widens what your Host may reach waits for your approval with your authenticator code. [What a Host cannot do](wiki:what-a-host-cannot-do) covers each wall.

## What is not covered?

A Host is software running on your machine, so whoever controls that machine controls the Host. The walls above limit what the model and its agents can do; they do not protect a Host from a person or a program with administrator rights on the machine it runs on.
