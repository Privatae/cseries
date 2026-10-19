# Getting Started

The first-run walkthrough: six slides, one action each. An app slide has
one button that opens the real app; when that app's window closes (or the
user clicks back into the tour) the slide's live check runs, and a pass
moves to the next slide. Skip is always there; nothing is a dead end.

Auto-launches on boot until the user finishes or leaves the tour (see
`dashboard/base/js/walkthrough-autolaunch.js`). Reopen from the dock any
time; it picks up where things stand — a step already done on the host
passes on its own.

## Files

- `steps.js` — `GS_SLIDES`: every word and every check. Reorder freely.
- `app.js` — the engine: render a slide → open the app → on the shell's
  `window:closed` (carries `appId`, `dashboard/base/js/desktop.js`) or
  window focus, run the check → pass advances, "not yet" keeps the slide.

## State

The shell-trusted appearance lane, unchanged from v1:

- read: `GET /api/appearance` → `desktop_layout.walkthrough`
- write: `PUT /api/appearance/layout` with ONLY `{walkthrough: {done, step, steps}}`

## Permissions

Each scope backs one declared check; `scripts/test_app_manifest_scopes.py`
proves the manifest covers every `endpoint:` in `steps.js`.

## Distribution

Image-baked (category 2 — NOT kernel-required): the id is in
`scripts/image_baked_apps.txt`. Bump `version` in `app.yaml` and re-pack
with `scripts/rebuild_factory_bundles.sh` on every change.
