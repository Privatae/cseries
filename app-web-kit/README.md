# app-web-kit — shared layer for the React system apps

One copy of the code the four React apps (circles-web, workbench-web,
community-web, sites-web) used to duplicate:

- `api.ts` — ApiResult + request with the app-token 401-retry + getJSON.
- `layout.tsx` — TwoPane (master-detail / stack phone modes) + pane primitives.
- `ui/` — the shadcn components (were byte-identical copies in circles + workbench).
- `utils.ts` — cn(), timeAgo().
- `effort.ts` — EffortOptions, the effort-options route's answer.
- `book/` — the book of pages' parts that belong to no one app: the rules
  (rules.ts: the instants, the light rule, the pages the Host is working on,
  each app supplying the reader from a running turn's key to its page, and
  what changed since the person looked), the running count and "Where this
  stands" (page-count.ts, page-top.tsx), hold-to-think and its press, the
  nudge line and the rules for a nudge's held copy (nudge.ts, which Circles'
  conversation and the Workbench's page both use), the colour reading
  (scheme.ts), and the look (book.css, every colour a desktop token) with its
  fonts. The tabs, the page turn, the riffle, the Index and the book's own
  layout stay in workbench-web, the one app that draws them. The pure .ts
  files import each other with relative `.ts` paths so the apps'
  `node --test` can load them; their tests are in `test/`, which both
  workbench-web's and circles-web's `npm test` run.
- `place.mjs` — puts a built dist/ into system-apps/<id>/ (`node ../app-web-kit/place.mjs <id>`).
- `third-party-licenses.mjs` — the Vite plugin each app's vite.config uses to
  write assets/THIRD-PARTY-LICENSES.txt: every npm package compiled into the
  bundle, with its version, declared licence and full licence/notice texts.

Apps consume it via the `@kit` alias (vite.config resolve.alias + tsconfig
paths). Files here may import `@/lib/utils` etc. — `@` resolves inside the
importing app, whose lib/utils re-exports this kit's utils, so the shadcn
convention keeps working unmodified.

Tailwind: each app's src/index.css declares `@source "../../app-web-kit";`
so classes used only in kit files still reach the generated CSS.

Adding an app: alias `@kit`, add the `@source` line, re-export what you need.
Do NOT copy this directory into an app.
