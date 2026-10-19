---
name: artifacts
description: How the host makes and iterates ARTIFACTS — disposable live HTML/JS/CSS builds in its own workspace that the user watches in the Artifacts window. One artifact per design, written in the host's own turn, refined in place, never copied through a model. The desktop is an artifact too (see the desktop skill). Also VISUALS — a small page drawn right in a chat reply with a ```visual block, never saved unless the user clicks — and when to use which.
allowed-tools: artifact, open_app, publish_site, context
---

# Artifacts SOP

An artifact is a DISTINCT primitive — not an app and not a normal project: a
disposable, lightweight LIVE build (HTML/JS/CSS/SVG) the host writes into a
workspace, renders instantly in-pod, and either throws away or converts into a
real app. It is not bound to the App SDK until you convert it (no conflation
with "app"). A finished artifact also publishes directly as a live WEBSITE via
`operate(tool='publish_site', args={project_id:<artifact_id>, slug:...})` — no conversion needed for
sites; converting is only for the App Store lane.

## artifact — guidance

**The ops.** `create` makes a new artifact — name (optional: left out, the
kernel names it from the description, else the kind and the date, and the
result carries `name_defaulted`), optional files=[{path, content}] to seed
the workspace (include an index.html to make it renderable), entry (the entry
file, default index.html), description; returns {artifact_id, preview_url}. `write` writes/overwrites files in an
existing artifact — artifact_id + files; iterate on a live build. `render`
returns the live build as a self-contained HTML document (asset-inlined,
sandbox-safe). `list` shows the host's artifacts. `convert` turns an artifact
into an installed app via the App SDK — the workspace MUST already contain a
provenance=custom app.yaml (write it first with set_project_manifest), then
this packs+signs+installs it via the same ship path apps use. `delete`
disposes of an artifact (drops it + wipes its workspace). All artifacts are
strictly the host's own.

**The user sees artifacts in the Artifacts window** (a native desktop
window): after create/write, call `operate(tool='open_app', args={app_id:'artifacts',
view:'artifact:<id>'})` to put it on their screen — the window live-refreshes
on every write, so iterate freely while they watch.

**Make it yourself, in your own turn.** `op='list'` is a direct call; every
other op runs as `operate(tool='artifact', args={op: …, …})` — one
deterministic call, no agent, the result returns to you now. An artifact is a
sketch, not a build; do not dispatch an agent to make one — the user sees
nothing while an agent works, and an agent copying bytes you had already
written cost 40k–70k tokens per artifact.

**One artifact per design.** Refine with `op='write'` on the same
artifact_id — a second create of the same design is a duplicate, not an
iteration.

**Tune mode.** `[artifact N · path · "text"]` pills in the user's messages are
exact element references from their clicks — edit THAT element; "changed X →
Y" pills are edits they already applied. Give action buttons
`data-cseries-action='<kebab-id>'` attributes — clicks arrive as the user's own
message.

**Variants.** Write variant-*.html files alongside index.html for
alternatives; the window shows tabs and "Use this one" is an OPERATION — the
shell copies the pick over index.html itself (POST /promote), no message, no
model. If you are asked to promote one, `op='promote'` (artifact_id + from,
optional to) does the same copy server-side in one revision.

**Never copy a file by rendering it and writing its content back** — that
re-types the whole document through you (measured: 5 minutes and ~11k tokens
for one 37 KB copy). `op='promote'` and `op='import'` (artifact_id +
project_id + files=[{from, to?}] — pull a build's landed files out of a
project workspace) exist so no file ever passes through a model to move.

**Every artifact wears the desktop.** There is no per-project style: an
artifact takes the look of the desktop the user is running. When the
Artifacts window shows an artifact, the platform puts the desktop's token
sheet first in its `<head>` — every `--cseries-*` token (surfaces
`ink night twilight silk veil`, hairline `mist`, text `paper linen ash`,
accents `ember dusk`, and the optional ones in the desktop skill §4) plus
`--font-body`, `--font-display`, `--font-mono` and the `--radius-*` set — and
gives the page the desktop's background, text colour and type. So write
colours, type and radii as `var(--cseries-…)` / `var(--font-body)`, not
literal hex: the artifact then follows the desktop, including a desktop the
user switches to later. Anything you style yourself wins over those defaults,
so a literal value is still yours to choose when the user asks for a look of
its own. Publishing an artifact as a site freezes the look it has that day.

**A desktop in a website's look.** `op='desktop_from_url'` (url, optional
name) reads the site's palette and fonts with no model and creates a desktop
artifact in them — see the desktop skill §1.

**Taste signals.** Variant picks and kept artifacts are the user's taste
talking; the look itself comes from the desktop, so a change of look is a
change of desktop (desktop skill), never a per-artifact palette.

**Change history.** Every create/write/tune-edit is a revision — `op='history'`
lists them newest first, a page at a time (rev, ts, actor, note; `limit` +
`offset`, with total and next_offset; the user sees the same timeline in the
window), `op='revert'` (artifact_id + rev) restores one exactly; a revert is
itself recorded, so nothing is ever lost. Every revision keeps every file,
whatever its size, and every revision is kept; the oldest go only when the
disk cannot take the next one, and that revision says so (pruned_for_space).
`op='list'` pages the same way. When the user says "go back to how
it was before X", use history + revert instead of rewriting from memory.

**The desktop.** The whole shell is the design: the desktop surface, its bar
when it declares one (desktop skill §2a), and through its tokens the windows
and every app, come from ONE artifact of yours. `op='set_desktop'` (artifact_id; optional entry) makes it
the live desktop; omit artifact_id to restore the default. Iterate with
`op='write'` on the SAME artifact: the desktop re-renders while the user
watches. The floor is mandatory — `set_desktop` refuses (with a report) any
document missing it, and every write on a desktop document returns
`contract` so you fix it in the same turn. The floor, the tokens, the shell
API and the template are in the **desktop** skill (§2–§5): read it before
writing a desktop. Write it yourself in your turn (operate(tool='artifact',
args={op:'create', …}) then op='set_desktop') — never dispatch an agent to
design a desktop (the user sees nothing while it works, and every agent-made
desktop so far shipped as a stub). When the user asks for a new look: ask at
most one question, write it, set it, open the Artifacts window. Honour
prefers-reduced-motion; motion is cheap and runs all day.


## Visuals — a page in your reply

A VISUAL is a small page drawn right in your chat reply, in Circles and the
Workbench: a fenced block whose language is `visual`, optionally titled.

    ```visual title="Orbit demo"
    <!DOCTYPE html><html>…</html>
    ```

**Visual or artifact.** A visual is part of the answer — an animation, a
little diagram or chart, an interactive explainer that says it better than
words. It is NOT saved: it lives in that reply, and the user has Save as
artifact, Download, Copy and Full size on it (never saved
automatically). Build an ARTIFACT instead for anything the user will iterate
on or keep, or that is a design, mockup or page. Never create an artifact to
hold a visual; saving it is the user's click.

**How it is drawn.** The same sealed frame as an artifact: no network, so
inline every asset — no CDN libraries; write the SVG, canvas or CSS yourself.
It wears the user's desktop like an artifact does, so write colours and type
as `var(--cseries-…)` / `var(--font-body)`. The frame takes the page's height
(up to a limit, then it scrolls), so let the page grow with its content and
never set `100vh`. While your reply streams the user sees a placeholder; the
page runs once the block closes, so close it.

**Not a visual.** A plain ` ```html ` block stays source code — use it to SHOW
code, ` ```visual ` to show the result. A visual is a page you write; for a
picture, use the image tool. Visuals run only in the user's own Host's
replies; in a Peers or People thread they are shown as code.

## Parameter notes (full text of shortened argument descriptions)

- **artifact** `artifact_id`: The artifact's id (from create/list). Required for every op but create, list and a default-restoring set_desktop.
- **artifact** `files`: create/write: [{path, content}] workspace-relative. import: [{from, to?}] copied from the source project.
- **artifact** `kind`: create only: 'artifact' (default) or 'desktop'. With 'desktop' and no `files` the kernel seeds the entry from the platform's desktop template — the copy is server-side, you never retype it — and the result always carries `contract`, the desktop floor's verdict. Then `op='write'` your palette and layout onto it (skill 'desktop').
