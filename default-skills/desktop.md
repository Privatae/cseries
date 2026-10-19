---
name: desktop
description: How to design the user's whole machine — the desktop surface, its own bar, and through its design tokens the windows and every app — as ONE self-contained HTML document with a desktop layout AND a phone layout. The mandatory floor (set_desktop refuses anything less), the phone layout and how it is checked, every capability the shell gives the document, every token it reads, and a complete working template to start from. No mocks, no stubs, no dead buttons, never an agent, never a screenshot, never an external asset.
allowed-tools: artifact, open_app
---

# Desktop SOP — "make my desktop look like …"

On Privatae Host the shell is one design surface. The document you write IS the
desktop — and, when it declares one, the BAR: the shell hides its own taskbar
and your document draws the launcher, the dock and the rest (§2a). The tokens
it declares dress everything above it: windows, menus, every app. So this is
a design task, and the whole machine is the deliverable.

The `artifact` tool does all of it. Its description carries this contract;
this file is the same recipe for the person reading it in the Skills app and
for any coding engine that receives it in a build prompt.

## 1. The flow (one conversation, live on screen)

1. **Ask at most one question**, and only if the vibe is genuinely unclear
   ("calm and minimal, or busy and alive?"). A one-line request is enough.
   **"Make it look like <website>"** → `artifact op='desktop_from_url'` with
   the `url` (and a `name` if the user gave one). The kernel fetches the page
   and its stylesheets, reads the site's background, text, accent and fonts
   with no model, and creates the template in that palette — already passing
   the floor. The result says what it took from the site (`taken_from_the_site`)
   and which values it had to derive; tell the user in a line, then go on from
   step 3 with the returned `artifact_id`. It is not the desktop until you
   `set_desktop` it.
2. **Seed the template as an OPERATION.** `artifact op='create'` with
   `kind='desktop'`, a name after the vibe, and NO `files` — the kernel copies
   the §5 template into `index.html` itself. You never retype it, and what
   lands already passes the floor. Switching a shell is an op, not a model
   rewriting a file.
3. **Then make it yours:** `artifact op='write'` on that `artifact_id`,
   changing the palette, the type, the layout and the ambient motion — keep
   every widget real. `create` and every `write` return `contract`, the
   floor's verdict on your document. Fix anything under `errors` before step
   4. (Writing the whole document at `create` still works — pass
   `files=[{path: 'index.html', content: …}]` — but seeding first gives you
   the template's working widgets for free.)
4. `artifact op='set_desktop'` with that `artifact_id`. The shell mounts it
   under every window and recolours itself from your tokens. **Refused?** The
   result says exactly what is missing. Fix the document; set it again.
   It goes on the user's phone too (§2b). Before it is set, the platform
   LOADS the document in a real browser the way the shell mounts it (a
   stand-in `CSeriesDesktop` with this Host's apps), at desktop size and then
   at phone size, and refuses one that is BROKEN at either: it throws an
   uncaught error, or inside a
   `CSeriesDesktop.on` callback — the refusal names the error, the line and
   that line's text — crashes its page, reloads or navigates itself away,
   shows nothing at all (one colour, no visible text, image or canvas), has
   nothing carrying `data-cseries-app` in 'desktop' mode, covers every visible
   tile with something else, or declares `cseries-bar` and draws no bar. Tiles
   a menu reveals, an auto-hiding bar and a slow fade-in are fine — the check
   asks what the shell asks. A result with a `NOT RENDER-CHECKED` warning
   means no browser could be asked: it WAS set, unlooked-at — say so rather
   than calling it checked. A version that FAILED stays refused, browser or
   not, until a browser passes it.
5. `operate(tool='open_app', args={app_id:'artifacts', view:'artifact:<id>'})` so the user can tune
   it by pointing, pick between variants, and go back in history.
6. Every later change is `artifact op='write'` on the SAME artifact — the
   desktop re-renders within seconds and each write returns `contract`.
   A write to the desktop the user is WEARING is loaded in a real browser
   first. Their screen moves to it on a pass, or — when no browser can be
   asked — with a `NOT RENDER-CHECKED` warning you pass on; never when it
   fails. The result's `desktop` says which. `advanced: false` means the
   screen stayed on the last good version and `message` names the error and
   its line — fix it in this turn; the file itself keeps your edit. A
   version worn unchecked is checked once a browser answers and taken off if
   it fails. Your appearance line says what is on screen each turn.
   Alternatives are `variant-*.html` files; the user's pick is an operation
   the window performs itself ("Use this one" → the kernel copies it over
   index.html). You never copy a file by reading it and writing it back.
7. "Put it back" / "normal desktop" = `artifact op='set_desktop'` with NO
   `artifact_id`. Settings → Appearance offers "Host design" beside the
   ordinary swatches, so the user can switch by hand.
8. **"Send it to the design app"** = `artifact op='publish_design'` with a
   `name` (and `artifact_id`, or leave it out for the desktop being worn). The
   kernel renders it, checks the floor and loads it in a real browser (the
   same render check as step 4 — and if no browser can be asked, nothing is
   published; try again shortly); the PLATFORM picks the shelf — a
   design from one of the platform's own Hosts goes on "Ours" in the Designs
   app for everyone, anyone else's on their own shelf. Say which one the
   result reports. Every desktop you make already shows under "Yours" there,
   published or not.

**This conversation is the place.** Design a desktop right here in the chat
— never create a project for it (a Workbench project for a desktop is an
empty project the user has to clear away).

**Write it yourself, in your turn.** Never dispatch an agent to design a
desktop: the user sees nothing while it works, and every agent-made desktop
so far shipped as a stub with invented apps.

## 2. The floor — mandatory, or `set_desktop` refuses

1. **Mode**, in static `<head>` markup:
   - `<meta name="cseries-desktop" content="desktop">` — YOU draw the app tiles;
     the shell hides its icon column and presence line.
   - `<meta name="cseries-desktop" content="wallpaper">` — a live backdrop; the
     shell keeps drawing its own icons over it. Draw no tiles of your own.
2. **Tokens**, in static `<head>` markup, declaring AT LEAST these eleven:
   `--cseries-ink --cseries-night --cseries-twilight --cseries-silk --cseries-veil`
   (surfaces, dark → light) · `--cseries-mist` (hairlines) ·
   `--cseries-paper --cseries-linen --cseries-ash` (text, strong → muted) ·
   `--cseries-ember --cseries-dusk` (accent fill, accent text).
   `<meta name="cseries-tokens" content="--cseries-ink:#0a0e0f; --cseries-night:#0e1416; …">`
   Values: hex, `rgb()/rgba()/hsl()/hsla()`, CSS colour names (`white`,
   `rebeccapurple`), numbers with units, or font stacks. Nothing that reaches
   out (`url()`, `var()`). The desktop's own palette and these tokens are ONE
   palette, **and the floor now MEASURES that palette**, in every colour form
   it accepts — naming the eleven is not enough:
   - **Refused:** `--cseries-paper` below 4.5:1 against `--cseries-ink`,
     `--cseries-night` or `--cseries-twilight`, or `--cseries-linen` below 3:1
     against `--cseries-night`. The refusal names the pair and the measured
     ratio, so you fix it in the same turn. This is not a style rule: the shell
     paints windows from `--cseries-night` and their text from `--cseries-paper`,
     so inverting them is black text on black everywhere.
   - **Warned:** the five surfaces `ink → night → twilight → silk → veil` not
     running one way. The shell stacks them in that order, so look at the
     taskbar and a window after `set_desktop` — a base lighter than the chrome
     is a real choice, it just has to be the one you meant.
   Only a value carrying no colour at all — a font stack, a length, `var()` —
   is skipped. A translucent BACKGROUND is skipped too, because what shows
   through it is not in the document; translucent text over an opaque surface
   is composited and measured.
3. **Real apps** (desktop mode): tiles come from `CSeriesDesktop.apps` at
   runtime with `data-cseries-app="<id>"` on each, or from
   `<div data-cseries-apps></div>`. A literal list of `{id, name, …}` objects
   is refused — you do not know the user's apps; the shell does.
4. **No external assets.** The frame has no network. System fonts, CSS
   gradients, inline SVG, canvas, and **your own images**: name one by its
   `/api/images/<id>` (an `image_generate` result, a Blender Engine render, a
   PhotoCraft Engine composite) in `src` or `url(...)`, and the platform puts
   the image itself into the document every time it renders it. Keep the mode
   meta and the tokens first in `<head>`: they are read from the first 64 KB,
   and an image takes room.
5. **No mocks, stubs, or dead controls.** Every button does a real thing or
   does not exist. Do not draw a context menu, fake windows, fake mail, fake
   music, fake notifications, fake balances — the shell draws the real
   windows and menus, in your tokens. The bar is yours only when you declare
   it (§2a); otherwise the shell draws its taskbar and you draw none.

## 2a. The bar — declare it, then draw all of it

`<meta name="cseries-bar" content="bottom 52">` in static `<head>` markup: the
edge (`top` `bottom` `left` `right`) and the thickness in px (28–120). Needs
`desktop` mode. The shell then hides its taskbar and keeps every window out of
that strip (maximized ones included); your document, full-screen underneath,
draws the bar there. Declaring it means drawing what the taskbar did — real,
every piece:

- **Required — a way back to every open window.** The dock:
  `CSeriesDesktop.dock` (`{id, name, icon, pinned, running, focused}`, in the
  shell's order) with `data-cseries-activate="<id>"` on each item — a click
  opens, focuses or minimizes, a right-click is the shell's pin / close menu —
  or `<div data-cseries-dock></div>`. Windows that belong to no app come from
  `CSeriesDesktop.windows` (`{id, app_id, title, icon, focused, minimized}`) with
  `data-cseries-window="<id>"`.
- **Required — a launcher.** `data-cseries-launcher` on a button.
- **Warned without — show desktop.** `data-cseries-show-desktop`;
  `CSeriesDesktop.desktopShown` says whether windows are held down.
- **Yours to place:** `data-cseries-help` (the shell's help), the host
  (`CSeriesDesktop.host`) and `CSeriesDesktop.machine` (the memory line). Put
  `data-cseries-machine` on the memory line (or call `CSeriesDesktop.openMachine()`)
  and a click opens the shell's machine panel — memory, each app, every
  process. The shell draws that panel; the process list never enters your
  document, so do not try to build one.
- **Only while `CSeriesDesktop.ownsBar` is true.** On a phone the shell keeps its
  own taskbar and `ownsBar` is false — draw nothing then, or there are two bars.

Leave `cseries-bar` out and the shell keeps its taskbar, dressed in your tokens.

Warned (mounts, but fix it in the same turn): tiles in wallpaper mode (two
icon sets), `href="#"` / `onclick="return false"`, placeholder text,
`CSeriesDesktop` used without `.on()` (a desktop that never updates).

## 2b. The phone — the same document, laid out for a phone

A desktop you set is worn on the user's phone too: `set_desktop` moves the
phone onto it (the user can still pick a different phone wallpaper in
Settings → Appearance, and your appearance line says when they have). A
`write` to the desktop they are wearing does not move the phone; when they
want the design on their phone and the appearance line says it shows its own
wallpaper, `set_desktop` the same artifact again. On a
phone the shell mounts the SAME document, with three differences:
`CSeriesDesktop.phone` is true (re-sent through `.on()` when the screen crosses
the phone boundary), the shell keeps its own taskbar below your document
(`ownsBar` is false — draw no bar), and every window opens full-screen over
you. Your document is the phone's home screen. Give it a phone layout, not a
shrunken desktop:

- **Switch on `.phone`, inside `.on()`.** Toggle a class (the template's
  `body.phone`) and write the phone layout under it. `.phone` is the shell's
  own definition of a phone — portrait or landscape, and a narrow window — so
  do not invent your own breakpoint for the switch. Inside it,
  `@media (orientation: landscape)` covers a phone on its side.
- **Fit 360 px wide.** That is the narrowest width common phones report; a
  layout that fits it fits the wider ones. Nothing keeps a desktop's fixed
  pixel positions: a centred row of fifteen 52 px tiles is 920 px wide, and on
  a 360 px phone eight of them sit wholly past the edges.
- **Every tile inside the width.** Wrap the tiles into a grid — four columns
  is a phone's home screen. When there are more apps than the screen holds,
  let the grid scroll DOWN (`overflow-y:auto` on the grid, with the page
  itself a column so the grid gets the height that is left), never sideways
  off the screen. A drawer that slides the tiles in on a tap is fine: make it
  `visibility:hidden` while it is closed (and visible when it opens) — a
  closed drawer only moved off the screen still counts as tiles past the edge.
- **Essentials first, smaller.** The greeting, the clock and the presence
  line stay, compact, at the top; ornaments that only fill a wide screen go.
- **Touch, not hover.** A phone has no hover: names stay visible (shorten
  them with an ellipsis rather than hiding them), no hover-only controls, no
  hover lift, and tap targets at least 44 px.
- **On its side.** A phone in landscape is about 360 px tall: put the header
  beside the tiles instead of above them.
- **Same palette, same idea.** It is one design on two screens — the tokens,
  the colours and the ambient motion carry over; only the layout changes.

**How it is checked.** `set_desktop`, a write to the desktop the user is
wearing, and `publish_design` load the document a second time as a phone, at
360×640 with `CSeriesDesktop.phone` true. It is REFUSED there for what refuses
it at desktop size — it throws, shows nothing, has no tile, or something
covers every tile — with "on a phone" in the reason. Tiles that sit past the
side edges of the phone screen are WARNED (the result's `render.warnings`,
and the write's `desktop.warning`): every app can still be opened from the
shell's own launcher, so it is not refused, but it is a layout to fix in the
same turn.

## 3. Every capability the shell gives your document

`window.CSeriesDesktop` is injected before any of your scripts run.

**Three members depend on the mode you declared.** `.apps`, `.all` and
`.presence` are filled for a `desktop` document, which is the one that draws
the icons. In a `wallpaper` they are empty (`[]`, `[]`, `""`), because the shell
keeps drawing the icon column and the presence line on top of a wallpaper — so
a wallpaper is not handed an inventory it cannot use. Do not write a wallpaper
that reads them.

| member | what it is |
|---|---|
| `.apps` | desktop mode only. the user's desktop apps, their order: `{id, name, description, icon, running, focused}`. `icon` is inline markup — an SVG glyph, an `<img>` data URL, or `<span class="sk-app-letter">` — insert it as-is and size all three with CSS |
| `.all` | desktop mode only. every installed app, `{id, name}`; any may be launched |
| `.host` | the host's display name |
| `.presence` | desktop mode only. the one-line "what the host is doing" sentence |
| `.phone` | true on a phone, portrait or landscape: the shell keeps its taskbar and windows fill the screen. Switch to your phone layout on it (§2b) |
| `.reducedMotion` | the user's prefers-reduced-motion |
| `.mode` | the mode you declared |
| `.on(cb)` | runs `cb(state)` as soon as state has arrived and on every change (an app opens or closes, focus moves, the presence line moves, the desktop set changes, an app installs). Draw inside it. Returns an unsubscribe |
| `.launch(id)` | launch an app — only within 1 s of a real click/key in the frame |
| `.openLauncher()` | open All Apps — same gesture rule |
| `.render(el)` | draw the shell's default grid into `el` |
| `.ownsBar` | true while your document draws the bar (declared `cseries-bar`, desktop mode, not a phone) |
| `.dock` | bar owners only: the dock, `{id, name, icon, pinned, running, focused}` |
| `.windows` | bar owners only: open windows, `{id, app_id, title, icon, focused, minimized}` |
| `.desktopShown` | bar owners only: show-desktop is holding windows down |
| `.machine` | bar owners only: the memory line, e.g. `1.2G of 4G` |
| `.activate(id)` / `.focusWindow(id)` / `.toggleDesktop()` / `.help()` / `.openMachine()` | bar owners only; same gesture rule as `.launch` (or use the `data-cseries-*` attributes) |

Any element with `data-cseries-app="<id>"` launches that app on a trusted
click. `<div data-cseries-apps></div>` is filled by the shell with
`button.sk-app[.is-running][.is-focused] > .sk-app-icon + .sk-app-label` and
restyled by your CSS. Right-click anywhere is the shell's own menu; Escape
closes it. Nothing else crosses the frame — no fetch, no tokens, no data.

**Widgets that are real today**, built from the above: clock / date /
greeting · the host's name and presence · the app grid with running and
focused state · the bar (dock, windows, launcher, host, machine, help,
show-desktop) · ambient motion
that honours reduced-motion · phone-aware layout.

## 4. Every token the shell reads (declare what you change)

Required (§2): `ink night twilight silk veil · mist · paper linen ash · ember dusk`.

Optional — each of these is read by the shell; nothing else is:

| token | dresses |
|---|---|
| `--cseries-mist-strong` | strong hairlines (focused window border) |
| `--cseries-bright` | the lit accent (logo mark) |
| `--cseries-moss` `--cseries-clay` `--cseries-gold` | success · danger · warning |
| `--cseries-ember-soft` `--cseries-ember-ring` | accent fills and rings (active dock pill, selected icon) |
| `--cseries-seam` `--cseries-seam-active` | the hairline seams under headers and across the taskbar |
| `--cseries-glass` `--cseries-glass-blur` | taskbar and menu glass (use rgba) and its blur (e.g. `18px`) |
| `--cseries-glow` | accent glow (running dots, the logo) |
| `--cseries-tile-bg` `--cseries-tile-bg-2` | the desktop icon tile gradient, light and dark edge |
| `--cseries-icon-size` | the shell's desktop icon tile (e.g. `56px`) |
| `--cseries-font-body` `--cseries-font-display` `--cseries-font-mono` | type, everywhere |
| `--cseries-taskbar-h` `--cseries-window-head-h` | chrome heights (e.g. `52px`, `36px`) |
| `--cseries-radius-sm/-md/-lg/-xl` | corner radii, everywhere |

## 5. The template — passes the floor as written; every widget is real

Start here. Change the palette (in BOTH the tokens meta and the `:root` block
— one palette), the type, the layout and the ambient motion. Keep the app
grid, the bar, the launcher and the presence line wired exactly as
they are: that wiring is what makes it a desktop and not a picture of one.

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="cseries-desktop" content="desktop">
<meta name="cseries-bar" content="bottom 52">
<meta name="cseries-tokens" content="--cseries-ink:#0a0e0f; --cseries-night:#0e1416; --cseries-twilight:#131b1d; --cseries-silk:#1a2427; --cseries-veil:#223034; --cseries-mist:#263135; --cseries-mist-strong:#3b4c51; --cseries-paper:#e5e7eb; --cseries-linen:#9ca3af; --cseries-ash:#6b7280; --cseries-ember:#4f7f6a; --cseries-dusk:#7fcfae; --cseries-bright:#9be3c2; --cseries-glass:rgba(14,20,22,0.72); --cseries-glow:rgba(127,207,174,0.45); --cseries-font-body:Inter, system-ui, sans-serif; --cseries-font-display:Inter, system-ui, sans-serif; --cseries-radius-md:6px; --cseries-taskbar-h:52px">
<title>Threshold</title>
<style>
  /* ONE palette: these are the same values as the cseries-tokens line above. */
  :root {
    --ink:#0a0e0f; --night:#0e1416; --twilight:#131b1d; --silk:#1a2427; --veil:#223034;
    --mist:#263135; --paper:#e5e7eb; --linen:#9ca3af; --ash:#6b7280;
    --ember:#4f7f6a; --dusk:#7fcfae; --bright:#9be3c2; --glass:rgba(14,20,22,0.72);
    --cseries-dusk:#7fcfae;            /* recolours the shell's glyph accents */
    --font:Inter, system-ui, sans-serif;
  }
  html, body { margin:0; height:100%; overflow:hidden; background:var(--ink); color:var(--paper);
    font:14px/1.45 var(--font); user-select:none; -webkit-user-select:none; }
  canvas#sky { position:fixed; inset:0; width:100%; height:100%; display:block; }

  /* Header: greeting, clock, the host's presence — all live. */
  header { position:fixed; left:48px; top:40px; right:48px; display:flex; align-items:flex-end;
    justify-content:space-between; gap:24px; pointer-events:none; }
  h1 { margin:0; font-weight:500; font-size:34px; letter-spacing:-.01em; }
  #presence { margin:6px 0 0; color:var(--linen); font-size:13px; letter-spacing:.04em; }
  #clock { font-size:44px; font-weight:300; letter-spacing:-.02em; line-height:1; text-align:right; }
  #date { color:var(--linen); font-size:13px; text-align:right; margin-top:6px; letter-spacing:.04em; }

  /* The app grid — every tile is a real installed app, drawn from CSeriesDesktop.apps. */
  .apps { position:fixed; left:48px; top:150px; right:48px; bottom:120px;
    display:grid; grid-template-columns:repeat(auto-fill, 112px); grid-auto-rows:112px;
    gap:14px; align-content:start; }
  .tile { position:relative; display:flex; flex-direction:column; align-items:center; justify-content:center;
    gap:8px; border:1px solid var(--mist); border-radius:14px; cursor:pointer; color:var(--linen);
    background:linear-gradient(160deg, rgba(34,48,52,.82), rgba(14,20,22,.88));
    transition:transform .18s ease, border-color .18s ease, color .18s ease; padding:0; font:inherit; }
  .tile:hover { transform:translateY(-3px); border-color:var(--ember); color:var(--paper); }
  .tile:focus-visible { outline:2px solid var(--dusk); outline-offset:2px; }
  .tile .ic { width:44px; height:44px; display:grid; place-items:center; }
  .tile .ic svg, .tile .ic img { width:36px; height:36px; }
  .tile .ic .sk-app-letter { font-size:22px; font-weight:600; }
  .tile .nm { font-size:11px; letter-spacing:.08em; text-transform:uppercase; }
  .tile .dot { position:absolute; bottom:8px; left:50%; transform:translateX(-50%); width:5px; height:5px;
    border-radius:50%; background:transparent; }
  .tile.is-running .dot { background:var(--dusk); box-shadow:0 0 6px var(--dusk); }
  .tile.is-focused { border-color:var(--dusk); }

  /* The bar — this document draws it (cseries-bar above: bottom, 52 px) and the
     shell keeps windows out of that strip. Every control is the shell's own:
     the launcher, the dock (pinned + running apps), shell windows, the host,
     the machine, help, show-desktop. Hidden whenever the shell keeps its own
     taskbar (a phone), so there is never a second bar. */
  #bar { position:fixed; left:0; right:0; bottom:0; height:52px; display:none; align-items:center; gap:6px;
    padding:0 12px; background:var(--glass); border-top:1px solid var(--mist);
    backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px); font-size:12px; color:var(--linen); }
  body.owns-bar #bar { display:flex; }
  #bar button { font:inherit; color:inherit; background:none; border:0; cursor:pointer; border-radius:8px;
    height:40px; min-width:40px; display:inline-grid; place-items:center; padding:0 8px; }
  #bar button:hover { background:var(--silk); color:var(--paper); }
  #bar button:focus-visible { outline:2px solid var(--dusk); outline-offset:1px; }
  #launch svg { width:18px; height:22px; }
  #dock, #wins { display:flex; align-items:center; gap:2px; min-width:0; overflow:hidden; }
  #dock button { position:relative; }
  #dock button span.ic { width:26px; height:26px; display:grid; place-items:center; }
  #dock button span.ic svg, #dock button span.ic img { width:22px; height:22px; }
  #dock button .ic .sk-app-letter { font-size:14px; font-weight:600; }
  #dock button.on::after { content:""; position:absolute; left:50%; bottom:3px; width:4px; height:4px; margin-left:-2px;
    border-radius:50%; background:var(--dusk); box-shadow:0 0 6px var(--dusk); }
  #dock button.focus { background:var(--veil); color:var(--paper); }
  #wins button.min { opacity:.6; }
  .grow { flex:1; }
  #who { display:flex; align-items:center; gap:8px; padding:0 8px; white-space:nowrap; }
  #who i { width:7px; height:7px; border-radius:50%; background:var(--dusk); box-shadow:0 0 6px var(--dusk); }
  #machine { white-space:nowrap; letter-spacing:.04em; }
  #machine[hidden] { display:none; }
  #showdesk.on { color:var(--dusk); }

  /* Phone (CSeriesDesktop.phone, desktop skill §2b). The same document, laid
     out for a phone: the shell keeps its own taskbar below this frame, so no
     strip is kept for a bar. The page becomes a column — a compact header,
     then the tiles in four columns filling the width; when there are more
     apps than the screen holds, the grid scrolls down, never off the side.
     Turned on its side, the header moves beside the tiles. No hover lift:
     a touch screen has no hover. */
  body.phone { display:flex; flex-direction:column; }
  body.phone header { position:static; margin:20px 20px 14px; flex-direction:column; align-items:flex-start; gap:8px; }
  body.phone h1 { font-size:22px; }
  body.phone #clock { font-size:30px; text-align:left; }
  body.phone #date { text-align:left; margin-top:4px; }
  body.phone .apps { position:static; flex:1; min-height:0; margin:0 14px 14px; overflow-y:auto;
    grid-template-columns:repeat(4, minmax(0, 1fr)); grid-auto-rows:84px; gap:10px; }
  body.phone .tile .nm { max-width:100%; padding:0 4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    font-size:10px; letter-spacing:.04em; }
  body.phone .tile:hover { transform:none; }
  @media (orientation: landscape) {
    body.phone { flex-direction:row; }
    body.phone header { width:34%; flex:none; margin:20px 8px 20px 24px; }
    body.phone .apps { margin:14px 14px 14px 0; grid-template-columns:repeat(auto-fill, minmax(76px, 1fr)); }
  }
</style>
</head>
<body>
<canvas id="sky" aria-hidden="true"></canvas>
<header>
  <div><h1 id="greet">Good day.</h1><p id="presence"></p></div>
  <div><div id="clock">--:--</div><div id="date"></div></div>
</header>
<div class="apps" id="apps" role="list"></div>
<nav id="bar" aria-label="Taskbar">
  <button id="launch" type="button" title="All apps" aria-label="All apps" data-cseries-launcher>
    <svg viewBox="-0.5 -0.5 17 21" aria-hidden="true"><rect x="0" y="0" width="16" height="4" rx="1.5" fill="none" style="stroke:var(--ember)"/><rect x="0" y="8" width="16" height="4" rx="1.5" style="fill:var(--bright)"/><rect x="0" y="16" width="16" height="4" rx="1.5" fill="none" style="stroke:var(--ember)"/></svg>
  </button>
  <div id="dock"></div>
  <div id="wins"></div>
  <span class="grow"></span>
  <span id="who"><i></i><span id="who-name"></span></span>
  <button type="button" id="machine" title="Your machine — what is running" data-cseries-machine></button>
  <button type="button" title="Help" aria-label="Help" data-cseries-help>?</button>
  <button id="showdesk" type="button" title="Show desktop" aria-label="Show desktop" data-cseries-show-desktop>
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="1.5" y="2.5" width="13" height="9" rx="1.5"/><path d="M5 14h6"/></svg>
  </button>
</nav>
<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  // ── Clock + greeting (real time, ticks each minute) ──────────────────
  function tick() {
    var d = new Date(), h = d.getHours();
    $("clock").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    $("date").textContent = d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
    var name = CSeriesDesktop.host ? " " + CSeriesDesktop.host + " is here." : "";
    $("greet").textContent = (h < 5 ? "Late night." : h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening.") + name;
  }
  tick(); setInterval(tick, 15000);

  // ── Apps, the bar, presence — from the shell, re-drawn on change ──────
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  // Rewrite a region only when its markup changed: the shell re-posts state on
  // every window and dock event, and replacing a button between press and
  // release loses the click.
  var drawn = {};
  function put(id, html) { if (drawn[id] !== html) { drawn[id] = html; $(id).innerHTML = html; } }
  function draw(S) {
    document.body.classList.toggle("phone", !!S.phone);
    document.body.classList.toggle("owns-bar", !!CSeriesDesktop.ownsBar);
    $("presence").textContent = S.presence || "";
    put("apps", S.apps.map(function (a) {
      return '<button class="tile' + (a.running ? " is-running" : "") + (a.focused ? " is-focused" : "") +
        '" type="button" role="listitem" data-cseries-app="' + esc(a.id) + '" title="' + esc(a.description || a.name) + '">' +
        '<span class="ic">' + (a.icon || "") + "</span><span class=\"nm\">" + esc(a.name) + '</span><span class="dot"></span></button>';
    }).join(""));
    // The dock: pinned apps and running ones — a click opens, focuses or
    // minimizes; a right-click is the shell's pin / close menu.
    put("dock", S.dock.map(function (a) {
      return '<button type="button" class="' + (a.running ? "on" : "") + (a.focused ? " focus" : "") +
        '" data-cseries-activate="' + esc(a.id) + '" title="' + esc(a.name) + '"><span class="ic">' + (a.icon || "") + "</span></button>";
    }).join(""));
    // Shell windows that belong to no app (the dock covers app windows).
    put("wins", S.windows.filter(function (w) { return !w.app_id; }).map(function (w) {
      return '<button type="button" class="' + (w.minimized ? "min" : "") + '" data-cseries-window="' + esc(w.id) +
        '" title="' + esc(w.title) + '">' + esc(w.title) + "</button>";
    }).join(""));
    $("who-name").textContent = CSeriesDesktop.host || "";
    $("machine").textContent = S.machine || "";
    $("machine").hidden = !S.machine;
    $("showdesk").classList.toggle("on", !!CSeriesDesktop.desktopShown);
    tick();
  }
  CSeriesDesktop.on(draw);

  // ── Ambient sky: one canvas, few particles, honours reduced motion ─────
  // ONE loop, ever: rafId is the running guard. motion() is called on every
  // shell state post (window open/close/focus, presence, layout), so without
  // the guard each post would start another permanent parallel loop.
  var c = $("sky"), ctx = c.getContext("2d"), pts = [], W = 0, H = 0, still = false, rafId = 0;
  // The sky is drawn in the palette above (read once), so a new palette
  // recolours it with no edit here.
  var css = getComputedStyle(document.documentElement);
  function tone(name, a, fallback) {
    var v = (css.getPropertyValue(name) || fallback).trim(), m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
    if (!m) return v;
    var h = m[1].length === 3 ? m[1].replace(/./g, "$&$&") : m[1];
    return "rgba(" + parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4, 6), 16) + "," + a + ")";
  }
  var glow0 = tone("--dusk", 0.08, "#7fcfae"), glow1 = tone("--dusk", 0, "#7fcfae"), dot = tone("--paper", 0.55, "#e5e7eb");
  function size() { W = c.width = innerWidth * devicePixelRatio; H = c.height = innerHeight * devicePixelRatio; }
  function seed() { pts = []; for (var i = 0; i < 90; i++) pts.push({ x: Math.random() * W, y: Math.random() * H,
    r: (Math.random() * 1.4 + .4) * devicePixelRatio, v: (Math.random() * .08 + .02) * devicePixelRatio, a: Math.random() * Math.PI * 2 }); }
  function frame() {   // draws one frame; never schedules
    ctx.clearRect(0, 0, W, H);
    var g = ctx.createRadialGradient(W * .5, H * 1.15, 0, W * .5, H * 1.15, H * .9);
    g.addColorStop(0, glow0); g.addColorStop(1, glow1);
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = dot;
    for (var i = 0; i < pts.length; i++) { var p = pts[i];
      if (!still) { p.y -= p.v; p.a += .01; if (p.y < -4) { p.y = H + 4; p.x = Math.random() * W; } }
      ctx.globalAlpha = .35 + Math.sin(p.a) * .25; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.283); ctx.fill(); }
    ctx.globalAlpha = 1;
  }
  function loop() { rafId = 0; frame(); if (!still) rafId = requestAnimationFrame(loop); }
  function motion() {
    still = CSeriesDesktop.reducedMotion || (matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    if (still) { if (rafId) { cancelAnimationFrame(rafId); rafId = 0; } frame(); }
    else if (!rafId) rafId = requestAnimationFrame(loop);
  }
  addEventListener("resize", function () { size(); seed(); if (still) frame(); });
  size(); seed(); motion();
  CSeriesDesktop.on(motion);
})();
</script>
</body>
</html>
```

Everything the user sees here is real: the clock is the clock, the greeting
carries the host's name, the presence line is the host's, every tile is an
installed app with its running state, the bar's dock is the shell's dock
(pinned and running apps), its launcher opens the real launcher, the memory
line is the machine's, show-desktop holds the real windows down, and the
`cseries-tokens` line carries the same palette into every app window. The kernel pins this
template against the floor (`scripts/test_desktop_contract.py`).

## 6. Quality bar

- **One idea per desktop.** A single strong visual concept, carried all the
  way. Not five effects.
- **Tiles stay legible and reachable.** Names visible or one hover away; hit
  targets at least 40 px; running apps visibly marked; on a desktop screen the
  whole desktop set fits without scrolling — wrap into columns when there are
  many apps. On a phone the grid may scroll down (§2b).
- **Motion is cheap and honest.** One canvas, a few hundred particles at most,
  `requestAnimationFrame`, and it stops under reduced-motion.
- **Readable over the scene.** Text over motion gets a plate or a shadow.
- **A real phone layout.** Read `.phone` and lay the page out for a phone
  (§2b): 360 px wide, every tile inside the width, essentials at the top, no
  hover-only affordances, and a landscape arrangement too.
- **The whole machine matches.** After `set_desktop`, look at the taskbar and
  open a window: if they look like a different design, your tokens are
  incomplete or your palette and your tokens disagree.

## 7. What NOT to do

- Do not make a screenshot or a picture of a desktop: no image of windows,
  tiles, buttons or a bar standing in for the real ones. The desktop is a
  document. Artwork in it is welcome: a rendered backdrop, textures and
  illustrations from your own images, under the live document (§2 rule 4).
  For an image alone as the wallpaper, with the shell's own icons over it,
  `operate(tool='artifact', args={op:'set_wallpaper', image_url:'/api/images/<id>'})` wears it.
- Do not build it with an agent. Write it yourself, in your turn, and iterate.
- Do not draw a context menu, or fake windows and widgets. The shell owns
  the real ones and wears your tokens.
- Do not draw a bar you have not declared with `cseries-bar` — the shell's
  taskbar is still there, and two bars is a bug. Declared, draw all of it
  (§2a) and only while `CSeriesDesktop.ownsBar` is true.
- Do not hard-code app ids or names. The shell knows the user's apps; you do
  not.
- Do not leave the phone with the desktop's layout. A row or ring of tiles
  placed in fixed pixels for a wide screen runs off a phone; switch on
  `.phone` and lay it out again (§2b).
- Do not copy a file by reading it and writing it back. `promote` and
  `import` exist so no file ever passes through you to move.
- Do not load anything from the network. It will not arrive, and the design
  will look broken.
