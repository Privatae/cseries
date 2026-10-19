---
name: connectors-discord
description: Setting up the Discord connector, step by step — the person's own bot in the Discord Developer Portal, added to a server of theirs so they can message it, its token asked for on a card in the chat and stored in Keys, the Discord app installed from the App Store (id discord), and the pairing. The Host holds Discord's gateway connection with the token in it; the app never sees it. The shared flow, routing and revoke are skill 'connectors'.
allowed-tools: install_app, list_app_catalog, escalate, propose_nac_policy, nac_status, schedule_task, revoke_nac_rule, system.app_status
---

# Discord connector

The Discord app (id `discord`) is the whole connector, made and signed by
Privatae and installed from the App Store, tested end to end by
scripts/test_connector_discord_example.py. Never build one and never write
its code. How a message finds its thread, what the person sees, revoking and
the connector contract are in skill `connectors`.

Discord sends a bot its messages over a live connection (the Gateway) whose
bot token travels inside it. Your Host holds that connection and puts the
token from Keys into it; the app keeps it alive and never sees the token.
One key: the bot token.

## In order

1. **The person's bot**, at discord.com/developers/applications, signed in to
   their Discord account: New Application, a name, Create. On the Bot page:
   Reset Token, and copy the token (Discord shows it once). They can turn
   Public Bot off, so only they can add it. No privileged intent is needed:
   a bot reads the text of its own direct messages without Message Content
   (Discord's gateway docs list "Content in DMs with the app" as an
   exception).
2. **A server they share with it.** A person can message a bot that is in a
   server they are in. On the Installation page, choose Guild Install with
   the scope `bot` (no permissions are needed: it only answers direct
   messages), open the install link, and add the bot to a server they own
   (a new private server is fine). In that server, they click the bot's name
   in the member list and Message it. Discord's privacy setting "Allow direct
   messages from server members" must be on for that server.
3. **The token, on a card** (skill `connectors`, "Keys come in through the
   chat"): `escalate` with one option carrying `credential_request`
   {name='discord-bot', cred_type='opaque', purpose: the Discord bot's token
   from the Developer Portal}. You never see it. The chat does not recognise
   a pasted Discord token, so ask for it only through the card.
4. **The app.** When Discord is not installed yet, the person clicks Install
   on Discord in the App Store, or you call `operate(tool='install_app', args={app_id:'discord'})`.
   Until the pairing, its page says "waiting for the pairing".
5. **Start the pairing:**
   `operate(tool='propose_nac_policy', args={kind:'connector', app_id:'discord', platform:'discord', token_name:'discord-bot'})`.
   Then the shared flow in skill `connectors`: the code card in the chat, one
   check after the code runs out, and the person's approval. They send the
   code to the bot in its direct message.

The Discord app answers one account, in direct messages only. A message in a
server channel is never passed on, and anyone else who writes to the bot is
dropped by the connector.

## In Discord

A typing indicator when the Host starts on a message. The reply comes as messages
answering theirs, a picture as an image attached to a message, a link as its
label, with no preview card and no one mentioned. A direct message has no
threads, so a reply to one of the bot's messages is what keeps a
conversation in its thread. A message the person forwards reaches you
labelled as forwarded from an unnamed source: Discord leaves the original's
author out of a forward. `/threads`, `/thread` and `/new` are typed as
ordinary messages. Messages sent while the connector was briefly down arrive
when it reconnects, as long as Discord kept the session (a few minutes).

When the app's page says Discord refused the bot (4004), the token in Keys is
not this bot's: the person resets it on the Bot page and you ask for it again
on a card, then revoke and pair again.
