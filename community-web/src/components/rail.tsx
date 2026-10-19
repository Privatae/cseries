import { Search, Users, X } from "lucide-react";

import type { Category } from "@/lib/types";
import { cn } from "@/lib/utils";

export function Rail({
  categories,
  selected,
  onSelect,
  search,
  onSearch,
}: {
  categories: Category[];
  selected: string; // "" = all
  onSelect: (slug: string) => void;
  search: string;
  onSearch: (q: string) => void;
}) {
  return (
    // Phone: hidden — PhoneNav (search + category chips) takes over below md.
    <aside className="hidden w-52 shrink-0 flex-col border-r border-border bg-sidebar md:flex">
      <div className="flex items-center gap-2 px-3 py-3">
        <Users size={16} className="text-primary" />
        <div>
          <div className="text-sm font-semibold text-foreground">Community</div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            members only
          </div>
        </div>
      </div>

      <div className="px-3 pb-2">
        <div className="flex items-center gap-1.5 rounded-md border border-input bg-background px-2 py-1">
          <Search size={12} className="shrink-0 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Search threads"
            className="w-full bg-transparent text-xs outline-none placeholder:text-muted-foreground"
          />
          {search && (
            <button onClick={() => onSearch("")} className="text-muted-foreground hover:text-foreground">
              <X size={12} />
            </button>
          )}
        </div>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <RailItem label="All threads" active={selected === "" && !search} onClick={() => onSelect("")} />
        {categories.map((c) => (
          <RailItem
            key={c.slug}
            label={c.title}
            title={c.description}
            active={selected === c.slug && !search}
            onClick={() => onSelect(c.slug)}
          />
        ))}
      </nav>
    </aside>
  );
}

/** Phone replacement for the rail: a search row + a horizontally scrollable
 * category chip row, rendered above the thread list (md:hidden). */
export function PhoneNav({
  categories,
  selected,
  onSelect,
  search,
  onSearch,
}: {
  categories: Category[];
  selected: string;
  onSelect: (slug: string) => void;
  search: string;
  onSearch: (q: string) => void;
}) {
  return (
    <div className="border-b border-border md:hidden">
      <div className="px-3 pt-2">
        <div className="flex items-center gap-1.5 rounded-md border border-input bg-background px-2 py-1.5">
          <Search size={14} className="shrink-0 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Search threads"
            className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          {search && (
            <button onClick={() => onSearch("")} className="text-muted-foreground hover:text-foreground">
              <X size={14} />
            </button>
          )}
        </div>
      </div>
      <nav className="flex gap-1.5 overflow-x-auto px-3 py-2">
        <Chip label="All threads" active={selected === "" && !search} onClick={() => onSelect("")} />
        {categories.map((c) => (
          <Chip
            key={c.slug}
            label={c.title}
            active={selected === c.slug && !search}
            onClick={() => onSelect(c.slug)}
          />
        ))}
      </nav>
    </div>
  );
}

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "shrink-0 whitespace-nowrap rounded-full border px-3 py-1 text-xs transition-colors",
        active
          ? "border-primary bg-primary/15 text-foreground"
          : "border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}

function RailItem({
  label,
  title,
  active,
  onClick,
}: {
  label: string;
  title?: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={cn(
        "block w-full rounded-md px-2.5 py-1.5 text-left text-xs transition-colors",
        active
          ? "bg-sidebar-accent text-foreground"
          : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}
