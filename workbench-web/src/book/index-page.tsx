import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
} from "react";
import { timeAgo } from "@kit/utils";
import { createBucket, deleteBucket, searchIndex, updateBucket } from "@/lib/api";
import { prefersReducedMotion } from "@/lib/book";
import {
  BOOK_DRAG_TYPE,
  INDEX_DEBOUNCE_MS,
  INDEX_LIMIT,
  INDEX_MODE_KEY,
  chapterCount,
  chaptersOf,
  excerptParts,
  modeToKeep,
  pickLine,
  readIndexMode,
  type Chapter,
  type IndexAnswer,
  type IndexMode,
  type IndexPick,
} from "@/lib/book-index";
import { BUCKET_COLORS } from "@/lib/project-groups";
import type { Bucket, Project } from "@/lib/types";

/** The Index's box, which "/" and the Index tab put the keyboard in. */
export const INDEX_INPUT_ID = "book-index-q";

function storedMode(): IndexMode {
  try {
    return readIndexMode(window.localStorage.getItem(INDEX_MODE_KEY));
  } catch {
    return "send";
  }
}

function chapterKey(c: Pick<Chapter, "id">): string {
  return c.id == null ? "loose" : String(c.id);
}

/**
 * The Index: a page of the book of its own, drawn over the page
 * in front (which stays as it is underneath, its conversation and streams
 * untouched). The person types what they remember, or simply their message,
 * and the pages re-sort as they type (POST /api/projects/index, no model call,
 * order only). Enter, in "send" mode, gives the message to the page the Host
 * picks (the book lands it, book-app.tsx); in "open" mode it opens the top
 * page; in "new" mode it starts a new page with the words as its first
 * message, so a pick that went wrong can be overridden. Under the pages, the
 * chapters (the buckets): each opens with a riffle through its pages, takes a
 * tab dragged onto it, and is renamed, coloured and deleted (its pages stay,
 * in no chapter); "Loose pages" holds the archived pages in no chapter.
 *
 * The message in hand (`pick`) is the book's, not this page's: while the Host
 * picks (or the new page is made), the Index says so and takes no second
 * Enter, also when it was closed and opened again meanwhile; what an answer
 * that landed nowhere came to stays until the person reads it and types on.
 */
export function IndexPage({
  rows,
  buckets,
  dragging,
  pick,
  onOpen,
  onSend,
  onStartPage,
  onPickRead,
  onAskPick,
  onChapter,
  onFile,
  onBucketsChanged,
  onClose,
}: {
  rows: Project[];
  buckets: Bucket[];
  /** A tab being dragged (src/book/tabs.tsx): the chapters show where it may
   * go. */
  dragging: number | null;
  /** The message sent from the Index and what it came to (book-app.tsx). */
  pick: IndexPick | null;
  /** Open a page: a turn, or a riffle when it is archived. */
  onOpen: (id: number) => void;
  /** Enter in "send" mode: the book sends the words and lands them (it
   * sends nothing while `pick` is still out). */
  onSend: (text: string) => void;
  /** Enter in "new" mode: the book starts a new page with the words as its
   * first message and turns to it (it starts nothing while `pick` is still
   * out). */
  onStartPage: (text: string) => void;
  /** What `pick` came to has been read: the person typed on. */
  onPickRead: () => void;
  /** The Host could not pick, and the person did. */
  onAskPick: (id: number, text: string) => void;
  /** Open a chapter: the book riffles through its pages and calls `land`
   * once the page in front is copied, so the chapter is drawn underneath. */
  onChapter: (chapter: Chapter, land: () => void) => void;
  /** A tab dropped on a chapter: the page is filed there (bucket null:
   * filed in none). */
  onFile: (projectId: number, bucketId: number | null) => void;
  onBucketsChanged: () => void;
  onClose: () => void;
}) {
  // Opened again while a message is out, or before what it came to was
  // read: its words are back in the box.
  const held = pick != null && (pick.outcome == null || pickLine(pick) !== "");
  const [q, setQ] = useState(() => (held && pick ? pick.text : ""));
  const [answer, setAnswer] = useState<IndexAnswer | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [mode, setModeState] = useState<IndexMode>(storedMode);
  // "open" mode with nothing to open.
  const [openNote, setOpenNote] = useState<string | null>(null);
  const [chapterOpen, setChapterOpen] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [naming, setNaming] = useState<string | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [chapterNote, setChapterNote] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const busy = pick != null && pick.outcome == null;
  const said = pickLine(pick);
  const ask = pick?.outcome?.kind === "ask" ? { text: pick.text, candidates: pick.outcome.candidates } : null;
  const lineKind = busy ? "busy" : said ? pick?.outcome?.kind : openNote ? "error" : undefined;

  // An answer that landed nowhere: the keyboard is back in the box, by the
  // words, with the line saying why.
  const outcomeKind = pick?.outcome?.kind ?? null;
  useEffect(() => {
    if (outcomeKind && outcomeKind !== "landed" && outcomeKind !== "turn_refused") inputRef.current?.focus();
  }, [outcomeKind]);

  const setMode = (m: IndexMode) => {
    setModeState(m);
    // "new" is for this visit only (modeToKeep): the next one opens in the
    // mode kept before it.
    const keep = modeToKeep(m);
    if (keep) {
      try {
        window.localStorage.setItem(INDEX_MODE_KEY, keep);
      } catch {
        /* private mode: the choice lasts this visit */
      }
    }
    inputRef.current?.focus();
  };

  // The box takes the keyboard as the Index opens (the tab's click and "/"
  // have run by then).
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // The search, as the person types: the newest keystroke's answer only.
  const seqRef = useRef(0);
  useEffect(() => {
    const seq = ++seqRef.current;
    const t = window.setTimeout(
      async () => {
        const r = await searchIndex(q, INDEX_LIMIT);
        if (seq !== seqRef.current) return;
        if (r.ok) {
          setAnswer(r.data);
          setSearchError(null);
        } else {
          setSearchError(r.message);
        }
      },
      q.trim() ? INDEX_DEBOUNCE_MS : 0,
    );
    return () => window.clearTimeout(t);
  }, [q]);

  const chapters = useMemo(() => chaptersOf(rows, buckets), [rows, buckets]);
  const shownChapter = chapterOpen == null ? null : (chapters.find((c) => chapterKey(c) === chapterOpen) ?? null);
  const chapterName = (id: number | null | undefined) =>
    id == null ? null : (buckets.find((b) => b.id === id)?.name ?? null);

  // The pages re-sort in place: each result glides from where it was to
  // where it is now (none of it with reduced motion).
  const listRef = useRef<HTMLDivElement | null>(null);
  const tops = useRef(new Map<number, number>());
  useLayoutEffect(() => {
    const box = listRef.current;
    if (!box) return;
    const reduced = prefersReducedMotion(window);
    const before = tops.current;
    const next = new Map<number, number>();
    for (const el of box.querySelectorAll<HTMLElement>("[data-index-result]")) {
      const id = Number(el.dataset.indexResult);
      const top = el.offsetTop;
      next.set(id, top);
      if (reduced || typeof el.animate !== "function") continue;
      const was = before.get(id);
      if (was === undefined) {
        if (before.size) {
          el.animate([{ opacity: 0, transform: "translateY(14px)" }, { opacity: 1, transform: "none" }], {
            duration: 300,
            easing: "cubic-bezier(.2,.8,.2,1)",
          });
        }
      } else if (was !== top) {
        el.animate([{ transform: `translateY(${was - top}px)` }, { transform: "none" }], {
          duration: 450,
          easing: "cubic-bezier(.2,.8,.2,1)",
        });
      }
    }
    tops.current = next;
  }, [answer, chapterOpen]);

  const enter = () => {
    const text = q.trim();
    if (!text || busy) return;
    if (mode === "open") {
      const top = answer?.pages[0];
      if (top) onOpen(top.id);
      else setOpenNote("Nothing matches yet.");
      return;
    }
    if (mode === "new") {
      onStartPage(text);
      return;
    }
    onSend(text);
  };

  // Escape, from anywhere in the Index: out of a chapter, else back to the
  // page underneath.
  const onSectionKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    if (naming != null) return;  // the name box's own Escape
    e.preventDefault();
    if (chapterOpen != null) setChapterOpen(null);
    else onClose();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    // The Enter that ends an IME composition commits the words; it never
    // sends them (Safari reports isComposing false and keyCode 229).
    if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
    e.preventDefault();
    if (e.repeat) return;
    enter();
  };

  // ── Chapters: a tab dropped on one files its page there ──────────────────
  const takesTab = (e: DragEvent) => Array.from(e.dataTransfer.types).includes(BOOK_DRAG_TYPE);
  const overChapter = (key: string) => (e: DragEvent) => {
    if (!takesTab(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (over !== key) setOver(key);
  };
  const dropOn = (c: Chapter) => (e: DragEvent) => {
    if (!takesTab(e)) return;
    e.preventDefault();
    setOver(null);
    const raw = e.dataTransfer.getData(BOOK_DRAG_TYPE);
    const id = Number(raw);
    if (!raw || !Number.isFinite(id)) return;
    onFile(id, c.id);
  };

  // A name box (a chapter's key, or "new") is finished by Enter or by
  // leaving it, whichever comes first: the box losing the keyboard as an
  // Enter closes it finds nothing left to finish, so a name is written once.
  const namingRef = useRef<string | null>(null);
  const startNaming = (key: string) => {
    namingRef.current = key;
    setNaming(key);
  };
  const stopNaming = () => {
    namingRef.current = null;
    setNaming(null);
  };
  const takeNaming = (): boolean => {
    const was = namingRef.current != null;
    stopNaming();
    return was;
  };

  const rename = async (c: Chapter) => {
    if (!takeNaming()) return;
    const name = nameDraft.trim();
    if (c.id == null || !name || name === c.name) return;
    const r = await updateBucket(c.id, { name });
    if (!r.ok) setChapterNote(`Could not rename the chapter: ${r.message}`);
    onBucketsChanged();
  };
  const recolour = async (c: Chapter, color: string | null) => {
    if (c.id == null) return;
    const r = await updateBucket(c.id, { color: color ?? "" });
    if (!r.ok) setChapterNote(`Could not change the colour: ${r.message}`);
    onBucketsChanged();
  };
  const addChapter = async () => {
    if (!takeNaming()) return;
    const name = nameDraft.trim();
    if (!name) return;
    const r = await createBucket(name);
    if (!r.ok) setChapterNote(`Could not make the chapter: ${r.message}`);
    onBucketsChanged();
  };
  // A chapter deleted, after asking: its pages stay, in no chapter (DELETE
  // /api/projects/buckets/{id} unfiles them; no page is deleted).
  const removeChapter = async (c: Chapter) => {
    if (c.id == null) return;
    if (!window.confirm(`Delete the chapter “${c.name}”? Its pages stay, outside any chapter.`)) return;
    const r = await deleteBucket(c.id);
    if (!r.ok) {
      setChapterNote(`Could not delete the chapter: ${r.message}`);
      return;
    }
    setChapterNote(null);
    setChapterOpen(null);
    onBucketsChanged();
  };

  const openChapter = (c: Chapter) => {
    setChapterNote(null);
    onChapter(c, () => setChapterOpen(chapterKey(c)));
  };

  const pageCard = (p: { id: number; name: string; archived: boolean; when: string | null; where: string; parts?: ReturnType<typeof excerptParts> }, top: boolean) => (
    <button
      className="book-ix-res"
      data-archived={p.archived || undefined}
      data-index-result={p.id}
      data-top={top || undefined}
      key={p.id}
      onClick={() => onOpen(p.id)}
      type="button"
    >
      <span className="book-ix-t">
        {p.name}
        <small>{p.where}</small>
      </span>
      {p.parts && p.parts.length > 0 && (
        <span className="book-ix-s">
          {p.parts.map((x, i) => (x.mark ? <mark key={i}>{x.text}</mark> : <span key={i}>{x.text}</span>))}
        </span>
      )}
      {top && mode === "open" && <span className="book-ix-go">↵ opens</span>}
    </button>
  );

  const whereOf = (archived: boolean, bucketId: number | null, at: string | null) => {
    if (!archived) return "open page";
    const ch = chapterName(bucketId);
    const when = at ? timeAgo(at) : "";
    return [ch ? `${ch} chapter` : "loose pages", "archived", when].filter(Boolean).join(" · ");
  };

  // ── What the Index shows ─────────────────────────────────────────────────
  if (shownChapter) {
    const c = shownChapter;
    const key = chapterKey(c);
    return (
      <section
        aria-label={`Chapter: ${c.name}`}
        className="book-index"
        data-chapter-open={key}
        data-index
        onKeyDown={onSectionKey}
      >
        <div className="book-head">
          <span>Index · chapter</span>
          <span>{chapterCount(c)}</span>
        </div>
        <button className="book-ix-back" data-chapter-back onClick={() => setChapterOpen(null)} type="button">
          ← Every page
        </button>
        <div className="book-ix-chaphead">
          <i className="book-chip" style={c.color ? { background: c.color } : undefined} />
          {naming === key ? (
            <input
              aria-label="Chapter name"
              autoFocus
              className="book-ix-name"
              data-chapter-name-input
              maxLength={80}
              onBlur={() => void rename(c)}
              onChange={(e) => setNameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void rename(c);
                if (e.key === "Escape") {
                  e.preventDefault();
                  stopNaming();
                }
              }}
              value={nameDraft}
            />
          ) : (
            <h1 className="book-title" data-chapter-title>
              {c.name}
            </h1>
          )}
          {c.id != null && naming !== key && (
            <button
              className="book-ix-link"
              data-chapter-rename
              onClick={() => {
                setNameDraft(c.name);
                startNaming(key);
              }}
              type="button"
            >
              Rename
            </button>
          )}
          {c.id != null && naming !== key && (
            <button className="book-ix-link" data-chapter-delete onClick={() => void removeChapter(c)} type="button">
              Delete
            </button>
          )}
        </div>
        {c.id != null && (
          <div aria-label="Chapter colour" className="book-ix-swatches" role="group">
            {BUCKET_COLORS.map((col) => (
              <button
                aria-label={`Colour ${col}`}
                aria-pressed={c.color === col}
                className="book-swatch"
                data-swatch={col}
                key={col}
                onClick={() => void recolour(c, col)}
                style={{ background: col }}
                type="button"
              />
            ))}
            <button className="book-ix-link" onClick={() => void recolour(c, null)} type="button">
              No colour
            </button>
          </div>
        )}
        {chapterNote && <p className="book-ix-line" data-error>{chapterNote}</p>}
        <div className="book-ix-list" ref={listRef}>
          {c.archived.length === 0 && c.open.length === 0 && (
            <p className="book-ix-empty">
              {c.id == null
                ? "No archived pages are outside a chapter."
                : "No pages yet. Drag a tab onto this chapter to file its page here."}
            </p>
          )}
          {c.archived.map((p) =>
            pageCard({ id: p.id, name: p.name, archived: true, when: p.last_activity_at ?? null,
                       where: ["archived", p.last_activity_at ? timeAgo(p.last_activity_at) : ""].filter(Boolean).join(" · ") }, false),
          )}
          {c.open.length > 0 && <div className="book-ix-k">Open in the book</div>}
          {c.open.map((p) => pageCard({ id: p.id, name: p.name, archived: false, when: null, where: "open page" }, false))}
        </div>
      </section>
    );
  }

  const pages = answer?.pages ?? [];
  const typed = q.trim() !== "";
  return (
    <section
      aria-label="Index"
      className="book-index"
      data-index
      data-index-busy={busy || undefined}
      onKeyDown={onSectionKey}
    >
      <div className="book-head">
        <span>Index · every page, open and archived</span>
      </div>
      <h1 className="book-title">Index</h1>
      <div className="book-ix-input">
        <svg aria-hidden fill="none" height="18" stroke="currentColor" strokeWidth="1.3" viewBox="0 0 12 12" width="18">
          <circle cx="5" cy="5" r="3.8" />
          <path d="M8 8l3 3" />
        </svg>
        <input
          aria-describedby="book-index-mode"
          aria-label={
            mode === "send"
              ? "Your message: the Host finds its page"
              : mode === "new"
                ? "Your message: it starts a new page"
                : "What you remember"
          }
          autoComplete="off"
          data-index-q
          id={INDEX_INPUT_ID}
          onChange={(e) => {
            setQ(e.target.value);
            setOpenNote(null);
            if (said && !ask) onPickRead();
          }}
          onKeyDown={onKeyDown}
          placeholder={
            mode === "send"
              ? "Write your message. The Host finds its page."
              : mode === "new"
                ? "Write your message. It starts a new page."
                : "Type what you remember"
          }
          readOnly={busy}
          ref={inputRef}
          spellCheck
          value={q}
        />
      </div>
      <div className="book-ix-mode" id="book-index-mode">
        <span>Enter</span>
        <button aria-pressed={mode === "send"} data-mode-btn="send" onClick={() => setMode("send")} type="button">
          sends it to the page the Host picks
        </button>
        <button aria-pressed={mode === "open"} data-mode-btn="open" onClick={() => setMode("open")} type="button">
          only opens the page
        </button>
        <button aria-pressed={mode === "new"} data-mode-btn="new" onClick={() => setMode("new")} type="button">
          starts a new page
        </button>
      </div>
      <p aria-live="polite" className="book-ix-line" data-index-line data-kind={lineKind} role="status">
        {said || openNote || ""}
      </p>
      {ask && (
        <div className="book-ix-ask" data-index-ask>
          {ask.candidates.map((c) => (
            <button data-ask-page={c.project_id} key={c.project_id} onClick={() => onAskPick(c.project_id, ask.text)} type="button">
              {c.name}
              {c.archived ? <small> · archived</small> : null}
            </button>
          ))}
        </div>
      )}
      <div className="book-ix-list" data-index-results ref={listRef}>
        {!typed && pages.length > 0 && <div className="book-ix-k">Most recent</div>}
        {typed && answer && pages.length === 0 && (
          <p className="book-ix-empty">
            {mode === "send"
              ? "Nothing matches yet. Enter sends it anyway: the Host picks a page, or starts a new one."
              : "Nothing matches yet."}
          </p>
        )}
        {searchError && <p className="book-ix-empty">The search did not answer: {searchError}</p>}
        {pages.map((p, i) =>
          pageCard(
            {
              id: p.id,
              name: p.name,
              archived: p.archived,
              when: p.last_activity_at,
              where: whereOf(p.archived, p.bucket_id, p.last_activity_at),
              parts: excerptParts(p.snippet),
            },
            i === 0 && typed,
          ),
        )}
      </div>
      <div className="book-ix-chapters" data-chapters>
        <div className="book-ix-k">Chapters{dragging != null ? " · drop the tab on one to file its page there" : ""}</div>
        <div className="book-ix-chaps">
          {chapters.map((c) => {
            const key = chapterKey(c);
            return (
              <button
                className="book-chap"
                data-chapter={key}
                data-over={over === key || undefined}
                key={key}
                onClick={() => openChapter(c)}
                onDragEnter={overChapter(key)}
                onDragLeave={() => setOver((cur) => (cur === key ? null : cur))}
                onDragOver={overChapter(key)}
                onDrop={dropOn(c)}
                style={c.color ? { borderTopColor: c.color } : undefined}
                type="button"
              >
                <b>{c.name}</b>
                <span>{chapterCount(c)}</span>
              </button>
            );
          })}
          {naming === "new" ? (
            <input
              aria-label="New chapter's name"
              autoFocus
              className="book-chap book-chap-new"
              data-new-chapter-input
              maxLength={80}
              onBlur={() => void addChapter()}
              onChange={(e) => setNameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void addChapter();
                if (e.key === "Escape") {
                  e.preventDefault();
                  stopNaming();
                }
              }}
              placeholder="Name it"
              value={nameDraft}
            />
          ) : (
            <button
              className="book-chap book-chap-new"
              data-new-chapter
              onClick={() => {
                setNameDraft("");
                startNaming("new");
              }}
              type="button"
            >
              <b>+ New chapter</b>
              <span>name it, then drag tabs in</span>
            </button>
          )}
        </div>
        {chapterNote && <p className="book-ix-line" data-error>{chapterNote}</p>}
      </div>
    </section>
  );
}
