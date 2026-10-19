import { useEffect, useState } from "react";
import { Stands as KitStands, Tally as BookTally } from "@kit/book/page-top";

/** Which pages the person left "Where this stands" open on, on this device. */
const STANDS_OPEN_KEY = "workbench.stands.open";

function readStandsOpen(): Record<string, boolean> {
  try {
    const v = JSON.parse(window.localStorage.getItem(STANDS_OPEN_KEY) || "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, boolean>) : {};
  } catch {
    return {};
  }
}

function writeStandsOpen(pageId: string, open: boolean): void {
  try {
    const all = readStandsOpen();
    if (open) all[pageId] = true;
    else delete all[pageId];
    window.localStorage.setItem(STANDS_OPEN_KEY, JSON.stringify(all));
  } catch {
    /* private mode: remembered for this page view only */
  }
}

type StandsPage = Parameters<typeof KitStands>[0]["page"];

/** The collapsed line's words: the first line the open box shows (the kit's
 * Stands lists Goal, then Next, then Open), or what it says instead. */
export function standsFirstLine(page: StandsPage, error: string | null): string {
  if (page === undefined) return "Reading…";
  const w = page?.where_this_stands ?? null;
  if (w?.goal) return `Goal: ${w.goal}`;
  if (w?.next_step) return `Next: ${w.next_step}`;
  if (w?.open_work?.length) return `Open: ${w.open_work.join("; ")}`;
  if (error) return `Could not read it: ${error}`;
  return "Nothing yet";
}

/**
 * "Where this stands" at the top of a page, the kit's (app-web-kit/book/
 * page-top.tsx Stands), one line until it is opened, as Circles has it
 * (circles-web/src/cards/stands.tsx): its label and the first line, cut to
 * the width; a tap opens it in full, capped in height with its own scroll so
 * the conversation stays in view (book.css .book-stands-box). Each page
 * remembers which way it was left, on this device.
 */
export function Stands({
  page,
  pageId,
  seenBefore,
  error,
}: {
  page: StandsPage;
  pageId: string | number | null;
  seenBefore: string | null | undefined;
  error: string | null;
}) {
  const key = pageId == null ? null : String(pageId);
  const [open, setOpen] = useState(() => !!key && !!readStandsOpen()[key]);
  useEffect(() => setOpen(!!key && !!readStandsOpen()[key]), [key]);
  const first = standsFirstLine(page, error);
  return (
    <div className="book-stands-box" data-open={open || undefined} data-stands-box>
      <button
        aria-expanded={open}
        aria-label={open ? "Where this stands: close" : `Where this stands: ${first}`}
        className="book-stands-toggle"
        data-stands-toggle
        onClick={() => {
          const next = !open;
          setOpen(next);
          if (key) writeStandsOpen(key, next);
        }}
        type="button"
      >
        <span aria-hidden className="caret">
          {open ? "▾" : "▸"}
        </span>
        <span className="k">Where this stands</span>
        {!open && <span className="first">{first}</span>}
      </button>
      {open && <KitStands error={error} page={page} seenBefore={seenBefore} />}
    </div>
  );
}
import { openRepository } from "@/lib/code-approval";
import { COUNTER, engineLine } from "@/lib/page-count";
import type { Project, ProjectPage } from "@/lib/types";

/** The head of a page: its kind (a project, with the Code Repository its code
 * lives in, which opens from here), the page's running token count (slice
 * 3b), and the not-a-person disclosure, which is static and always on
 * (governance floor, SB 243 / NY §1700; scripts/test_governance_floor.py). */
export function PageHead({ row, page }: { row: Project | null; page?: ProjectPage | null }) {
  const group = row?.group && row.group !== "active" ? String(row.group) : null;
  return (
    <header className="book-head" data-page-head>
      <span>
        Project
        {group ? ` · ${group}` : ""}
        {row?.repo ? (
          row.code_on_machine ? (
            <span title="This project's code lives on your machine; the Host reaches it">
              {" · "}
              {row.repo} on your machine
            </span>
          ) : (
            <>
              {" · "}
              <button
                className="book-repo"
                onClick={() => openRepository(row.repo as string)}
                title="Where this project's code lives: open it"
                type="button"
              >
                {row.repo}
              </button>
            </>
          )
        ) : null}
      </span>
      <span className="book-head-right">
        <Tally page={page} projectId={row?.id ?? null} />
        <span className="book-ai">an AI, not a person</span>
      </span>
    </header>
  );
}

/**
 * The page's running token count (app-web-kit/book/page-top.tsx Tally), from
 * GET /api/projects/{id}/page `totals` in the Workbench's words, with the
 * coding-engine runs launched from the page (`engines`) as its hover's second
 * line.
 */
export function Tally({ page, projectId }: { page: ProjectPage | null | undefined; projectId: number | null }) {
  return <BookTally counter={COUNTER} extra={engineLine(page?.engines)} pageKey={projectId} totals={page?.totals} />;
}
