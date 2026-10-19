"""
Applications runtime for cseries.

Apps are not HTML pages with ambient access to the server. An app is:

  1. A manifest declaring an id, entrypoint, and a finite list of scopes
     it is permitted to use.
  2. A window the user opens, which mints a short-lived token bound to
     exactly the scopes from that manifest.
  3. Every /api/* call from that window carries the token. Middleware
     verifies the token and rejects calls outside the declared scopes.

This is Phase 1 of the rearchitecture: the trust boundary moves from
"trust the iframe origin" to "trust only what the manifest declared."
The 17 existing apps each gain a scopes block in their app.yaml; their
runtime behavior does not change.

Later phases:
  Phase 2 — bundle / install / uninstall lifecycle.
  Phase 3 — sidecar runtime (apps as processes, not just HTML).
"""
