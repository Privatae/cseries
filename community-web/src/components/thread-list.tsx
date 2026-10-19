import { MessageSquare } from "lucide-react";

import type { Category, Sort, Thread } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Byline, Empty, StatusChip, VotePill } from "@/components/bits";

const SORTS: Sort[] = ["hot", "new", "top"];

export function SortTabs({ sort, onSort }: { sort: Sort; onSort: (s: Sort) => void }) {
  return (
    <div className="flex gap-1">
      {SORTS.map((s) => (
        <button
          key={s}
          onClick={() => onSort(s)}
          className={cn(
            "rounded-md px-2.5 py-1 text-xs capitalize transition-colors",
            s === sort
              ? "bg-secondary text-foreground"
              : "text-muted-foreground hover:bg-accent",
          )}
        >
          {s}
        </button>
      ))}
    </div>
  );
}

export function ThreadList({
  threads,
  categories,
  showCategory,
  onOpen,
}: {
  threads: Thread[];
  categories: Category[];
  showCategory: boolean;
  onOpen: (id: number) => void;
}) {
  if (threads.length === 0) {
    return <Empty>Nothing here yet — start the first thread.</Empty>;
  }
  const titleOf = (slug: string) =>
    categories.find((c) => c.slug === slug)?.title ?? slug;

  return (
    <ul className="divide-y divide-border">
      {threads.map((t) => (
        <li
          key={t.id}
          onClick={() => onOpen(t.id)}
          className="flex cursor-pointer items-start gap-3 px-4 py-3 transition-colors hover:bg-accent/50"
        >
          <VotePill kind="thread" id={t.id} votes={t.votes} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-sm font-medium text-foreground">
                {t.title}
              </span>
              <StatusChip status={t.status} proposalRef={t.proposal_ref} />
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-2">
              <Byline author={t.author_name} at={t.created_at} />
              {showCategory && (
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
                  {titleOf(t.category)}
                </span>
              )}
            </div>
          </div>
          <span className="mt-0.5 flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
            <MessageSquare size={13} />
            <span className="tabular-nums">{t.replies}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}
