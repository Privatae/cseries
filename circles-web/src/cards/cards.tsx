import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { followScheme } from "@kit/book/scheme";
// The book's look, shared with the Workbench (the fonts, the desktop's
// colours on the shared components, the running count, "Where this stands",
// the nudge line and the press), then the cards' own.
import "@kit/book/book.css";
import "./cards.css";
import { CardsApp } from "./cards-app";
import { Guard } from "./guards";

/** Mount the cards, which are Circles (src/main.tsx). */
export function mountCards(root: HTMLElement): void {
  // Dark or light as the desktop's design is, from before the first paint.
  followScheme();
  createRoot(root).render(
    <StrictMode>
      {/* The last resort: a crash no inner guard caught still leaves a way
          back, never a blank Circles. */}
      <Guard label="circles">
        <CardsApp />
      </Guard>
    </StrictMode>,
  );
}
