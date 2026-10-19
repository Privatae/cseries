# cseries-browser sidecar

First OCI-image sidecar in the cseries platform. Runs Chromium under
Xvfb + Playwright + a FastAPI control API. Ships on the on-demand
harness lane (its own scale-to-zero Deployment, reached through the
kernel proxy); the legacy in-pod placement beside the cseries container
still works.

## The first cut's scope (historical)

- Image builds
- Container starts cleanly
- `GET /health` returns `{ok: true, chromium_version: "..."}`
- `GET /screenshot` returns a PNG of `about:blank`
- noVNC at `:8831/vnc.html` shows a blank Chromium window (this lane
  is GONE — see "The live viewport" below)

The first cut deliberately did NOT include: FSA polyfill, cookie auth,
session lifecycle, multi-context support, Connections registration,
or any user-facing app surface. The polyfill and cookie auth landed in
Ships 3–4; sessions/multi-context are below.

## The general lane — `/v1/sessions`

Everything above drives ONE shared page (single context, the claude.ai
storage_state, the FSA polyfill bound into it). `control/sessions.py`
adds named, isolated sessions beside it for an in-pod caller — a
`runtime.kind=process` app under the user's own `custom` provenance —
that wants to drive a site it named.

Three differences from the shared page, each deliberate:

- **Own context, own storage_state, held in memory only.** A recipe
  signed into site A cannot read site B's cookies, and neither reads the
  claude.ai blob. Cookies are never written to disk — a site session
  cookie is a bearer credential, so it belongs in the sealed vault. The
  caller (holding `read:vault`/`write:vault`) unseals it, passes it as
  `storage_state` on create, and collects it back with `export_state` to
  re-seal. The sidecar is a cache; the vault is the store of record.
- **No filesystem.** Session contexts get no `__cseries_fs_*` bindings and
  no polyfill. The shared page's job is handing page JS a directory
  handle; a session's job is the opposite.
- **A declared blast radius.** `allow[]` is required at create time and
  enforced by a context route guard on DOCUMENT requests, so a redirect
  or link click cannot walk the session onto an undeclared host.
  Subresources are not filtered (that breaks ordinary sites and buys
  little — the guard already pins which site the page is).

There is no `eval` action: snapshot plus named actions only.

```bash
BASE=http://127.0.0.1:8830

curl -s $BASE/v1/sessions -X POST -H 'content-type: application/json' \
  -d '{"name":"acme","allow":["app.example.com"],"persist":true}'

act() { curl -s $BASE/v1/sessions/acme/act -H 'content-type: application/json' -d "$1"; }
act '{"action":"navigate","url":"https://app.example.com/login"}'
act '{"action":"snapshot"}'          # -> elements[] with refs e1, e2, …
act '{"action":"type","ref":"e3","text":"me@example.com"}'
act '{"action":"click","ref":"e5"}'
act '{"action":"wait_for_text","text":"Dashboard"}'
act '{"action":"export_state"}'      # hand the cookies back to seal
curl -s -X DELETE $BASE/v1/sessions/acme
```

Actions: `navigate snapshot click type type_secret press select scroll
wait wait_for_text text screenshot pages switch_page close_page downloads
export_state`. Refs
come from `snapshot` and die on navigation — a stale ref is a 409 asking
for a fresh snapshot, never a click on whatever moved into place. A
`window.open` popup (an OAuth login) becomes a new page in the same
context: `pages` lists them, `switch_page` makes one active.

`*.example.com` matches subdomains only, **not** the apex — list
`example.com` as well if you need both.

One action at a time per session: the calls are serialised on a
per-session lock, because two concurrent actions share one page.

### Downloads

The deliverable of an "export my statement" recipe. Playwright refuses
downloads unless asked and deletes the temp file when the context
closes, so the session accepts them and saves eagerly on the event to
`<state-root>/browser-downloads/<session>/` on the entity PVC — the same
volume the kernel mounts, so the Host can read what landed. `action:
"downloads"` lists them with paths and sizes. The filename comes from
the site and is sanitised before it is joined onto a path.

Downloads fire as a *page* event, not a context one, so every page gets
its own listener — including popups, which is often where an export
actually happens.

### Passwords — the vault types them, nobody else sees them

A login form is not an API call, so the vault's `http_call` cannot
perform one. `browser_type` is the third broker action: the caller names
the credential, the session and the ref, and the kernel opens the sealed
envelope and posts the bytes to this sidecar over pod loopback. Only
which-field-was-filled comes back. The secret is never in the caller's
request, the response, the audit row, or a model's context.

```bash
# the caller holds no password — just its name in the vault
POST /api/vault/secrets/bank-login/use
  {"action": "browser_type",
   "request": {"session": "bank", "ref": "e4",
               "expect_host": "online.bank.example"}}
```

Two guards sit under it. The sidecar refuses to type a secret into
anything that is not `input[type=password]` — the model still chooses
which element, and a hostile page can offer a convincing decoy, so
`allow_non_password` is a deliberate opt-out rather than a default. And
`type_secret` ignores `submit`: pressing Enter stays a separate, visible
step, so a secret type can never also be the keystroke that logs in.

Screenshots always mask `input[type=password]`, whether or not a secret
was typed — an image goes back to the caller and usually onward to a
model, where a rendered credential is invisible to every other guard.

### Egress

Two lanes, and which one is in use decides what a website sees.

**The NAC app lane (`NAC_EGRESS_LANE`, v125) — preferred when set.** The kernel's
egress gate on :13129, off-loopback so a harness pod on another node can reach
it. Traffic through it is matched against the user's SIGNED egress rules under
the subject this container authenticates as (`harness:browser`), so a rule
pinning that subject to one of the user's own machines sends these pages out
that machine's connection. That is what makes a bank see the user's home
address, and it is the reason some sites, banks among them, would not complete a
login before v125.

The credential is BASIC over a 407 challenge, not a header: Chromium has no flag
for a proxy credential and answers `407 Proxy-Authentication-Required` with Basic
and nothing else. So the sidecar hands Playwright
`proxy={"server", "username", "password"}` and Playwright fills the challenge —
username is the subject, password is the per-subject token the operator injects
(`NAC_EGRESS_SUBJECT_TOKEN`, minted by `harness_backend.egress_subject_token`
under a domain prefix distinct from `app_source_token`, so a leaked source token
cannot be replayed for egress). The gate strips `Proxy-Authorization` before
forwarding anything onward. A lane with no token falls back to squid rather than
407ing every request — a browser loading from the wrong address and saying so
beats a browser that loads nothing.

**The node squid (`HTTP_PROXY`/`HTTPS_PROXY`) — the fallback, and what ran
before.** Be precise about what it buys (verified from inside a live
sidecar): the node squid is a plain forward proxy — a pod-CIDR source ACL and an
internal-destination deny, no per-entity policy and no peer — so the browser's
page traffic leaves from the NODE's own public address (`HIER_DIRECT`), shared
with every other entity on that node, and not through the user's bastion.
Measured from a pool node: a full HTTPS fetch of google/example/github
completes in 100–240 ms that way, so page loads are never the slow part of the
viewport.

Either way it is a launch ARGUMENT, never the bare env: Chromium on Linux does
not reliably follow those variables, and relying on them is the bad failure —
traffic still flows, it just leaves by the pod's default route instead of the
user's, and nothing looks broken. NO_PROXY is comma-separated; Chromium's bypass
list wants its own form, so it is translated rather than passed through.
Loopback is always bypassed — the sidecar serves its own test page on 127.0.0.1
and must not proxy to itself.

Tuning (env): `BROWSER_MAX_SESSIONS` (3), `BROWSER_SESSION_IDLE_TTL_S`
(1800), `BROWSER_NAV_TIMEOUT_MS` (30000), `BROWSER_ACTION_TIMEOUT_MS`
(10000), `BROWSER_DOWNLOAD_ROOT`. A timer reaps idle sessions — it
closes the context but keeps the login in memory, so it gives the
browser's RAM back without costing a sign-in. The default of 3 is sized
against the entity container's measured 2Gi limit on the base plan;
raise it against the browser sidecar's own limit once the operator sets
one.

### What this does NOT defend against

- **Prompt injection.** Snapshot names and page text are attacker-
  controlled and go straight to a model that picks the next action. The
  allow-list bounds *where* a recipe can act, never what a page can talk
  it into doing on the host it is already on.
- **The plain `type` action** takes a literal string, so whatever goes
  through it is held by the caller. Fine for a search box, wrong for a
  password — that is what `type_secret` and the vault's `browser_type`
  broker action are for (below). Nothing prevents a caller from putting
  a password through `type`; the point is that it no longer has to.
- **The port, historically.** The control API once bound `0.0.0.0`
  with no auth. It now defaults to loopback (`startup/entrypoint.sh`)
  and carries a bearer gate (`BROWSER_CONTROL_TOKEN`, control/app.py);
  the harness deployment sets the wide bind and the token together so
  neither ships without the other.

Tests: `pytest tests/test_browser_sessions.py` (63 cases, no Chromium
needed — sessions.py does not import playwright). Note what they cannot
prove: the stub never executes the in-page JavaScript, so `_SNAPSHOT_JS`
and the ref mechanism are unverified until this runs against real
Chromium.

## The live viewport — `WS /viewport/ws`

The user-facing view of the browser. NOT noVNC: the x11vnc →
websockify → noVNC chain, its 8831 port and the browser-novnc sister
app were removed. Two things were wrong with that lane, one
fatal: the harness Service only routes the control port, so the
viewport had no delivery path at all in the shipped on-demand
deployment, and desktop capture streams a root window with
window chrome instead of the page the user cares about.

The replacement is Chromium's own screencast — the same mechanism
browser-infra products (Browserbase, browserless, Steel) use for their
live views. One WebSocket on the control port carries JPEG frames out
(CDP `Page.startScreencast`) and input back in (`Input.dispatch*`).
Flow control is CDP's own: no new frame until the previous one is
acked, and the sidecar acks only after the frame reaches the client
socket, so a slow viewer throttles the stream instead of growing a
queue. The client (`system-apps/browser/viewport.js`) paints onto a
canvas, follows popups (an OAuth window becomes a page tab), and
resizes the page viewport to fit its element.

**Frames are BINARY WebSocket messages (raw JPEG bytes) and the stream
is paced — since v0.5.0.** Measured on a pool node with
the v0.4.x wire shape (base64 JPEG inside a JSON text message): 11.9 fps
while scrolling a Wikipedia article, 228 KB per frame, 22 Mbit/s to the
viewer, and ~2.1 cores — two Chromium processes at ~74% and the control
API at ~65%, the latter almost entirely `json.dumps` over a
quarter-megabyte string per frame. Under the harness pod's 1-core limit
the container was throttled in 179 of 604 scheduler periods, which is
what "unusably slow" was. Binary frames drop the base64 third and the
JSON encode; the ack is held until `1/BROWSER_VIEWPORT_MAX_FPS` (default
12, the measured natural scroll rate) has elapsed, so a scrolled page is
not slowed and a video or spinner is capped. JPEG quality defaults to
60 (`BROWSER_VIEWPORT_JPEG_QUALITY`). Binary is OPT-IN: the client sends
`{"t":"binary","on":true}` on open; until it does, frames are the
`{"t":"frame"}` JSON shape every earlier client paints. So the sidecar
can be pinned before the kernel carrying the new viewport.js rolls, and
the new client still works against a v0.4.x sidecar (which ignores the
opt-in). The `hello` message carries `"binary": true` to say the
capability exists.

The frames go to the USER driving their own browser, never to a model
— the model-facing image surface stays `/screenshot` and the session
`screenshot` action, which mask password inputs.

Auth: the control API's bearer middleware is HTTP-scope only and never
runs for websockets, so `control/viewport.py` enforces the same
`BROWSER_CONTROL_TOKEN` itself, and the kernel proxy injects the
header on the upstream WS handshake (`serve_app_ws`).

## The desktop render check — `POST /v1/verify/document`

`control/document_verify.py`. The kernel sends a
complete page it composed itself — a desktop design mounted the way the
shell mounts it, in a `sandbox="allow-scripts"` srcdoc frame under the
shared sandbox CSP, with the stand-in `CSeriesDesktop`
(`server/applications/desktop_render.py`). This route loads it from
memory at a name that never resolves, aborts every other request, waits
for the frame's own `load` plus two of its animation frames, watches for
`settle_ms`, and reports: uncaught exceptions from every frame (message,
stack, the document's line) and console errors, both read over CDP; the
page CRASHING (`crashed`) or the frame NAVIGATING ITSELF after it loaded
(`navigated`) — each the document's doing, reported with every error
already seen, never a 500; the frame's pixels (how much differs from its
most common colour) and where its visible content is (`content`: text,
images, canvases, and how many pixels of each are drawn); whether the
document locked its renderer after painting (`unresponsive`); and per
probe how many elements match a selector — `attached` at all, visibly
`shown`, or `reachable` by a click at their centre (with what took the
click from the rest). The frame is found by its place and element in the
composed page (the main frame's only child, `iframe#<subject>`), never by
its window.name. Everything run inside the frame runs in a CDP isolated
world, so a document that replaces its own DOM methods cannot change what
is measured, and the browser runs without the automation flag
(`CHECK_BROWSER_ARGS`, in the shared launch), so `navigator.webdriver` is
false as in a person's browser — the document cannot tell the check from
the desktop. It does not judge — the desktop contract lives in the
kernel, so a contract change needs no new image. Unlike `/v1/verify` it
dials nothing: no kernel origin, no network.

A kernel that carries the render check and meets an OLDER sidecar gets a
404 here and reads it as "not checked", by name (measured against the
unmodified v0.5.2 image). Publishing a design refuses on "not
checked", so **ship this image before the kernel that calls it**: publish
it, pin `BROWSER_TAG` in the operator and roll the operator (the pin never
precedes the push), confirm the `harness-browser` Deployments carry the new
tag, and only then update the kernel.

The image is NOT in the operator's harness prepull DaemonSet, so a wake
on a node that has not cached it pulls ~487 MiB first. A wake on a node that holds it measured 10.4 s and
10.5 s end to end at the operator.

Tests: `pytest tests/test_desktop_render_check.py` drives this route in
real Chromium with the kernel's own page and judge; the same payloads
were run against the live v0.5.2 image with this module mounted
with identical verdicts.

## Ports

| Port | Bind     | Purpose                              |
|------|----------|--------------------------------------|
| 8830 | 127.0.0.1 in-pod; `::` on the harness | FastAPI control API + `/viewport/ws` (kernel proxy only) |
| :99  | (X)      | Xvfb virtual display                 |

## Build

The operator pins the harness deployment to `BROWSER_TAG`; ship a new
tag and bump that pin together.

```bash
cd <cseries checkout>/sidecars/browser
docker build -t cseries-browser:vX.Y.Z .
```

## Run (standalone)

The control API binds the container's loopback, so `-p 8830:8830` does
**not** reach it — Docker publishes to the container's external
interface, and nothing is listening there. That is deliberate (the API
has no auth). Two ways to drive it:

```bash
# 1. From inside the container — what the smoke gate does.
docker run -d --name browser-smoke cseries-browser:v0.3.0
docker exec browser-smoke /app/startup/healthcheck.sh && echo healthy
docker exec browser-smoke curl -s http://127.0.0.1:8830/v1/sessions
docker rm -f browser-smoke

# 2. Deliberately opened, for local development ONLY. Never in a pod.
docker run --rm -e CONTROL_BIND=0.0.0.0 \
  -p 127.0.0.1:8830:8830 \
  cseries-browser:v0.3.0
curl -s http://127.0.0.1:8830/health
# viewport: connect a WS client to ws://127.0.0.1:8830/viewport/ws
# (the real client is system-apps/browser/viewport.js via the kernel).
```

Standalone is the only way to exercise `/v1/sessions` against real
Chromium. The in-page JavaScript IS executed by the test suite —
`tests/test_browser_snapshot_js.py` runs the extracted scripts under node
against a synthetic DOM — but the Playwright calls around it are stubbed,
so nothing here has driven a real browser yet.

## Run (in entity pod)

`Entity.spec.browserSidecar: true`. The operator appends the container,
its emptyDirs (`$HOME`, `/tmp`, a Memory-medium `/dev/shm`) and the
`entity-apps` mount at `/var/lib/cseries`; setting the flag back to false
removes it. Default is false, so no entity runs it unless asked.

For a one-off Ship-1-style check without the operator, patch the pod
spec by hand:

```yaml
- name: browser
  image: cseries-browser:v0.3.0
  ports:
    - containerPort: 8830
      name: control
```

The cseries container reaches the sidecar on `127.0.0.1:8830` because
they share the pod network namespace.
