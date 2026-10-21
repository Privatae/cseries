# Getting Started

The first-run walkthrough: a short set of slides, one action each. An app
slide has one button that opens the real app; when that app's window closes
(or the user clicks back into the tour) the slide's live check runs, and a
pass moves to the next slide. Every app slide can be skipped, and "Leave the
tour" is always there.

The shell opens it on boot until the tour is marked done, by finishing it or
leaving it (`dashboard/base/js/walkthrough-autolaunch.js`). It can be reopened
from the dock at any time; a step already done on the Host passes on its own.

## Files

- `steps.js` — `GS_SLIDES`: every slide's words and its check.
- `app.js` — the engine: render a slide → open the app → on the shell's
  `window:closed` event (which carries `appId`, `dashboard/base/js/desktop.js`)
  or window focus, run the check → a pass advances, "not yet" keeps the slide.

## State

Progress is stored in the appearance settings:

- read: `GET /api/appearance` → `desktop_layout.walkthrough`
- write: `PUT /api/appearance/layout` with only `{walkthrough: {done, step, steps}}`

## Permissions

The `read:` scopes in `app.yaml` back the slides' checks, and
`write:appearance` covers saving progress. `scripts/test_app_manifest_scopes.py`
checks that the manifest covers every `endpoint:` in `steps.js`.

## Distribution

The bundle ships inside the image (its id is in
`scripts/image_baked_apps.txt`), but it is not kernel-required. On every
change, bump `version` in `app.yaml` and re-pack with
`scripts/rebuild_factory_bundles.sh`.
