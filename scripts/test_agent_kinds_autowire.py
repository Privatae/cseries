#!/usr/bin/env python3
"""Agents vs apps, and embeddings that set themselves up.

WHAT THIS PROTECTS. A Host's first bastion (Windows, 0.13.7) reported the
Claude DESKTOP app as nine "claude-code" agents — eight of them Chromium
helper processes — and the host raised nine inbox alerts with nine kill
buttons. In the same session the host had no embeddings, a machine serving
one embedding model, and OFFERED to set it up instead of doing it; when told
to, it guessed a model the box did not serve. Three couplings, three gates:

  1. KIND. server/network/nac_agents.classify_snapshot derives agent / app /
     client for rows an older bastion sends raw: `--type=` helpers are
     dropped, a matched process sharing an executable with a helper is the
     app that owns them (renamed claude-code -> claude-desktop), the helper's
     socket is credited to the app, and rows share a FAMILY key so an app is
     one card and one alert. A bastion that already classified is trusted.
  2. THE CHOOSER. mastercode.embed_backends.choose_bastion_embed_model picks
     from what the machine actually serves — the embedding models the memory
     columns can hold, an indexable dim first, then the largest.
  3. THE DECISION. mastercode.embed_autowire.autowire_decision wires exactly
     one transition (unconfigured -> configured) when a caps-permitted host
     sees an online, embed-capable machine serving a known model.

Plus the host-facing words: the skill and tool text say DO it, not offer it.
Container-free. Run: APP_ROOT=<repo> python3 scripts/test_agent_kinds_autowire.py
"""
import importlib.util
import os
import sys

FAILED = 0
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED, CHECKS
    CHECKS += 1
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))


def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


def _load(repo: str, rel: str, name: str):
    """Import one module by path so the `server` package (FastAPI, asyncpg,
    the lot) is never imported for a pure function."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(repo, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EXE = r'"C:\Program Files\WindowsApps\Claude_1.40609.1.0_x64__pzs8sxrjxfjjc\app\claude.exe"'
MEASURED_MODELS = ["qwen3-embedding:4b", "openhermes:latest",
                   "hf.co/HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive:Q4_K_M",
                   "qwen3.5:9b", "assistant-chat:latest", "llama3.1:8b-instruct-q4_K_M", "qwen2.5:14b"]


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("agents vs apps + embedding auto-wire")

    # ── 1. KIND, from the measured snapshot of an OLD bastion (no kinds sent) ──
    na = _load(repo, "server/network/nac_agents.py", "nac_agents_under_test")
    helper_args = [
        ' --type=renderer --user-data-dir="C:\\Users\\u\\AppData\\Roaming\\Claude" --st',
        ' --type=utility --utility-sub-type=network.mojom.NetworkService --lang=en-US',
        ' --type=gpu-process --user-data-dir="C:\\Users\\u\\AppData\\Roaming\\Claude" -',
        ' --type=crashpad-handler --user-data-dir=C:\\Users\\u\\AppData\\Roaming\\Claud',
        ' --type=utility --utility-sub-type=video_capture.mojom.VideoCaptureService --',
        ' --type=renderer --user-data-dir="C:\\Users\\u\\AppData\\Roaming\\Claude" --st',
        ' --type=utility --utility-sub-type=audio.mojom.AudioService --lang=en-US --se',
        ' --type=utility --utility-sub-type=node.mojom.NodeService --lang=en-US --serv',
    ]
    snap = [{"pid": 4532, "signature": "claude-code", "user": "D\\alex", "cmdline": EXE, "llm_endpoints": []}]
    for i, args in enumerate(helper_args):
        snap.append({"pid": 5000 + i, "signature": "claude-code", "user": "D\\alex",
                     "cmdline": EXE + args,
                     "llm_endpoints": ["api.anthropic.com"] if "network.mojom" in args else []})
    snap.append({"pid": 38552, "signature": "codex", "user": "D\\alex",
                 "cmdline": r"c:\Users\u\.vscode\extensions\openai.chatgpt-26.5818.41705-win32-x64\bin\windows-x86_64\codex.exe -c features.code_mode_host=true app-server",
                 "llm_endpoints": []})
    snap.append({"pid": 17576, "signature": "llm-client", "user": "D\\alex",
                 "cmdline": r'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -File "C:\Users\u\code-index-embedder\emb.ps1"',
                 "llm_endpoints": ["local:11434 (ollama)"]})
    rows = na.classify_snapshot("WORKSTATION-1", snap)
    by_pid = {r["pid"]: r for r in rows}
    check("eleven rows in, three out: the eight helpers are dropped", len(rows) == 3, str(sorted(by_pid)))
    app = by_pid.get(4532) or {}
    check("the desktop app is kind=app, renamed claude-desktop",
          app.get("kind") == "app" and app.get("signature") == "claude-desktop", str(app))
    check("the network helper's api.anthropic.com socket is credited to the app",
          app.get("llm_endpoints") == ["api.anthropic.com"], str(app.get("llm_endpoints")))
    check("family = server | kind | executable, so the app is one card and one alert",
          app.get("family") == "WORKSTATION-1|app|claude", str(app.get("family")))
    check("codex stays an agent (stub: no special handling)",
          (by_pid.get(38552) or {}).get("kind") == "agent", str(by_pid.get(38552)))
    check("the PowerShell embedder is a client (network signal only)",
          (by_pid.get(17576) or {}).get("kind") == "client")
    check("a lone Claude Code CLI is an agent, name unchanged",
          na.classify_snapshot("h", [{"pid": 7, "signature": "claude-code", "cmdline": "claude --resume"}])[0]["kind"] == "agent")
    check("a macOS .app bundle is an app even with no helper in the snapshot",
          na.classify_snapshot("h", [{"pid": 8, "signature": "cursor",
                                      "cmdline": "/Applications/Cursor.app/Contents/MacOS/Cursor"}])[0]["signature"] == "cursor-app")
    trusted = na.classify_snapshot("h", [{"pid": 9, "signature": "claude-desktop", "kind": "app", "ppid": 3,
                                          "cmdline": "x"}])[0]
    check("a bastion that already classified (0.13.9+) is trusted: kind, name and ppid kept",
          trusted["kind"] == "app" and trusted["signature"] == "claude-desktop" and trusted["ppid"] == 3, str(trusted))
    # Lineage (0.13.9+ bastions send ppid): an agent and the sub-agent it spawned
    # are one family; two independent sessions of the same agent are two.
    lineage = na.classify_snapshot("h", [
        {"pid": 100, "ppid": 50, "signature": "claude-code", "kind": "agent", "cmdline": "claude"},
        {"pid": 101, "ppid": 100, "signature": "claude-code", "kind": "agent", "cmdline": "claude -p sub"},
        {"pid": 200, "ppid": 51, "signature": "claude-code", "kind": "agent", "cmdline": "claude"},
    ])
    fam = {r["pid"]: r["family"] for r in lineage}
    check("a spawned sub-agent joins its parent's family",
          fam[101] == fam[100] and fam[100].endswith("|100"), str(fam))
    check("an independent session of the same agent is its own family",
          fam[200] != fam[100] and fam[200].endswith("|200"), str(fam))
    check("without ppid (older bastion) each agent process is its own family — the pre-kind behaviour",
          len({r["family"] for r in na.classify_snapshot("h", [
              {"pid": 1, "signature": "aider", "cmdline": "aider"},
              {"pid": 2, "signature": "aider", "cmdline": "aider"}])}) == 2)
    check("a helper-only snapshot yields nothing",
          na.classify_snapshot("h", [{"pid": 1, "signature": "claude-code", "cmdline": EXE + " --type=gpu-process"}]) == [])
    check("junk rows (no pid / no signature / not a dict) are ignored",
          na.classify_snapshot("h", [None, {"signature": "x"}, {"pid": 1}, "s"]) == [])
    check("an app row is never a contain target: nac_mgmt refuses on KIND_APP",
          "KIND_APP" in _read(repo, "server/network/nac_mgmt.py")
          and "observation_kind" in _read(repo, "server/network/nac_mgmt.py"))
    na_src = _read(repo, "server/network/nac_agents.py")
    check("the unrequested alert, the overview badge count and the lens count all use ONE exclusion set",
          "_NEVER_ALERTED_SQL" in na_src and "NOT IN ('app', 'automation')" in na_src
          and "<> 'app'" not in na_src
          and "_NEVER_ALERTED" in _read(repo, "server/routes/network.py"))
    # Origin + evidence + automation (bastion 0.13.12, phase 1 of the detection plane).
    o = na.classify_snapshot("h", [
        {"pid": 40, "ppid": 39, "signature": "aider", "kind": "agent", "cmdline": "/tmp/aider run.py",
         "origin": "scheduler",
         "evidence": [{"signal": "agent-signature", "tier": "known", "value": "aider"},
                      {"signal": "scheduler-parent", "tier": "verified", "value": "cron pid 800"},
                      {"signal": "junk"}, "not a dict"]},
        {"pid": 41, "ppid": 1, "signature": "xdotool", "kind": "automation", "cmdline": "xdotool key a",
         "origin": "Interactive"},
        {"pid": 42, "ppid": 1, "signature": "codex", "kind": "agent", "cmdline": "codex"},
    ])
    ob = {r["pid"]: r for r in o}
    check("origin travels as sent, lower-cased",
          ob[40]["origin"] == "scheduler" and ob[41]["origin"] == "interactive", str({k: v.get("origin") for k, v in ob.items()}))
    check("evidence keeps only typed {signal, tier, value} lines",
          ob[40]["evidence"] == [{"signal": "agent-signature", "tier": "known", "value": "aider"},
                                 {"signal": "scheduler-parent", "tier": "verified", "value": "cron pid 800"}],
          str(ob[40]["evidence"]))
    check("a row with no origin/evidence reads None / [] — never invented",
          ob[42]["origin"] is None and ob[42]["evidence"] == [], str(ob[42]))
    check("automation is a kind the host keeps (not flattened to agent)",
          ob[41]["kind"] == "automation", str(ob[41]))
    check("an automation is keyed by lineage like an agent, in its own kind",
          ob[41]["family"] == "h|automation|xdotool|41", str(ob[41]["family"]))

    # ── 2. THE CHOOSER ────────────────────────────────────────────────────
    from mastercode.embed_backends import choose_bastion_embed_model, known_embed_models  # noqa: E402
    check("known_embed_models keeps only the embedding model among the measured tags",
          known_embed_models(MEASURED_MODELS) == [("qwen3-embedding:4b", 2560)],
          str(known_embed_models(MEASURED_MODELS)))
    check("the chooser picks it", choose_bastion_embed_model(MEASURED_MODELS) == ("qwen3-embedding:4b", 2560))
    check("an indexable dim beats a larger unindexable one (4096 has no HNSW index)",
          choose_bastion_embed_model(["qwen3-embedding:latest", "qwen3-embedding:0.6b"]) == ("qwen3-embedding:0.6b", 1024))
    check("among indexable dims the largest wins",
          choose_bastion_embed_model(["nomic-embed-text", "qwen3-embedding:4b"]) == ("qwen3-embedding:4b", 2560))
    check("no embedding model served → None, never a guess",
          choose_bastion_embed_model(["llama3.1:8b", "qwen2.5:14b"]) is None)
    check("empty / None → None", choose_bastion_embed_model([]) is None and choose_bastion_embed_model(None) is None)

    # ── 3. THE DECISION ───────────────────────────────────────────────────
    from mastercode.embed_autowire import autowire_decision  # noqa: E402
    box = {"hostname": "WORKSTATION-1", "online": True,
           "capabilities": ["embed", "agent-observe"], "models": MEASURED_MODELS}
    model, why = autowire_decision(False, box, True)
    check("unconfigured host + online embed-capable machine + caps → wires the served model",
          model == "qwen3-embedding:4b", f"{model!r} {why}")
    check("a configured host is never re-pointed", autowire_decision(True, box, True)[0] is None)
    nocaps = autowire_decision(False, box, False)
    check("no capability → nothing, and the reason names it",
          nocaps[0] is None and "capability" in nocaps[1], str(nocaps))
    check("offline machine → nothing", autowire_decision(False, {**box, "online": False}, True)[0] is None)
    check("no embed capability → nothing", autowire_decision(False, {**box, "capabilities": ["agent-observe"]}, True)[0] is None)
    check("no known embedding model → nothing",
          autowire_decision(False, {**box, "models": ["llama3.1:8b"]}, True)[0] is None)
    check("no bastion at all → nothing", autowire_decision(False, None, True)[0] is None)
    check("the heartbeat calls the auto-wire after ingest",
          "embed_autowire.maybe_autowire" in _read(repo, "server/routes/network.py"))
    check("the wire runs off the heartbeat's request path (a slow probe never delays the reply)",
          "asyncio.create_task(_autowire_now(" in _read(repo, "brain/mastercode/embed_autowire.py"))

    # ── 4. THE WORDS ──────────────────────────────────────────────────────
    skill = _read(repo, "default-skills/embedding.md")
    check("the skill says the default is to DO it, not offer it", "The default is to do it" in skill)
    check("the skill tells the host to name a model only from embed_models", "embed_models" in skill)
    # 2026-09-06 (registry slimming): the doctrine moved from the tool
    # description into the network skill the tool points at, so both checks
    # read the skill — in the registry "embed_models" now only survives on
    # embedding_route.model, which is a different tool.
    net_skill = _read(repo, "default-skills/network.md")
    check("get_network explains kinds and says an app is never an agent",
          "is what the process IS" in net_skill and "never contain it" in net_skill)
    check("get_network hands the host the served embedding models", "embed_models" in net_skill)
    check("use_bastion says to omit the model on a fresh host and not to ask first",
          "do not ask first" in skill)

    print()
    if FAILED:
        print(f"FAIL agent_kinds_autowire ({FAILED} of {CHECKS} checks failed)")
        return 1
    print(f"PASS agent_kinds_autowire ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
