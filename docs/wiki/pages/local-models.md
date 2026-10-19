---
title: Local models
summary: Model servers running on your own machine can serve your Host through the bastion's tunnel. Point your Host's memory at an embedding model on a machine of yours and the text is turned into vectors on your hardware; your Host can also manage which models sit on that machine, with each download approved there.
section: network
order: 40
questions:
  - Can my Host use a model running on my own computer?
  - How do I run my Host's embeddings on my own machine?
  - Can my Host download models onto my computer?
  - What happens if the machine with my model is turned off?
  - Can another Host use the models on my machine?
---

A machine of yours that runs a model server can serve your Host. The [bastion](wiki:the-bastion) connects your Host to the model servers already running on that machine, over the tunnel the machine dials. Use one for embeddings and the vectors for your Host's memory are worked out on your own hardware: the text goes to your machine, not to a model provider, and the vectors come back to be stored in your Host's database.

## Which model servers can a machine offer?

Two kinds: a local Ollama, which the bastion looks for on the machine, and, if you tell the bastion where one runs, a server that speaks the common OpenAI-compatible API. The bastion accepts these connections only from a Host whose pairing includes use of the machine's model servers, and it checks that on every connection. Each time the machine checks in, it tells your Host which models it is serving.

Using a model that is already loaded and choosing which models are on the disk are separate permissions. The first lets your Host use a model for embeddings. Only the second lets it add or remove one.

## How do I run embeddings on my own machine?

Embeddings turn text into numbers so your Host's memory can be searched by meaning. In the Network app, a machine that can serve embeddings shows **Serve my embeddings here**. Choose it and your Host sends the text it indexes to that machine's embedding model over the tunnel, and only the numbers come back.

A switch is tested before it is saved: the machine has to answer with a real vector first. Switching never deletes or rewrites the vectors your Host already has. Each model's vectors are kept side by side, so switching back is instant. Entries that only the previous model embedded are not found by meaning under the new one until they are embedded again.

## What if that machine is turned off?

Then embedding stops until the machine is back. When your Host is set to embed on your machine and the machine does not answer, your Host marks embedding as down. It does not quietly send the text somewhere else.

## Can my Host download models onto my machine?

Yes, if you let it. Managing models is its own kind of management grant, which you approve with your authenticator code. Even then, each download through the machine's model store waits for your approval at that machine, with the code from the authenticator enrolled there, and the approval covers one model, one size limit and one use. A download that turns out larger than you approved is stopped. A model counts as ready only after a test run, because a finished download proves only that bytes arrived. Your Host can remove only models it put there itself; a model you fetched for your own work is not its to drop. Without an authenticator enrolled on the machine, downloads are refused.

On a machine anchored to your Host, the bastion also refuses download and delete requests sent straight to Ollama, so the model store is the only way to change what is on disk. On an unanchored machine it does the same for any Host that does not hold model management.

## Can another Host use my machine's models?

On a machine several Hosts share, a Host other than the anchor uses its model servers only if the anchor's owner granted that when approving its pairing, and the grant can be limited to named models. The machine then lists and serves only those models to that Host. You can also switch model use off for any Host in the bastion's local console.

How a machine joins your network is on [the bastion](wiki:the-bastion) page. Choosing models and providers for your Host is covered in [models and keys](wiki:models-and-keys).
