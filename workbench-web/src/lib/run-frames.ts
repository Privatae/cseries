// Pure logic for the inline run terminal —
// frame→line rendering and the summary fold. Split from run-terminal.tsx so
// `node --test` exercises it directly (the run-live.ts pattern).
//
// The wire contract is the harness runtime's normalized event stream
// (system-apps/_harness_runtime/events.py): one JSON object per NDJSON line,
//   {ts, seq, kind, payload}   kind ∈ text | thinking | tool_call |
//   tool_result | file_edit | usage | error | status
// served by GET /api/review/sessions/{id}/stream/{harness}?from_seq=N
// (token-gated; the shell's fetch shim carries the app token). The final
// status frame's payload is the /run-shaped result (state complete|timeout,
// ok, exit_code, elapsed_seconds, model, retries?, first_attempt?); a
// mid-run {state:"retrying", attempt, reason} frame marks a transient
// provider-failure re-run (harness_core RUN_RETRY).

export interface RunFrame {
  ts?: number;
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
}

export type RunLineStyle =
  | "text"
  | "thinking"
  | "tool"
  | "file"
  | "error"
  | "status"
  | "retry";

export interface RunLine {
  seq: number;
  style: RunLineStyle;
  text: string;
}

export interface RunSummary {
  /** live until the terminal status frame lands. */
  state: "live" | "complete" | "timeout" | "failed";
  exitCode: number | null;
  elapsedS: number | null;
  model: string | null;
  /** paths seen in file_edit frames, deduped, order kept. */
  files: string[];
  retries: number;
  /** the CLI's own failure line (terminal error/status detail), if any. */
  failureLine: string | null;
  /** The kernel's NDJSON stream died and the run continued on buffered /run,
   * which emits no frames — so the scrollback stops here even though the run
   * did not. Kept separate from `state`: the RUN is still live, the FEED is
   * not, and showing a pulsing "live" badge over a frozen transcript is what
   * made a working build read as hung. */
  streamLost: boolean;
  /** The variant has not started: it is queued for memory behind the
   * harnesses the plan holds right now (review.py _apply_capacity writes a
   * `status/waiting` frame with the plain-English reason as `detail`). Kept
   * separate from `state` like streamLost: the RUN is still live — it will
   * start — but nothing is arriving, so the terminal must read "waiting",
   * not "live" (a pulse over nothing is what made a queued build read as
   * hung) and not "failed". Cleared by the first frame the run itself
   * writes. */
  waiting: string | null;
  /** Who killed the run ("user" | "host"), from the kernel's
   * `status/killed` frame or the terminal frame's `killed_by` (review.py
   * kill_session_runs). A killed run ends as state "failed" — nothing it was
   * asked for finished — but it reads "stopped by you", never "failed". */
  killedBy: string | null;
}

export const emptySummary = (): RunSummary => ({
  state: "live",
  exitCode: null,
  elapsedS: null,
  model: null,
  files: [],
  retries: 0,
  failureLine: null,
  streamLost: false,
  waiting: null,
  killedBy: null,
});

const s = (v: unknown): string => (v == null ? "" : String(v));

/** One normalized frame → 0..n terminal lines. Never drops a frame silently:
 * unknown kinds render as text (mirrors the events.py design rule). `usage`
 * and the terminal `status` frame render no body lines — they fold into the
 * summary instead. */
export function frameToLines(f: RunFrame): RunLine[] {
  const p = f.payload ?? {};
  const mk = (style: RunLineStyle, text: string): RunLine[] =>
    text.trim() === "" ? [] : text.split("\n").map((t) => ({ seq: f.seq, style, text: t }));
  switch (f.kind) {
    case "thinking":
      return mk("thinking", s(p.text));
    case "tool_call":
      return toolCallLines(f.seq, s(p.tool), p.args ?? p.input);
    case "tool_result":
      return toolResultLines(f.seq, s(p.tool), s(p.status), p.result ?? p.output ?? p.content);
    case "file_edit":
      return mk(
        "file",
        `+ ${s(p.path) || "file"}${typeof p.bytes === "number" ? ` (${fmtBytes(p.bytes)})` : ""}`,
      );
    case "error":
      return mk("error", s(p.message) || "error");
    case "status": {
      const st = s(p.state);
      if (st === "retrying") {
        return mk(
          "retry",
          // The prefix must not name a cause. A retrying frame now comes from
          // two places — the pod re-running the CLI after a transient PROVIDER
          // error, and the kernel re-dispatching after a dropped POD
          // connection — and `reason` already says which. Hardcoding
          // "provider" mislabelled every kernel re-dispatch.
          `⟳ retrying (attempt ${s(p.attempt) || "2"})${
            p.reason ? `: ${s(p.reason)}` : ""
          }`,
        );
      }
      if (st === "stream_lost") {
        // Never rendered as an error: nothing failed, and the run is still
        // working. Say what happened AND what to expect next, because the
        // scrollback is about to stop moving.
        return mk(
          "retry",
          "⚠ live feed lost — the run is still going on the harness; " +
            "its output lands here when it finishes",
        );
      }
      if (st === "complete" || st === "timeout" || st === "done") return [];
      const detail = s(p.detail);
      return mk("status", `· ${st}${detail ? ` — ${detail}` : ""}`);
    }
    case "usage":
      return [];
    default:
      return mk("text", s(p.text));
  }
}

function oneLine(v: unknown, max = 80): string {
  const t = typeof v === "string" ? v : JSON.stringify(v);
  const flat = (t ?? "").replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

/** Body caps for tool frames (workbench 0.13.4). The feed used to print
 * "→ Bash" / "✓" and nothing else — the command the harness ran and what
 * came back were dropped (the normalizer keys them `args` / `result`; the
 * renderer read `input` and ignored results), so users could not see what the
 * host sent the coding agent or the commands the harness ran. Show the
 * substance, bounded so a `cat` of a 30 KB file cannot
 * flood the scrollback; the raw toggle always has everything. */
export const TOOL_BODY_MAX_LINES = 40;
export const TOOL_BODY_MAX_CHARS = 3000;

/** Clip a multi-line body to the caps, appending an honest "+N lines" tail. */
export function clipBody(text: string): string[] {
  const all = text.replace(/\r\n?/g, "\n").split("\n");
  const out: string[] = [];
  let chars = 0;
  for (const line of all) {
    if (out.length >= TOOL_BODY_MAX_LINES || chars + line.length > TOOL_BODY_MAX_CHARS) {
      const left = all.length - out.length;
      out.push(`… +${left} more line${left === 1 ? "" : "s"} (raw shows all)`);
      return out;
    }
    out.push(line);
    chars += line.length + 1;
  }
  return out;
}

/** The args keys that carry the substance of a call, in display order. */
const ARG_TEXT_KEYS = ["command", "cmd", "script", "query", "pattern", "prompt"] as const;
const ARG_PATH_KEYS = ["file_path", "path", "filePath", "notebook_path", "url"] as const;

/** tool_call → a header line ("→ Bash — description") plus the call's
 * substance: a shell command prints verbatim (multi-line, clipped), a file
 * op prints its path, anything else prints its args JSON on one line.
 * Content/diff blobs (Write/Edit) are sized, not dumped. */
export function toolCallLines(seq: number, tool: string, args: unknown): RunLine[] {
  const name = tool || "tool";
  const a = (args && typeof args === "object" && !Array.isArray(args)
    ? (args as Record<string, unknown>)
    : null);
  const lines: RunLine[] = [];
  const push = (style: RunLineStyle, text: string) => {
    if (text.trim() !== "") lines.push({ seq, style, text });
  };
  if (!a) {
    push("tool", `→ ${name}${args != null && args !== "" ? ` ${oneLine(args, 300)}` : ""}`);
    return lines;
  }
  const desc = typeof a.description === "string" ? a.description : "";
  const path = ARG_PATH_KEYS.map((k) => a[k]).find((v) => typeof v === "string" && v) as
    | string
    | undefined;
  push("tool", `→ ${name}${path ? ` ${path}` : ""}${desc ? ` — ${desc}` : ""}`);
  const bodyKey = ARG_TEXT_KEYS.find((k) => typeof a[k] === "string" && (a[k] as string).trim());
  if (bodyKey) {
    const prefix = bodyKey === "command" || bodyKey === "cmd" || bodyKey === "script" ? "$ " : "";
    const body = clipBody(String(a[bodyKey]));
    body.forEach((l, i) => push("text", i === 0 ? `${prefix}${l}` : l));
  }
  // Sized, never dumped: file bodies / edits.
  for (const k of ["content", "new_string", "old_string", "diff", "patch"]) {
    const v = a[k];
    if (typeof v === "string" && v) push("status", `  ${k}: ${v.length} chars`);
  }
  if (!bodyKey && !path) {
    const rest = Object.fromEntries(
      Object.entries(a).filter(([k]) => k !== "description"),
    );
    if (Object.keys(rest).length) push("text", `  ${oneLine(rest, 300)}`);
  }
  return lines;
}

/** tool_result → "✓"/"✗" header and the result body (clipped). An empty
 * result stays a one-liner; an error result renders in the error style. */
export function toolResultLines(
  seq: number,
  tool: string,
  status: string,
  result: unknown,
): RunLine[] {
  const err = status === "error" || status === "failed";
  const head = `${err ? "✗" : "✓"}${tool ? ` ${tool}` : ""}${err ? " — error" : ""}`;
  const lines: RunLine[] = [{ seq, style: err ? "error" : "tool", text: head }];
  const body = typeof result === "string" ? result : result == null ? "" : JSON.stringify(result);
  if (body.trim() === "") return lines;
  for (const l of clipBody(body)) lines.push({ seq, style: err ? "error" : "text", text: `  ${l}` });
  return lines;
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Fold one frame into the running summary. The terminal status frame
 * (state complete|timeout) closes the run and carries the /run result. */
export function foldSummary(prev: RunSummary, f: RunFrame): RunSummary {
  const p = f.payload ?? {};
  // Queued for memory: the kernel's own frame, before any run frame. Any
  // other frame means the run has started, so it clears the wait.
  if (f.kind === "status" && s(p.state) === "waiting") {
    return { ...prev, waiting: s(p.detail) || "waiting for memory" };
  }
  if (prev.waiting != null) prev = { ...prev, waiting: null };
  if (f.kind === "file_edit" && p.path && !prev.files.includes(s(p.path))) {
    return { ...prev, files: [...prev.files, s(p.path)] };
  }
  if (f.kind === "error" && p.message) {
    return { ...prev, failureLine: s(p.message) };
  }
  if (f.kind === "status") {
    const st = s(p.state);
    if (st === "retrying") return { ...prev, retries: prev.retries + 1 };
    // Does NOT close the run — the harness is still working, only the feed is
    // gone. A terminal frame follows and sets the real state: review.py
    // appends one when the fallback resolves, on BOTH branches (the
    // frames-kept path and the buffered re-run). Without it a run that lost
    // its stream would stay "live" forever.
    if (st === "stream_lost") return { ...prev, streamLost: true };
    if (st === "killed") return { ...prev, killedBy: s(p.by) || "user" };
    if (st === "complete" || st === "timeout") {
      const ok = p.ok === true;
      const killedBy = s(p.killed_by) || prev.killedBy;
      const rc = typeof p.exit_code === "number" ? p.exit_code : null;
      return {
        ...prev,
        state: st === "timeout" ? "timeout" : ok ? "complete" : "failed",
        exitCode: rc,
        elapsedS:
          typeof p.elapsed_seconds === "number" ? p.elapsed_seconds : prev.elapsedS,
        model: s(p.model) || prev.model,
        retries:
          typeof p.retries === "number" && p.retries > prev.retries
            ? p.retries
            : prev.retries,
        failureLine: ok ? prev.failureLine : lastErrorLine(s(p.stderr)) ?? prev.failureLine,
        killedBy: killedBy || null,
      };
    }
  }
  return prev;
}

/** The CLI's own failure line off a stderr tail — last line matching an
 * error marker, else the last non-empty line (mirror of the server-side
 * review._run_failure_reason pick, display-sized). */
export function lastErrorLine(stderrTail: string): string | null {
  const lines = stderrTail.split("\n").map((l) => l.trim()).filter(Boolean);
  if (lines.length === 0) return null;
  const marked = lines.filter((l) =>
    /\b(error|failed|exception|denied|unauthori[sz]ed|rate.?limit|timed?.?out|terminated|refused)\b/i.test(l),
  );
  return (marked.length ? marked[marked.length - 1] : lines[lines.length - 1]).slice(0, 300);
}

export interface FoldState {
  lines: RunLine[];
  summary: RunSummary;
  nextSeq: number;
  /** The open token-delta run: prose/reasoning chunks joined into one block
   * until a non-delta frame closes it. null when nothing is open. */
  pending: PendingDelta | null;
}

interface PendingDelta {
  style: RunLineStyle;
  seq: number;
  text: string;
  /** how many lines at the tail of `lines` this run currently contributes. */
  lineCount: number;
}

export const emptyFold = (): FoldState => ({
  lines: [],
  summary: emptySummary(),
  nextSeq: 0,
  pending: null,
});

/** Cap the in-memory scrollback: a multi-MB reasoning stream must not live
 * in React state forever. Keeps the last MAX_LINES; the raw file is always
 * re-readable from seq 0. */
export const MAX_LINES = 4000;

export function foldFrame(st: FoldState, f: RunFrame): FoldState {
  // from_seq replay can re-deliver the boundary frame; fold each seq once.
  if (typeof f.seq === "number" && f.seq < st.nextSeq) return st;
  const nextSeq = typeof f.seq === "number" ? f.seq + 1 : st.nextSeq;
  const p = f.payload ?? {};
  // TOKEN-LEVEL chunks join into ONE block. Vendors stream prose and reasoning
  // a word or two at a time — one grok build emitted 675 such frames, and
  // antigravity splits a sentence across its ACTIVE/DONE pair ("…declaring id,
  // title, " + "entrypoint, zero required permissions…")
  // — so one line per frame renders shredded prose. Only frames the PARSER
  // marked `delta` are joined (events.py sets it on grok's text/thought,
  // cursor's partial output and antigravity's text_delta); a whole-message
  // frame or a never-drop raw line is never glued to its neighbour.
  const isDelta = (f.kind === "text" || f.kind === "thinking") && p.delta === true;
  if (isDelta) {
    const style: RunLineStyle = f.kind === "thinking" ? "thinking" : "text";
    // Rejoin only when the open run is the same style AND its lines are all
    // still in the buffer (MAX_LINES trimming can eat them — then it starts a
    // fresh block rather than rewriting lines it no longer owns).
    const prev =
      st.pending &&
      st.pending.style === style &&
      st.lines.length >= st.pending.lineCount
        ? st.pending
        : null;
    const seq = prev ? prev.seq : f.seq;
    const text = (prev ? prev.text : "") + s(p.text);
    const base = prev ? st.lines.slice(0, st.lines.length - prev.lineCount) : st.lines;
    const added =
      text === "" ? [] : text.split("\n").map((t) => ({ seq, style, text: t }));
    return {
      lines: [...base, ...added].slice(-MAX_LINES),
      summary: foldSummary(st.summary, f),
      nextSeq,
      pending: { style, seq, text, lineCount: added.length },
    };
  }
  const added = frameToLines(f);
  const lines =
    added.length === 0
      ? st.lines
      : [...st.lines, ...added].slice(-MAX_LINES);
  return {
    lines,
    summary: foldSummary(st.summary, f),
    nextSeq,
    // Any non-delta frame closes the open run: the next chunk starts a new
    // block instead of being appended after a tool call or a status line.
    pending: null,
  };
}

/** One-line collapsed header for a finished run:
 * "exit 0 · 2m 16s · 2 files" / "failed · exit 1 · …" */
export function summaryLine(sm: RunSummary): string {
  const bits: string[] = [];
  // While still running, a lost feed is the ONE thing worth saying — the
  // scrollback has stopped and the user needs to know that is expected.
  if (sm.state === "live" && sm.streamLost) return "running · no live feed";
  // Not started yet — queued behind the harnesses the plan holds right now.
  if (sm.state === "live" && sm.waiting != null) return "waiting for memory";
  if (sm.killedBy) bits.push(`stopped by ${sm.killedBy === "host" ? "your Host" : "you"}`);
  else if (sm.state === "timeout") bits.push("timed out");
  else if (sm.state === "failed") bits.push("failed");
  if (sm.exitCode != null) bits.push(`exit ${sm.exitCode}`);
  if (sm.elapsedS != null) bits.push(fmtElapsed(sm.elapsedS));
  if (sm.files.length) bits.push(`${sm.files.length} file${sm.files.length === 1 ? "" : "s"}`);
  if (sm.retries) bits.push(`${sm.retries} retr${sm.retries === 1 ? "y" : "ies"}`);
  return bits.join(" · ") || (sm.state === "live" ? "running" : sm.state);
}

/** The stream ended without an error. Is the run over, or should the reader
 * reconnect from its cursor? The route (review.py
 * stream_session_harness_events) returns at the terminal frame — and ALSO
 * after _STREAM_TAIL_IDLE_S of silence, with no terminal frame. A variant
 * queued for memory (fan-out waves) is silent for as long as the harnesses
 * ahead of it run, so its tab hits that limit in the ordinary case; treating
 * the clean end as terminal froze it on "waiting for memory" and its output
 * never arrived without a reload. The summary knows: a terminal frame moves
 * `state` off "live", so a clean end while still live means resume. Same
 * rule survives a proxy cutting a long connection. */
export function resumeAfterCleanEnd(sm: RunSummary): boolean {
  return sm.state === "live";
}

/** Pause before resuming a cleanly-ended live stream. Short: the route
 * itself holds the next connection open for up to its idle window, so this
 * is not a poll interval, only a guard against a tight loop if the route
 * ever returns immediately. */
export const RESUME_DELAY_MS = 1000;

export function fmtElapsed(sec: number): string {
  const t = Math.round(sec);
  if (t < 60) return `${t}s`;
  return `${Math.floor(t / 60)}m ${t % 60}s`;
}

/** Read the NDJSON run stream, yielding parsed frames. Resumes from
 * `fromSeq`; the route tails while live and returns at the terminal frame.
 * Retries the app-token delivery race (the shell wraps fetch on 'load' —
 * same backoff the api.ts request helper uses). Malformed lines are
 * surfaced as text frames, never dropped. */
export async function* readRunStream(
  sessionId: string,
  harness: string,
  fromSeq: number,
  signal?: AbortSignal,
): AsyncGenerator<RunFrame> {
  let res: Response | null = null;
  for (let attempt = 0; ; attempt++) {
    res = await fetch(
      `/api/review/sessions/${encodeURIComponent(sessionId)}/stream/` +
        `${encodeURIComponent(harness)}?from_seq=${fromSeq}`,
      { signal, headers: { Accept: "application/x-ndjson" } },
    );
    if (res.status === 401 && attempt < 3) {
      await new Promise((r) => setTimeout(r, 120 * (attempt + 1)));
      continue;
    }
    break;
  }
  if (!res.ok || !res.body) {
    throw new Error(`run stream HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let seq = fromSeq;
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = done ? "" : (lines.pop() ?? "");
    for (const line of lines) {
      const t = line.trim();
      if (!t) continue;
      try {
        const o = JSON.parse(t) as RunFrame;
        if (typeof o.seq === "number") seq = o.seq;
        yield o;
      } catch {
        yield { seq: ++seq, kind: "text", payload: { text: t } };
      }
    }
    if (done) return;
  }
}
