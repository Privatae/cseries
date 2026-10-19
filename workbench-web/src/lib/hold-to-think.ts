// Hold-to-think on the book's Send is the kit's (app-web-kit/book/
// hold-to-think.ts, with its press, app-web-kit/book/press-button.tsx),
// re-exported here under the name the Workbench has always used. Imported by
// a relative path: plain `node --test` cannot read the app's @kit alias
// (test/hold-to-think.test.ts).

export * from "../../../app-web-kit/book/hold-to-think.ts";
