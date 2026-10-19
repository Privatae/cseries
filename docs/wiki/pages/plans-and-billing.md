---
title: Plans and billing
summary: You pay for capacity, measured as resident memory, and spread it across as many Hosts as you like. Hosts themselves are free, model tokens are never billed, and a Host you give no memory sleeps and costs nothing. Prices are on the pricing page.
section: account
order: 10
questions:
  - What do I pay for on Privatae?
  - Do I pay per Host or per seat?
  - Does Privatae charge for AI tokens?
  - What happens to a Host with no memory allocated?
  - What happens if my Hosts are given more memory than I pay for?
  - Is there a free trial?
  - What happens if I cancel?
  - How much does Privatae cost?
---

You pay for capacity: an amount of resident memory, which is how much your Hosts can hold open at once. You buy it once for your account and divide it among your Hosts however you like. Hosts themselves cost nothing, model tokens are never billed, and a Host you give no memory sleeps. For current prices, see [pricing](https://privatae.ai/pricing).

## What am I paying for?

Memory, at a flat rate per gigabyte. Storage and a monthly allowance of sandbox time, the time your coding engines' machines spend running for you, come with each gigabyte and are included in the same price. The rate depends on where the memory runs: on shared machines, or on machines reserved for you.

Some usage is metered on top of that, such as sandbox time beyond what your capacity includes. It is measured as you use it and added to your next monthly bill. The pricing page lists what is metered and at what rate.

## Do I pay per Host or per person?

Neither. You can run one Host with all of your memory or many Hosts sharing it, and the bill is the same. There is no per-seat charge: the memory you hold already limits how much can run at once.

## What is never billed?

- **Model tokens.** Privatae does not bill for inference or embeddings. Model calls run on your own keys or accounts, with the providers you choose. See [Models and keys](wiki:models-and-keys).
- **Your Hosts' own network traffic.** It is measured, but not billed.
- **Hosts.** Creating another Host does not change what you pay.

## What happens to a Host with no memory?

It sleeps. You can set any Host's memory to zero to park it, and its memory goes straight back to your pool for your other Hosts. A parked Host keeps its disk and its data, and does not start again until you give it memory.

Memory and storage behave differently. Memory can be moved between Hosts at any time. Storage is a disk, so once a Host holds it, it stays with that Host until the Host is deleted.

## What if my Hosts are given more memory than my capacity?

They run smaller, never beyond what your capacity covers right now. Each Host runs at its share of your capacity, in proportion to what you gave it. If there is not room for every Host at the smallest size a Host can run at, the ones given the least are paused. Your split is not changed: once your capacity can hold them again, for example after you add capacity or take memory back from a Host, they return to full size by themselves. Your account page says when this is happening and why.

## Is there a free trial?

No. Instead, cancelling refunds the unused part of your billing period automatically, so a first month traps no money.

## What happens if I change my mind?

You can grow your capacity at any time, and the change is pro-rated. Shrinking credits the unused time to your next bill, and cancelling refunds it. Cancelling is a control in your account on privatae.ai. When a subscription ends, its Hosts stop, and their data is kept for the period set out in the [Terms](https://privatae.ai/terms) so that resubscribing can restore it.

## Where do I manage this?

In your account on privatae.ai, where you buy capacity, see how it is divided, and set each Host's memory.
