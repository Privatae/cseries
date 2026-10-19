---
name: tools-reference
description: Parameter notes for tools that have no skill of their own — the full text of every argument description that was shortened in the tool schema (2026-09-06 registry slimming). Read the tool's schema for what it does; read this for the detail behind an argument.
allowed-tools: context, list_skills
---

# Tools reference


## Parameter notes (full text of shortened argument descriptions)

- **harness_costs** `phase_id`: Optional: what one phase of a plan cost — the unit the user approved. Compares, one-shot runs and merges are not part of a phase's spend.
- **harness_transcript** `run_id`: The run NUMBER the user sees in the Workbench runs log and says out loud (harness_run.id). Preferred address; alone it is sufficient.
- **local_grep** `path`: Optional subtree under /var/lib/cseries/ to narrow the search (e.g. 'apps' or 'cc-sessions'); paths outside that root are denied.
- **harness_transcript** `session_id`: The compare session id. Only needed when you do not have a run_id; must be paired with harness.
- **image_generate** `action`: generate = create image(s) from a prompt (default); look = your own model sees pictures (images, question); gallery = list recent images.
