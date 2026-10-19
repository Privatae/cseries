# The public wiki — how to write a page

The pages in `pages/` are published at `https://privatae.ai/wiki/<slug>`, where
anyone can read them and ask questions of them. Search engines index them, AI
answer engines (ChatGPT search, Perplexity, Claude, Google's AI answers) read
them to answer questions about Privatae, and every Host reads the same pages
through its `capabilities` tool. This file is the guide for writing them; it
is not a page and is not in the image.

## What a page may say

**What the product does and why, at the level of detail the published blog
already uses. Never how it is built inside.** Whatever a page says is public
forever, to every person and every model. A detailed architecture description
is reconnaissance material: the image's docs ratchet (`Dockerfile.linux`)
records two models reading an internal architecture doc and choosing their
target from it. The Ask box only ever sees these pages, so a page is the most
anything on the site can reveal.

Never on a page:

- the internal product name (legal), a person's name, internal agent names
- host names, addresses, regions, providers or counts of the fleet
- source paths, module or service names, tool names, API routes, config keys,
  environment variables, tool-call syntax, prompt text or skill text
- ticket, finding or design-doc references; dated decision notes
- security findings, open weaknesses, or which component holds which secret
  beyond what the blog already says
- prices and plan numbers — link to https://privatae.ai/pricing instead, so
  there is one place to change them
- anything on the platform's "Do not claim these" list (kept in the platform
  repo, not in this one)

`scripts/wiki_pages.py` has the leak scan that enforces the mechanical part of
this; the build fails on a hit. The rest is judgement, and review.

Keep the honest parts the blog keeps: we run the cluster the Host lives in, and
we can reach into it; a Host's memory lives in its own database in the cloud;
output comes from third-party models.

## How a page reads

- Plain words for a person who has never seen the product. Short.
- Open with the answer in one or two sentences. Then `##` headings phrased as the
  questions people ask ("## Can I use my own models?"): that is what search
  and answer engines match.
- Vocabulary: **Host** is the product noun (capital H). A computer is a
  **machine**, never "compute". The category is **an AI DMZ**. The **bastion**
  is the program on your own machine. App names as the dock shows them.
- Say what is true in this release. No roadmap, no "soon", no "planned".
- Link another page as `[text](wiki:slug)`; the check refuses a link to a page
  that does not exist. Link the site with full `https://privatae.ai/...` URLs.

## Front matter

A page file holds only what is published:

```yaml
---
title: What a Host is                # the page title
summary: One or two plain sentences. # search snippet; the Host's card `what`
section: host                        # start | host | network | building | apps | security | account
order: 10                            # position inside the section
questions:                           # questions this page answers, in a visitor's words
  - What is a Privatae Host?
app: workbench                       # only on a page that documents one app
---
```

What the page describes lives in `sources.yaml`, beside the pages, which is
never copied into the image — pods carry the explanations, not a map from each
one to the code behind it:

```yaml
what-a-host-is:
  sources:
    - server/network/nac_egress.py::resolve_exit
    - system-apps/network/app.yaml
    - external:hosted-platform
  verified:                          # written by --verify; never by hand
    date: '2026-09-30'
    digest: sha256:…
```

`sources` are what make the version stamp honest. Name the code whose change
would make the page wrong, narrowed to a symbol (`path.py::func`,
`path.py::Class.method`, decorators included) where you can, so an unrelated
edit to a big file does not flag the page. A source in another repo is
`external:<repo>/<path>`; a page citing one is published with its checked date
rather than as current for the release, because this repo's check cannot see
that code change. The digest covers the page's own text too: editing a page
un-checks it until it is verified again.

## Status

`status.json` is generated. Each page is:

- **current** — neither the page nor its sources changed since it was checked;
  published as checked against the release being built
- **changed** — the page or a source changed or vanished; published with its
  checked date and a note that it may be out of date
- **unchecked** — unchanged here, but it cites another repo; published with its
  checked date

## Workflow

```bash
# edit or add a page (and its entry in sources.yaml), then read the code it
# cites and fix what is wrong
python3 scripts/generate_wiki.py --root . --verify <slug>   # record the check
python3 scripts/generate_wiki.py --root . --check           # everything passes?
```

A code change that touches a page's sources makes `--check` report the status
file as out of date. Run `--write-status` and commit it: the PR then shows the
page going **changed**, which is the cue to re-read it and `--verify`.
