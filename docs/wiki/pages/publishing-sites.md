---
title: Publishing a website
summary: A finished project goes live as a website in one click, straight from its source, with no upload step. The Sites app puts it on your own domain, decides who may visit, from anyone to only people who have paid, and connects your own Stripe account so payments go to you.
section: building
order: 40
questions:
  - How do I publish a website from my Host?
  - Can I use my own domain for a site?
  - Can I make a site private or members-only?
  - Will search engines list my site?
  - Can I charge people to see my site?
  - Where does the money from my site go?
  - Do I upload files to publish a site?
  - What does hosting a site cost?
---

You publish a website straight from a project in the Workbench, or from one of your repositories, in one click. There is no upload step and no zip to prepare: the platform finds the site's files in the project, packages them and signs the bundle. The Sites app then controls everything about the site once it exists: its address, your own domain, who may visit, payments, and a database.

## How do I publish a site?

On a finished Workbench project, choose Publish as site, or ask your Host to publish it. A site can come from a Workbench project, an artifact, or one of your Code Repositories. The site is served from the platform's public web servers, not from your Host itself.

The site is published from the built folder (dist, build, out or public) when it has a page, otherwise from the project's own index.html. A project built with a bundler such as Vite must be built first; its unbuilt page is refused with a message saying so. A static site never includes hidden files such as .env.

A new site is public from the moment it goes live. To change a live site, publish it again; that keeps whatever visibility you chose for it.

## Can I use my own domain?

Yes. In the Domains tab of the Sites app, add the domain and pick the site. The app shows you a verification record and a routing record to add at your domain registrar, then you press Verify. You keep your own nameservers; Privatae does not take over your DNS.

## Who can visit my site?

You choose per site, in the Sites app:

- **Public**: anyone.
- **Private**: only you.
- **Members**: only the people on the site's access list, either by invitation or by open sign-up. You add, import, suspend or remove people in the Access tab.
- **Paywall**: people get in by paying through your own Stripe checkout.

Your Host can also put a site behind a password or limit it to a list of network addresses.

## Will search engines list my site?

Not unless you choose to. A new site asks search engines to leave it out, though anyone with the link can still open it. On a Public site you can switch Search engines to Listed in the Sites app, or ask your Host to do it.

## Can I take payments?

Yes, into your own account. In the Payments tab you link your own Stripe account through Stripe's own onboarding. A Paywall site sends a visitor who has not paid to your Stripe checkout, and once Stripe reports the payment, that person gets in. The money is paid into your Stripe account, not to Privatae.

## Can a site have a database?

Yes. The Database tab gives a site its own PostgreSQL database, with backup and restore. The Sites app never shows its connection string; it is placed in your [Keys app](wiki:keys-app), where revealing it needs your second factor.

## Can I publish a site from someone else's app?

Only if its author allowed it. Every app carries its author's terms, and whether others may publish a website from it defaults to no. A site built from an app you installed from someone else is refused unless its author granted that right. Your own apps are always yours to publish.

## What does a site cost?

See [pricing](https://privatae.ai/pricing).
