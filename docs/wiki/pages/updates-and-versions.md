---
title: Updates and versions
summary: A Host moves to a new version when its software is swapped for the published release and its pod is rebuilt around the same disks, so your conversations, memory, apps, projects and files stay. You can apply an update yourself in Settings, and we also roll new versions out to Hosts.
section: host
order: 80
questions:
  - How do I update my Host?
  - Where can I see which version my Host is running?
  - Will an update delete my conversations or files?
  - Can I pick a version or roll back?
  - Do apps update separately from the Host?
  - Does the bastion on my computer update itself?
---

A Host moves to a new version when its software is swapped for the published release. The pod is rebuilt from the new version and the same two disks are attached again, so your conversations, memory, apps, projects and files carry straight over. New Hosts start on the published version.

## How does my Host get a new version?

Two ways, and both end in the same change.

- **You apply it.** In the Settings app, under Software Updates, you can check whether a newer version has been published and apply it. That updates your own Host and nobody else's.
- **We roll it out.** We also move Hosts to a new version ourselves, one Host at a time, checking that each one comes back healthy before moving on to the next. A roll stops at a Host with a coding build in progress, when it can tell, rather than cut the build off, unless we deliberately override that.

Applying an update yourself restarts your Host straight away, even if a build is running, so it is best done between builds.

## What changes, and what stays?

**What changes:** the Host's own software and the apps built into it. Its database and network containers are brought up to their current versions at the same time.

**What stays:** everything on the Host's two disks. One holds the database, meaning every conversation and everything your Host remembers. The other holds your installed apps and their data, your projects, your files and your settings. The Host's certificate is kept by the cluster outside the pod and handed back in at every start. Only scratch space is cleared.

[What a Host is](wiki:what-a-host-is) describes the pod and its disks.

## Where can I see my version?

In the Settings app:

- **About** shows the version your Host is running, and "Last updated", which is the date that version was built.
- **Software Updates** says whether a newer version is available and when it was released, with a button to apply it.
- **History** lists the updates applied from Settings. Versions we roll out are not listed there.

## Can I pick a version or roll back?

No. Updating always moves to the version that is currently published, and there is no button to go back to an earlier one.

## Do apps update separately?

The apps built into the Host change only when the Host itself updates. Apps you installed from the App Store update on their own. Each time your Host starts, it installs newer versions of your store apps, and only ever moves them forward. The App Store shows an Update button when one is waiting, and a Notice on the Support card in Circles tells you when a new version of one of your apps comes out. A paid app whose subscription has lapsed stays on the version you have.

## Does my Host tell me what changed?

Every Host is set up with a weekly note, on the Support card in Circles, about what changed in how it works: rules it has learned from your corrections, how its coding runs went and what they cost, how its memory is forming, and what is not set up yet. It runs with no model, and you can stop it from the Agents app. It reports on your Host's behaviour, not on software versions.

## Does the bastion on my computer update itself?

Yes. Your Host tells your bastion when a newer version has been published. Before swapping, the bastion checks the release's signature and the download's checksum, and it refuses anything that does not match. [The bastion](wiki:the-bastion) has more on the program itself.
