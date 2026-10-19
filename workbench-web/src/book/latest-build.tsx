import { type CSSProperties, useEffect, useState } from "react";
import { SquareArrowOutUpRightIcon } from "lucide-react";
import type { RunMeta } from "@/components/workbench/run-card";
import { RunVersionWindow, runMetaOf } from "@/components/workbench/run-terminal";
import { fetchRunDetail } from "@/lib/api";
import { engineName } from "@/lib/run-steps";
import { engineColor, latestBuildKey, runTitle, type LatestBuild } from "@/lib/strands";

/**
 * "This page"'s Open for the page's latest finished build (lib/strands.ts
 * latestBuild): what its run built, opened without scrolling the conversation
 * up to the run's card.
 *
 * Each finished engine's Open is the run card's own: the same version window
 * (run-terminal.tsx RunVersionWindow), with what the card knows of the run
 * read the same way (runMetaOf, off GET /api/review/sessions/{id}), so it
 * opens on the same page, or on the changes when the run left none. The card
 * offers one Open per engine, whatever the run built, an app or a site, and
 * so does this. Nothing shows until a run on the page has finished; when a
 * newer one finishes, this moves to it.
 */
export function LatestBuildOpen({ build }: { build: LatestBuild | null }) {
  const key = latestBuildKey(build);
  // What the card knows of each finished engine's run. Read again when the
  // build moves or another of its engines finishes: whether a worktree has a
  // page to show is answered when it is asked (the card asks again at its
  // run's end too).
  // Kept by run, so a window opened on one run never reads another's.
  const [metas, setMetas] = useState<Record<string, Record<string, RunMeta>>>({});
  useEffect(() => {
    if (!build) return;
    let alive = true;
    void (async () => {
      const r = await fetchRunDetail(build.sessionId);
      if (!alive || !r.ok) return;
      const byEngine = Object.fromEntries(build.done.map((h) => [h, runMetaOf(r.data, h)]));
      setMetas((cur) => ({ ...cur, [build.sessionId]: { ...cur[build.sessionId], ...byEngine } }));
    })();
    return () => {
      alive = false;
    };
    // `key` names the build and its finished engines.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  // The window stays on the run it was opened for, whatever finishes after.
  const [open, setOpen] = useState<{ sessionId: string; harness: string } | null>(null);

  const versionWindow = open && (
    <RunVersionWindow
      harness={open.harness}
      meta={metas[open.sessionId]?.[open.harness]}
      onClose={() => setOpen(null)}
      sessionId={open.sessionId}
    />
  );
  if (!build) return versionWindow || null;
  return (
    <div className="book-latest" data-latest-build={build.sessionId}>
      <div className="book-ctl">
        <span>Latest build</span>
        <span className="book-latest-what" title={runTitle(build.sessionId, build.engines)}>
          {runTitle(build.sessionId, build.engines)}
        </span>
      </div>
      {build.done.map((h) => (
        <div className="book-ctl" data-ctl="open-build" data-harness={h} key={h}>
          <span className="book-latest-eng" style={{ "--engine-color": engineColor(h) } as CSSProperties}>
            <i aria-hidden />
            {engineName(h)}
          </span>
          <button
            aria-label={`Open version: what ${engineName(h)} built`}
            className="book-open"
            data-open-latest={h}
            onClick={() => setOpen({ sessionId: build.sessionId, harness: h })}
            title="Open what this run built: its own files, whether or not it was adopted, and its changes"
            type="button"
          >
            <SquareArrowOutUpRightIcon aria-hidden className="size-[13px]" />
            Open version
          </button>
        </div>
      ))}
      {versionWindow}
    </div>
  );
}
