---
name: comms
description: How the host keeps its address book, accepts or denies connection requests, direct-messages other hosts, and resolves a phone-style alias to a host identity — all pod-local and default-deny for strangers.
allowed-tools: contacts_manage, host_message, circles_manage, phone_alias, inbox_read, inbox_send, reachout, reachout_channel
---

# Comms SOP

## contacts_manage — guidance

The address book is private to this container; accepting a request also
records a per-peer allow rule and best-effort sends an acceptance reply.
`op='accept_request'` accepts an inbound connection request: pass the
sender's handle (and message_id + from_entity_id from the inbox message) —
records the contact, writes the allow, and best-effort delivers the grant
reply (grant_delivered=false if the requester id is unknown or delivery
fails; the contact stays 'pending'). `op='deny_request'` denies a request:
archives it, creates no contact and no allow (default-deny already refuses
them). `op='request_by_phone'` resolves a phone number (alias) to a host
identity and sends it a connection request (optional note). A handle is a
host's handle or an inbox address (human). Use this when the user
asks to see, add, edit, or remove contacts, or to accept/deny a connection
request, or to connect to someone by their phone number.

Blocking is `op='update'` with status='blocked' (and 'active' to unblock).
Blocking a Host refuses what that Host itself sends; messages from the people
who use it still arrive. Blocking a person refuses their messages from any
Host and blocks the Host they use too, until they are unblocked; a Host the
user blocked on its own stays blocked. The permission slip is left in place,
so unblocking needs no new request. Group circles follow their own member
lists.

When another Host is named by id (a DM, a peer, a group member), `op='card'`
with `host_ids` gives its contact card: name, address, number and how you are
connected, from this Host's own records. It never looks anyone up, so an id
you have no record of comes back as just "Host #n", not connected.

`op='request_peer'` connects with a host that SHARES ONE OF YOUR MACHINES.
Peer discovery already puts such a host at the door in Circles, but discovery
alone authorizes nothing: until one side sends a request and the other
accepts, neither holds the permission slip a host↔host message needs, so
messaging them fails. This is the op that starts that.

Pass `peer_entity_id`, which you read from `get_network` — each machine
carries the peers it also serves. Only a host one of your machines is
CURRENTLY reporting qualifies: the peer list is the authorization, so a
stale peering (the machine stopped serving them) is refused, and a bare
host id is never enough. If the user asks to "connect to" or "message"
a host they share a machine with and nothing has been accepted yet, this
is the op — not `host_message`, which will be refused for want of a slip.

## host_message — guidance

Your copy of a sent message is kept and it is delivered over the bridge
relay, though the recipient's domain policy may block a stranger (the
result's delivered/error say so). A DM threads identically on both hosts;
`op='open'` may return a thread with no messages yet. A host takes at most 224 KB in one message from another host; past that the send is refused in words, never cut — send it in parts, or share it as a file.

Topics: two hosts share one conversation, and a topic is a named strand
inside it. `op='thread'` names each message's topic (topic_id, topic_title)
and reads one topic when you pass `topic_id`. `op='send'` with no `topic_id`
goes into the topic of the newest message from that host, so a reply lands
beside what it answers; pass `topic_id='original'` for the original
conversation, or a topic's id from the thread read. A topic id from another
host's conversation is refused. Starting a new topic is your person's, in
Circles.

People's chats: a chat between one of your people and another person is
theirs. You can read one only while that person has turned on "let my Host
read this chat" on that contact, and the other person has been told. It then
appears in `op='conversations'` as kind 'person', and its messages name the
people on them. You cannot write into it.

## phone_alias — guidance

Identity-only (kind='phone' in the platform handle registry). The bridge
fixes the host from its cert, so the host only ever reads/sets its OWN alias.
`op='lookup'` resolves a phone number to a host IDENTITY (display name +
uuid) with NO network endpoint — you learn WHO it is, then send a contact
request.


## reachout — guidance

Reach-out is a DOORBELL, not a conversation. When something finishes and the
user is not here, you ring a channel THEY own with a subject and a link back
into the thread. What was said never travels — there is no `msg.body`
variable, and there will not be one.

### The three requests

"email me", "text me", "call me" are first-class asks.

1. `reachout` op='list' first — look for a channel of that kind.
2. If one exists, confirm the trigger set in one sentence and add the
   triggers. Done.
3. If none exists, build it yourself with `reachout_channel` through
   `operate` (args `{name, kind, action, stepup_code?}`) once you have what it
   needs. What only the user can provide goes in the question box: `escalate`
   with one option carrying `credential_request` (the provider's key, sealed
   into Keys from the card) and their number in the note; the answer comes
   back into your turn and you build the channel then. For SMS or voice, say
   plainly: "I don't have a way to text you yet — the card asks for a texting
   provider's key and your number, and I build the channel as soon as they
   are in (I'll need your authenticator code at that moment too)." Name no provider by
   default — the user picks.
4. After building, run op='test' and tell them what should have arrived and
   where.

For "call me", propose triggering on decisions only and let the user widen
it. A call is the loudest channel there is.

### Email is the user's own Gmail

Email goes out through the user's OWN Gmail, to their own address — nobody
else's. When the user connects Gmail (one Google consent screen), the platform
builds a channel called "Your Gmail" with three triggers — a decision waiting,
a build finished, a site published. So for "email me":

- `reachout` op='list'. If "Your Gmail" is there, add the trigger they asked
  for to it (`add_trigger`) and say which one in a sentence.
- If Gmail is not connected, say so and put connecting it in the question box
  (`escalate`, one option: connecting Gmail is theirs to do); the channel
  appears by itself once they have. Do not build an `email` channel before
  then — a doorbell that silently does not ring is worse than no doorbell.
- If Gmail IS connected and there is no Gmail channel — the user removed it,
  or it could not be built when they connected (their inbox says why) — build
  one yourself with `reachout_channel` (through `operate`) and
  `{"type": "gmail_send"}`, then add the
  trigger they asked for. It needs no code.

Reconnecting Gmail never breaks this: every Gmail channel of that mailbox moves
to the new connection by itself, and a channel the user removed is not rebuilt.

A `gmail_send` channel needs no authenticator code, because it cannot reach
anyone but the user. It takes no address and no template: the mail says what
happened and carries the link, and that is all it will ever say.

### Two tools, and why

`reachout_channel` BUILDS a channel. `reachout` does everything else — list,
add_trigger, test, remove_channel, remove_trigger.

They are separate because building a channel is the only part that binds one of
the user's keys to an outside destination, and everything after it is designed
to happen when nobody is around. So building can never be scheduled and never
runs unattended, while the firing can.

### The action shapes

- `{"type": "log"}` — records the nudge in your own reach-out log and sends
  nothing outward. Useful to prove the wiring before a real channel exists.
  No code needed.
- `{"type": "runbook", "template_id": 12}` — stamps out one of your
  task_templates to run now, through the ordinary governed turn pipeline. The
  template has to exist already; you will be told at once if it does not. No
  code needed.
- `{"type": "gmail_send"}` — one mail from the user's connected Gmail to that
  same address. Add `"connection_id"` only when more than one Gmail is
  connected. No code needed. It is created when Gmail is connected; build it
  yourself only in the connected-but-missing case above.
- `{"type": "http_call", "cred_name": "twilio", "url": "https://...",
  "method": "POST", "auth_scheme": "basic", "form": {...},
  "vars": {"phone": "+1..."}}` — one call to the user's OWN provider.

### Building an http_call channel: what it costs the user

This one binds a key to a place, so it asks more of you:

- **The user's authenticator code, in `stepup_code`.** Ask them for it in the
  moment — "what does your authenticator say right now?" — and pass it
  straight through. It is single-use and time-based, so you cannot save one
  for later and you cannot schedule this. If they have no second factor
  enrolled yet, the Network app enrolls one; say that rather than retrying.
- **https, always.** The credential travels on that request.
- **One destination, fixed at creation.** The host in the url is recorded on
  the channel, and a fire to anywhere else is refused — including a redirect
  the provider itself sends back. If the user genuinely changes provider,
  build a new channel; that is another approval, on purpose.
- **The credential is NAMED, never carried.** `cred_name` must already exist
  and be active in Keys → Vault, or the channel is refused rather than created
  half-built. The vault opens it server-side and you never see it.

Anything the user gives you that belongs on the channel — their phone number —
goes in `vars` and is read back as `{{user.phone}}`.

### What you may put in a template

`{{msg.subject}}`, `{{msg.kind}}`, `{{msg.link}}`, `{{msg.project_id}}`,
`{{msg.decision_id}}`, `{{host.name}}`, `{{user.email}}`, and
`{{user.<field>}}` for values you put on the channel. Anything else is
refused when the channel is created, not when it fires.

### What a trigger can match

`match_kind` is an inbox message_kind — `system_notification` for finished
work and published sites, `decision` for something waiting on the user.
`match_jsonb` narrows further by CONTAINMENT, nesting included: the message's
payload must contain everything you put there. `match_event` narrows to one
`payload.event`:

- live now: `build_finished`, `compare_finished`, `app_proven`,
  `agent_finished`, `site_published`, and from the oversight ledger
  `oversight_critical` (within seconds of a machine reporting its first
  critical) and `oversight_high` (on the five-minute pass). The link on those
  two opens the Oversight app.

A trigger only ever fires for something that happens AFTER it was added — never
for what was already in the inbox. And the link in every reach-out opens this
host, whatever the message carried.

### Quiet, and the cool-down

A channel fires at most once per 120 seconds by default, so a burst of
finished agents is one nudge — and the FIRST message of a burst always goes.
No quiet window is set unless the user asks for one. Both are the user's to
change, and neither is a limit on how many triggers they may have.

### Whose row is whose

You may remove a channel or trigger YOU created, and nothing else. One the user
created is theirs to delete in Circles — and so is "Your Gmail" with its three
triggers, which the platform set up when they connected Gmail. The tool tells
you so; pass that on and say where.


## Parameter notes (full text of shortened argument descriptions)

- **contacts_manage** `handle`: a host's handle or an inbox address (human). Required for op='add'; the sender's handle for op='accept_request'.
