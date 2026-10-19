#!/usr/bin/env python3
"""fan-out d33393f9 (2026-09-06, project 220, five harnesses).

What the user saw: "kimi and grok are hung, no activity; can't view
antigravity's output". What the session files said:

  * kimi and grok FAILED AT WAKE 3 s after queueing — operator
    /harness/wake 402 capability_over_plan (needs 512Mi, headroom 32Mi).
    per_harness_state.json carried `failed` + the error; NO events file was
    ever created. The run terminal closes only on a complete/timeout frame
    and the per-variant stream route tails for one for 1800 s, so both
    rendered as "live" over an empty scrollback: hung, to a human.
  * antigravity finished ok / exit 0 with 36k output tokens, and its
    worktree came back EMPTY (diff_bytes 0). Its own frames show why: agy's
    init reported cwd=<worktree>, the model's first `pwd` answered
    …/.agy-home/.gemini/antigravity-cli/scratch, and both write_to_file
    calls (app.yaml, a 67 KB index.html) targeted that scratch dir. The
    prompt note in HARNESS_PROMPT_NOTES did not take.
  * The kernel's bridge reconnect after the node's bridge pod was replaced
    walked 7 cross-node addresses at 5 s each: bridge_client._candidate_addrs
    only put HOST_IP first when DNS returned it, and a not-Ready pod is
    absent from a headless Service's answer.

What this pins (review of af68187d folded in):
  * _close_run_terminal writes the terminal frame a never-ran variant needs,
    an `error` frame with the PLAIN-ENGLISH reason before it (N2), and is
    idempotent;
  * the fallback collect is provenance-by-snapshot (B1): two variants run
    sequentially on ONE warm pod, and A's file must NOT land in B's worktree;
    a file B changed must; no clock is compared with another clock;
  * a credential file by name never lands, whatever the export held (N1.2);
  * the sidecar's /workspace/export refuses a dir reached through a symlink
    (N1.1 — ships with the next sidecar image; N1.2 covers the deployed one);
  * directory members are never taken (only files, parents created on
    extract);
  * _candidate_addrs dials HOST_IP first whether or not DNS listed it;
  * the call sites: the wake-failure branch of _harness_group, and the
    snapshot-before-/run + collect-after in _run_on_pod.

Container-free: tmp dirs, in-memory tars, a stub http, a patched
getaddrinfo, the sidecar module imported against a tmp workspace. No pod,
no network.

Run: APP_ROOT=<repo> python3 scripts/test_fanout_wake_failure_closes_terminal.py
"""
import asyncio
import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.routes import review as rv  # noqa: E402
from server.bridge import bridge_client as bcm  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def frames(session_dir, label):
    p = rv._events_path(session_dir, label)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


WAKE_402 = ("harness wake failed: operator /harness/wake 402: "
            '{"detail":{"error":"capability_over_plan","harness":"kimi",'
            '"needs_mi":512,"headroom_mi":32,"upgrade_to":"pro"}}')

print("== a variant that never ran gets its terminal frame, with the reason ==")
with tempfile.TemporaryDirectory() as td:
    sd = pathlib.Path(td)
    rv._close_run_terminal(sd, "kimi", ok=False, error=WAKE_402)
    fr = frames(sd, "kimi")
    check("two frames: an error frame, then the terminal status frame",
          len(fr) == 2 and fr[0]["kind"] == "error" and fr[1]["kind"] == "status"
          and fr[1]["payload"].get("state") == "complete"
          and fr[1]["payload"].get("ok") is False, fr)
    msg = fr[0]["payload"].get("message", "") if fr else ""
    check("the reason is plain English, not the operator's codes",
          msg == "kimi could not start: this plan has 32Mi free and kimi needs 512Mi "
                 "(the pro plan has room for it)", msg)
    check("the raw error still rides the status frame",
          fr and fr[1]["payload"].get("error") == WAKE_402)
    check("seq numbering is contiguous from 0", [f["seq"] for f in fr] == [0, 1])
    rv._close_run_terminal(sd, "kimi", ok=False, error=WAKE_402)
    check("idempotent: a second close adds nothing", len(frames(sd, "kimi")) == 2)

    rv._append_run_frame(sd, "codex", "status", {"state": "thinking"})
    rv._append_run_frame(sd, "codex", "status", {"state": "complete", "ok": True, "exit_code": 0})
    rv._close_run_terminal(sd, "codex", ok=False, error="late")
    fr = frames(sd, "codex")
    check("a run that already closed itself is left alone",
          len(fr) == 2 and fr[-1]["payload"].get("ok") is True, fr)

    rv._append_run_frame(sd, "grok", "status", {"state": "timeout", "ok": False})
    rv._close_run_terminal(sd, "grok", ok=False, error="late")
    check("...a timeout frame counts as terminal too", len(frames(sd, "grok")) == 1)

    rv._close_run_terminal(sd, "cursor", ok=True, error=None)
    fr = frames(sd, "cursor")
    check("an ok close writes no error frame", len(fr) == 1 and fr[0]["kind"] == "status")

    rv._close_run_terminal(None, "x", ok=False, error="no session dir")
    check("no session dir -> no-op, no raise", True)

check("a non-402 wake failure is passed through, prefixed with who",
      rv._plain_run_failure("grok", "harness wake failed: no url")
      == "grok could not start: harness wake failed: no url")
check("a run-time error is passed through unchanged",
      rv._plain_run_failure("codex", "codex exceeded 10min cap") == "codex exceeded 10min cap")
check("an empty reason is still a sentence",
      rv._plain_run_failure("kimi", None) == "no reason recorded")

print("== the empty-worktree signature (log wording only now) ==")
with tempfile.TemporaryDirectory() as td:
    wt = pathlib.Path(td)
    (wt / ".git").mkdir()
    check("only .git -> no files", not rv._worktree_has_files(wt))
    (wt / "index.html").write_text("<html>")
    check("index.html -> has files", rv._worktree_has_files(wt))
    check("a missing dir -> no files (never raises)",
          not rv._worktree_has_files(wt / "nope"))

print("== fallback collect: provenance by snapshot, never .git, never a credential ==")


def _tar(entries):
    """entries: [(name, text_or_None_for_dir, mtime)] -> tar bytes shaped like
    the sidecar's /workspace/export (arcname '.', so names are './…')."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, text, mtime in entries:
            ti = tarfile.TarInfo(name)
            ti.mtime = mtime
            if text is None:
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tf.addfile(ti)
            else:
                data = text.encode()
                ti.size = len(data)
                ti.mode = 0o644
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class _Content:
    """aiohttp's response.content, as far as the capped readers use it."""

    def __init__(self, body):
        self._body = body

    async def iter_chunked(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    async def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        # The collect/export readers are capped (review._read_capped): they
        # read content_length and stream content, as with a real response.
        self.content_length = len(body)
        self.content = _Content(body)

    async def read(self):
        return self._body

    async def text(self):
        return self._body.decode(errors="replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    """Answers each GET with the next (status, body) in `script`; the last one
    repeats. Models one warm pod whose scratch dir changes between calls."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def get(self, url, **kw):
        self.calls.append((url, kw.get("params")))
        status, body = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return _Resp(status, body)


SCRATCH = ".agy-home/.gemini/antigravity-cli/scratch"
T = time.time()
A_WROTE = _tar([("./index.html", "<html>A</html>", T), ("./old-empty", None, T - 9000)])
B_CHANGED = _tar([("./index.html", "<html>B — longer</html>", T + 5),
                  ("./notes.md", "b", T + 5), ("./old-empty", None, T - 9000)])

print("-- two sequential variants on ONE warm pod")
try:
    with tempfile.TemporaryDirectory() as td:
        wt_a, wt_b, wt_b2 = (pathlib.Path(td) / n for n in ("a", "b", "b2"))
        for w in (wt_a, wt_b, wt_b2):
            w.mkdir()
        # Variant A: cold pod -> the snapshot sees nothing (404), then A's file.
        http = _Http((404, b"no such workspace dir"), (200, A_WROTE))
        before_a = run(rv._snapshot_fallback_dir(http, "http://pod.test:8848", SCRATCH))
        check("a cold pod's snapshot is empty (404 -> {})", before_a == {}, before_a)
        n_a = run(rv._collect_out_fallback(http, "http://pod.test:8848", SCRATCH, wt_a, before=before_a))
        check("A lands its own file", n_a == 1 and (wt_a / "index.html").read_text() == "<html>A</html>", f"n={n_a}")
        check("...and not the earlier empty dir member", not (wt_a / "old-empty").exists())
        # Variant B, same pod, A's file still in scratch and UNCHANGED.
        http = _Http((200, A_WROTE))
        before_b = run(rv._snapshot_fallback_dir(http, "http://pod.test:8848", SCRATCH))
        check("B's snapshot holds A's file", "./index.html" in before_b, before_b)
        n_b = run(rv._collect_out_fallback(http, "http://pod.test:8848", SCRATCH, wt_b, before=before_b))
        check("B lands NOTHING of A's (the file preview/adopt would read)",
              n_b == 0 and not (wt_b / "index.html").exists(), f"n={n_b}")
        # Variant B2, same pod, B2 rewrote index.html and added notes.md.
        http = _Http((200, A_WROTE), (200, B_CHANGED))
        before_b2 = run(rv._snapshot_fallback_dir(http, "http://pod.test:8848", SCRATCH))
        n_b2 = run(rv._collect_out_fallback(http, "http://pod.test:8848", SCRATCH, wt_b2, before=before_b2))
        check("B2 lands exactly what it changed or added",
              n_b2 == 2 and (wt_b2 / "index.html").read_text() == "<html>B — longer</html>"
              and (wt_b2 / "notes.md").read_text() == "b", f"n={n_b2}")
        check("the export is asked for the vendor's dir",
              all(c[1] == {"dir": SCRATCH} for c in http.calls), http.calls)
except Exception as e:  # noqa: BLE001 — red on a tree without the snapshot design
    check("two sequential variants on one warm pod (snapshot design present)", False, repr(e))

print("-- hardening")
with tempfile.TemporaryDirectory() as td:
    wt = pathlib.Path(td)
    leak = _tar([("./index.html", "<html>", T), ("./.git/config", "[core]", T),
                 ("./antigravity-oauth-token", '{"access_token":"x"}', T),
                 ("./sub/page.html", "<p>", T)])
    n = run(rv._collect_out_fallback(_Http((200, leak)), "http://pod.test:8848", SCRATCH, wt, before={}))
    check("a credential file by name never lands",
          not (wt / "antigravity-oauth-token").exists())
    check("never lands .git", not (wt / ".git").exists())
    check("a nested file lands with its parent created", (wt / "sub" / "page.html").exists() and n == 2, f"n={n}")
    try:
        run(rv._collect_out_fallback(_Http((500, b"boom")), "http://pod.test:8848", SCRATCH, wt, before={}))
        check("a non-200/404 raises (the caller logs it)", False, "no raise")
    except RuntimeError as e:
        check("a non-200/404 raises (the caller logs it)", "500" in str(e), str(e))
    check("a 404 after the run lands nothing and does not raise",
          run(rv._collect_out_fallback(_Http((404, b"gone")), "http://pod.test:8848", SCRATCH, wt, before={})) == 0)

check("antigravity's scratch dir is the registered fallback",
      rv.HARNESS_FALLBACK_COLLECT.get("antigravity") == SCRATCH)

print("== the sidecar refuses to export a dir reached through a symlink ==")
with tempfile.TemporaryDirectory() as td:
    ws = pathlib.Path(td) / "ws"
    (ws / "vendor" / "scratch").mkdir(parents=True)
    (ws / "vendor" / "scratch" / "index.html").write_text("<html>")
    (ws / "vendor" / "antigravity-oauth-token").write_text("secret")
    (ws / "vendor" / "linked").symlink_to(ws / "vendor")          # scratch swapped for a link to its parent
    os.environ["PRIVATAE_WORKSPACE"] = str(ws)
    os.environ.setdefault("PORT", "8845")
    sys.path.insert(0, str(ROOT / "cli"))
    import importlib
    import cseries_sidecar  # noqa: E402
    sc = importlib.reload(cseries_sidecar)

    def _export(d):
        return sc._workspace_export(sc._Request("GET", "/workspace/export", f"dir={d}", {}, b""))

    out = _export("vendor/scratch")
    check("a real dir exports (tar bytes)", isinstance(out, (bytes, bytearray)) and len(out) > 0, type(out).__name__)
    out = _export("vendor/linked")
    check("a symlinked dir is refused with 400",
          isinstance(out, tuple) and out[1] == 400 and "symlink" in str(out[0]), str(out)[:120])
    out = _export("vendor/nope")
    check("a missing dir is still 404", isinstance(out, tuple) and out[1] == 404, str(out)[:80])
    os.environ.pop("PRIVATAE_WORKSPACE", None)

print("== the bridge client dials its own node first ==")


async def _addrs(host_ip, dns):
    bc = bcm.BridgeClient()
    loop = asyncio.get_running_loop()
    orig = loop.getaddrinfo

    async def fake(host, port, **kw):
        return [(None, None, None, "", (ip, port)) for ip in dns]

    loop.getaddrinfo = fake
    old = os.environ.get("HOST_IP")
    try:
        if host_ip is None:
            os.environ.pop("HOST_IP", None)
        else:
            os.environ["HOST_IP"] = host_ip
        return await bc._candidate_addrs()
    finally:
        loop.getaddrinfo = orig
        if old is None:
            os.environ.pop("HOST_IP", None)
        else:
            os.environ["HOST_IP"] = old


cross = ["2001:db8:fd::235", "2001:db8:fd::236", "2001:db8:fd::237"]
out = run(_addrs("2001:db8:fd::240", cross))
check("HOST_IP absent from DNS (pod not Ready) -> still dialed FIRST",
      out[0] == "2001:db8:fd::240" and out[1:] == cross, out)
out = run(_addrs("2001:db8:fd::236", cross))
check("HOST_IP present in DNS -> moved first, not duplicated",
      out == ["2001:db8:fd::236", "2001:db8:fd::235", "2001:db8:fd::237"], out)
out = run(_addrs(None, cross))
check("no HOST_IP -> DNS order untouched", out == cross, out)

print("== the call sites are wired ==")
src = (ROOT / "server/routes/review.py").read_text()
i_group = src.index("async def _harness_group(")
i_wake_fail = src.index('err = (f"harness wake failed: "', i_group)
check("_harness_group closes the terminal on a wake failure",
      "_close_run_terminal(session_dir, vid, ok=False, error=err)" in src[i_wake_fail:i_wake_fail + 1200])
i_run = src.index("async def _run_on_pod(")
i_payload = src.index('payload = {"prompt": prompt, "project_dir": in_container_dir,', i_run)
check("_run_on_pod snapshots the vendor dir BEFORE /run",
      "_snapshot_fallback_dir(http, base, fallback_dir)" in src[i_run:i_payload])
i_collect = src.index("await _collect_out(http, base, dir_name, worktree)", i_run)
# From the collect to the run's catch-all: a structural window, not a byte
# count (a collect-failure report added above the fallback once pushed the
# checked lines past a fixed 2500 characters).
tail = src[i_collect:src.index("if _transient_pod_conn(e):", i_collect)]
check("_run_on_pod collects what changed since the snapshot, on every run of such a harness",
      "before=fallback_before" in tail and "fallback_before is not None" in tail
      and "not _worktree_has_files(worktree)" not in tail.split("empty_before =")[0])
check("...and computes the greenfield diff host-side once files landed (off the event loop)",
      'run_out["diff"] = await asyncio.to_thread(_worktree_diff_host, worktree)' in tail)
i_catch = src.index("if _transient_pod_conn(e):", i_collect)
check("_run_on_pod's catch-all closes the terminal",
      "_close_run_terminal(session_dir, label, ok=False" in src[i_catch:i_catch + 1500])

print()
if FAILURES:
    print(f"FAIL {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all green")
