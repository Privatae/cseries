---
name: connectors
description: How ONE of the person's own messaging accounts talks to them through their chat. Telegram, Slack and Discord are apps in the App Store (ids telegram, slack and discord) that the person or the host installs, never ones the host writes; each platform's setup is its own skill (connectors-telegram, connectors-slack, connectors-discord). Here, the shared flow — the keys asked for in the chat, the pairing, the approval — how each message finds its thread, forwarded messages, what the person sees, revoke, and the contract every connector keeps.
allowed-tools: install_app, list_app_catalog, escalate, propose_nac_policy, nac_status, schedule_task, revoke_nac_rule, system.app_status, context
---

# Connectors SOP

A connector lets ONE account on a messaging app talk to the person's Host. A
message from that account starts a turn in the person's own chat, in one of
their threads, with their full chat permissions, exactly as if they had typed
it in Circles. It never approves anything: a card still waits on the person's
own device, and the reply says so.

The platform gives you the grant, the pairing, the capability, the routes, the
thread routing and the reply as ready-to-send parts, and the connector app
too, in the App Store, made and signed by Privatae: **Telegram** (id
`telegram`), **Slack** (id `slack`) and **Discord** (id `discord`). Install
the one the person wants; never build a connector and never write its code.

## Which platforms, and the skill to load

Each platform's setup steps are their own skill. Load the one the person
wants before you start: `context(action='read',
path='default-skills/connectors-<platform>')`.

- **Telegram:** skill `connectors-telegram`. The person makes a bot with
  @BotFather; one key.
- **Slack:** skill `connectors-slack`. The person makes their own Slack app
  from a manifest at api.slack.com; two keys.
- **Discord:** skill `connectors-discord`. The person makes a bot in the
  Discord Developer Portal and adds it to a server of theirs; one key. Your
  Host holds Discord's gateway connection, which carries the token inside
  it.
- **Matrix and others:** not yet. Say so plainly; nothing is gained by
  building half of one.

## Keys come in through the chat

A connector's token is asked for on a card in this chat, never by sending the
person to another app. Call `escalate` with one option that carries a
`credential_request`: the card has a box where they paste the token, it is
sealed straight into Keys under the name you gave, and you get the name back,
never the value. One card per key, one after the other in the same turn: each
card's answer comes back into your turn before you ask for the next. A name
the person already has in Keys is used as it is; the card asks only for a
new one. A token pasted into the chat itself has been read by the model, and
the chat offers to store it in Keys; tell the person to replace it with a new
one from the platform, through the card.

## The pairing, the same for every platform

After the platform's skill has the keys in Keys and the app installed,
`operate(tool='propose_nac_policy', args={kind:'connector', app_id:…, platform:…, token_name:…})`
starts the pairing (Slack also names `app_token_name`). It is refused when
the app is not installed, or when Keys holds no key by that name. It mints
the connector's capability and restarts the app once to hand it over (a
minute or two). The kernel makes an 8-letter code, good for 10 minutes, and
shows it to the person on a card in their chat and in the Network app; you
never see it and never ask for it. Tell them the code card is in the chat
and to send the code to the bot in a direct message.

Then set one check for after the code runs out, and end the turn. Never wait
on the approval itself: their answer, on the card in this chat or in the
Network app, reaches you as their next message. Cover only a pairing nobody
answers: `operate(tool='schedule_task', args={op:'create', title:…, fire_at:<just after the 10
minutes>, prompt:<read nac_status and tell the person where the pairing
stands>})`, with no `wake_on`, and cancel it when their answer arrives.

The connector reports the code with their account; the bot answers "Code
accepted", and a message sent before the approval is not passed on (the bot
says so). Five wrong codes from anyone close the pairing. The person
approves with their authenticator code on the connector's card, in the
Network app (Ingress, Messaging connectors) or in Circles; `approve_nac_draft`
refuses a connector. From then on that account's messages start turns.
`nac_status` shows the grant at every stage with its id, never the code;
`system.app_status` says whether the app is running, with its log's tail.

## How a message finds its thread

The platform places every message before any turn runs, in this order
(server/connectors/turns.py, `route`):

1. **Commands**, which the platform handles itself (a command never reaches
   you; a message written after one does): `/threads` lists the person's
   open threads, newest first (up to 30), numbered; `/thread <number>`
   switches the chat to that thread, and `/thread <number> <message>` sends
   the message there; `/new` makes their next message start a new thread,
   and `/new <message>` starts one with that message.
2. **A reply** (Telegram's, Discord's, or a message in a Slack thread) to a
   message the platform has on record lands in that message's thread, while
   that thread is open.
3. **A Telegram topic, or a Slack thread,** the platform has on record lands
   in its thread.
4. **Otherwise the Host picks.** One call to the chat model is shown the open
   threads, newest first, and picks one or starts a new one
   (server/connectors/thread_pick.py); the pick is written into the thread as
   a line. When that call does not pick, the person is asked: "Which thread
   is this for? Send /threads to choose one, or /new to start a new one."

A message sent while a turn runs in its thread is added to that turn. All of
this exists already: when the person asks how their messages are routed, say
this, and never offer to build any of it.

## Forwarded messages

A message the person forwards, or sends through another bot, was written by
someone else. Its header says so, in the platform's words: for example
`[Telegram message, forwarded from a bot “Hermes” (id 42)]` or `[Discord
message, forwarded from an unnamed source]`. The kernel writes that label
from what the connector reported, and keeps it on the person's message
(`connector_origin`); the thread shows it. A forwarded message is never read
as a command or a pairing code.

## What the person sees

- **The reply** comes as messages in the same chat, answering theirs where
  the platform allows; its first line names the thread when it changed
  ("New thread: …", "Back in: …"). A picture the Host could not draw comes as
  one line and a link to the thread, where it shows live. A picture over the
  Host's 10 MiB limit for a file comes as a line saying it is in the Host. An
  artifact comes as a link that opens it in the Host. Links show no preview.
  Each platform's skill says what is particular to it.
- **Cards.** An approval never travels: the reply says a card is waiting and
  links to the Host, where they answer it.
- **In Circles.** Every connector thread is an ordinary thread with the whole
  conversation, and they can carry on there.
- **Everyone else.** The bot answers one account, in direct messages only;
  anyone else's message never reaches you.

## Revoke, re-pair, delete

- `operate(tool='revoke_nac_rule', args={kind:'connector', id:<grant>})` is free. A reply in
  progress stops, every call the connector makes is refused from that moment
  (Discord's gateway connection is closed at once), and it sends nothing
  more. The app keeps running until it is uninstalled, and uninstalling it
  revokes too.
- Another account for the same connector: start the pairing again. Approving
  the new account replaces the old one in the same step; until then, and
  after a "Don't pair", the old account keeps working.
- Another bot (another key) for the same connector: revoke first, then pair
  again. A pairing that names other keys while a grant is live is refused.
- A pairing after a revoke mints a new capability, and the app restarts once
  to receive it.

## The contract every connector keeps

The store apps keep exactly this contract; it is here to explain what they
do, not to build another. The app is a process app in its own runtime pod
(`runtime.kind: process` with `runtime.image`), `runtime.lifecycle:
resident`, with a `network[]` holding only that platform's hosts.

- **Environment.** `PORT`, `PRIVATAE_APP_ID`, `CSERIES_KERNEL_URL` (the Host, port
  8800), and `CSERIES_CONNECTOR_CAP`, which arrives with a restart once the
  pairing starts. Every call to the Host carries `X-CSeries-App-Id` and
  `X-CSeries-Connector-Cap`.
- **Stages.** Before the approval the capability opens `POST /pair` while
  the pairing is open, and `POST /platform` and the gateway until the person
  approves or declines; after the approval, the message routes too (all
  under `/api/connector`). A 403 means not paired, not this account, or
  revoked.
- **Every platform call goes through the Host.** `POST /api/connector/platform
  {method, path, json | form, files?, timeout?}`; the Host puts the token
  from Keys where the platform takes it. The answer is `{status, body, json,
  truncated}`, plus `retry_after` (seconds) when the platform asked to wait
  (its Retry-After header, or Discord's and Telegram's own field); the
  connector waits that long. A call without files is at most 16 KiB of JSON;
  `files: [{field, filename, data_b64, mime}]` makes it multipart, at most
  10 files, 10 MiB each, 12 MiB in all. One connector has at most 8 calls in
  flight; past that the Host answers 429, a send to try again later.
- **Discord's gateway.** WebSocket `/api/connector/gateway[?url=<READY's
  resume_gateway_url>]`: the Host opens Discord's gateway (only a discord.gg
  host, with v=10&encoding=json) and relays text frames, filling `d.token`
  of Identify and Resume; one per app.
- **Pairing.** `POST /api/connector/pair {code, account_id, display_name}`:
  200 matched, 403 "wrong code", 410 expired.
- **A message.** `POST /api/connector/messages {chat_id, message_id, text,
  account_id, display_name?, reply_to_message_id?, native_thread_id?,
  origin?}` (`origin`: {kind forward|via_bot, source
  user|bot|chat|channel|hidden, name?, id?}) streams one JSON object per
  line: `thread`, `turn` (`started`), `alive` every 10 s, the parts `text`,
  `image` and `link` with their `seq`, then `done`. Other `turn` states:
  `nudged` (send its `note` when `mine` is false), `ask`, `command`,
  `refused`, `duplicate`.
- **Delivery.** After each part, `POST /api/connector/messages/sent {turn_id,
  chat_id, sent: [{seq, message_id}], native_thread_id?}`. A dropped stream
  is picked up with `GET /api/connector/turns/<turn_id>`, which leaves out
  the parts already reported.
- **What the connector keeps**, in its data dir: its read position (or
  Discord's session), the paired account, and the replies on their way.
