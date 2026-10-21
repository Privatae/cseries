# app-web-kit — shared layer for the React system apps

Code shared by the React apps (`circles-web`, `workbench-web`,
`community-web`, `sites-web`) so each one does not keep its own copy. Main
pieces:

- `api.ts` — `ApiResult`, `request` (retries a 401 briefly while the app
  token arrives) and `getJSON`.
- `layout.tsx` — `TwoPane` and the pane primitives around it.
- `ui/` — the shadcn components and a few shared overlays.
- `utils.ts` — `cn()`, `timeAgo()` and other small helpers.
- `effort.ts` — `EffortOptions`, the shape of the effort-options route's answer.
- `book/` — the parts of the book view that Circles and the Workbench share:
  its rules, the page count, hold-to-think, the nudge line, the colour
  scheme, and its stylesheet (colours from the desktop tokens) and fonts.
- `visuals/`, `ask.ts`, `close-turn.tsx` and the other top-level modules —
  conversation pieces Circles and the Workbench both render.
- `test/` — `node --test` tests (the book rules, the crisis notice, the
  licence plugin), run by `circles-web`'s and `workbench-web`'s `npm test`.
  A module a test loads must use only relative imports, with the `.ts`
  extension, so Node can load it without the app's build.
- `place.mjs` — copies a built `dist/` into `system-apps/<id>/`
  (`node ../app-web-kit/place.mjs <id>`).
- `verify-node-modules.mjs` — refuses a build whose `node_modules` does not
  match `package-lock.json`; every app's `ship` script runs it first.
- `third-party-licenses.mjs` — the Vite plugin each app's `vite.config.ts`
  uses to write `assets/THIRD-PARTY-LICENSES.txt`: every npm package compiled
  into the bundle, with its version, declared licence and licence/notice texts.

Apps import it through the `@kit` alias (`resolve.alias` in `vite.config.ts`
and `paths` in `tsconfig.json`). Files here may import `@/lib/utils`: `@`
resolves inside the importing app, whose `src/lib/utils.ts` re-exports this
kit's `utils`, so the shadcn convention keeps working unmodified.

Tailwind: each app's `src/index.css` declares `@source "../../app-web-kit";`
so classes used only in kit files still reach the generated CSS.

Adding an app: add the `@kit` alias and the `@source` line, and import what
you need. Do not copy this directory into an app.
