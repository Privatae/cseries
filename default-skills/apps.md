---
name: apps
description: How the host ships, proves, interrogates and publishes its own apps and sites — install is not delivery, a 200 is not proof, a page complaint is evidence to load rather than a reason to ask for a hard refresh — and how it runs the browser agents the user recorded.
allowed-tools: ship_project_app, app_probe, check_page, system.app_status, publish_app, publish_site, manage_site, manage_site_domain, browser_agent, install_app, uninstall_app, purchase_app, list_app_catalog, refresh_app_store, open_app, kernel_op, build
---

# Apps SOP

## ship_project_app — guidance

In a project with modules, name the one to install: `module=<name>` (a
private or store app; a public site is published with publish_site, a
component ships inside another module — both are refused here).

Shippability is a property of the project TREE, not the plan state: any
project ships unless a build is in flight (unfinished phases are disclosed
as open_phases in the result, not refused). Its app.yaml must declare
provenance=custom; the build passes the build-contract validator and the
App-SDK integrity gate before signing (custom apps may declare any
runtime.kind — the static-or-helper restriction applies only to STORE
publishing). INSTALL IS NOT DELIVERY (2026-09-04): after this returns the
platform starts the app and PROVES it runs — its own ready_path answering ok
plus a clean log tail — and reports the verdict in the project thread as a
'prove: <app>' task (verify_task_id). Say the app is installed and being
proven; never call it delivered or "live" until that verdict lands. A failed
proof carries the log tail and a ready-made repair brief; when the project's
auto-deliver switch is on the platform dispatches that repair itself (up to 2
rounds), otherwise you run it yourself with build(action='continue',
message=<the seed>). A proof whose verdict is 'not checked' means the
platform never brought the app up (its runtime pod did not wake): nothing
about the app was measured, so it is not the app's failure, it carries no
repair brief and it spends no repair round — say the platform did not start
it, and ship again to re-run the proof. Use create_project / the Workbench build tools to
produce the tree first.

## app_probe — guidance

Every op is a read of your own app: call it through `read`
(`read(tool='app_probe', args={app_id, op, path?})`), never through an agent.

`op='request'`: use it to ask the app's own API ('/api/v1/count', '/status')
instead of guessing from the log, and to open the exact URL a user says is
broken. CHECK content_type: a page that renders blank is very often a script
or stylesheet coming back as text/html — the browser cannot execute an HTML
document, so nothing mounts and the log stays clean. A 200 is not proof; a
catch-all that serves the shell for every path returns 200 for assets that
do not exist. `op='logs'` is the full log with `contains` to filter and
`limit` lines, not the fixed tail app_status returns. `op='files'` is where
a running app's real configuration lives (its config, its database, whatever
it writes at runtime), read-only and jailed to that app; workspace_file
reads the project SOURCE, which is what you shipped, not what is running.
`path` is a path, never a URL — the address comes from the app itself, so
you cannot point this anywhere else. To change the app, build and ship a new
version.

## check_page — guidance

LOAD IT AND LOOK. A request the page made OUTSIDE its own /system-apps/<id>/
mount means the app was built for the wrong base path and its assets 404 —
the single most common cause of "the shell renders but nothing works". Pass
`path` to look at a DEEP route: an app's entry page very often renders
perfectly while /trade or /dashboard is empty, because only a navigation
pulls a lazily-loaded chunk. USE THIS BEFORE asking the user to hard-refresh,
clear their cache or describe the screen again — their report is the
symptom, this is the evidence. `checked: false` means the browser could not
be asked; that is never a pass, so say so plainly rather than reporting the
page as fine. One browser wake per call, released immediately.

check_page is a browser of your own. The Browser app's viewport is the
user's to drive: they click, type, scroll and sign in there, so guide them
through a page ("click Sign in, top right") rather than saying you pressed
something. That browser keeps nothing between sessions, so a page worth
keeping is saved with `links` (source='browser').

## publish_app — guidance

Every publish is HUMAN-REVIEWED before it appears in the store: (1)
`op='agreement'` fetches the publishing agreement (text_md +
agreement_version) — show it to the USER and proceed only on their explicit
yes; (2) `op='publish'` packs+signs one of the host's Workbench projects
(project_id; any state except a build in flight) and SUBMITS it for review —
pass the accepted agreement_version (required — never send it without that
yes), optional audience ('public' lists it, 'internal' hides it, 'circle' lists it
to ONE group's members only), summary,
version, fork_policy ('open' = anyone may publish flavors of this app
[default] / 'closed' = no published flavors; private modification by buyers
is always allowed either way); price_micros must be 0 — the store is FREE AT
LAUNCH. Success is a PENDING request (request_id), not a live listing; (3)
`op='requests'` shows this publisher's requests with status
(pending_review/approved/rejected) and the reviewer's reason when declined.
The platform fee is platform-owned and not settable.

AUDIENCE 'circle' needs `group_ref`: the id of a group THIS host owns
(grp-...). Its roster decides who sees the app in the catalog and who may
install it — the same roster you manage in Circles, so adding a member gives
them the app and removing one takes it away on the next roster sync. A group
this host merely belongs to is refused: a circle app is gated on a roster its
publisher holds, because the publisher's host is what tells the store who is
on it. List the eligible groups with `read(tool='kernel_op', args={method:'GET',
path:'/api/groups'})` (the ones marked owned).
Use it for an app built for a community you run rather than for the world —
'internal' hides an app from everyone, 'circle' shows it to a chosen few.

## publish_site — guidance

In a project with modules, `module=<name>` publishes that public-site module
(any other target is refused by name).

Artifacts publish as-is, no convert-to-app needed; list_projects shows both
projects and artifacts with their kind. A gitea repo source is any repo this
host can read with its own identity, e.g. one the user granted it in the Git
permissions panel. A privatae.json manifest at the root means a DYNAMIC app
(the whole tree ships; the zone builds it with the manifest's build command
and runs its start command in an isolated node/python pod); otherwise the
first of dist/build/out/public carrying index.html, else a root index.html
(STATIC). The platform annotates the pages for live-edit and signs the
bundle. A NEW site goes live public; the owner sets other visibility in
the Sites app, and republishing an existing slug keeps it.

There is no zip upload or folder drag anywhere in this lane: when the user
asks where to upload a site, the answer is to publish the project or repo.
Visibility has six settings in the Sites app: Public, Private (owner only),
Password, IP allowlist, Members (the people in the site's Access tab, open
sign-up or invite only) and Paywall (Stripe checkout buys entry). A
published site starts unlisted, so search engines are asked to leave it out;
on a Public site the owner switches Search engines to Listed in My Sites, or
you do it with `manage_site` op='expose' and policy {tier: 'public',
indexable: true}. A Catalog app's name becomes its address
(<name> under the sites zone), so it must be free across the platform; a taken name is
refused and the user picks another.

## browser_agent — guidance

You do not write the steps and you do not improvise missing ones: if a step
fails the run stops and reports which step and why. When a run returns
'waiting', tell the user exactly what the site asked and pass their reply
with op='resume' (name + answer — a one-time code, usually). To run one on a
schedule use schedule_task with op_tool='browser_agent' and op_args={op:'run',
name:'<agent>'}. A run wakes the browser for about a minute and uses that
much of the machine's memory while it runs.

The agents are the user's: you list and run them, and a change to one is the
user re-recording it or editing its file. A password never goes through the
chat: the user types it into the page in the Browser viewport, or the vault
types a stored sign-in (Keys → Sign-ins, where a site must be allowed before
an agent signs in unattended).

## Store lane — guidance

`list_app_catalog` joins the store with this host's install state
(not_installed / installed / up_to_date / update_available) and price.
`purchase_app` records a free entitlement; idempotent — re-calling on an
owned app returns the existing entitlement. Paid apps go through Stripe
checkout: `purchase_app` returns checkout_required, and the user buys the app
themselves with Buy in the Store app (you never spend money for them); then
`install_app`. `install_app` fetches the signed bundle, verifies the
signature and installs; paid apps must be bought first. `uninstall_app` keeps config and workspace unless
purge=true (no undo); native/kernel apps are refused. `refresh_app_store`
reinstalls every non-native app that has a newer store version, in place.


## Parameter notes (full text of shortened argument descriptions)

- **publish_app** `fork_policy`: For op='publish': whether others may publish flavors of this app (default 'open'; private modification by buyers is always allowed either way).
- **manage_site** `policy`: expose: {tier: public|ip_allowlist|password|authenticated, …}; omit allow_emails on 'authenticated' to make it private to its owner. `indexable: true` lets search engines list the site. A site starts unlisted (the zone asks search engines to leave it out), so set it only when the owner asks; `indexable: false` unlists it again. A policy without `indexable` keeps the current setting.
- **check_page** `path`: Optional sub-path inside the app to load instead of its entry page, e.g. 'trade' or 'ui/dashboard' — when a particular screen is broken.
- **publish_app** `price_micros`: New price in micro-dollars — required for op='set_price'. For op='publish' it must be 0 (free at launch).
- **publish_site** `project_id`: Source A: the Workbench project OR artifact id to publish (from list_projects; artifacts need no conversion).
- **app_probe** `path`: request: the path to fetch (default '/'). files: a path inside the app's data dir (default the root).
