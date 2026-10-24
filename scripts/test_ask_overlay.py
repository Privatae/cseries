#!/usr/bin/env python3
"""test_ask_overlay.py — host-authored ops: the ask card, an overlay over
the chat, never a message in it.

A host had no way to ask with a survey box or anything like it, only typed
responses; the ask belongs in an overlay over the chat, not in the chat.

The pieces, and what pins each:
  1. The Host asks with the tool it already has (`escalate` → a decision
     ticket). The chat client reads the call + result off the turn stream it
     already consumes and raises the card from them: app-web-kit/ask.ts
     (pure; circles-web/test/ask.test.ts runs it under node --test, run from
     here too).
  2. The card is a dialog over the conversation (app-web-kit/ui/ask-overlay),
     mounted by BOTH chat surfaces from their hooks' `asks` queue — one at a
     time, the same ticket never twice.
  3. The click decides the ticket through the route the Tickets pane uses,
     then the Host reads the person's words: a nudge into a running turn
     (Circles), the next message when the turn has ended (both).
  4. "Not now" leaves the ticket open in Tickets; nothing is decided.

Container-free: pure functions + source pins. APP_ROOT=<repo> python3 scripts/<this>.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def src(rel: str) -> str:
    return (ROOT / rel).read_text()


print("1. the pure half, under node --test")
node = shutil.which("node")
if node:
    r = subprocess.run([node, "--test", "test/ask.test.ts"], cwd=ROOT / "circles-web",
                       capture_output=True, text=True, timeout=120)
    tail = (r.stdout + r.stderr)[-600:]
    check("circles-web/test/ask.test.ts passes (askFromTool / queue / the words)",
          r.returncode == 0 and "# fail 0" in r.stdout, tail)
else:
    check("node present to run the kit test (SKIPPED — no node on PATH)", True)
kit = src("app-web-kit/ask.ts")
check("an Ask is raised only from an escalate whose result is an OPEN ticket",
      't.name === "escalate"' in kit and 'res.status !== "open"' in kit and "auto_resolved === true" in kit)
# The pipeline proposal and the op card (host-authored ops): a NAMED platform tool whose result carries the ask the
# kernel filed — never any tool's ask-shaped result (ask.test.ts pins that a
# forged one from another tool raises nothing).
check("…or from a NAMED platform tool's result ask, never any tool's",
      'const RESULT_ASK_TOOLS = new Set(["pipeline", "mgmt_run", "context_ingest", "turn_type"]);' in kit
      and "RESULT_ASK_TOOLS.has(opName(t))" in kit and "} else {\n    return null;" in kit
      # the Host runs a registry op through `operate`: the op is args.tool
      and 'if (t.name !== "operate") return t.name;' in kit)
check("one card at a time, the same ticket never twice",
      "queue.some((a) => a.ticketId === ask.ticketId)" in kit)

print("\n2. the overlay, mounted by both chat surfaces")
ov = src("app-web-kit/ui/ask-overlay.tsx")
check("the card is a Dialog over the chat (not a message)", "<Dialog " in ov and 'from "./dialog"' in ov)
check("the card shows the sitrep, the choices with the recommended one marked, a note, and Not now",
      "ask.sitrep" in ov and "o.recommend && <Badge>Recommended" in ov and "<Textarea" in ov and "Not now" in ov)
# Nobody should have to chase apps in the middle of a conversation: a
# credential ask is answered ON the card when the app gives onSealCredential —
# a sealed password field whose value goes only to
# the vault call, the ticket then decided with the key's NAME (sealedNote).
# Without it the card says to answer it from Waiting on you in Circles.
_seal = ov.split("const sealAndAnswer")[1].split("return (")[0]
check("a credential ask is sealed on the card: a password field, the value only to the vault, the answer only the name",
      'type="password"' in ov and "const why = await onSealCredential(req, value)" in _seal
      and "onAnswer(o, sealedNote(req.name, note))" in _seal and "onAnswer(o, value" not in ov
      and "Answer this one in Circles, from Waiting on you" in ov)
check("closing the dialog is Not now, with the note (never mid-seal)",
      "const locked = busy || sealing !== null" in ov and "if (!open && !locked) onNotNow(note)" in ov)
# Circles' chat is your Host's back on the cards: own-back.tsx mounts the card and
# cards/use-host-thread.ts answers it.
CIRCLES_PANE = ("circles-web/src/cards/own-back.tsx", "circles-web/src/cards/use-host-thread.ts")
for app, hook, pane in (("circles", "circles-web/src/hooks/use-circles-chat.ts", CIRCLES_PANE),
                        ("workbench", "workbench-web/src/hooks/use-workbench-chat.ts",
                         ("workbench-web/src/components/workbench/chat-pane.tsx",))):
    h, p = src(hook), "\n".join(src(f) for f in pane)
    check(f"{app}: the hook keeps the call side of each tool and reads the result as an Ask",
          "toolMetaRef.current.set(toolCallId, { name: toolName, args })" in h
          and "askFromTool({ id: toolCallId, name: meta.name, args: meta.args, result })" in h)
    check(f"{app}: the hook queues the ask and exposes asks + settleAsk",
          "queueAsk(q," in h and "settleAsk" in h and "asks," in h)
    check(f"{app}: the pane mounts AskOverlay", "<AskOverlay" in p and 'from "@kit/ui/ask-overlay"' in p)
    check(f"{app}: the click decides the ticket through the Tickets route first",
          "await decideTicket(ask.ticketId, option.id" in p)
    check(f"{app}: the Host reads the person's words (answerText)", "answerText(ask, option, note)" in p)
    check(f"{app}: a failed decide is said on the card and nothing is sent",
          "setAskError(`Couldn’t record that" in p)
circ = src("circles-web/src/cards/use-host-thread.ts")
ch0_pre = src("circles-web/src/hooks/use-circles-chat.ts")
check("circles: the road is chosen AFTER the decide, from the turn's status now (a ref), not the click's",
      "const st = statusRef.current;" in circ.split("const tellHost")[1])
# One conversation: tellHost takes the
# topic the card was asked on. Its running turn is nudged through the hook,
# which holds a refused nudge and sends it as that topic's next message when the
# turn ends (the hold is pinned below); a caller's own send() would be refused
# while the turn streams, so tellHost must not fall back to one there.
tell = circ.split("const tellHost")[1].split("const answerAsk")[0]
_tell_run = tell.split('if (st === "submitted" || st === "streaming") {')[1].split("\n      if (fallback) await send(")[0]
check("circles: a running turn is nudged through the hook; a refused nudge falls through to a message",
      "await nudgeRunningTurn(SELF, text, topic, fallback);\n          return;" in _tell_run
      and "send(SELF" not in _tell_run
      and "if (fallback) pendingNudgesRef.current.push(entry);" in ch0_pre
      and "if (fallback) await send(SELF, text, { threadId: topic });" in tell)
ch0 = src("circles-web/src/hooks/use-circles-chat.ts")
# The words the nudge route TOOK are the server's (pending_words:
# written before it answers, read by the turn or sent on as the thread's next
# turn, after a restart too): the hook drops its copy on the 200. Only words
# the server never took — the request failed, or the turn had no session yet —
# stay with the page and go as the next message when the turn ends.
_nrt = ch0[ch0.index("const nudgeRunningTurn = useCallback("):]
_nrt = _nrt[:_nrt.index("[echoNudges],")]
check("circles: a nudge is held until the server takes it; a taken one is the server's and never sent again; "
      "the receipt still marks its line read",
      "if (fallback) pendingNudgesRef.current.push(entry);" in _nrt
      # Matched by the id the words were nudged with, which the receipt echoes,
      # on the book's rule (app-web-kit/book/nudge.ts readIndex); by the text's
      # opening only for a receipt with no id.
      and 'it.type === "nudge_read" && it.text' in ch0
      and "readIndex(pendingNudgesRef.current, HELD_KEY, seen, readId)" in ch0
      # (the cards' opt-in nudge echo imports nudgeKey from the same module)
      and re.search(r'import \{[^}]*\breadIndex\b[^}]*\btype HeldSend\b[^}]*\} from "@kit/book/nudge";', ch0)
      and _nrt.index("if (fallback) pendingNudgesRef.current.push(entry);") < _nrt.index("const r = await nudgeSession(sid, text, entry.clientId);")
      and _nrt.index("const r = await nudgeSession(sid, text, entry.clientId);") < _nrt.index("if (r.ok) {")
      < _nrt.index("pendingNudgesRef.current.splice(at, 1);") < _nrt.index("return fallback;", _nrt.index("if (r.ok) {"))
      and _nrt.index("if (fallback) pendingNudgesRef.current.push(entry);") < _nrt.index("if (!sid) return fallback;"))
check("circles: a nudge whose request FAILED while the turn streams stays pending and goes as the next message (never a send whose guard refuses)",
      "      return fallback;\n    },\n    [echoNudges]," in ch0
      and 'if (status !== "ready" && status !== "error") return;' in ch0
      and "void send(p.target, p.text, { threadId: p.threadId, onAccepted: settle, clientId: p.clientId })" in ch0
      and "settle();\n        setDrainTick((t) => t + 1);" in ch0
      and "}, [status, drainTick]);" in ch0)
bs = src("server/brain_service.py")
check("runtime: a drained nudge is receipted to the client as a 2: nudge_read frame (its own type — never a second `nudge` bubble)",
      '"event": "nudge_read"' in turn_source() and '{"type": "nudge_read", "text": event.get("text") or ""}' in bs
      and '{"type": "nudge", "text"' not in bs)
# The topic that asked is the ticket's own (asked_in, read for every topic of
# the conversation at once), not the one the person last wrote to; only a card
# raised live before that read falls back to the running turn's topic.
answer = circ.split("const answerAsk")[1].split("const notNowAsk")[0]
check("circles: the message goes to the THREAD that asked, never the original conversation",
      "const topic = topicOfAsk(ask.ticketId);" in answer
      and "await tellHost(answerText(ask, option, note), topic);" in answer
      and "void reattachRef.current(topic);" in answer
      and "askTopicRef.current.get(ticketId) ??" in circ.split("const topicOfAsk")[1].split("const tellHost")[0]
      and "const room = roomOf(tk.metadata?.asked_in);" in circ
      and 'askTopicRef.current.set(tk.id, room.slice("thread:".length));' in circ
      and "threadId: topic," in tell
      and "await send(SELF, text);" not in circ and "send(SELF, text, { threadId });" not in circ)
notnow = circ.split("const notNowAsk")[1].split("},\n    [")[0]
check("circles: Not now carries the note; a running turn is nudged; an ended turn gets the note as a message, a bare Not now nothing; decides nothing",
      "notNowText(ask, note)" in notnow and "decideTicket" not in notnow
      and "await tellHost(notNowText(ask, note), topicOfAsk(ask.ticketId), note.trim().length > 0);" in notnow
      # tellHost's `fallback` is "has a note": a bare Not now only nudges the
      # asking topic's running turn, starts no turn beside, sends no message.
      and "if (!fallback) return;" in _tell_run.split("await nudgeRunningTurn")[1]
      and "if (fallback) await send(SELF, text, { threadId: topic });" in tell)
ch = src("circles-web/src/hooks/use-circles-chat.ts")
check("circles: a ticket read that resolves after a thread switch lands nowhere; one whose thread is not yet known (reload) is kept",
      "threadKeyRef.current = threadKey;" in ch and "if (raisedOn && now && raisedOn !== now) return;" in ch
      and "reset(mapped, key);" in circ)
sess = turn_source()
exit_block = sess.split("if not tool_calls:")[1].split("break  # Normal text response, exit loop")[0]
check("runtime: a nudge that arrives while the closing reply streams is seen at loop exit and gets a round — "
      "left queued for the drain, never taken off and pushed back",
      "if has_pending_nudge(sess.session_id):" in exit_block and "consume_nudges(" not in exit_block
      and "push_nudge(" not in exit_block and exit_block.rstrip().endswith("continue"))
drain = sess.split("_nudges_timed = consume_nudges_timed(sess.session_id)")[1].split("for nt in _nudges:")[0]
check("runtime: the reply a nudge interrupted (already streamed) goes into the model's context and the durable turn, at the ONE drain",
      # Every completion's streamed prose is part of the reply (the thread
      # IS the chain of thought) — a round whose tool calls the
      # nudge drops keeps its prose too. The
      # durable turn gets it from the single recorder, where it was sent; the
      # drain gives the model what was sent (the WM strip's remainder).
      '_seen_reply = strip_wm(turn.result.get("content") or "")' in drain
      and 'grok_messages.append({"role": "assistant", "content": _seen_reply})' in drain
      and "_record_part" not in drain and "_late_draft" not in sess)
check("runtime: ONE ordered list of what was sent (claim guard's draft included) is the stored reply",
      "_pieces: list[dict] = []" in sess and 'return join_pieces(self._pieces)' in sess
      and "content = _reply_all()" in sess and "_claim_draft" not in sess)
nudge_call = sess.split("call_model as _cm_nudge")[1].split("# METER IT")[0]
check("runtime: the nudge re-call STREAMS its reply like every other call",
      "stream_cb=await _next_stream_cb()," in nudge_call)
check("circles: the card offers the decision In full (the Decisions sheet: its context, Dismiss, and similar going forward)",
      "onDetails={onDecisionDetails}" in src("circles-web/src/cards/own-back.tsx")
      and "onDetails && (" in ov and "onClick={() => onDetails(ask.ticketId)}" in ov
      and "setDecisionsSheet({ focus: id })" in src("circles-web/src/cards/cards-app.tsx"))
wb = src("workbench-web/src/components/workbench/chat-pane.tsx")
wh = src("workbench-web/src/hooks/use-workbench-chat.ts")
# `echoId`: a page's nudge line, which gives
# way to the message when its words go as one.
check("workbench: a waiting answer carries ITS thread, lives in the hook's module (survives the pane), goes when THAT thread is idle",
      "const PENDING_SENDS: Array<{\n  key: string;\n  projectId: number | null;\n  text: string;" in wh
      and "const st = threadsRef.current[p.key]?.status" in wh and "PENDING_SENDS.splice(PENDING_SENDS.indexOf(p), 1);" in wh
      and "chat.sendWhenIdle(projectId, answerText(ask, option, note));" in wb)
check("workbench: the turn's nudge_read receipt settles a held answer (the decide spoke it into the turn)",
      'it.type === "nudge_read"' in wh and "readIndex(PENDING_SENDS, key, it.text, frameNudgeId)" in wh
      and "const settled = i >= 0 ? PENDING_SENDS.splice(i, 1)[0] : null;" in wh)
check("workbench: a pending entry is removed BEFORE its send (never twice)",
      wh.index("PENDING_SENDS.splice(PENDING_SENDS.indexOf(p), 1);") < wh.index("void send(p.projectId, p.text, { onAccepted: settle, clientId: cid })"))
check("workbench: the answer never goes through the composer's sendText (the draft stays)",
      "sendText(" not in wb.split("const onAskAnswer")[1].split("// A credential ask")[0])
nn = wb.split("const onAskNotNow")[1].split("// A credential ask")[0]
# "Not now" is remembered on the device: the project's ticket list
# raises its open tickets as cards, and a card set aside must not come back.
check("workbench: Not now sets the card aside (remembered); a note goes to the Host as the next message; a bare Not now says nothing",
      "chat.settleAsk(ask.ticketId, true);" in nn and "if (note.trim())" in nn and "notNowText(ask, note)" in nn and "decideTicket" not in nn)
# A credential ask is answered on the card (nobody should have to chase apps
# in the middle of a conversation): it no longer
# sends the person to Circles › Tickets.
check("workbench: a credential ask is sealed on the card, not sent to Circles › Tickets",
      "onSealCredential={onSealCredential}" in wb
      and '{ type: "cseries:open-app", app_id: "circles", view: "tickets" }' not in wb)
wapi = src("workbench-web/src/lib/api.ts")
check("workbench: decideTicket uses the Tickets route and never crystallises a pattern from a click",
      "`/api/tickets/${id}/decide`" in wapi and "and_similar: false" in wapi)
wman = src("system-apps/workbench/app.yaml")
check("workbench: the manifest declares the Tickets scopes the card needs",
      "  - write:tickets\n" in wman and "  - read:tickets\n" in wman)
check("circles: the manifest already holds write:tickets", "write:tickets" in src("system-apps/circles/app.yaml"))
for app, hook in (("circles", "circles-web/src/hooks/use-circles-chat.ts"),
                  ("workbench", "workbench-web/src/hooks/use-workbench-chat.ts")):
    h = src(hook)
    check(f"{app}: a settled ticket never comes back as a card on a replayed stream",
          "settledRef.current.add(ticketId);" in h and "!settledRef.current.has(ask.ticketId)" in h)
    check(f"{app}: a card is raised only after the server says the ticket is still open",
          "getTicket(ask.ticketId).then" in h and "ticketIsOpen(r.data)" in h)
    leave = "setAsks([])" if app == "circles" else "q.filter((a) => a.key !== key)"
    check(f"{app}: leaving the thread leaves its cards", leave in h)
    check(f"{app}: the settled check runs BEFORE the ticket read, and again after it",
          h.index("!settledRef.current.has(ask.ticketId)") < h.index("getTicket(ask.ticketId).then")
          < h.index("settledRef.current.has(ask.ticketId)) return;"))
check("the overlay hands Not now the person's note", "onNotNow(note)" in ov and "onNotNow: (note: string) => void" in ov)

print("\n3. the built apps carry it")
for app in ("circles", "workbench"):
    # Any chunk: Circles loads the cards from a chunk of their own.
    assets = list((ROOT / "system-apps" / app / "assets").glob("*.js"))
    built = any("Your Host is asking" in a.read_text(errors="replace") for a in assets)
    check(f"{app}: the shipped bundle contains the card", built, [a.name for a in assets])

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
