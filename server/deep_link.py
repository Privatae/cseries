"""The host's deep-link contract: a link that names the app to open.

    /?open=<app_id>[&view=<view>]

dashboard/base/js/deep-link.js is the reader. Every inbox row that points the
user somewhere writes its `payload.link` with app_link(), so every writer
produces the same shape, and a reach-out makes it absolute against the host's
own address (reachout/service.doorbell_link) because it is read outside the
host.

WHY THE QUERY AND NOT THE FRAGMENT. A reach-out is opened wherever the mail
lands — often a phone with no session on the host. The direct-router then
sends the browser to sign in and back, carrying the request's path AND QUERY
through `rd` (the router's no-session redirect builds
`/__host/session?rd=<path>` and safe_rd sends it back). A fragment is never
sent to a server, so it cannot ride `rd`, and the sign-in pages navigate by
script, which drops it — a fragment link opened signed-out lands on the bare
desktop. The query survives. Its cost is that the app id and view appear where
request paths are logged; both are identifiers, never content.

`view` is the app's own vocabulary and reaches the app through AppRunner.launch
exactly as the host's open_app tool passes one: `findings` / `finding:<id>` in
Oversight, `project:<id>` in the Workbench, `decision:<id>` (that older ask, in
full), `inbox` (At the door), `tickets` (Waiting on you), `support` and
`support:<id>` (a support draft, on the Support card), `thread:<id>` and
`card:<kind>:<id>` in Circles (circles-web/src/lib/cards.ts cardsTarget) — each
app's own reader of the view. An app that does not read views simply opens on
its first screen.

A `+` in a view is encoded as %2B, but the router's safe_rd unquotes `rd` a
second time, so across a sign-in it can arrive as a space. No view written
today contains one.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

# The manifest's own id rule (server/applications/manifest.py _ID_RE), so a
# link can only ever name an app that could be installed.
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def app_link(app_id: str, view: str | None = None) -> str:
    """`/?open=<app_id>[&view=<view>]`, percent-encoded.

    `:` is left readable because every view vocabulary uses it; everything else
    that is not unreserved is encoded — a `+` in a view becomes %2B, so the
    browser's query parser cannot read it back as a space.
    """
    if not _APP_ID_RE.match(app_id or ""):
        raise ValueError(f"not an app id: {app_id!r}")
    params = {"open": app_id}
    if view:
        params["view"] = str(view)
    return "/?" + urlencode(params, quote_via=quote, safe=":")
