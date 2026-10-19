// The run card's model (harness-feed redesign, workbench 0.24.0): the same
// normalized frames run-frames.ts folds into terminal lines, folded instead
// into STEPS — one row per command, read, search or file write, the engine's
// messages as prose, thinking as one timed row — plus the counts, state and
// "now" the card shows. Pure, so `node --test` exercises it directly.
//
// Why steps: one Claude Code build sent
// 68 thinking heartbeats that printed as 68 "· thinking — …" lines, and a
// tool result as up to 40 lines under a "✓" with no command beside it. A step
// keeps the command and its output together and shows output only on request.

import { lastErrorLine, type RunFrame } from "./run-frames.ts";

export type ToolCat = "cmd" | "read" | "search" | "edit" | "web" | "plan" | "other";
export type StepStatus = "run" | "ok" | "err" | "unknown";

export type Step =
  | { type: "say"; id: string; t0: number; t1: number; text: string; delta: boolean }
  | {
      type: "think";
      id: string;
      t0: number;
      t1: number | null;
      text: string;
      /** the latest "extended thinking — N heartbeats" count, when the
       * engine sends one (Claude Code's status frames, ~1.5 s apart) */
      beats?: number;
    }
  | {
      type: "tool";
      id: string;
      t0: number;
      t1: number | null;
      tool: string;
      cat: ToolCat;
      args: unknown;
      status: StepStatus;
      result: unknown;
      resultLines?: number;
      exit?: number;
      orphan?: boolean;
      /** set when a later file_edit frame confirmed this edit call's write */
      editPath?: string;
      change?: string;
    }
  | {
      type: "edit";
      id: string;
      t0: number;
      t1: number | null;
      path: string;
      change: string;
      tool: string;
      args: unknown;
      status: StepStatus;
      result?: unknown;
      resultLines?: number;
    }
  | { type: "error"; id: string; t0: number; text: string }
  | { type: "retry"; id: string; t0: number; attempt: string; reason: string }
  | { type: "lost"; id: string; t0: number }
  | { type: "killed"; id: string; t0: number; by: string }
  | { type: "report"; id: string; t0: number; text: string }
  | { type: "note"; id: string; t0: number; text: string };

export type ToolStep = Extract<Step, { type: "tool" }>;
export type EditStep = Extract<Step, { type: "edit" }>;

export interface Acceptance {
  ran: boolean;
  ok: boolean;
  checks: number;
  failed: string[];
}

export interface RunModel {
  steps: Step[];
  state: "live" | "complete" | "timeout" | "failed";
  live: boolean;
  exit: number | null;
  elapsed: number | null;
  model: string | null;
  failure: string | null;
  retries: number;
  lost: boolean;
  /** Who stopped the run ("user" | "host"): the kernel's `status/killed`
   * frame or the terminal frame's `killed_by` (review.py kill_session_runs).
   * A killed run ends "failed" — nothing it was asked for finished — but it
   * reads "stopped by you", never as a failure (run-frames.ts foldSummary). */
  killedBy: string | null;
  /** queued for memory: the kernel's own reason, until the run's first frame */
  waiting: string | null;
  queuedAt: number | null;
  accept: Acceptance | null;
  usage: Record<string, unknown>[];
  tokensTotal: number | null;
  /** files the ENGINE reported writing (file_edit frames), in order */
  written: Map<string, string>;
  counts: Partial<Record<ToolCat, number>>;
  reads: Set<string>;
  errors: number;
  msgs: number;
  /** first frame the run itself wrote (a waiting frame is not the run) */
  start: number | null;
  lastT: number | null;
  lastAct: number | null;
  /** the open thinking span, while live */
  think: Extract<Step, { type: "think" }> | null;
  /** calls still waiting for their result */
  pending: (ToolStep | EditStep)[];
}

/** Lifecycle frames the vendors emit that say nothing about the work. Kept
 * in Raw; not rows. */
export const LIFECYCLE = new Set([
  "thread.started", "turn.started", "turn.completed", "init", "user_input",
  "checkpoint", "system.version", "session.resume_hint",
  // events.py: grok's mid-step tool_call_update (status null / in_progress,
  // nothing to show) and cursor's whole-message frame that repeats the
  // fragments already joined. Both printed as raw JSON rows before.
  "tool_update", "message_end",
]);

/** A frame's time in seconds: the stream's own `ts` (epoch seconds). */
const tOf = (f: RunFrame, prev: number): number =>
  typeof f.ts === "number" && Number.isFinite(f.ts) ? Math.max(f.ts, prev) : prev;

const str = (v: unknown): string => (v == null ? "" : String(v));

export function catOf(tool: string): ToolCat {
  const t = tool.toLowerCase();
  if (/^(bash|shell|run_command|exec_command|exec|terminal|run_terminal_cmd|shell_command|powershell)$/.test(t)) return "cmd";
  if (/^(read|read_file|view_file|readfile|view|open_file|view_code_item|read_many_files)$/.test(t)) return "read";
  if (/^(grep|glob|ls|list_dir|list_directory|find|find_by_name|grep_search|search|codebase_search|search_files|list_files)$/.test(t)) return "search";
  if (/^(write|edit|multiedit|write_to_file|replace_file_content|multi_replace_file_content|apply_patch|strreplacefile|writefile|str_replace|create_file|notebookedit|edit_file)$/.test(t)) return "edit";
  if (/^(webfetch|web_fetch|read_url_content|websearch|web_search|search_web)$/.test(t) || t.startsWith("browser")) return "web";
  if (/^(todowrite|todo_write|update_plan|todolist|settodolist|todo)$/.test(t)) return "plan";
  return "other";
}

export function argObj(a: unknown): Record<string, unknown> {
  return a && typeof a === "object" && !Array.isArray(a) ? (a as Record<string, unknown>) : {};
}

export function argStr(a: unknown, keys: readonly string[]): string {
  const o = argObj(a);
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "string" && v.trim()) return v;
  }
  return "";
}

export const argPath = (a: unknown): string =>
  argStr(a, ["file_path", "path", "AbsolutePath", "filePath", "target_file", "TargetFile", "notebook_path", "file"]);

/** A path relative to the run's worktree when it is inside one. Every harness
 * works in /var/lib/cseries/projects/<dir>/ (review.py prepare_workspace). */
export function shortPath(p: string): string {
  const m = /^\/var\/lib\/cseries\/projects\/[^/]+\/(.+)$/.exec(p);
  return m ? m[1] : p;
}

/** The command a shell call ran, without the `bash -lc "…"` wrapper codex and
 * others put round it (sometimes twice, sometimes with mixed quotes: build
 * 426 sent `/usr/bin/bash -lc "python - <<'PY' … EOF'`). Display only. */
export function cleanCmd(raw: string): string {
  let c = raw.trim();
  for (let k = 0; k < 3; k++) {
    const m = /^\/(?:usr\/)?bin\/(?:ba|z)?sh\s+-l?c\s+(["'])([\s\S]*?)["']?$/.exec(c.trim());
    if (!m) break;
    c = m[2].replace(/'"'"'/g, "'").replace(/'"'/g, "'").replace(/\\"/g, '"');
  }
  return c.trim();
}

export function firstLine(t: string): string {
  const l = t.split("\n").find((x) => x.trim()) ?? "";
  return l.length > 240 ? `${l.slice(0, 240)}…` : l;
}

export function foldSteps(frames: RunFrame[]): RunModel {
  const M: RunModel = {
    steps: [], state: "live", live: true, exit: null, elapsed: null, model: null,
    failure: null, retries: 0, lost: false, killedBy: null, waiting: null, queuedAt: null, accept: null,
    usage: [], tokensTotal: null, written: new Map(), counts: {}, reads: new Set(),
    errors: 0, msgs: 0, start: null, lastT: null, lastAct: null, think: null, pending: [],
  };
  let think: Extract<Step, { type: "think" }> | null = null;
  let say: Extract<Step, { type: "say" }> | null = null;
  const pending: (ToolStep | EditStep)[] = [];
  const byId = new Map<string, ToolStep | EditStep>();
  const seen = new Set<number>();
  let t = 0;
  const add = <S extends Step>(s: S): S => (M.steps.push(s), s);

  for (const f of frames) {
    // from_seq replay can re-deliver the boundary frame; fold each seq once.
    if (typeof f.seq === "number") {
      if (seen.has(f.seq)) continue;
      seen.add(f.seq);
    }
    t = tOf(f, t);
    M.lastT = t;
    const p = f.payload ?? {};
    const k = f.kind;
    const st = k === "status" ? str(p.state) : "";
    const id = `s${f.seq}`;
    if (k === "usage") { M.usage.push(p); continue; }
    if (st === "waiting") {
      M.waiting = str(p.detail) || "waiting for memory";
      if (M.queuedAt == null) M.queuedAt = t;
      continue;
    }
    M.waiting = null;
    if (LIFECYCLE.has(st)) continue;
    if (M.start == null) M.start = t;
    const isThink = k === "thinking" || st === "thinking";
    if (!isThink && think) { think.t1 = t; think = null; }
    if (k !== "text") say = null;
    M.lastAct = t;
    if (isThink) {
      if (!think) think = add({ type: "think", id, t0: t, t1: null, text: "" });
      const hb = /(\d+)\s+heartbeats?\b/.exec(str(p.detail));
      if (hb) think.beats = Math.max(think.beats ?? 0, Number(hb[1]));
      if (k === "thinking") {
        const tx = str(p.text);
        think.text += p.delta === true || !think.text ? tx : `\n\n${tx}`;
      }
      continue;
    }
    switch (k) {
      case "text": {
        const tx = str(p.text);
        if (!tx.trim() && p.delta !== true) break;
        if (p.delta === true && say && say.delta) { say.text += tx; say.t1 = t; }
        else { say = add({ type: "say", id, t0: t, t1: t, text: tx, delta: p.delta === true }); M.msgs++; }
        break;
      }
      case "tool_call": {
        const tool = str(p.tool);
        const cat = catOf(tool);
        const s = add<ToolStep>({ type: "tool", id, t0: t, t1: null, tool, cat, args: p.args ?? p.input, status: "run", result: null });
        pending.push(s);
        if (p.id != null) byId.set(str(p.id), s);
        M.counts[cat] = (M.counts[cat] ?? 0) + 1;
        if (cat === "read") M.reads.add(argPath(s.args) || id);
        break;
      }
      case "tool_result": {
        let s = p.id != null ? byId.get(str(p.id)) : undefined;
        if (s && s.status !== "run") s = undefined;
        if (!s && p.tool) {
          const tl = str(p.tool).toLowerCase();
          s = pending.find((x) => x.status === "run" && x.tool.toLowerCase() === tl);
        }
        if (!s) s = pending.find((x) => x.status === "run");
        const err = p.status === "error" || p.status === "failed";
        const res = p.result ?? p.output ?? p.content;
        if (s) {
          s.status = err ? "err" : "ok";
          s.result = res;
          s.t1 = t;
          if (s.type === "tool" && typeof p.exit_code === "number") s.exit = p.exit_code;
          pending.splice(pending.indexOf(s), 1);
        } else {
          add<ToolStep>({ type: "tool", id, t0: t, t1: t, tool: str(p.tool) || "output", cat: "other", args: null, status: err ? "err" : "ok", result: res, orphan: true });
        }
        if (err) M.errors++;
        break;
      }
      case "file_edit": {
        const path = str(p.path) || "file";
        const change = str(p.change) || (/write|create/i.test(str(p.tool)) ? "write" : "edit");
        if (!p.tool) {
          // antigravity sends the write_to_file CALL and then a file_edit for
          // the same path: one row, not "Edited x" and "Wrote x".
          const call = [...M.steps].reverse().find(
            (x): x is ToolStep => x.type === "tool" && x.cat === "edit" && !x.editPath,
          );
          if (call && shortPath(argPath(call.args)) === shortPath(path)) {
            call.editPath = path;
            call.change = change;
            if (!M.written.has(path)) M.written.set(path, change);
            break;
          }
        }
        const s = add<EditStep>({
          type: "edit", id, t0: t, t1: p.tool ? null : t, path, change, tool: str(p.tool),
          args: p.args, status: p.tool ? "run" : "ok",
        });
        if (p.tool) pending.push(s);
        if (p.id != null) byId.set(str(p.id), s);
        if (!M.written.has(path)) M.written.set(path, change);
        break;
      }
      case "error":
        M.failure = str(p.message) || "error";
        M.errors++;
        add({ type: "error", id, t0: t, text: M.failure });
        break;
      case "status": {
        if (st === "retrying") {
          M.retries++;
          add({ type: "retry", id, t0: t, attempt: str(p.attempt) || "2", reason: str(p.reason) });
          break;
        }
        if (st === "stream_lost") { M.lost = true; add({ type: "lost", id, t0: t }); break; }
        if (st === "killed") {
          // Does not close the run: the terminal frame follows.
          M.killedBy = str(p.by) || "user";
          add({ type: "killed", id, t0: t, by: M.killedBy });
          break;
        }
        if (st === "complete" || st === "timeout") {
          const ok = p.ok === true;
          M.state = st === "timeout" ? "timeout" : ok ? "complete" : "failed";
          M.exit = typeof p.exit_code === "number" ? p.exit_code : null;
          M.elapsed = typeof p.elapsed_seconds === "number" ? p.elapsed_seconds : null;
          M.model = str(p.model) || null;
          if (p.killed_by) M.killedBy = str(p.killed_by);
          if (typeof p.tokens_total === "number") M.tokensTotal = p.tokens_total;
          if (typeof p.retries === "number" && p.retries > M.retries) M.retries = p.retries;
          if (!ok) { const l = lastErrorLine(str(p.stderr)); if (l) M.failure = l; }
          break;
        }
        if (st === "acceptance") {
          const failed = Array.isArray(p.failed) ? p.failed.map(str) : [];
          M.accept = { ran: p.ran === true, ok: p.ok === true, checks: typeof p.checks === "number" ? p.checks : failed.length, failed };
          break;
        }
        if (st === "done") {
          // antigravity's closing report rides on its `done` frame.
          const d = str(p.detail);
          if (d.trim().length > 80) add({ type: "report", id, t0: t, text: d });
          break;
        }
        add({ type: "note", id, t0: t, text: st + (p.detail ? ` — ${str(p.detail)}` : "") });
        break;
      }
      default: {
        // Never drop a frame silently: an unknown kind with text is prose.
        const tx = str(p.text);
        if (tx.trim()) add({ type: "say", id, t0: t, t1: t, text: tx, delta: false });
      }
    }
  }
  M.live = M.state === "live";
  if (!M.live) {
    if (think) think.t1 = M.lastT;
    think = null;
    for (const s of pending) if (s.status === "run") s.status = "unknown";
  }
  M.think = think;
  M.pending = pending;
  return M;
}

/** A thinking block with no text that lasted under 3s carries nothing a row
 * could show (Claude Code's "thinking block (text not returned)"). It stays
 * in the strip and in Raw. */
export const quietThink = (s: Step): boolean =>
  s.type === "think" && !s.text.trim() && s.t1 != null && s.t1 - s.t0 < 3;

export type Item = Step | { type: "group"; id: string; steps: ToolStep[] };

/** Three or more reads/commands/searches in a row fold into one row, so the
 * engine's messages and the files it writes stay in view. */
export function groupSteps(steps: Step[]): Item[] {
  const out: Item[] = [];
  let buf: ToolStep[] = [];
  const flush = () => {
    if (buf.length >= 3) out.push({ type: "group", id: `g${buf[0].id}`, steps: buf });
    else out.push(...buf);
    buf = [];
  };
  for (const s of steps) {
    if (quietThink(s)) continue;
    if (s.type === "tool" && s.cat !== "plan" && s.cat !== "edit") buf.push(s);
    else { flush(); out.push(s); }
  }
  flush();
  return out;
}

const plural = (n: number, w: string, ws?: string) => `${n} ${n === 1 ? w : ws ?? `${w}s`}`;

export function groupTitle(steps: ToolStep[]): string {
  const c: Partial<Record<ToolCat, number>> = {};
  const reads = new Set<string>();
  for (const s of steps) {
    c[s.cat] = (c[s.cat] ?? 0) + 1;
    if (s.cat === "read") reads.add(argPath(s.args) || s.id);
  }
  const parts: string[] = [];
  if (c.read) parts.push(`read ${plural(reads.size, "file")}`);
  if (c.cmd) parts.push(`ran ${plural(c.cmd, "command")}`);
  if (c.search) parts.push(plural(c.search, "search", "searches"));
  if (c.web) parts.push(plural(c.web, "web lookup"));
  if (c.other) parts.push(plural(c.other, "other tool call"));
  const t = parts.join(", ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/** What a step's row says. `subject` is plain text; `code` marks it as a
 * command/pattern (mono); `path` marks it as a file path; `extra` is the dim
 * trailing note ("lines 1–240", "+10 lines"). */
export interface StepLabel {
  verb: string;
  subject: string;
  kind: "text" | "code" | "path" | "none";
  extra?: string;
  /** the full command, for the output block */
  cmd?: string;
}

export function toolLabel(s: ToolStep, live: boolean): StepLabel {
  const a = argObj(s.args);
  const ing = live && s.status === "run";
  switch (s.cat) {
    case "cmd": {
      const c = cleanCmd(argStr(s.args, ["command", "cmd", "CommandLine", "script"]) || (typeof s.args === "string" ? s.args : ""));
      const d = typeof a.description === "string" ? a.description : "";
      const more = c.split("\n").filter((x) => x.trim()).length - 1;
      return d
        ? { verb: ing ? "Running" : "Ran", subject: d, kind: "text", cmd: c }
        : { verb: ing ? "Running" : "Ran", subject: firstLine(c), kind: "code", extra: more > 0 ? `+${plural(more, "line")}` : undefined, cmd: c };
    }
    case "read": {
      const p = argPath(s.args);
      const off = typeof a.offset === "number" ? a.offset : typeof a.StartLine === "number" ? a.StartLine : null;
      const lim = typeof a.limit === "number" ? a.limit : null;
      const extra = off != null || lim != null ? `lines ${off || 1}${lim ? `–${(off || 1) + lim - 1}` : "+"}` : undefined;
      return p ? { verb: ing ? "Reading" : "Read", subject: p, kind: "path", extra } : { verb: ing ? "Reading" : "Read", subject: s.tool, kind: "text" };
    }
    case "search": {
      const q = argStr(s.args, ["pattern", "query", "Query", "regex", "glob", "Pattern"]);
      const p = argPath(s.args) || argStr(s.args, ["SearchPath"]);
      return { verb: ing ? "Searching" : "Searched", subject: q || s.tool, kind: q ? "code" : "text", extra: p ? `in ${shortPath(p)}` : undefined };
    }
    case "web": {
      const q = argStr(s.args, ["query", "url", "Url", "q"]);
      const search = /search/i.test(s.tool);
      return { verb: search ? (ing ? "Searching the web" : "Searched the web") : ing ? "Fetching" : "Fetched", subject: q, kind: q ? "code" : "none" };
    }
    case "edit": {
      const p = argPath(s.args) || s.editPath || "";
      const w = /write|create/i.test(s.change || s.tool);
      const verb = s.tool === "apply_patch" ? (ing ? "Applying a patch" : "Applied a patch") : w ? (ing ? "Writing" : "Wrote") : ing ? "Editing" : "Edited";
      return { verb, subject: p, kind: p ? "path" : "none" };
    }
    case "plan":
      return { verb: ing ? "Updating the plan" : "Updated the plan", subject: "", kind: "none" };
    default: {
      const rest = Object.entries(a)
        .filter(([key]) => !key.startsWith("__"))
        .map(([key, v]) => `${key}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
        .join(", ");
      return { verb: s.orphan ? "Output" : s.tool || "Tool", subject: rest.slice(0, 200), kind: rest ? "text" : "none" };
    }
  }
}

export function editVerb(s: EditStep, live: boolean): string {
  const ing = live && s.status === "run";
  if (/write|create|add/i.test(s.change)) return ing ? "Writing" : "Wrote";
  if (s.change === "delete") return "Deleted";
  return ing ? "Editing" : "Edited";
}

/** What the engine is doing this second, for the card's footer. */
export type NowLine =
  | { kind: "waiting"; detail: string; since: number | null }
  | { kind: "starting" }
  | { kind: "lost" }
  | { kind: "thinking"; since: number; beats?: number }
  | { kind: "step"; step: ToolStep | EditStep; since: number }
  | { kind: "writing" }
  | { kind: "working"; lastAct: number | null };

export function nowLine(M: RunModel, now: number): NowLine | null {
  if (!M.live) return null;
  if (M.waiting != null) return { kind: "waiting", detail: M.waiting, since: M.queuedAt };
  if (M.start == null) return { kind: "starting" };
  if (M.lost) return { kind: "lost" };
  if (M.think) {
    return M.think.beats != null
      ? { kind: "thinking", since: M.think.t0, beats: M.think.beats }
      : { kind: "thinking", since: M.think.t0 };
  }
  const run = [...M.pending].reverse().find((s) => s.status === "run");
  if (run) return { kind: "step", step: run, since: run.t0 };
  const last = M.steps[M.steps.length - 1];
  if (last && last.type === "say" && now - last.t1 < 3) return { kind: "writing" };
  return { kind: "working", lastAct: M.lastAct ?? M.start };
}

/** The stats line when the engine has run no tool and written no file yet.
 * A thinking stretch IS activity (build 434 sent a thinking frame
 * every ~24 s for five minutes while this line read "No activity yet"). */
export function activityNote(M: RunModel, now: number): string {
  const beats = (n?: number) => (n ? ` · ${n} heartbeat${n === 1 ? "" : "s"}` : "");
  if (M.live && M.think) return `Thinking ${clock(now - M.think.t0)}${beats(M.think.beats)}`;
  const thought = M.steps.filter((s): s is Extract<Step, { type: "think" }> => s.type === "think");
  if (thought.length) {
    const d = thought.reduce((a, s) => a + ((s.t1 ?? s.t0) - s.t0), 0);
    return d >= 1 ? `Thought for ${dur(d)}` : "Thought briefly";
  }
  return "No activity yet";
}

/** What the run card shows as "What your Host sent": the message THIS run's
 * harness actually received (contracts and learned preferences included),
 * else the task prompt older runs stored. The manifest keys it by run label,
 * `sent_prompt: {<label>: <string>}` (review.py _record_sent_prompt), because
 * a fan-out sends each variant its own string; any other shape, a missing key
 * or an empty entry counts as not recorded. Never returns "". */
export function hostBrief(
  m: { prompt?: unknown; sent_prompt?: unknown } | undefined | null,
  label: string | null | undefined,
): string | undefined {
  if (!m) return undefined;
  const sent = m.sent_prompt;
  if (label && sent && typeof sent === "object" && !Array.isArray(sent) && Object.hasOwn(sent, label)) {
    const v = (sent as Record<string, unknown>)[label];
    if (typeof v === "string" && v) return v;
  }
  return typeof m.prompt === "string" && m.prompt ? m.prompt : undefined;
}

/** Tokens as the engine reported them: the terminal frame's total when it
 * names one, else a single usage frame's in/out. Several usage frames are
 * not summed — their semantics differ by vendor and are unverified. */
export function tokensLine(M: RunModel): string | null {
  const k = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(n));
  if (M.tokensTotal) return `${k(M.tokensTotal)} tokens`;
  if (M.usage.length === 1) {
    const u = M.usage[0];
    const i = typeof u.input_tokens === "number" ? u.input_tokens : 0;
    const o = typeof u.output_tokens === "number" ? u.output_tokens : 0;
    if (i || o) return `${k(i)} in · ${k(o)} out`;
  }
  return null;
}

export interface PatchFile {
  path: string;
  add: number;
  del: number;
  status: "added" | "modified" | "deleted";
  lines: string[];
}

/** A run's patch (the diff route: diffs/<harness>.patch) per file. */
export function parsePatch(text: string): PatchFile[] {
  const files: PatchFile[] = [];
  let cur: PatchFile | null = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
      cur = { path: m ? m[2] : line.slice(11), add: 0, del: 0, status: "modified", lines: [] };
      files.push(cur);
      continue;
    }
    if (!cur) continue;
    if (line.startsWith("new file mode")) { cur.status = "added"; continue; }
    if (line.startsWith("deleted file mode")) { cur.status = "deleted"; continue; }
    if (/^(index |similarity|rename |old mode|new mode|--- |\+\+\+ |Binary files)/.test(line)) continue;
    if (line.startsWith("+")) cur.add++;
    else if (line.startsWith("-")) cur.del++;
    cur.lines.push(line);
  }
  return files;
}

/** A markdown table's line in an engine's prose: `| a | b |`. */
export const isTableLine = (line: string): boolean => /^\s*\|.*\|\s*$/.test(line);

/** A table line's cells, trimmed, without the outer pipes. */
export const tableCells = (line: string): string[] =>
  line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

/** A run of table lines as a table (app.js:283-290): a separator row
 * (`|---|:--:|`) right under the first row makes that row the header, and
 * separator rows are never drawn. */
export function tableOf(rows: string[][]): { head: string[] | null; body: string[][] } {
  const sep = (r: string[]) => r.length > 0 && r.every((c) => /^:?-{3,}:?$/.test(c));
  const body = rows.filter((r) => !sep(r));
  const head = rows.length > 1 && sep(rows[1]) ? (body.shift() ?? null) : null;
  return { head, body };
}

export const ENGINE_NAMES: Record<string, [string, string]> = {
  "claude-code": ["Claude Code", "CC"],
  codex: ["Codex", "CX"],
  grok: ["Grok", "GK"],
  kimi: ["Kimi Code", "KM"],
  antigravity: ["Antigravity", "AG"],
  cursor: ["Cursor", "CU"],
  opencode: ["opencode", "OC"],
};

/** A variant id is the harness, or harness-sN for a Self-MoA sample
 * (review.py variant labels: "codex-s2"). */
export function engineName(harness: string): string {
  const m = /^(.+?)-s(\d+)$/.exec(harness);
  const base = m && ENGINE_NAMES[m[1]] ? m[1] : harness;
  const name = ENGINE_NAMES[base]?.[0] ?? harness;
  return m && ENGINE_NAMES[m[1]] ? `${name} · sample ${m[2]}` : name;
}

export function engineMono(harness: string): string {
  const base = /^(.+?)-s\d+$/.exec(harness)?.[1] ?? harness;
  return ENGINE_NAMES[base]?.[1] ?? harness.slice(0, 2).toUpperCase();
}

export function clock(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = String(s % 60).padStart(2, "0");
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

export function dur(sec: number): string {
  const t = Math.round(sec);
  if (t < 60) return `${t}s`;
  const m = Math.floor(t / 60);
  return m < 60 ? `${m}m ${t % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export const durShort = (sec: number): string => (sec < 1 ? `${Math.max(0, sec).toFixed(1)}s` : sec < 60 ? `${Math.round(sec)}s` : dur(sec));
