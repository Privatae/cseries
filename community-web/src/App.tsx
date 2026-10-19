// Community — the members' discussion board. Left rail (categories +
// search), main pane (thread list with hot/new/top, or one thread's tree).
// Everything reads/writes /api/community/* — the kernel relays to the
// platform board via the bridge; a board outage surfaces as the Unavailable
// state (503 fail-closed), never a cached or empty-looking board.

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, PenSquare } from "lucide-react";

import * as api from "@/lib/api";
import type { Category, Sort, Thread } from "@/lib/types";
import { PhoneNav, Rail } from "@/components/rail";
import { Composer } from "@/components/composer";
import { SortTabs, ThreadList } from "@/components/thread-list";
import { ThreadView } from "@/components/thread-view";
import { Empty, Unavailable } from "@/components/bits";

export default function App() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [category, setCategory] = useState(""); // "" = all
  const [sort, setSort] = useState<Sort>("hot");
  const [threads, setThreads] = useState<Thread[]>([]);
  // #/thread/<id> keeps the open thread across refreshes (and deep-links).
  const [openThread, setOpenThread] = useState<number | null>(() => {
    const m = window.location.hash.match(/^#\/thread\/(\d+)$/);
    return m ? Number(m[1]) : null;
  });
  const [composing, setComposing] = useState(false);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const searchTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const want = openThread !== null ? `#/thread/${openThread}` : "";
    if (window.location.hash !== want) {
      history.replaceState(null, "", window.location.pathname + window.location.search + want);
    }
  }, [openThread]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const [cats, list] = await Promise.all([
      categories.length ? null : api.fetchCategories(),
      search.trim() ? api.searchThreads(search.trim()) : api.fetchThreads(category, sort),
    ]);
    if (cats) {
      if (!cats.ok) {
        setError(cats.message);
        setLoading(false);
        return;
      }
      setCategories(cats.data);
    }
    if (list.ok) setThreads(list.data);
    else setError(list.message);
    setLoading(false);
    // categories.length intentionally read once — refetching them on every
    // list reload would be noise; they change via operator action only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category, sort, search]);

  useEffect(() => {
    // Debounce search keystrokes; category/sort changes load immediately.
    window.clearTimeout(searchTimer.current);
    if (search.trim()) {
      searchTimer.current = window.setTimeout(() => void load(), 250);
      return () => window.clearTimeout(searchTimer.current);
    }
    void load();
    return undefined;
  }, [load, search]);

  const categoryTitle = search.trim()
    ? `Search: ${search.trim()}`
    : category
      ? (categories.find((c) => c.slug === category)?.title ?? category)
      : "All threads";

  return (
    <div className="flex h-full bg-background text-foreground">
      <Rail
        categories={categories}
        selected={category}
        onSelect={(slug) => {
          setSearch("");
          setCategory(slug);
          setOpenThread(null);
        }}
        search={search}
        onSearch={(q) => {
          setSearch(q);
          setOpenThread(null);
        }}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        {openThread !== null ? (
          <ThreadView id={openThread} onBack={() => setOpenThread(null)} />
        ) : (
          <>
            <PhoneNav
              categories={categories}
              selected={category}
              onSelect={(slug) => {
                setSearch("");
                setCategory(slug);
                setOpenThread(null);
              }}
              search={search}
              onSearch={(q) => {
                setSearch(q);
                setOpenThread(null);
              }}
            />
            <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2">
              <span className="truncate text-sm font-medium">{categoryTitle}</span>
              <div className="flex items-center gap-2">
                {!search.trim() && <SortTabs sort={sort} onSort={setSort} />}
                <button
                  onClick={() => setComposing(true)}
                  className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:opacity-90"
                >
                  <PenSquare size={13} /> New thread
                </button>
              </div>
            </div>

            {composing && (
              <Composer
                categories={categories}
                initialCategory={category}
                onClose={() => setComposing(false)}
                onCreated={(t) => {
                  setComposing(false);
                  setOpenThread(t.id);
                }}
              />
            )}

            <div className="min-h-0 flex-1 overflow-y-auto">
              {loading ? (
                <div className="flex h-full items-center justify-center">
                  <Loader2 className="animate-spin text-muted-foreground" size={20} />
                </div>
              ) : error ? (
                <Unavailable message={error} retry={() => void load()} />
              ) : search.trim() && threads.length === 0 ? (
                <Empty>No threads match.</Empty>
              ) : (
                <ThreadList
                  threads={threads}
                  categories={categories}
                  showCategory={category === "" || !!search.trim()}
                  onOpen={setOpenThread}
                />
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
