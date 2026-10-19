---
name: connectors-slack
description: Setting up the Slack connector, step by step — the person's own Slack app made from a manifest at api.slack.com (Socket Mode, the bot scopes, message.im, the slash commands), its two tokens asked for on cards in the chat and stored in Keys, the Slack app installed from the App Store (id slack), and the pairing. The shared flow, routing and revoke are skill 'connectors'.
allowed-tools: install_app, list_app_catalog, escalate, propose_nac_policy, nac_status, schedule_task, revoke_nac_rule, system.app_status
---

# Slack connector

The Slack app (id `slack`) is the whole connector, made and signed by
Privatae and installed from the App Store, tested end to end by
scripts/test_connector_slack_example.py. Never build one and never write its
code. How a message finds its thread, what the person sees, revoking and the
connector contract are in skill `connectors`.

Slack needs two keys: the bot token (`xoxb-`), for everything the app sends,
and the app-level token (`xapp-`), which only opens the connection Slack
delivers messages on (Socket Mode, so the person's Slack app needs no public
web address).

## In order

1. **The person's Slack app**, at api.slack.com/apps, signed in to their
   workspace. The quickest way: Create New App, From a manifest, pick the
   workspace, paste the manifest below, Create. It sets everything the
   connector needs: Socket Mode on; the bot scopes `chat:write`,
   `im:history`, `files:write`, `users:read` and `commands`; the
   `message.im` event; the Messages tab with sending allowed; and the
   `/threads`, `/thread` and `/new` commands. Give them the manifest
   exactly as it is; they may change the two names.

   ```json
   {"display_information": {"name": "Host"},
    "features": {"app_home": {"home_tab_enabled": false, "messages_tab_enabled": true,
                              "messages_tab_read_only_enabled": false},
                 "bot_user": {"display_name": "Host", "always_online": true},
                 "slash_commands": [
                   {"command": "/threads", "description": "List your Host threads", "should_escape": false},
                   {"command": "/thread", "description": "Switch to a Host thread",
                    "usage_hint": "<number> [message]", "should_escape": false},
                   {"command": "/new", "description": "Start a new Host thread",
                    "usage_hint": "[message]", "should_escape": false}]},
    "oauth_config": {"scopes": {"bot": ["chat:write", "im:history", "files:write",
                                        "users:read", "commands"]}},
    "settings": {"event_subscriptions": {"bot_events": ["message.im"]},
                 "socket_mode_enabled": true, "org_deploy_enabled": false,
                 "token_rotation_enabled": false}}
   ```

2. **The app-level token.** In the new app: Basic Information, App-Level
   Tokens, Generate Token and Scopes, add the scope `connections:write`,
   Generate. It starts with `xapp-`.
3. **Install it to the workspace.** Install App, Install to Workspace, Allow.
   The Bot User OAuth Token on that page starts with `xoxb-`.
4. **Both tokens, on two cards** (skill `connectors`, "Keys come in through
   the chat"): first `credential_request` {name='slack-token',
   cred_type='opaque', purpose: the Bot User OAuth Token, xoxb-}, then
   `credential_request` {name='slack-app-token', cred_type='opaque', purpose:
   the app-level token, xapp-}. These are the names the chat stores a pasted
   Slack token under, so either way they match.
5. **The app.** When Slack is not installed yet, the person clicks Install on
   Slack in the App Store, or you call `operate(tool='install_app', args={app_id:'slack'})`. Until
   the pairing, its page says "waiting for the pairing".
6. **Start the pairing** with both keys:
   `operate(tool='propose_nac_policy', args={kind:'connector', app_id:'slack', platform:'slack', token_name:'slack-token', app_token_name:'slack-app-token'})`.
   Then the shared flow in skill `connectors`: the code card in the chat, one
   check after the code runs out, and the person's approval. They find their
   app in Slack under Apps (or search its name) and send the code in its
   Messages tab.

The Slack app answers one account, in that account's direct messages with
the app. In a workspace it pairs with one person; anyone else who writes to
it is dropped by the connector.

## In Slack

The reply comes in their direct message with the app, and inside the Slack
thread when they wrote in one. Slack shows no typing indicator for an app. A
picture comes as a file shared into the chat, a link as its label. `/threads`,
`/thread` and `/new` are the app's own slash commands (Slack takes any
message that starts with `/` as a command), so they work once the app was
made from the manifest above. A message the person shares into the DM
reaches you labelled as forwarded (from a bot when Slack marks it as a bot's).
Slack delivers a message to the connector while it is running and tries again
for a short while after; a message sent while the connector was down for
longer may not arrive, and they send it again.
