---
title: Local models
summary: Your Host can run on a model server on a machine that runs a bastion, the computer your Host runs on included, through that bastion, with nothing to set up. A model server on another computer is used once you give your Host its address. Your Host can also manage which models sit on a bastion's machine, with each download approved there.
section: network
order: 40
questions:
  - Can my Host use a model running on my own computer?
  - How do I run my Host's embeddings on my own machine?
  - Can my Host download models onto my computer?
  - What happens if the machine with my model is turned off?
  - Can another Host use the models on my machine?
---

Your Host can use a model server on your own hardware: Ollama, or a server that speaks the common OpenAI-compatible API, such as llama.cpp, vLLM or LM Studio. On a machine that runs a bastion, the computer your Host runs on included, the bastion finds the server and your Host reaches it through the bastion, with nothing to set up. A server on another computer joins once you give your Host its address. Either way its models join the ones your Host can choose from, and the text you send that model goes to your own hardware, not to a model provider.

## How do I add a model server?

In the Orchestrator app, under **Model servers on your network**, type the server's address and press **Add**. Your Host asks the server which models it serves, and asks again within half a minute of each answer, so the list follows what is running now. A server that does not answer adds no models, and a role set to one of its models tells you nothing on your network serves it rather than waiting on a dead address. You can add up to ten servers.

An address you add has to be on another computer. Your Host runs in a virtual machine that is walled off from the computer it runs on, so a model server on that same computer cannot be reached by address. It is reached through the bastion on that computer instead, as the next answer but one describes.

## Can my Host's memory search run on my own hardware?

Yes. Embeddings turn text into numbers so your Host's memory can be searched by meaning. On a machine that runs a bastion, the computer your Host runs on included, your Host reaches an embedding model the same way it reaches a chat model: through the bastion, on Ollama or on an OpenAI-compatible server such as llama.cpp. A Host with no embedding model set up starts using one it recognises there by itself. For a server on another computer, ask your Host to use it for its embeddings, naming the server's address and the embedding model. Either way the switch is tested before it is saved: the server has to answer with a real vector first, and one that cannot is refused.

Switching never deletes or rewrites the vectors your Host already has. Each model's vectors are kept side by side, so switching back is instant. Entries that only the previous model embedded are not found by meaning under the new one until they are embedded again.

## What if that machine is turned off?

Then that model stops answering until the machine is back. Memory search marks embedding as down and does not quietly send the text somewhere else. For the Host model, only a fallback provider you chose in the Orchestrator answers instead; with none set, you get an error.

## Can my Host use the model servers on a machine that runs a bastion?

Yes, for chat, for memory search and for a PII sterilizer set up there. The bastion finds a model server on its own machine: one whose address is set there for the bastion, or else one answering on that machine's own ports 8080 (llama.cpp), 8000 (vLLM) or 1234 (LM Studio), and Ollama's 11434. It reports the models to your Host, and your Host asks the bastion for that server by name over its connection. Your Host never names an address: the bastion reaches only a server on the computer it runs on and refuses one on another computer. On a machine several Hosts share, a Host reaches the models only if it holds model use there, and only the models it was granted. With no key or sign-in for any provider, your Host uses such a model on the computer it runs on, or on a machine it is in charge of, until you choose another.

## Can my Host download models onto my machine?

Yes, if you let it, through the bastion on that machine. Managing models is its own kind of management grant, which you approve with your authenticator code. Even then, each download through the machine's model store waits for your approval at that machine, with the code from the authenticator enrolled there, and the approval covers one model, one size limit and one use. A download that turns out larger than you approved is stopped. A model counts as ready only after a test run, because a finished download proves only that bytes arrived. Your Host can remove only models it put there itself; a model you fetched for your own work is not its to drop. Without an authenticator enrolled on the machine, downloads are refused.

On a machine anchored to your Host, the bastion also refuses download and delete requests sent straight to Ollama, so the model store is the only way to change what is on disk. On a machine anchored to no Host, the pairing itself is the permission: a Host paired there counts as holding model management, so that second check does not stop it, unless you switched model use off in the bastion's local console.

## Can another Host use my machine's models?

On a machine several Hosts share, a Host other than the anchor holds model use only if the anchor's owner granted that when approving its pairing, and the grant can be limited to named models. The machine then lists only those models to that Host. You can also switch model use off for any Host in the bastion's local console.

How a machine joins your network is on [the bastion](wiki:the-bastion) page. Choosing models and providers for your Host is covered in [models and keys](wiki:models-and-keys).
