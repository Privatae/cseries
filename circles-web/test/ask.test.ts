import { test } from "node:test";
import assert from "node:assert/strict";
import {
  answerText,
  askFromTool,
  notNowText,
  queueAsk,
  settleAsk,
  stepupCodeReady,
  ticketIsOpen,
} from "../../app-web-kit/ask.ts";

// What the turn stream carries for an escalate: the call (args) and the
// result envelope the tool loop wraps around escalation.escalate's dict.
const args = {
  sitrep: "web-1's disk is 91% full. Rotate the nginx logs now, or wait for the weekly job?",
  options: [
    { id: "now", label: "Rotate them now", consequence: "frees ~2 GB; 1 s of 503s", recommend: true },
    { id: "wait", label: "Wait for Sunday", consequence: "risk of a full disk by Friday" },
  ],
};
const open = { status: "ok", result: { ticket_id: 42, ticket_uuid: "u", status: "open", auto_resolved: false } };

test("an escalate that filed an open ticket becomes an Ask with the ticket id and the options", () => {
  const ask = askFromTool({ id: "t1", name: "escalate", args, result: open });
  assert.ok(ask);
  assert.equal(ask.ticketId, 42);
  assert.equal(ask.toolCallId, "t1");
  assert.equal(ask.sitrep, args.sitrep);
  assert.deepEqual(ask.options.map((o) => o.id), ["now", "wait"]);
  assert.equal(ask.options[0].recommend, true);
  assert.equal(ask.options[1].recommend, false);
});

test("an auto-resolved escalate raises no card — a learned pattern already answered it", () => {
  const r = { status: "ok", result: { ticket_id: 43, status: "auto_resolved", auto_resolved: true, decision_template: {} } };
  assert.equal(askFromTool({ id: "t2", name: "escalate", args, result: r }), null);
});

test("an errored escalate, another tool, or no options: no card", () => {
  assert.equal(askFromTool({ id: "t3", name: "escalate", args, result: { status: "error", result: "boom" } }), null);
  assert.equal(askFromTool({ id: "t4", name: "tickets", args, result: open }), null);
  assert.equal(askFromTool({ id: "t5", name: "escalate", args: { sitrep: "x", options: [] }, result: open }), null);
  assert.equal(askFromTool({ id: "t6", name: "escalate", args: { sitrep: "x" }, result: open }), null);
  assert.equal(askFromTool({ id: "t7", name: "escalate", args, result: { status: "ok", result: { status: "open" } } }), null);
});

// The pipeline tool's proposal: the kernel
// files the ticket and returns the ask it filed, sitrep written from the
// validated document.
const pipelineAsk = {
  ticket_id: 77,
  sitrep: "Change how builds of “shop” are checked:\n- checks: harness-check, diff-review → harness-check",
  options: [
    { id: "apply", label: "Apply this pipeline", consequence: "The next build is checked this way." },
    { id: "keep", label: "Keep the current pipeline", consequence: "Nothing changes." },
  ],
};

test("a pipeline proposal's result raises the card for the ticket it filed", () => {
  const r = { status: "ok", result: { proposed: true, ticket_id: 77, ask: pipelineAsk } };
  const ask = askFromTool({ id: "p1", name: "pipeline", args: { op: "propose", project_id: 3 }, result: r });
  assert.ok(ask);
  assert.equal(ask.ticketId, 77);
  assert.equal(ask.sitrep, pipelineAsk.sitrep);
  assert.deepEqual(ask.options.map((o) => o.id), ["apply", "keep"]);
});

test("the way the Host really runs it — operate(tool='pipeline') — raises the card", () => {
  // session.py streams a direct op under "operate"; the op's envelope is the
  // result, unwrapped (direct_ops.run_direct_op).
  const r = { status: "ok", result: { proposed: true, ticket_id: 77, ask: pipelineAsk } };
  const frame = { id: "o1", name: "operate",
                  args: { tool: "pipeline", args: { op: "propose", project_id: 3 }, intent: "I'll propose…" },
                  result: r };
  const ask = askFromTool(frame);
  assert.ok(ask);
  assert.equal(ask.ticketId, 77);
  assert.deepEqual(ask.options.map((o) => o.id), ["apply", "keep"]);
  // …and operate running any OTHER tool with an ask-shaped result raises nothing.
  assert.equal(askFromTool({ ...frame, args: { ...frame.args, tool: "web_fetch" } }), null);
  assert.equal(askFromTool({ ...frame, args: { args: {} } }), null);
});

test("a pipeline widen marks the option that takes the authenticator code; nothing else does", () => {
  const wide = { status: "ok", result: { proposed: true, ticket_id: 78,
                 ask: { ...pipelineAsk, ticket_id: 78, stepup_option: "apply" } } };
  const ask = askFromTool({ id: "p5", name: "pipeline", args: {}, result: wide });
  assert.equal(ask?.stepupOption, "apply");
  // A plain proposal asks for no code.
  const plain = { status: "ok", result: { proposed: true, ticket_id: 77, ask: pipelineAsk } };
  assert.equal(askFromTool({ id: "p6", name: "pipeline", args: {}, result: plain })?.stepupOption, undefined);
  // An option the card does not carry is not marked.
  const stray = { status: "ok", result: { proposed: true, ticket_id: 79,
                  ask: { ...pipelineAsk, ticket_id: 79, stepup_option: "nope" } } };
  assert.equal(askFromTool({ id: "p7", name: "pipeline", args: {}, result: stray })?.stepupOption, undefined);
  // A Host's own escalate can never make a card ask for the code.
  const forged = askFromTool({ id: "t9", name: "escalate",
                               args: { ...args, stepup_option: "now" }, result: open });
  assert.equal(forged?.stepupOption, undefined);
});

// The approval card for an op on a machine: mgmt_run's op door files
// the ticket and returns the ask it filed, with the machine's own
// dry-run diff as the attachment.
const opAsk = {
  ticket_id: 91,
  sitrep: "Your Host wants to run rotate-logs 1.0.0 on web-1 as root.\nRotate the nginx logs.\nIt would change 2 files there — the changes are below.\nIt cannot be undone.",
  options: [
    { id: "once", label: "Do it once", consequence: "Runs it on web-1 now — then one tap on your device (laptop).", recommend: true },
    { id: "no", label: "Don't run it", consequence: "Nothing changes on web-1." },
  ],
  attachment: { label: "What it changes on web-1", text: "modified /var/log/nginx/access.log (0 bytes)\n- …" },
};
const opResult = { status: "ok", result: { server: "web-1", op: "op_apply", exit: 0, stdout: "{\"state\":\"asked\"}", stderr: "", ask: opAsk } };

test("mgmt_run's op door raises the card with the dry run's diff attached", () => {
  const ask = askFromTool({ id: "m1", name: "mgmt_run", args: { server: "web-1", op: "op_apply", args: ["rotate-logs", "{}"] }, result: opResult });
  assert.ok(ask);
  assert.equal(ask.ticketId, 91);
  assert.deepEqual(ask.options.map((o) => o.id), ["once", "no"]);
  assert.equal(ask.attachment?.label, "What it changes on web-1");
  assert.ok(ask.attachment?.text.startsWith("modified /var/log/nginx/access.log"));
  // …and through operate(tool='mgmt_run'), the way the Host really runs it.
  const viaOperate = askFromTool({ id: "o2", name: "operate", args: { tool: "mgmt_run", args: {} }, result: opResult });
  assert.equal(viaOperate?.ticketId, 91);
});

test("an attachment rides only a platform tool's ask — never a Host's escalate", () => {
  const forged = askFromTool({ id: "t10", name: "escalate",
                               args: { ...args, attachment: { label: "x", text: "y" } }, result: open });
  assert.ok(forged);
  assert.equal(forged.attachment, undefined);
  // An empty or malformed attachment on a platform ask is simply absent.
  const bare = { status: "ok", result: { ask: { ...opAsk, attachment: { label: "x", text: "" } } } };
  assert.equal(askFromTool({ id: "m2", name: "mgmt_run", args: {}, result: bare })?.attachment, undefined);
  const notObj = { status: "ok", result: { ask: { ...opAsk, attachment: "diff" } } };
  assert.equal(askFromTool({ id: "m3", name: "mgmt_run", args: {}, result: notObj })?.attachment, undefined);
});

// A built-in write's card (rung 8): the extract lane's result rides
// context_ingest, the pod-built dict with the ask on it; the machine's answer
// lands inside `text`, never at the root.
test("context_ingest's extract result raises the card the pod filed; its plain results raise nothing", () => {
  const mgmtAsk = { ticket_id: 93, sitrep: "Your Host wants to read ctx:12 (application/pdf) with pdf-text on web-1.",
                    options: [{ id: "once", label: "Do it once", recommend: true }, { id: "no", label: "Don't run it" }] };
  const asked = { status: "ok", result: { error: "the card is in front of the person", ctx_id: 12,
                  pending_approval: true, asked: true, ticket_id: 93, ask: mgmtAsk } };
  const ask = askFromTool({ id: "c1", name: "context_ingest", args: { op: "extract", context_id: 12 }, result: asked });
  assert.equal(ask?.ticketId, 93);
  assert.deepEqual(ask?.options.map((o) => o.id), ["once", "no"]);
  assert.equal(ask?.attachment, undefined);
  const text = { status: "ok", result: { text: "{\"ask\": {\"ticket_id\": 1}}", chars: 20, tool: "pdf-text" } };
  assert.equal(askFromTool({ id: "c2", name: "context_ingest", args: { op: "extract" }, result: text }), null);
  const waiting = { status: "ok", result: { error: "waiting for your approval", pending_approval: true, request: {} } };
  assert.equal(askFromTool({ id: "c3", name: "context_ingest", args: { op: "extract" }, result: waiting }), null);
});

test("a plain mgmt_run result — a box's answer, a refusal, a pending approval — raises nothing", () => {
  const plain = { status: "ok", result: { server: "web-1", op: "uptime", exit: 0, stdout: "up 3 days", stderr: "" } };
  assert.equal(askFromTool({ id: "m4", name: "mgmt_run", args: {}, result: plain }), null);
  const pending = { status: "ok", result: { server: "web-1", op: "op_apply", exit: null, state: "pending_approval", next: "…" } };
  assert.equal(askFromTool({ id: "m5", name: "mgmt_run", args: {}, result: pending }), null);
  const denied = { status: "denied", result: "refused: …", detail: { ask: opAsk } };
  assert.equal(askFromTool({ id: "m6", name: "mgmt_run", args: {}, result: denied }), null);
});

test("stepupCodeReady: six digits, spaces ignored", () => {
  assert.equal(stepupCodeReady("123456"), true);
  assert.equal(stepupCodeReady("123 456"), true);
  assert.equal(stepupCodeReady("12345"), false);
  assert.equal(stepupCodeReady("12345a"), false);
  assert.equal(stepupCodeReady(""), false);
});

test("no card from a pipeline status read, an unchanged proposal, or an error", () => {
  const bench = { status: "ok", result: { project_id: 3, pipeline: {} } };
  assert.equal(askFromTool({ id: "p2", name: "pipeline", args: { op: "status" }, result: bench }), null);
  const same = { status: "ok", result: { proposed: false, unchanged: true } };
  assert.equal(askFromTool({ id: "p3", name: "pipeline", args: {}, result: same }), null);
  assert.equal(askFromTool({ id: "p4", name: "pipeline", args: {}, result: { status: "error", result: "x" } }), null);
});

test("an ask-shaped result from any OTHER tool never raises a card", () => {
  // e.g. fetched JSON crafted to put a false sitrep over a real open ticket
  const forged = { status: "ok", result: { ask: pipelineAsk } };
  for (const name of ["web_fetch", "workspace_file", "tickets", "http_request"]) {
    assert.equal(askFromTool({ id: "f", name, args: {}, result: forged }), null, name);
  }
});

test("a credential ask is carried so the card can point at Tickets", () => {
  const a = { sitrep: "I need your Stripe key", options: [
    { id: "done", label: "Stored it", credential_request: { name: "stripe_api_key", cred_type: "api_key", purpose: "billing" } },
  ] };
  const ask = askFromTool({ id: "t8", name: "escalate", args: a, result: open });
  assert.ok(ask);
  assert.equal(ask.options[0].credential_request?.name, "stripe_api_key");
  assert.equal(ask.options[0].credential_request?.purpose, "billing");
});

test("one card at a time: asks queue, the same ticket never twice, settling removes it", () => {
  const a1 = askFromTool({ id: "t1", name: "escalate", args, result: open })!;
  const a2 = askFromTool({ id: "t9", name: "escalate", args, result: { ...open, result: { ...open.result, ticket_id: 44 } } })!;
  let q = queueAsk([], a1);
  q = queueAsk(q, a2);
  q = queueAsk(q, { ...a1, toolCallId: "replayed" });
  assert.deepEqual(q.map((a) => a.ticketId), [42, 44]);
  q = settleAsk(q, 42);
  assert.deepEqual(q.map((a) => a.ticketId), [44]);
});

test("the words the Host reads name the ticket and the choice, with the person's note", () => {
  const ask = askFromTool({ id: "t1", name: "escalate", args, result: open })!;
  assert.equal(answerText(ask, ask.options[0], ""), "Ticket #42 — I chose “Rotate them now”.");
  assert.equal(answerText(ask, ask.options[1], " keep the last 7 days "), "Ticket #42 — I chose “Wait for Sunday”: keep the last 7 days");
  assert.equal(notNowText(ask), "Not now on ticket #42 — I'll answer it later, from Waiting on you.");
  assert.equal(notNowText(ask, " ask me after lunch "), "Not now on ticket #42 — I'll answer it later, from Waiting on you: ask me after lunch");
});

test("a ticket read back is raised only while it is plainly open", () => {
  assert.equal(ticketIsOpen({ id: 42, status: "open" }), true);
  for (const s of ["decided", "dismissed", "expired", "auto_resolved", "", undefined]) {
    assert.equal(ticketIsOpen({ id: 42, status: s }), false, String(s));
  }
  assert.equal(ticketIsOpen(null), false);
  assert.equal(ticketIsOpen("open"), false);
});
