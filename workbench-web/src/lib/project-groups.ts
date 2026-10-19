// A project's status folded to four words, and its bucket (a chapter of the
// book's Index).
//
// The status words below mirror server/routes/projects.py
// PROJECT_GROUP_OF_STATUS (the server stamps `group` on every list/search
// row; this is the same mapping for rows without it and for optimistic
// updates). 'review' folds into Active: work in flight, not a resting state.

import type { Bucket, Project, ProjectGroup } from "@/lib/types";

/** The swatches a bucket can take, as a chapter of the book's Index.
 * Hex, because the route stores #rrggbb or a short token and both style
 * themselves only from a hex they can trust. */
export const BUCKET_COLORS = ["#64748b", "#ef4444", "#f59e0b", "#10b981", "#0ea5e9", "#8b5cf6", "#ec4899"];

export const GROUP_LABEL: Record<ProjectGroup, string> = {
  active: "Active",
  pending: "Pending",
  completed: "Completed",
  archived: "Archived",
};

export function groupOf(status?: string | null): ProjectGroup {
  switch ((status || "").trim()) {
    case "active":
    case "review":
      return "active";
    case "complete":
      return "completed";
    case "archived":
      return "archived";
    default:
      return "pending";
  }
}

export function groupOfProject(p: Pick<Project, "status" | "group">): ProjectGroup {
  return (p.group as ProjectGroup | undefined) ?? groupOf(p.status);
}

export type GroupCounts = Record<ProjectGroup, number> & { total: number };

export function emptyCounts(): GroupCounts {
  return { active: 0, pending: 0, completed: 0, archived: 0, total: 0 };
}

export function countsOf(projects: Project[]): GroupCounts {
  const c = emptyCounts();
  for (const p of projects) {
    c[groupOfProject(p)] += 1;
    c.total += 1;
  }
  return c;
}

/** How a bucket orders its projects. "manual" = the order the user dragged
 * (sort_order), never-placed projects after, newest activity first — the
 * same order the server renumbers by (projects.py _PLACE_ORDER_SQL).
 * "recent" = newest activity first, the order the server lists in. */
export type BucketSort = "manual" | "recent";

/** Sort one bucket's projects. The input is the server's list order (last
 * activity DESC, id DESC), and the sort is stable, so ties keep it. */
export function sortInBucket(projects: Project[], mode: BucketSort): Project[] {
  if (mode === "recent") return [...projects];
  return [...projects].sort((a, b) => {
    const x = a.sort_order ?? null;
    const y = b.sort_order ?? null;
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return x - y;
  });
}

/** The optimistic result of one drop, applied to the whole list: the moved
 * project takes the target bucket and every project in that bucket gets the
 * position the server is about to write (1..n, the moved one in front of
 * `beforeId`, or last). Mirrors projects.py place_project_service so the book
 * does not jump when the answer lands. `beforeId` must be in the target
 * bucket; when it is not, the project goes last — the server refuses such a
 * drop, and the reload that follows puts the truth back. */
export function applyPlace(
  projects: Project[],
  buckets: Bucket[],
  movedId: number,
  bucketId: number | null,
  beforeId: number | null,
): Project[] {
  const knownBucket = bucketId != null && buckets.some((b) => b.id === bucketId) ? bucketId : null;
  const inTarget = (p: Project) => {
    const b = p.bucket_id != null && buckets.some((x) => x.id === p.bucket_id) ? p.bucket_id : null;
    return b === knownBucket;
  };
  const order = sortInBucket(
    projects.filter((p) => p.id !== movedId && inTarget(p)),
    "manual",
  ).map((p) => p.id);
  const at = beforeId != null ? order.indexOf(beforeId) : -1;
  if (at >= 0) order.splice(at, 0, movedId);
  else order.push(movedId);
  const pos = new Map(order.map((id, i) => [id, i + 1]));
  return projects.map((p) => {
    if (p.id === movedId) return { ...p, bucket_id: knownBucket, sort_order: pos.get(p.id) ?? null };
    const n = pos.get(p.id);
    return n != null ? { ...p, sort_order: n } : p;
  });
}

/** A title offered from what the user typed — the description is never the
 * thing they have to invent, and the title is one keystroke from being
 * theirs. First clause, stripped of a leading verb phrase like "I want to" /
 * "help me", up to eight words, first letter capitalised, 60 chars max. */
export function suggestTitle(text: string): string {
  let t = (text || "").replace(/\s+/g, " ").trim();
  if (!t) return "";
  // First sentence or clause.
  const cut = t.search(/[.!?\n;:—–]|\s[-]\s/);
  if (cut > 0) t = t.slice(0, cut);
  t = t.replace(/^(i want to|i'd like to|i would like to|i need to|let's|lets|help me|can you|could you|please|i want a|i need a)\s+/i, "");
  // "build a budget tracker" → "budget tracker": the verb is what every
  // project does, not what this one is.
  t = t.replace(/^(build|make|create|write|design|set up|setup|develop|start)\s+(me\s+)?/i, "");
  t = t.replace(/^(a|an|the|my|our)\s+/i, "");
  const words = t.split(" ").filter(Boolean).slice(0, 8);
  let s = words.join(" ").replace(/[,\s]+$/, "");
  if (s.length > 60) s = s.slice(0, 60).replace(/\s+\S*$/, "");
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}
