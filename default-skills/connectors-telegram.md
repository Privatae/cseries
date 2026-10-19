---
name: connectors-telegram
description: Setting up the Telegram connector, step by step — the person's bot from @BotFather, its token asked for on a card in the chat and stored in Keys, the Telegram app installed from the App Store (id telegram), the pairing and the approval, and moving a Host-built Telegram connector to the store app. The shared flow, routing and revoke are skill 'connectors'.
allowed-tools: install_app, list_app_catalog, escalate, propose_nac_policy, nac_status, schedule_task, revoke_nac_rule, system.app_status
---

# Telegram connector

The Telegram app (id `telegram`) is the whole connector, made and signed by
Privatae and installed from the App Store. Never build one and never write
its code. The platform's own gate (scripts/test_connector_telegram_example.py)
installs it from its signed bundle and runs it end to end against the real
connector routes. How a message finds its thread, what the person sees,
revoking and the connector contract are in skill `connectors`.

## In order

1. **The bot.** The person creates a bot with Telegram's @BotFather: send it
   `/newbot`, choose a name and a username ending in `bot`, and BotFather
   gives the bot's token. A lost or exposed token is replaced with `/token`.
2. **The token, on a card.** `escalate` with one option carrying
   `credential_request` {name='telegram-bot', cred_type='opaque', purpose:
   the Telegram bot's token from BotFather}. You never see it. That is also
   the name the chat stores a pasted Telegram token under.
3. **The app.** When Telegram is not installed yet, the person clicks
   Install on Telegram in the App Store, or you call
   `operate(tool='install_app', args={app_id:'telegram'})`. Nothing is built and nothing is written.
   Until the pairing below, the app's page says "waiting for the pairing":
   that is the healthy state of an unpaired connector.
4. **Start the pairing.** `operate(tool='propose_nac_policy', args={kind:'connector',
   app_id:'telegram', platform:'telegram', token_name:'telegram-bot'})`.
   It is refused when the app is not installed, or when Keys holds no key by
   that name. It mints the connector's capability and restarts the app once
   to hand it over (a minute or two). The kernel makes an 8-letter code, good
   for 10 minutes, and shows it to the person on a card in their chat (their
   browser fetches the code; it never passes through you) and in the Network
   app. You never see the code and never ask for it. Tell the person the code
   card is in the chat, and to send the code to the bot in a direct message.
5. **Set one check for after the code runs out, then end the turn.** Never
   wait on the approval itself: when the person approves or declines, on the
   card in this chat or in the Network app, their answer reaches you here as
   their next message, which starts your turn. A wake on `connector_approved`
   would run a second turn for the same approval, and a wake on
   `connector_expired` would not fire on time (an expiry is only recorded
   when something reads the pairing). Cover only a pairing nobody answers:
   `operate(tool='schedule_task', args={op:'create', title:…, fire_at:<just after the code's
   10 minutes run out>, prompt:<read nac_status and tell the person where the
   pairing stands>})`, with no `wake_on`; reading `nac_status` brings the
   pairing's state up to date. When the person's answer arrives, cancel it
   (`operate(tool='schedule_task', args={op:'cancel', event_id:…})`).
6. **The person sends the code.** The connector reports it with their
   Telegram account, and the Network app shows that account waiting for
   approval. The bot answers "Code accepted": between the code and the
   approval the connector still reaches Telegram, and nothing else. A message
   sent before the approval is not passed on: the bot says so, and the person
   sends it again once they have approved. Five wrong codes from anyone close
   the pairing.
7. **The person approves**, with their authenticator code, on the
   connector's card: in the Network app (Ingress, Messaging connectors) or in
   Circles. `approve_nac_draft` refuses a connector: this step is theirs
   alone. From then on that account's messages start turns.

`nac_status` shows the grant at every stage (pairing, awaiting approval,
active) with its id; it never shows the code. `system.app_status` says
whether the app is running and ready, with the tail of its log.

## In Telegram

A typing indicator while the Host works. The reply comes as messages
answering theirs. A picture comes as a photo, or as a file when it is too big
for a photo. A reply to one of the bot's messages, or a message in a Telegram
topic, stays in its thread. A message the person forwards, or one sent
through an inline bot, reaches you labelled with who wrote it (a person, a
bot, a group or a channel, with its name). Messages sent while the connector
was down arrive when it is back, if that is within the 24 hours Telegram
keeps them.

## A Host-built Telegram connector

A Host may already run a Telegram connector it built from an older version of
skill `connectors` (often with the id `telegram-connector`). It is a separate
app, and the store's Telegram app never replaces or updates it. Telegram
hands a bot's messages to one poller at a time, so one bot gets one
connector: to move the person's bot to the store app, revoke the old
connector (`operate(tool='revoke_nac_rule', args={kind:'connector', id:<its grant>})`), install
Telegram, and pair it with the same key. The old app can then be
uninstalled, if the person wants.
