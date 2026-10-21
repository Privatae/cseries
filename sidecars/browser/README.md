# cseries-browser sidecar

The browser container for a C-Series Host: Chromium under Xvfb and openbox,
driven through Playwright by a FastAPI control API on port 8830. It runs in
its own container beside the Host, never inside it. On a Host's own machine
the local runtime (`vm/runtime`) starts it on demand, on the runtime's apps
network, with the `seccomp-browser.json` profile and a control token.

## The shared page

The API's top-level routes (`/screenshot`, `/navigate`, `/profile`,
`/grant-fs` and others in `control/app.py`) drive one shared page in one
browser context. The File System Access polyfill (`polyfill/fsa-bridge.js`)
is bound into that context, so page code can be handed a directory. The
shared page starts signed out; `/profile` exports or restores its login as a
Playwright `storage_state`, which the Host keeps sealed. Nothing of it is
written to the container's disk.

## Sessions — `/v1/sessions`

`control/sessions.py` adds named, isolated sessions for a caller that wants
to drive a site it named.

- **Own context, own login, in memory only.** A session signed into one
  site cannot read another's cookies or the shared page's. Cookies are never
  written to disk: the caller passes `storage_state` on create and collects
  it back with the `export_state` action to seal it in the vault.
- **No filesystem.** Session contexts get no `__cseries_fs_*` bindings and no
  polyfill.
- **A declared allow-list.** `allow[]` is required at create time and enforced
  by a route guard on navigation requests, so a redirect or a link cannot take
  the session to an undeclared host. Subresources are not filtered.
  `*.example.com` matches subdomains only, not `example.com` itself; a bare
  `*` is refused.

```bash
BASE=http://127.0.0.1:8830

curl -s $BASE/v1/sessions -X POST -H 'content-type: application/json' \
  -d '{"name":"acme","allow":["app.example.com"],"persist":true}'

act() { curl -s $BASE/v1/sessions/acme/act -H 'content-type: application/json' -d "$1"; }
act '{"action":"navigate","url":"https://app.example.com/login"}'
act '{"action":"snapshot"}'          # -> elements with refs e1, e2, …
act '{"action":"type","ref":"e3","text":"me@example.com"}'
act '{"action":"click","ref":"e5"}'
act '{"action":"wait_for_text","text":"Dashboard"}'
act '{"action":"export_state"}'      # hand the cookies back to seal
curl -s -X DELETE $BASE/v1/sessions/acme
```

Actions: `navigate snapshot click type type_secret press select scroll wait
wait_for_text text screenshot pages switch_page close_page downloads
export_state`. There is no `eval`. Refs come from `snapshot`; a ref whose
element is gone is a 409 asking for a fresh snapshot, never a click on
whatever moved into its place. A popup (an OAuth login, for example) becomes
a new page in the same context: `pages` lists them, `switch_page` picks one.
Actions on one session run one at a time.

When a control token is set, every route except `/health` needs it, as
`X-Browser-Token` or `Authorization: Bearer`.

### Downloads

A session accepts downloads and saves each one as it arrives, under
`BROWSER_DOWNLOAD_ROOT/<session>/` (default
`/var/lib/cseries/browser-downloads/<session>/`). The filename comes from the
site and is reduced to a safe name first. `action: "downloads"` lists them,
and `GET /v1/sessions/<name>/downloads/<filename>` returns one file, which is
how the Host collects it.

### Passwords

The vault's `browser_type` action types a stored secret into a session: the
caller names the credential, the session, the ref and the host it expects,
and the Host opens the secret and sends it to this sidecar's `type_secret`
action. Only which field was filled comes back. The secret is never in the
caller's request, the response or the audit record.

```bash
# the caller holds no password, just its name in the vault
POST /api/vault/secrets/bank-login/use
  {"action": "browser_type",
   "request": {"session": "bank", "ref": "e4",
               "expect_host": "online.bank.example"}}
```

`type_secret` refuses any target that is not a real `input[type=password]`
unless the caller passes `allow_non_password`, and refuses a contenteditable
element even then. It has no `submit` option, so pressing Enter is always a
separate step. A session's `screenshot` action always masks
`input[type=password]` in every frame.

### Egress

Chromium's proxy is set as a Playwright launch argument, not left to the
environment, because Chromium on Linux does not reliably follow the proxy
variables and has no flag for a proxy credential.

- **`NAC_EGRESS_LANE` with `NAC_EGRESS_SUBJECT_TOKEN`:** the Host's egress
  gate. The browser authenticates as its subject (`NAC_EGRESS_SUBJECT`, else
  `harness:browser`) with the token as the Basic password, and the owner's
  signed egress rules for that subject decide the way out. The gate removes
  the credential before forwarding. This is what the local runtime sets; it
  gives the browser no other proxy.
- **Otherwise `HTTPS_PROXY`/`HTTP_PROXY`,** a plain forward proxy, if set.
  A lane with no token falls back to this rather than failing every request.

`NO_PROXY` is translated into Chromium's bypass-list form, and loopback is
always bypassed.

### Tuning

Environment: `BROWSER_MAX_SESSIONS` (3; the local runtime sets 2),
`BROWSER_SESSION_IDLE_TTL_S` (1800), `BROWSER_NAV_TIMEOUT_MS` (30000),
`BROWSER_ACTION_TIMEOUT_MS` (10000), `BROWSER_DOWNLOAD_ROOT`. An idle session
is reaped: its context closes but its login stays in memory.

### What this does not defend against

- **Prompt injection.** Snapshot names and page text come from the site and go
  to a model that picks the next action. The allow-list bounds where a session
  can act, not what a page can talk it into on a host it is already on.
- **Secrets through `type`.** The plain `type` action takes a literal string.
  Nothing stops a caller from putting a password through it; `type_secret`
  and the vault's `browser_type` exist so it does not have to.

## The live viewport — `WS /viewport/ws`

The person's live view of the browser, on the control port. It is Chromium's
own screencast: JPEG frames go out (CDP `Page.startScreencast`) and mouse and
keyboard input comes back (`Input.dispatch*`). A frame is acknowledged to
Chromium only after it has been sent to the viewer, and no sooner than
`1/BROWSER_VIEWPORT_MAX_FPS` (default 12) after the previous one, so a slow
viewer slows the stream instead of growing a queue. JPEG quality is
`BROWSER_VIEWPORT_JPEG_QUALITY` (default 60).

Frames are JSON text messages (`{"t":"frame",...}`) until the client sends
`{"t":"binary","on":true}`; after that they are binary messages of raw JPEG
bytes. The `hello` message on connect carries `"binary": true` to say this
is supported. The client is `system-apps/browser/viewport.js`.

The frames go to the person driving the browser, not to a model. The
bearer middleware does not run for WebSockets, so `control/viewport.py`
checks `BROWSER_CONTROL_TOKEN` itself; the Host's app proxy adds the token on
the upstream handshake.

## The desktop render check — `POST /v1/verify/document`

`control/document_verify.py`. The Host sends a complete page it composed (a
desktop design mounted the way the shell mounts it, built by
`server/applications/desktop_render.py`). The route loads it from memory at a
`.invalid` address, aborts every other request, waits for the frame to load
and settle, and reports what happened: uncaught exceptions and console
errors, whether the page crashed or the frame navigated itself away, whether
it stopped responding, what it drew, and for each probe how many elements
match a selector (`attached`, `shown` or `reachable` by a click). Scripts run
in the frame use a CDP isolated world, so the document cannot change what is
measured, and Chromium runs without the automation flag, so
`navigator.webdriver` is false here as in a person's browser. The route
reports; the Host judges. It makes no network requests.

## Ports

| Port | Bind | Purpose |
|---|---|---|
| 8830 | `127.0.0.1` by default (`CONTROL_BIND`); `0.0.0.0` when the local runtime starts it, with a token | control API and `/viewport/ws` |
| :99 | X display | Xvfb |

## Build and run

From the repository root:

```bash
docker build -t cseries-browser:dev sidecars/browser
```

To try it on its own, the way the release workflow checks it:

```bash
docker run -d --name browser-smoke --network none --cap-drop ALL \
  --user 1000:1000 --shm-size 256m \
  --security-opt seccomp="$PWD/seccomp-browser.json" cseries-browser:dev
docker exec browser-smoke /app/startup/healthcheck.sh && echo healthy
docker exec browser-smoke curl -s http://127.0.0.1:8830/health
docker rm -f browser-smoke
```

The control API listens on the container's loopback by default, so a
published port does not reach it. To reach it from the machine for local
development, set `CONTROL_BIND=0.0.0.0` and a `BROWSER_CONTROL_TOKEN`, and
publish to `127.0.0.1` only.

Tests: `tests/test_browser_sessions.py` (sessions, with Playwright stubbed),
`tests/test_browser_snapshot_js.py` (the in-page scripts under node, against
a synthetic DOM) and `tests/test_desktop_render_check.py` (the render check
in real Chromium, skipped where none is installed).
