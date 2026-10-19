import { type CSSProperties, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useRunFeeds, useRunPatches } from "@/hooks/use-run-feed";
import { fetchRunDetail } from "@/lib/api";
import { modelOf } from "@/lib/run-feed";
import { isLiveRun } from "@/lib/run-live";
import { clock, engineName } from "@/lib/run-steps";
import {
  briefLine,
  engineColor,
  LANE_WORDS,
  podNow,
  runEngines,
  runLabel,
  runSkew,
  runSummary,
  runTitle,
  scoreboard,
  sessionSeconds,
  strands,
  strandWords,
  type Strand,
} from "@/lib/strands";
import type { RunSession } from "@/lib/types";

/**
 * The bench in the book: the run board on the right side of the page, drawn as
 * strands. The brief splits into one line per engine; each line carries that
 * engine's steps over time (an open dot for a command or a read, a dot for a file
 * written, red for a call that failed, a soft band while it thinks) and ends in
 * its finish mark, or a slow pulse at now while it runs. Under it the scoreboard
 * says who finished first. A strand, its name or its row turns the page to that
 * engine's card.
 *
 * The maths is lib/strands.ts (tested under node); the frames are the cards' own (lib/run-feed.ts), so
 * showing a run here opens no second stream. Times are on the pod's clock.
 */

// The drawing's measures (px): the names' column, the brief's row, a lane,
// and the time axis under the lanes.
const NAMES_W = 104;
const BRIEF_Y = 9;
const LANES_TOP = 22;
const LANE_H = 34;
const AXIS_H = 22;
const PLOT_LEFT = NAMES_W + 26;
const PLOT_RIGHT = 14;

export function BookBench({
  sessions,
  projectId,
  onJump,
}: {
  /** The page's runs, newest first (the bench's GET /api/review/sessions). */
  sessions: RunSession[];
  projectId: number;
  /** Turn to that engine's card on the page; false when it is not there. */
  onJump: (sessionId: string, harness: string) => boolean;
}) {
  const runs = sessions.filter((s) => s.project_id == null || s.project_id === projectId);
  const [picked, setPicked] = useState<string | null>(null);
  const row = runs.find((s) => s.session_id === picked) ?? runs[0] ?? null;
  if (!row) return null;
  return <RunBoard key={row.session_id} onJump={onJump} onPick={setPicked} row={row} runs={runs} />;
}

function RunBoard({
  row,
  runs,
  onPick,
  onJump,
}: {
  row: RunSession;
  runs: RunSession[];
  onPick: (sessionId: string) => void;
  onJump: (sessionId: string, harness: string) => boolean;
}) {
  const sid = row.session_id;
  const engines = runEngines(row);
  const feeds = useRunFeeds(sid, engines);
  const models = feeds.map((f) => modelOf(f.frames));
  // Running while an engine's frames say so and the run's row agrees: an old
  // run's frames read as live for the moment before they have loaded.
  const live = models.some((M) => M.live) && (row.status == null || isLiveRun(row.status));
  // One clock for the board, on the pod's time (strands.ts podNow), ticking
  // once a second while anything runs.
  const skew = runSkew(feeds.map((f) => f.skew));
  const [deviceNow, setDeviceNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(() => setDeviceNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [live]);
  const now = podNow(live ? deviceNow : Date.now() / 1000, skew);
  const files = useRunPatches(sid, engines, models.map((M) => !M.live));
  const eng = engines.map((id, i) => ({ id, M: models[i], ph: row.per_harness?.[id] ?? null, files: files[i] }));

  // The drawing's width is the right side's.
  const box = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(340);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const fit = () => el.clientWidth && setWidth(el.clientWidth);
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const geo = strands(row, eng, now, { left: PLOT_LEFT, right: PLOT_RIGHT, width, ticks: 4 });
  const score = scoreboard(eng, now);
  const secs = sessionSeconds(row, models, now, live);

  const [brief, setBrief] = useState<string | null>(null);
  const [briefOpen, setBriefOpen] = useState(false);
  const toggleBrief = async () => {
    const open = !briefOpen;
    setBriefOpen(open);
    if (open && brief == null) {
      // The list carries the brief's first 120 characters; the whole of it
      // is on the run's own record.
      setBrief("");
      const r = await fetchRunDetail(sid);
      setBrief(String((r.ok && r.data.manifest?.prompt) || row.prompt || ""));
    }
  };
  const [missed, setMissed] = useState<string | null>(null);
  const jump = (h: string) => {
    const ok = onJump(sid, h);
    setMissed(ok ? null : `${engineName(h)}'s card is not in this page's conversation.`);
  };

  const H = LANES_TOP + Math.max(1, engines.length) * LANE_H + AXIS_H;
  const laneY = (i: number) => LANES_TOP + i * LANE_H + LANE_H / 2;
  const bx = NAMES_W + 8;
  const first = briefLine(sid, row.prompt);
  return (
    <section aria-label="This page's run" className="book-sec book-bench" data-bench={sid} data-live={live || undefined}>
      <div className="book-k">
        <span>Bench</span>
        <span className="book-live" data-live={live || undefined}>
          {live ? "Live" : "Finished"}
        </span>
      </div>
      <div className="book-run-head">
        <b data-run-title>{runTitle(sid, engines)}</b>
        {secs != null && (
          <span className="book-run-clock" data-session-clock title="Time since your Host sent the brief">
            {clock(secs)}
          </span>
        )}
      </div>
      <p className="book-run-brief">
        {first && <span>{first} </span>}
        <button aria-expanded={briefOpen} data-brief-toggle onClick={() => void toggleBrief()} type="button">
          {briefOpen ? "Hide the brief" : "Brief from your Host"}
        </button>
      </p>
      {briefOpen && <pre className="book-run-brief-full">{brief || "Loading…"}</pre>}

      <div className="book-strands" ref={box}>
        <svg aria-hidden className="book-strands-svg" data-strands height={H} viewBox={`0 0 ${width} ${H}`} width={width}>
          {geo.ticks.map((t) => (
            <g key={`t${t.label}`}>
              <line className="book-strand-grid" x1={t.x} x2={t.x} y1={LANES_TOP - 4} y2={H - AXIS_H + 4} />
              <text className="book-strand-tick" textAnchor="middle" x={t.x} y={H - 6}>
                {t.label}
              </text>
            </g>
          ))}
          {geo.nowX != null && (
            <line className="book-strand-now" x1={geo.nowX} x2={geo.nowX} y1={LANES_TOP - 6} y2={H - AXIS_H + 4} />
          )}
          {geo.strands.map((s, i) => (
            <StrandLane
              bx={bx}
              by={BRIEF_Y}
              color={engineColor(s.id)}
              key={s.id}
              left={PLOT_LEFT}
              onJump={() => jump(s.id)}
              s={s}
              t0={geo.t0}
              y={laneY(i)}
            />
          ))}
          <circle className="book-brief-dot" cx={bx} cy={BRIEF_Y} r={4} />
        </svg>
        <span className="book-brief-word" style={{ top: BRIEF_Y - 7 }}>
          brief
        </span>
        {engines.map((id, i) => (
          <button
            aria-label={`${engineName(id)}: show its card on the page`}
            className="book-strand-name"
            data-strand={id}
            key={id}
            onClick={() => jump(id)}
            style={{ top: laneY(i) - 15, width: NAMES_W, "--engine-color": engineColor(id) } as CSSProperties}
            title={engineName(id)}
            type="button"
          >
            <b>
              <i aria-hidden />
              {engineName(id)}
            </b>
            <span>
              {LANE_WORDS[score[i].state]}
              {/* With several engines the scoreboard carries the times. */}
              {engines.length === 1 && score[i].time !== "—" ? ` · ${score[i].time}` : ""}
            </span>
          </button>
        ))}
      </div>
      <ul className="sr-only" data-strands-list>
        {score.map((r) => (
          <li key={r.id}>{strandWords(r)}</li>
        ))}
      </ul>
      {missed && (
        <p className="book-bench-note" role="status">
          {missed}
        </p>
      )}

      {score.length > 1 && (
        <table className="book-score" data-scoreboard>
          <caption className="sr-only">Who finished first</caption>
          <thead className="sr-only">
            <tr>
              <th scope="col">Engine</th>
              <th scope="col">Status</th>
              <th scope="col">Run time</th>
              <th scope="col">Changes</th>
            </tr>
          </thead>
          <tbody>
            {score.map((r) => (
              <tr data-score-row={r.id} data-state={r.state} key={r.id} onClick={() => jump(r.id)}>
                <td>
                  <span className="book-score-eng" style={{ "--engine-color": engineColor(r.id) } as CSSProperties}>
                    <i aria-hidden />
                    {engineName(r.id)}
                    {r.first && <span className="book-first">first to finish</span>}
                  </span>
                  <span className="book-score-sub">
                    {[
                      r.commands ? `${r.commands} command${r.commands === 1 ? "" : "s"}` : "",
                      r.read ? `${r.read} read` : "",
                      r.written ? `${r.written} written` : "",
                      r.messages ? `${r.messages} message${r.messages === 1 ? "" : "s"}` : "",
                      r.failed ? `${r.failed} failed` : "",
                    ]
                      .filter(Boolean)
                      .join(" · ") || "nothing yet"}
                  </span>
                </td>
                <td className="book-score-state" data-state={r.state}>
                  {LANE_WORDS[r.state]}
                </td>
                <td className="book-num">{r.time}</td>
                <td className="book-num">
                  {r.changes ? (
                    <>
                      <span className="book-add">+{r.changes.add}</span> <span className="book-del">−{r.changes.del}</span>
                    </>
                  ) : (
                    <span className="book-dim">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {runs.length > 1 && (
        <label className="book-run-pick">
          <span>Runs on this page</span>
          <select data-run-pick onChange={(e) => onPick(e.target.value)} value={sid}>
            {runs.map((r) => (
              <option key={r.session_id} value={r.session_id}>
                {runLabel(r)}
              </option>
            ))}
          </select>
        </label>
      )}
      <p className="sr-only" role="status">
        {runSummary(models)}
      </p>
    </section>
  );
}

/** One engine's strand: forked from the brief, along its lane, to its end. */
function StrandLane({
  s,
  y,
  bx,
  by,
  left,
  color,
  t0,
  onJump,
}: {
  s: Strand;
  y: number;
  bx: number;
  by: number;
  left: number;
  color: string;
  t0: number;
  onJump: () => void;
}) {
  const fork = `M ${bx} ${by} C ${bx} ${by + (y - by) * 0.75}, ${bx + 6} ${y}, ${left} ${y}`;
  const x0 = s.run?.x0 ?? s.queued?.x1 ?? left;
  return (
    <g
      className="book-strand"
      data-lane={s.id}
      data-state={s.state}
      onClick={onJump}
      style={{ "--engine-color": color } as CSSProperties}
    >
      <title>{`${engineName(s.id)} · ${LANE_WORDS[s.state]}`}</title>
      {/* the whole lane takes the click, not only its thin line */}
      <rect className="book-strand-hit" height={LANE_H} width="100%" x={0} y={y - LANE_H / 2} />
      <path className="book-strand-fork" d={fork} />
      {x0 > left + 1 && <line className="book-strand-wait" x1={left} x2={x0} y1={y} y2={y} />}
      {s.queued && (
        <rect
          className="book-strand-queued"
          height={12}
          rx={6}
          width={Math.max(2, s.queued.x1 - s.queued.x0)}
          x={s.queued.x0}
          y={y - 6}
        >
          <title>{s.queued.note || "waiting for memory"}</title>
        </rect>
      )}
      {s.thinks.map((t, i) => (
        <rect className="book-strand-think" height={10} key={`k${i}`} rx={5} width={Math.max(3, t.x1 - t.x0)} x={t.x0} y={y - 5} />
      ))}
      {s.run && <line className="book-strand-line" x1={s.run.x0} x2={s.run.x1} y1={y} y2={y} />}
      {s.marks.map((m, i) => (
        <circle
          className="book-strand-mark"
          cx={m.x}
          cy={y}
          data-kind={m.kind}
          key={`m${i}`}
          r={m.kind === "tool" ? 2.3 : 2.7}
        >
          <title>{`${clock(m.t - t0)} · ${m.label}`}</title>
        </circle>
      ))}
      {s.end?.kind === "live" && (
        <g data-end="live">
          <circle className="book-strand-halo" cx={s.end.x} cy={y} r={8} />
          <circle className="book-strand-head" cx={s.end.x} cy={y} r={3.6} />
        </g>
      )}
      {s.end && s.end.kind !== "live" && (
        <g data-end={s.end.kind}>
          <circle className="book-strand-finish" cx={s.end.x} cy={y} data-kind={s.end.kind} r={6.5} />
          <path
            className="book-strand-glyph"
            d={
              s.end.kind === "done"
                ? `M${s.end.x - 3} ${y}l2 2.1 4-4.3`
                : s.end.kind === "stopped"
                  ? `M${s.end.x - 2.2} ${y - 2.2}h4.4v4.4h-4.4z`
                  : `M${s.end.x - 2.5} ${y - 2.5}l5 5M${s.end.x + 2.5} ${y - 2.5}l-5 5`
            }
          />
        </g>
      )}
    </g>
  );
}
