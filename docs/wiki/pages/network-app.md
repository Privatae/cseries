---
title: The Network app
summary: Network is the control panel for the machines paired with your Host, showing who may reach in, what your Host may reach out to, and what it may do on your machines. Nothing that widens access happens without your code. It comes with every Host.
section: apps
order: 10
questions:
  - How do I pair a bastion with my Host?
  - How do I block or allow what my Host can reach on the internet?
  - Can my Host change my network rules without asking me?
  - How do I cut off a machine I no longer trust?
  - How do I update the bastion on my machines?
  - How do I sign in to an MCP server my Host connects to?
  - Can I message my Host from Telegram, Slack or Discord?
app: network
---

Network is the control panel for the machines paired with your Host: who may reach in, what your Host may reach out to, and what it may do on your machines. It comes with every Host.

## What are the views?

- **Map**: how everything connects. **Logical** puts your Host in the middle with everything around it. **Physical** draws the path as a tunnel through a relay, which a Host with no platform connection does not have, so use Logical.
- **Bastions**: every machine running a [bastion](wiki:the-bastion). **Set up a bastion** gives you a one-time pairing token that lasts 15 minutes. Each machine shows its version; **Update all** asks every out-of-date machine to update in one step, and automatic updates are on unless you turn them off. Each machine also lists the other Hosts it says it serves: tap a Host's name for its card, **Message** one you are connected with (it opens Circles), or **Request connection** to one you are not.
- **NAC** (network access control): your network access rules.
- **Agents**: AI agents and automations your bastions see running on your machines, split into the ones you asked for and the ones you did not. You can acknowledge one or stop it.
- **Bastion agents**: the small indexing program your bastions run for each folder you connected, and whether it is working.
- **Servers**: servers your Hosts built on your machines, and which Host may reach each.
- **Peers**: the other Hosts you talk to, who may reach you, and your rules for single senders. A rule lets one sender, named by handle or address, into your inbox or keeps it out, whatever the setting says. It does not stop messages from a Host you gave a permission slip; block that Host in Circles instead.

## Who approves network changes?

You. Rules sit in three lanes, each with a default. **Ingress**, who may reach in, admits the machines you have paired until a rule says otherwise. **Management**, what may be run on your machines, is denied: nothing runs without a grant. **Egress**, what your Host may reach, is open and yours to close. Your Host can draft rules, and you can add egress rules yourself; either way, a rule that widens access waits for your authenticator code and one that tightens needs none. A rule that sends traffic out through one of your own machines can only be approved here, on the card that names that machine. The same holds for a rule your Host drafts to connect an MCP server: its card names the server, its address and any key from [Keys](wiki:keys-app) it uses, approving the rule connects the server, and removing the rule disconnects it. A server that has you sign in instead of handing over a key shows **Sign in** on that card once it is connected. You sign in on the service's own page, in a new tab, and approve what it asks for; the token it gives back is kept in your Host's vault, where your Host never sees it, and it renews by itself while the service allows. When the service wants more access for something your Host asked to do, the button reads **Sign in again**, and **Sign out** ends the sign-in. See [Approvals](wiki:approvals).

## Can I message my Host from Telegram, Slack or Discord?

Yes, from one account, through that service's app from the [App Store](wiki:app-store-app). The connector runs in a container of its own on your machine. When your Host starts the pairing, the Ingress lane in NAC shows a one-time code with a countdown: send it to your bot in a direct message from the account you want to let in. That account then waits for you here. **Let this account in** asks for your authenticator code, and **Don't pair** needs no code. Messages from it start turns in your chat with your full chat permissions, but it cannot approve anything. **Revoke** needs no code and refuses its next message.

## What can I do about a machine?

**Suspend** pauses management and build work on it while keeping its grants. **Quarantine** cuts its connection completely, and releasing it brings it back without pairing again. **Revoke** removes it. Resuming and releasing both ask for your code. On a machine you have anchored, a new Host's pairing waits here until you choose what it may do and approve with your code, or deny it.
