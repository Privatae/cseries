import type { ApprovalLineEvent } from "./types";

// Code Approvals, read for a person. The body the Host writes has one fixed shape (server/routes/
// code_approvals.py build_body): bold section headings, bullet lines, a folded
// <details> block and hidden markers. This turns it into sections the card
// renders as plain elements — never as HTML: the words are the Host's, and a
// body written by anyone else is shown as the text it is.

export const FORMAT_MARKER = "<!-- code-approval v1 -->";

export interface ApprovalSection {
  heading: string;
  lines: string[];
}

export interface ParsedApproval {
  /** Written in the one plain-English format. */
  plain: boolean;
  sections: ApprovalSection[];
  /** The folded Details lines. */
  details: string[];
  /** Everything else, for a body not in the format. */
  text: string;
}

const HEADING = /^\*\*(.+)\*\*$/;

export function parseApprovalBody(body: string | null | undefined): ParsedApproval {
  const raw = body ?? "";
  if (!raw.includes(FORMAT_MARKER)) {
    return { plain: false, sections: [], details: [], text: raw.trim() };
  }
  const sections: ApprovalSection[] = [];
  const details: string[] = [];
  let inDetails = false;
  let cur: ApprovalSection | null = null;
  for (const line0 of raw.split("\n")) {
    const line = line0.replace(/<!--[\s\S]*?-->/g, "").trimEnd();
    const t = line.trim();
    if (t === "<details>") {
      inDetails = true;
      continue;
    }
    if (t === "</details>") {
      inDetails = false;
      continue;
    }
    if (/^<summary>.*<\/summary>$/.test(t)) continue;
    if (!t) continue;
    if (inDetails) {
      details.push(t.replace(/^- /, ""));
      continue;
    }
    const h = HEADING.exec(t);
    if (h) {
      cur = { heading: h[1], lines: [] };
      sections.push(cur);
      continue;
    }
    if (cur) cur.lines.push(line.replace(/^\s*- /, (m) => (m.startsWith(" ") ? "  • " : "")).trimEnd());
  }
  return { plain: true, sections, details, text: "" };
}

export type LinkPart = { text: string; href?: string };

/** A line's markdown links as parts — only https links become links, so a
 * body can never point the person at a script or another scheme. */
export function splitLinks(line: string): LinkPart[] {
  const out: LinkPart[] = [];
  const re = /\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) out.push({ text: line.slice(last, m.index) });
    out.push({ text: m[1], href: m[2] });
    last = m.index + m[0].length;
  }
  if (last < line.length) out.push({ text: line.slice(last) });
  return out;
}

/** The Gitea app's view of one Code Repository (system-apps/gitea/app.js). */
export function repoView(repo: string): string {
  return `repo:${repo}`;
}

/** Open a Code Repository in the Host's own viewer, the way the desktop opens
 * any app (dashboard/base/js/app-runner.js, cseries:open-app). */
export function openRepository(repo: string): void {
  try {
    (window.parent || window).postMessage(
      { type: "cseries:open-app", app_id: "gitea", view: repoView(repo) },
      window.location.origin,
    );
  } catch {
    /* not framed — nothing to open it in */
  }
}

/** The states a person reads, most urgent first. */
export const STATE_ORDER: Record<string, number> = {
  waiting: 0,
  changes_asked: 1,
  approved: 2,
  closed: 3,
};


/** The chat's merge-line wait after one more merge of `projectId` (App's
 * single ApprovalLineEvent). A merge that lands while a wait runs joins it, so
 * no earlier line loses its wait; a line once known to be written stays so; a
 * different project's merge starts a wait of its own. Returns `cur` unchanged
 * when nothing is new; `nextSeq` is called only for a new event. */
export function addApprovalLine(
  cur: ApprovalLineEvent | null,
  projectId: number,
  number: number,
  written: boolean,
  nextSeq: () => number,
): ApprovalLineEvent | null {
  const same = cur && cur.projectId === projectId ? cur.lines : [];
  const prior = same.find((l) => l.number === number);
  if (prior && (prior.written || !written)) return cur;
  return {
    seq: nextSeq(),
    projectId,
    lines: [...same.filter((l) => l.number !== number), { number, written: written || !!prior?.written }],
  };
}


/** App's merge-line waits, one per project. A merge touches only its own
 * project's wait, so a late answer for the project just left can never
 * discard the wait of the one on screen; an unchanged map is returned as is. */
export function withApprovalLine(
  waits: Record<number, ApprovalLineEvent>,
  projectId: number,
  number: number,
  written: boolean,
  nextSeq: () => number,
): Record<number, ApprovalLineEvent> {
  const cur = waits[projectId] ?? null;
  const next = addApprovalLine(cur, projectId, number, written, nextSeq);
  return next === cur || next === null ? waits : { ...waits, [projectId]: next };
}

/** A wait ended: drop the entry holding that seq (seq is unique across all
 * projects), so no remount runs it again. An old seq — a wait a newer merge
 * already joined — matches nothing and changes nothing. */
export function withoutApprovalLine(
  waits: Record<number, ApprovalLineEvent>,
  seq: number,
): Record<number, ApprovalLineEvent> {
  const done = Object.values(waits).find((e) => e.seq === seq);
  if (!done) return waits;
  const { [done.projectId]: _gone, ...rest } = waits;
  return rest;
}
