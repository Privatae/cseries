import type { FlipMotion } from "@/lib/book";

/**
 * A still copy of the page in front, safe to lay over the book for a moment
 * (the page turn below, and the riffle's first page, src/book/riffle.ts):
 * frames and media are replaced by blank blocks of the same size (a copied
 * frame would load its page again), ids are dropped (they would repeat), it
 * takes no input and is hidden from assistive technology, and every element
 * that was scrolled keeps its scroll (a copy starts at the top, which would
 * make the conversation jump). It is appended to `book` and returned.
 */
export function copyOfPage(sheet: HTMLElement, book: HTMLElement): HTMLElement {
  // What the copy needs from the live page, carried by marks it copies along:
  // where each scrolled element is scrolled, and how big each frame is.
  const scrolls: Array<[number, number]> = [];
  const sizes: Array<[number, number]> = [];
  const marked: HTMLElement[] = [];
  for (const el of [sheet, ...sheet.querySelectorAll<HTMLElement>("*")]) {
    if (el.scrollTop || el.scrollLeft) {
      el.dataset.flipScroll = String(scrolls.length);
      scrolls.push([el.scrollTop, el.scrollLeft]);
      marked.push(el);
    }
  }
  for (const el of sheet.querySelectorAll<HTMLElement>("iframe, video, audio, object, embed, canvas")) {
    el.dataset.flipMedia = String(sizes.length);
    sizes.push([el.offsetWidth, el.offsetHeight]);
    marked.push(el);
  }
  const ghost = sheet.cloneNode(true) as HTMLElement;
  for (const el of marked) {
    delete el.dataset.flipScroll;
    delete el.dataset.flipMedia;
  }

  ghost.removeAttribute("id");
  for (const el of ghost.querySelectorAll("[id]")) el.removeAttribute("id");
  for (const el of ghost.querySelectorAll<HTMLElement>("[data-flip-media]")) {
    const [w, h] = sizes[Number(el.dataset.flipMedia)] ?? [0, 0];
    const blank = document.createElement("div");
    blank.style.width = `${w}px`;
    blank.style.height = `${h}px`;
    el.replaceWith(blank);
  }
  ghost.setAttribute("aria-hidden", "true");
  ghost.inert = true;
  book.appendChild(ghost);

  for (const el of [ghost, ...ghost.querySelectorAll<HTMLElement>("[data-flip-scroll]")]) {
    if (el.dataset.flipScroll == null) continue;
    const [top, left] = scrolls[Number(el.dataset.flipScroll)] ?? [0, 0];
    el.scrollTop = top;
    el.scrollLeft = left;
    delete el.dataset.flipScroll;
  }
  return ghost;
}

/**
 * Turn the page in front away, as the mockup does
 * (docs/design/workbench-book/mockup-pages-v2.html, flipTo): a still copy of
 * the page as it was (copyOfPage) is laid over the book while the book draws
 * the next page underneath, and the copy turns about its left edge (or fades,
 * for reduced motion) and is removed. The live page is never moved or
 * remounted, so the conversation and its streams are untouched by a turn.
 */
export function turnAway(
  sheet: HTMLElement,
  book: HTMLElement,
  motion: FlipMotion,
  done: () => void,
): void {
  const ghost = copyOfPage(sheet, book);
  ghost.classList.add("book-ghost");
  ghost.dataset.motion = motion.kind;

  // Two frames: the copy must be drawn where the page was before it moves.
  requestAnimationFrame(() => requestAnimationFrame(() => ghost.classList.add("is-turning")));
  window.setTimeout(() => {
    ghost.remove();
    done();
  }, motion.ms + 40);
}
