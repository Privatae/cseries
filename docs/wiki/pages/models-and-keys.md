---
title: Models and keys
summary: Your Host runs on models you choose, from providers you hold keys for, and its memory search can run on a model on your own machine. You set them per role in the Orchestrator app, your keys are sealed in the Keys app, and Privatae does not sell, proxy or bill model usage.
section: host
order: 50
questions:
  - Can I use my own API keys with my Host?
  - Which AI providers and models can my Host use?
  - Can I use my Claude or ChatGPT subscription?
  - Can my Host's memory search run on my own machine?
  - Does Privatae sell or bill model usage?
  - What happens if my model provider goes down?
  - Where are my API keys kept?
---

You bring the models. In the Orchestrator app you choose, for each role your Host has, a provider you hold a key for and one of its models. Your keys are sealed in the Keys app. Privatae is not an inference provider: it does not sell, proxy or bill model usage, and a request goes from your Host straight to the provider you chose.

## Which providers can I use?

The Keys app has slots for OpenRouter, Anthropic, OpenAI, xAI, Google, Kimi, DeepInfra, Together, Fireworks and Groq. Your Host's memory search can also run on an embedding model on your own machine, through the bastion. [Local models](wiki:local-models) explains how.

There is no model built into your Host and none supplied by Privatae. Until you choose one, the Host tells you it has no model. The Getting Started app walks you through adding a key and applying the recommended setup in the Orchestrator.

## What does the Orchestrator let me set?

Two text roles, each routed on its own:

- **Host model**: the Host itself, in conversation and in its work, including what it keeps in memory as it answers, and the agents it dispatches, which run on the same model unless the Host picks another for a particular task.
- **Review**: reads what a coding engine built and checks it against what was asked.

Alongside those you choose where embeddings come from (for search by meaning) and which model makes images. Pictures you share are read by the Host model itself, so choose one that reads images; if it does not, it is told a picture was there that it cannot see, and the turn goes on without it.

For each text role you pick a provider and a model, and you can add a **fallback provider** that answers when the first one returns an error or does not offer that model. Where the provider accepts it, you can also set a reasoning effort. When a fallback answers, the result is marked as failed over. With no fallback set, a failure reaches you as an error rather than as a silent switch to some other model.

## Can I use a subscription I already pay for?

For coding, yes. Coding engines that have a sign-in of their own, such as Claude Code or Codex, connect by running that vendor's own login inside the engine's container. The vendor's program does the whole exchange, and your Host keeps the resulting sign-in so the engine stays connected. Privatae does not create that sign-in or go through a login page of its own. Other engines run on a key you add in the Keys app. [Coding agents](wiki:coding-agents) covers the engines.

The Host model and its agents run on API keys from the providers above, with one exception: a sign-in with xAI. In the Keys app, press **Sign in with xAI** on the xAI row and finish on xAI's own page; your Host can also start the same sign-in if you ask it to connect Grok. A Host routed to xAI then runs on that sign-in, so it uses the plan you already pay for. If your Host had no model yet, signing in also sets one from xAI that meets the Host model standard, and the Keys app tells you which. While you are signed in, your xAI API key, which bills per token, is not used at all. If the sign-in has expired or xAI turns it away, the Host tells you so rather than switching to the key; press **Reconnect with xAI** on the same row to sign in again. To use the key instead, press **Deactivate** on `harness:grok:auth` in the Vault section of the Keys app. The Orchestrator shows which one your Host is using.

## Where are my keys kept?

In the Keys app, sealed in your Host's own vault. Your Host uses a key by making calls with it. Showing a key in clear text takes a code from your authenticator, in the Keys app only. You can switch a key off without deleting it, or remove it.

If you paste a key into the chat by mistake, anything shaped like a known key or token is cut out of your message before it reaches the model or your Host's memory.

## Does Privatae bill for tokens?

No. Model calls run on your own keys and embeddings on the endpoint you choose, so Privatae is not in that billing path. What a provider charges you is between you and the provider. The [Compute app](wiki:compute-app) shows an estimate of it from token counts.

## Can I change models later?

Yes, at any time and per role. Your Host, its memory, its certificate and its rules stay where they are; only the route changes. Switching the embedding model keeps the vectors already made. [How a Host remembers](wiki:memory) covers what that means for search.
