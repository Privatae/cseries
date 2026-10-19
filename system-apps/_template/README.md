# C-Series App Template

Copy this directory to create a new system app. Every app follows the same shape; design work happens in `app.js` (logic) and occasionally in `style.css` (LAYOUT only, never colors/type/motion).

## Create a new app

From the repository root:

```
cp -r system-apps/_template system-apps/<your-id>
```

Then edit:

1. **`app.yaml`** — set `id`, `name`, `description`, window size, permissions.
2. **`index.html`** — replace the `TEMPLATE` slug with your `id` in the four href/src paths. Fill in real content.
3. **`style.css`** — layout adjustments only. DO NOT add colors, font-families, font-sizes (outside the --text-* scale), radii, or durations. If you need one, it goes in `dashboard/base/css/tokens.css` first.
4. **`app.js`** — your app's logic.
5. **Icons** — swap `icon.png` (256×256) and `icon-64.png` (64×64) for your app's artwork. PNG with transparency. Shield or emblem style matches Privatae Host.

## Rules

The design system, in short:
- Use the tokens. Never hex, never raw px for spacing.
- Use the component classes. `cseries-btn`, `cseries-pill`, `cseries-card`, `cseries-nav`, `cseries-nav-btn`, `cseries-row`, `cseries-empty`, `cseries-seam`.
- Small-caps labels on everything persistent (`.label-sys` or `.cseries-nav-btn`).
- One Threshold Seam under the header, never more.
- No bounce, no glow, no gradient backgrounds, no decoration.

## File layout

```
_template/
├── app.yaml              # manifest — id, name, icon, window, permissions
├── index.html            # app shell; loads tokens.css + cseries-components.css + local style.css
├── style.css             # per-app layout (not colors/type)
├── app.js                # app client logic
├── icon.png              # 256×256 app icon (desktop + launcher)
├── icon-64.png           # 64×64 smaller icon (in-window header, taskbar)
└── README.md             # (this file — delete when copying to a real app)
```

## API access

Apps run in a same-origin sandboxed iframe and can call:

- `/api/system/*` — status, entity info
- `/api/files/*` — file gateway (CEIGAS-gated)
- `/api/backup/*` — PG backup
- `/api/projects/*` — app projects
- `/api/ceigas/*` — CEIGAS policy, signals, incidents
- Plus any routes your app's permissions list grants access to.

## Testing

1. Copy the template.
2. Edit.
3. Rebuild the image: `/pc-update v0.2.X`
4. On your PC, run a fresh FDA cycle → new `docker run` → reload the dashboard.
5. Your app's icon appears on the desktop + in the launcher.

Do not stage files outside the platform. No SCP. No /tmp drops. Use the flows the platform already has.
