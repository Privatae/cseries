# Release click-through

A person-level check of a C-Series Host before a release: the Host's own
desktop in headless Chromium, every console message, page error, failed
request and HTTP error captured per step. Run it against a Host running the
release's image, on a test machine with invented data only.

## One command

```sh
pip install playwright && python3 -m playwright install chromium   # once
CSERIES_HOST_URL=http://home.cseries.localhost:8800 \
CSERIES_CLICKTHROUGH_TEST_HOST=http://home.cseries.localhost:8800 \
CSERIES_OWNER_EMAIL=owner@example.com CSERIES_OWNER_PASSWORD='...' \
  python3 scripts/release-clickthrough/run.py /tmp/clickthrough-0.1.17 --sweep
```

Every script refuses to run unless `CSERIES_CLICKTHROUGH_TEST_HOST` is set to
the same URL as `CSERIES_HOST_URL`: the steps seal stand-in keys over an
engine's own key, revoke them and chat, and the default URL is a local
install, which can be a person's real Host.

The owner is the test Host's own (invented) account. The password is read from
the environment, never the command line. `run.py` signs in, runs the steps, and
with `--sweep` opens every installed app; it exits 1 when a step's script
failed. What each step saw is in `<outdir>/clickthrough/steps.log`, with
`events.jsonl` (console, page errors, failed requests, HTTP >= 400) and a
screenshot per step beside it.

## Steps (drive.py)

| step | what it checks |
|---|---|
| `state` | the stored and effective chat route, providers with a key, `engine_keys` |
| `clear-chat` | setup: removes the chat route (nobody has chosen a chat model) |
| `race` | the Orchestrator opened with its frame-token answer held 8 s: no read refused, cards drawn |
| `simple` | the Simple view says which model chat uses and why |
| `local` | GET /local-models marks embedders; the chat row offers only chat models |
| `kimi-models` | the Kimi Code row's model list or its reason |
| `revoke-<engine>` | Keys > Vault > Revoke `harness-<engine>` |
| `connect-<engine>` | the engine's Connect / Add a key opens Keys on the form with the key's name filled in; a stand-in value is sealed; the same Orchestrator window then reads Connected |
| `claude-signin` | "Sign in with your Claude account" opens the pop-up with Anthropic's link (nobody signs in) |
| `connect-claude` | Connect Claude Code opens the same pop-up |
| `chat` | Circles sends one message and records the answer and the model that gave it |

Pick steps with `--steps "race simple local chat"`. `clear-chat` and
`revoke-<engine>` change the Host's settings; they are not in the default list.

## Token sweep (token_sweep.py)

Every installed app opened from the desktop; every `/api/` request recorded
with the frame that made it, whether it carried `X-Application-Token`, and
whether it left before that frame's load event; every 401 "application token
required" listed. Modes: `normal`, `hold-token` (the isolated app's
frame-token answer held 8 s), `hold-launch` (the launch answer held 8 s),
`hold-init` (the shell's `cseries:app-init` held 8 s inside each frame):
`--sweep-modes "normal hold-token hold-launch hold-init"`. A normal pass over
about twenty apps takes about four minutes; each held mode about seven.

## Not covered here

The resize (`npx cseries resize <size>` on the machine itself, then the `chat`
step again) and loading a release candidate image into the Host's guest are
machine steps, run in a terminal on the test machine.
