---
name: integrations
description: How the user connects a third-party data account (Gmail, Calendar, Drive, Contacts, YouTube) to their host, and how an app is granted the use of one. The platform's own OAuth client serves it, so connecting is one consent screen; the host holds the tokens and apps never see them.
allowed-tools: integrations_manage
---

# Integrations SOP

## The one thing to understand first

**Connecting an account is one click.** The platform holds the OAuth client, so
the user opens the authorize URL, picks their Google account, approves, and the
connection is live. There is no project to create, no console to visit, no
client ID to paste.

If you find yourself about to explain Google Cloud to someone who asked to
connect their Gmail, stop — you have taken the wrong path. The console steps
below exist for a user who *asks* to use their own project, and for nobody else.

What the host does that a browser sign-in cannot: the token exchange happens
server-side inside the user's own pod, so the host holds a refresh token and
renews it forever. The connection survives closing the tab.

**Where the tokens live.** Access and refresh tokens are created inside the
user's pod and sealed in the user's own vault. The platform's callback sees an
authorization code in flight and nothing else. The OAuth client being the
platform's does not make the platform a reader of their mail — say that plainly
if they ask, because it is the reasonable thing to wonder.

## Connecting an account

1. **`op='families'`** first if you are unsure of state. `source: platform` is
   the normal answer and means everything below just works.
2. **The user opens `/api/integrations/providers/{key}/authorize`** in their
   browser and approves. A sign-in is theirs; this tool never performs one.
3. That is the whole procedure.

Google will show a consent screen naming Privatae and the access being asked
for. Gmail is connected with `gmail.modify` — read, send, drafts, labels and
trash, but **not** permanent delete, which is deliberately never requested
because there is no undo for it.

If `source: none`, the host could not obtain a client from the platform.
That is a platform-side fault, not a missing setup step — say so, and retry.

## If the user wants to use their own Google project

Only on request. One client per FAMILY: registering `google` once serves gmail,
calendar, contacts, drive and youtube together.

1. **They create a project** at console.cloud.google.com, enable the APIs they
   want (Gmail API, Calendar API…), and create an OAuth client of type **Web
   application**.

2. **Publishing status must be "In Production".**

   > While the consent screen is in **Testing**, Google expires refresh tokens
   > after **7 DAYS**. A connection made that way works, then dies a week later
   > and looks like a platform bug.

   Say this BEFORE they finish the console steps, not after. Anything telling
   them to add themselves as a "test user" is prescribing that failure. A
   self-built client used by its own creator shows an "unverified app" screen on
   consent — expected, and they can continue past it.

3. **The redirect URI must match exactly.** They read it from
   `GET /api/integrations/families` **in their own browser**, which is the only
   place that knows the address they opened their host on. Have them copy the
   string; do not guess it or describe its shape. A mistyped redirect is the
   single most common reason consent fails.

   `redirect_uris_need_registering: false` in that response means they are on
   the platform's client and have nothing to register.

4. **`op='set_client'`** with family + client_id + client_secret. Sealed into
   the vault immediately. Never read a client secret back to the user, and never
   put one in a message.

5. **They consent** as above. Their own client takes precedence from then on,
   and existing connections made against it keep working untouched.

## Granting an app

A connection belongs to the USER, not to whatever app asks for it. An app —
even one they installed — gets nothing until `op='grant_app'` names it and the
connection together.

- Grant only what was asked for. If someone installs a mail app and wants it to
  read their Gmail, grant that app that connection. Do not grant it the
  calendar because it is there.
- Narrow with `methods` when the app needs less than everything. `["GET"]` is
  read-only, and now that Gmail is connected with `modify` the real question is
  whether this app needs to SEND and delete mail, or only read it. A summariser
  gets `["GET"]`: grant the narrow one first; a wider grant, when the user's
  request does not settle it, is theirs to decide — in the question box.
- `op='list_grants'` answers "what can this app reach", and `op='revoke_app'`
  takes it back without disconnecting the account or uninstalling the app —
  three separate acts, on purpose.
- Disconnecting the account (`op='revoke'`) cascades every grant away with it.

The app never receives a token. It names a path; the host attaches the
credential and returns only the response.

## When a connection stops working

`needs_reauth` on a connection means the refresh token is gone — revoked at the
provider, or expired because a user-owned client was left in Testing. The fix is
the user re-consenting, not a retry.
