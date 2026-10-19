import { installViewInbox } from "./lib/app-view";
import { installComposeInbox } from "./lib/compose-inbox";
import "./index.css";

// Circles is the cards (src/cards/; the rail and its panes are gone). Every view the desktop hands
// this window, thread:, support, decision:, inbox, tickets, bastions and
// card:<kind>:<id>, is read by src/lib/cards.ts cardsTarget.
const root = document.getElementById("root");
if (!root) throw new Error("Circles: #root mount node missing");

// Before the first render: a view the desktop posts on this frame's load can
// land before any effect has run (lib/app-view.ts says why).
installViewInbox();
// The same for what another app hands your Host's chat (lib/compose-inbox.ts):
// held until your Host's composer is drawn.
installComposeInbox();
// The book's tokens hang on this attribute (app-web-kit/book/book.css).
document.documentElement.dataset.page = "book";
void import("./cards/cards").then((m) => m.mountCards(root));
