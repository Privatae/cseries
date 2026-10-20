---
title: The Keys app
summary: Keys holds your credentials, sealed, from model provider keys and sign-ins to SSH keys and database addresses. Your Host uses them without ever being handed the values. It comes with every Host.
section: apps
order: 10
questions:
  - Where do I put my OpenAI or Anthropic API key?
  - Where do I connect my Claude subscription?
  - Can my Host see my passwords and API keys?
  - How do I view a key I saved?
  - Can I turn a key off without deleting it?
  - Can my Host fill in website logins for me?
app: keys
---

Keys is where you keep everything that proves who you are to somewhere else: API keys from model providers, sign-ins, SSH keys, tokens, two-factor secrets, and the database addresses your sites use. Every value is stored sealed. Keys comes with every Host.

## What is in it?

Five sections: **Providers**, the accounts that bill you for models; **Sites**, the database addresses your sites use; **Privatae**, your Gitea sign-in; **Sign-ins**, the website logins you saved in the [Browser](wiki:browser-app); and **Vault**, for SSH keys, tokens and anything else.

## Where do I connect my Claude subscription?

You connect the coding engine that uses it. When you connect an engine such as Claude Code through your Host, the vendor's own login runs, and the matching provider's sign-in slot here shows as connected through that engine. There is nothing to paste for it. The one sign-in you can start from Keys itself is xAI's: **Sign in with xAI** on the xAI row opens xAI's own sign-in page, and **Reconnect with xAI** signs in again when the sign-in has expired. [Models and keys](wiki:models-and-keys) explains what your Host runs on it. An API key is the one thing you add by hand, and marketplace providers such as OpenRouter take only an API key.

## Can my Host see my keys?

Your Host is not handed a stored value to read. When it needs one, it asks for an action, such as an authenticated request or a signature, and the key is added on the way out. You can see a value in the clear only in this app, after entering your second-factor code; if you have not set one up, Keys walks you through that first.

## Can I switch a key off without deleting it?

Yes. **Deactivate** keeps the value sealed while everything treats it as missing, and **Activate** brings it back with no new key needed. **Revoke** removes it; revoking a coding engine's credential also disconnects that engine.

## Can my Host fill in logins for me?

Only where you allow it. In Sign-ins you choose which saved site logins your Host may fill without asking each time, and only this screen can grant that. Your Host can see a grant and withdraw one, but cannot create one. When it fills one in, a saved password is only typed into a real password box.

[Models and keys](wiki:models-and-keys) explains how your keys pay for the models your Host uses.
