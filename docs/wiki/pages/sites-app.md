---
title: Sites
summary: Sites is the control panel for the public websites you publish from your Host, covering who can see each one, your own domains, payments into your own Stripe account, and a database per site. It comes with every Host.
section: apps
order: 10
questions:
  - What is the Sites app?
  - How do I upload my website?
  - Can I put my site on my own domain?
  - How do I make a site private or password protected?
  - Can I charge people to see my site?
  - Can my site have a database?
  - Will search engines find my site?
app: sites
---

Sites is the control panel for the public websites you run from your Host: everything about a site once it exists. It comes with every Host. The sites themselves are served from a separate public web zone, not from your Host.

## How do I upload my site?

You don't. There is no upload in Sites, and the app says so. A site is published from a project you built in the [Workbench](wiki:workbench-app), with "Publish as site", or by asking your Host to publish it. [Publishing sites](wiki:publishing-sites) explains the whole path.

A newly published site is public as soon as it is live. Publishing a new version of an existing site keeps whatever visibility you already chose.

## What can I do in Sites?

There are seven tabs:

- **My Sites**: every site with its address, status and traffic, where you can restart or suspend one and set its visibility.
- **Catalog**: ready-made apps you can deploy as a site in one click.
- **Domains**: put a site on a domain you own. You add the records at your own registrar and keep your own nameservers, and you can edit the domain's DNS records from here.
- **Access**: the people allowed into your members-only sites. Add them one at a time, import a list, suspend or remove someone.
- **Payments**: link your own Stripe account through Stripe's own onboarding. Payments go straight into your Stripe account, never through us.
- **Database**: give a site its own PostgreSQL database, back it up and restore it.
- **Data**: back up and restore the storage of apps deployed from the Catalog.

## Who can see a site?

In the app you choose between Public, Private (only you), Members (only the people in Access, with open sign-up or by invitation) and Paywall (entry bought through your Stripe checkout). A site can also be protected by a password or limited to a list of addresses; Sites shows those settings but does not change them.

## Will search engines find my site?

Not unless you say so. A published site starts hidden from search engines: anyone with the link can open it, but search engines are asked not to list it. On a Public site, switch **Search engines** from **Hidden** to **Listed** in My Sites when you want it found. Changing who can see a site keeps that choice.

## Where does the database password go?

Not on the page. Sites never shows a database's connection string. You can save it into the [Keys app](wiki:keys-app), where seeing it takes your second factor, or copy it straight to your clipboard for another tool.
