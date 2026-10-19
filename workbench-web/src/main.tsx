import { installViewInbox } from "./lib/app-view";
import { mountBook } from "./book/book";
import "./index.css";

const root = document.getElementById("root");
if (!root) throw new Error("Workbench: #root mount node missing");

// The Workbench is the book of pages (src/book/). Before the first render:
// a view the desktop posts on this frame's load can land before any effect
// has run (lib/app-view.ts says why).
installViewInbox();
// Paper: the book's tokens hang on this attribute (src/book/book.css).
document.documentElement.dataset.page = "book";
mountBook(root);
