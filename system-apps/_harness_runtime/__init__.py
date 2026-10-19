"""_harness_runtime — shared runtime library for the coding-harness sidecars.

NOT an app (no app.yaml; the bundle packer skips this dir). The five vendor
harness mains (codex / cursor / grok / opencode / kimi) are thin shims over
harness_core.py + their HarnessSpec row in specs.py; claude_creds.py is the one
implementation of the Claude subscription-OAuth read/refresh/get-token logic.

Where it ships:
  * Lane-A harness images: COPY'd to /app/lib/_harness_runtime/ (PYTHONPATH=/app/lib)
  * entity image:          COPY'd to /app/cli/_harness_runtime/ (the shims already
                           sys.path.insert "/app/cli" for cseries_sidecar)
  * source tree:           system-apps/_harness_runtime/ (shims add system-apps/)

Keep this __init__ empty of imports: claude_creds must be importable without
pulling harness_core (which imports cseries_sidecar).
"""
