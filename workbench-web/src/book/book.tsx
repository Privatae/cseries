import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BookApp } from "./book-app";
// The book's look (shared with Circles), then what only the Workbench's
// pages draw: the bench, the run cards, a project's build.
import "@kit/book/book.css";
import "./book.css";
import { followScheme } from "@kit/book/scheme";

/** Mount the book: the Workbench's one layout (src/main.tsx). */
export function mountBook(root: HTMLElement): void {
  // Dark or light as the desktop's design is, from before the first paint.
  followScheme();
  createRoot(root).render(
    <StrictMode>
      <BookApp />
    </StrictMode>,
  );
}
