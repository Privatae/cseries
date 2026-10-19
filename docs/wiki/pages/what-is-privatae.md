---
title: What is Privatae?
summary: Privatae is an AI DMZ. You get a Host, a machine of your own in the cloud where your AI lives and remembers you, on a private network you own, working on models and keys you choose, behind limits that are enforced in code rather than asked for in a prompt.
section: start
order: 10
questions:
  - What is Privatae?
  - What does "an AI DMZ" mean?
  - How is Privatae different from a chatbot?
  - Do my files have to be uploaded to use it?
  - Which AI models does Privatae use?
  - What can I build with Privatae?
---

Privatae is an AI DMZ: a network you own, with a machine on it that is yours. The machine is called a Host. Your AI lives there, remembers you and does its work. Your own computers join the same private network, and the models you choose reach your work through the Host, on your own keys, one request at a time.

## What is a Host?

A Host is one machine in the cloud that belongs to you: its own database, its own disks, its own certificate, and its own address on your private network. It has no public address and no open port. You reach it from any browser, through the front door you sign in at, with nothing to install. [What a Host is](wiki:what-a-host-is) opens it up.

## How is it different from a chatbot?

A chatbot is a conversation. A Host is a machine you talk to, and it acts. It reads what you asked and picks a path: it answers, runs one known operation with no model involved, hands code to a coding engine in a sandbox of its own, or, for untrusted work outside the platform such as the open web, sends out a narrow agent that starts with no permissions at all. [How a Host handles a request](wiki:how-a-host-handles-a-request) follows each path, and [agents](wiki:agents) explains why an agent here is a worker the Host sends, not something you chat with.

It also remembers. Every conversation is written down in its own database, and what is worth keeping is drawn out, corrected and replaced over time, never quietly rewritten. [How a Host remembers](wiki:memory) covers it.

## What does "an AI DMZ" mean?

A DMZ is how businesses and governments isolate what they cannot afford to lose: a separate zone, with rules enforced by the network rather than by anyone's good behaviour. Most of what an AI system does is network traffic, so Privatae is built that way. Your Host has no open port. Each of your machines reaches it through a tunnel that goes nowhere else. An agent it dispatches holds only what it was granted, and the grant is checked in code at every call. Anything that widens your network waits for a code from your authenticator. [What a Host cannot do](wiki:what-a-host-cannot-do) lists the walls.

We run the cluster your Host lives in, and we can reach into it. [What the platform can see](wiki:what-the-platform-can-see) is plain about what that means.

## What is the network?

Each computer you add, whether a laptop, a machine at home or a server, runs a small program called the [bastion](wiki:the-bastion). It dials out to your Host and holds a private tunnel open, and that tunnel reaches your Host and nothing else. Nothing dials in to your machine. Once the tunnel is up, your Host can use what the machine offers and you allow: a folder, a model running on it, its internet connection. [Your private network](wiki:your-private-network) covers the whole of it.

## Do my files have to be uploaded?

No. A folder on one of your machines joins your network by a grant you approve. Your Host gets a map of what is there and reads what it needs, one file at a time, only as far as the grant allows. Nothing is uploaded or synced, and there is no way to delete through it. [Connected folders](wiki:connected-folders) explains how. Your Host's own memory is a different thing: it lives in the Host's database, in the cloud.

## Whose models does it use?

Yours. For each role your Host has you choose a provider and a model, on your own key or sign-in. Privatae does not make the models and never bills you for tokens. [Models and keys](wiki:models-and-keys) has the details.

## What can I build?

You build by talking to your Host in the [Workbench](wiki:workbench). Coding engines you connect write the code, and each change comes back to you as a Code Approval that is merged only when you approve it, unless you have switched on Auto-Approve for that project. A finished project can be published as a site on the public web or as an app. See [publishing sites](wiki:publishing-sites) and [building apps](wiki:building-apps).

New to the words? The [glossary](wiki:glossary) defines each one.
