# C-Series App Template

Copy this directory to start a new system app. Logic goes in `app.js`;
`style.css` holds layout only, never colours, type or motion.

## Create a new app

From the repository root:

```
cp -r system-apps/_template system-apps/<your-id>
```

Then edit:

1. **`app.yaml`** — set `id` (lowercase letters, digits and hyphens), `name`,
   `description`, `version`, the `window` size and `permissions`.
2. **`index.html`** — replace `TEMPLATE` with your `id` in the three
   `/system-apps/TEMPLATE/...` paths (style, header icon, script). Fill in
   real content.
3. **`style.css`** — layout adjustments only. Do not add colours, font
   families, font sizes outside the `--text-*` scale, radii or durations. If
   you need one, it goes in `dashboard/base/css/tokens.css` first.
4. **`app.js`** — your app's logic.
5. **Icons** — replace `icon.png` (256×256) and `icon-64.png` (64×64) with
   your app's artwork, PNG with transparency.

## Rules

The design system, in short:
- Use the tokens. Never hex, never raw px for spacing.
- Use the component classes: `cseries-btn`, `cseries-pill`, `cseries-card`,
  `cseries-nav`, `cseries-nav-btn`, `cseries-row`, `cseries-empty`,
  `cseries-seam`.
- Small-caps labels on everything persistent (`.label-sys` or
  `.cseries-nav-btn`).
- One seam (`cseries-seam`) under the header, never more.
- No bounce, no glow, no gradient backgrounds, no decoration.

## File layout

```
_template/
├── app.yaml              # manifest: id, name, icons, entrypoint, window, permissions
├── index.html            # app shell; loads tokens.css, motion.css, cseries-components.css, then style.css
├── style.css             # per-app layout (not colours or type)
├── app.js                # app logic
├── icon.png              # 256×256 app icon
├── icon-64.png           # 64×64 icon (the in-window header uses it)
└── README.md             # this file; delete it in a real app
```

## API access

An `/api` route either requires a permission or is listed, with its reason,
in `server/applications/route_policy.py`. A call to a route whose permission
the app's `app.yaml` did not declare is refused. `server/applications/scopes.py`
is the full list of permissions. Declare only what the app uses.

## Building

`scripts/rebuild_factory_bundles.sh` packs and signs every app under
`system-apps/` (except `_template`) into `factory-bundles/`. Only the ids
listed in `scripts/image_baked_apps.txt` are also copied into
`native-bundles/` and ship inside the image.
