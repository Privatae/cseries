import type { FlipMotion } from "@/lib/book";
import { riffleSchedule } from "@/lib/book-index";
import { copyOfPage, turnAway } from "./flip";

/** One page that goes past in a riffle: what its head and title say. */
export interface RiffleLeaf {
  /** Its chapter's name, as the head names it. */
  chapter: string;
  /** When it was last written on, for the head's right side. */
  when: string;
  title: string;
}

function leafOf(leaf: RiffleLeaf): HTMLElement {
  const sheet = document.createElement("article");
  sheet.className = "book-sheet book-leaf";
  sheet.dataset.riffleLeaf = "";
  sheet.setAttribute("aria-hidden", "true");
  sheet.inert = true;
  const shade = document.createElement("div");
  shade.className = "book-shade";
  const inner = document.createElement("div");
  inner.className = "book-inner";
  const head = document.createElement("div");
  head.className = "book-head";
  const left = document.createElement("span");
  left.textContent = leaf.chapter;
  const right = document.createElement("span");
  right.textContent = leaf.when;
  head.append(left, right);
  const title = document.createElement("h1");
  title.className = "book-title";
  title.textContent = leaf.title;
  const note = document.createElement("p");
  note.className = "book-leaf-note";
  note.textContent = "Archived";
  inner.append(head, title, note);
  sheet.append(shade, inner);
  return sheet;
}

/**
 * The riffle (docs/design/workbench-book/mockup-pages-v2.html, riffle): the
 * page in front and a run of a chapter's pages are laid over the book, the
 * page in front on top, and turn away one after another, quickly, the last
 * ones slowing, while the book draws the page it lands on underneath
 * (`land`, called once the copies are in place). Like a page turn, it never
 * moves or remounts the live page. With reduced motion it is the page turn's
 * crossfade, and no run of pages is drawn.
 */
export function riffle(
  sheet: HTMLElement,
  book: HTMLElement,
  leaves: RiffleLeaf[],
  motion: FlipMotion,
  land: () => void,
  done: () => void,
): void {
  if (motion.kind === "fade" || leaves.length === 0) {
    turnAway(sheet, book, motion, done);
    land();
    return;
  }
  const cover = copyOfPage(sheet, book);
  cover.classList.add("book-leaf");
  cover.dataset.riffleCover = "";
  const nodes = [cover, ...leaves.map((l) => {
    const n = leafOf(l);
    book.appendChild(n);
    return n;
  })];
  nodes.forEach((n, i) => {
    n.style.zIndex = String(60 + nodes.length - i);
  });
  land();
  const { delays, total } = riffleSchedule(nodes.length);
  nodes.forEach((n, i) => window.setTimeout(() => n.classList.add("is-turning"), delays[i]));
  window.setTimeout(() => {
    for (const n of nodes) n.remove();
    done();
  }, total);
}
