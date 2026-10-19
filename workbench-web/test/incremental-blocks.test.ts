import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import remend from "remend";
import { parseMarkdownIntoBlocks } from "streamdown";
import { createIncrementalSplit } from "../../app-web-kit/incremental-blocks.ts";

// The incremental splitter must return EXACTLY what Streamdown's own splitter
// returns, at every point of a stream. Replays each document the way a reply
// arrives — CHUNK characters at a time, each step passed through remend first,
// as Streamdown does in streaming mode — and compares block arrays at every step.

const CHUNK = 30;
const REPO = join(import.meta.dirname, "..", "..");

function replay(label: string, doc: string, chunk = CHUNK): number {
  const inc = createIncrementalSplit(parseMarkdownIntoBlocks);
  let steps = 0;
  for (let end = chunk; ; end += chunk) {
    const text = remend(doc.slice(0, Math.min(end, doc.length)));
    const want = parseMarkdownIntoBlocks(text);
    const got = inc(text);
    steps++;
    if (got.length !== want.length || got.some((b, i) => b !== want[i])) {
      const i = got.findIndex((b, k) => b !== want[k]);
      assert.fail(`${label}: diverged at ${Math.min(end, doc.length)}/${doc.length} chars, ` +
        `block ${i}: got ${JSON.stringify(got[i]?.slice(0, 80))} want ${JSON.stringify(want[i]?.slice(0, 80))}`);
    }
    if (end >= doc.length) break;
  }
  return steps;
}

function markdownFiles(dir: string, out: string[] = [], depth = 0): string[] {
  if (depth > 4) return out;
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".") || name === "dist") continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) markdownFiles(p, out, depth + 1);
    else if (name.endsWith(".md") && st.size < 60_000) out.push(p);
  }
  return out;
}

test("benchmark fixture: incremental split equals full split at every chunk", () => {
  const doc = readFileSync(join(REPO, "scripts/perf/bench-reply.md"), "utf8");
  assert.ok(replay("bench-reply.md", doc) > 1000);
});

test("every markdown file in the repo: equal at every chunk", () => {
  const files = markdownFiles(REPO);
  assert.ok(files.length > 100, `corpus too small: ${files.length}`);
  let steps = 0;
  // 89 chars (prime, so boundaries land everywhere) keeps this under a minute;
  // the fixture and constructs above run at 1-30.
  for (const f of files) steps += replay(f.slice(REPO.length + 1), readFileSync(f, "utf8"), 89);
  assert.ok(steps > 5_000, `steps ${steps}`);
});

test("odd chunk sizes (1, 7, 113 chars) on the fixture", () => {
  const doc = readFileSync(join(REPO, "scripts/perf/bench-reply.md"), "utf8").slice(0, 12_000);
  for (const c of [1, 7, 113]) replay(`fixture@${c}`, doc, c);
});

// One update can carry far more than a network chunk: the hook folds a whole
// frame's text into one update, and a background tab throttles the 100 ms
// fallback timer to ~1 s and later ~1 min. So the splitter also sees jumps of
// thousands of characters at once — replayed here on the fixture and corpus.
test("large jumps (2,000 and 5,000 chars per update)", () => {
  const docs = [join(REPO, "scripts/perf/bench-reply.md"), ...markdownFiles(REPO)];
  for (const f of docs) {
    const doc = readFileSync(f, "utf8");
    for (const c of [2_000, 5_000]) replay(`${f.slice(REPO.length + 1)}@${c}`, doc, c);
  }
});

// Constructs that re-type an earlier block when more text arrives, or that
// Streamdown merges across tokens.
const CASES: Record<string, string> = {
  setext: "para one\n\npara two\n\nTitle line\n---\n\nafter\n\nmore text here\n\nand more\n\nTitle two\n===\n\nend\n",
  table_arrives: "a\n\nb\n\nc\n\n| h1 | h2 |\n|----|----|\n| 1 | 2 |\n| 3 | 4 |\n\ntext after table\n\nx\n\ny\n",
  long_fence: "intro\n\n" + "p\n\n".repeat(6) + "```ts\n" + "const x = 1;\n".repeat(200) + "```\n\nafter code\n\n" + "q\n\n".repeat(5),
  unclosed_fence_at_end: "a\n\nb\n\nc\n\nd\n\n```python\n" + "print('hi')\n".repeat(80),
  html_block: "a\n\nb\n\nc\n\n<details>\n<summary>more</summary>\n\ninside one\n\ninside two\n\n</details>\n\nafter\n\nz\n\nw\n",
  math: "a\n\nb\n\nc\n\n$$\nx = 1\n\ny = 2\n$$\n\nafter\n\nm\n\nn\n",
  lists: "a\n\nb\n\nc\n\n- one\n- two\n\n- three after blank\n  continued\n\n1. x\n2. y\n\ntext\n\nq\n\nr\n",
  blockquote_lazy: "a\n\nb\n\nc\n\n> quoted\nlazy continuation\n\n> second\n\nafter\n\nx\n\ny\n",
  footnote_late: "a\n\nb\n\nc\n\nd\n\ne\n\nf\n\nsee note[^1]\n\n[^1]: the note\n",
  crlf: "a\r\n\r\nb\r\n\r\nc\r\n\r\nd\r\n\r\ne\r\n",
  bold_open: "a\n\nb\n\nc\n\nd\n\n**bold that stays open for a while and keeps going " + "word ".repeat(60),
};

test("constructs that change earlier blocks or merge tokens", () => {
  for (const [name, doc] of Object.entries(CASES)) {
    for (const c of [1, 3, 30]) replay(`${name}@${c}`, doc, c);
  }
});

test("a non-append change (edit / new message) falls back to the full split", () => {
  const inc = createIncrementalSplit(parseMarkdownIntoBlocks);
  const a = "one\n\ntwo\n\nthree\n\nfour\n\nfive\n\nsix\n";
  inc(a);
  const b = "ONE\n\ntwo\n\nthree\n\nfour\n\nfive\n\nsix\n\nseven\n";
  assert.deepEqual(inc(b), parseMarkdownIntoBlocks(b));
  const c = "short\n";
  assert.deepEqual(inc(c), parseMarkdownIntoBlocks(c));
});

test("the tail is what gets split, not the whole message", () => {
  let lexed = 0;
  const counting = (s: string) => { lexed += s.length; return parseMarkdownIntoBlocks(s); };
  const inc = createIncrementalSplit(counting);
  const doc = readFileSync(join(REPO, "scripts/perf/bench-reply.md"), "utf8");
  let fullCost = 0;
  for (let end = CHUNK; end < doc.length; end += CHUNK) {
    const t = remend(doc.slice(0, end));
    fullCost += t.length;
    inc(t);
  }
  // Every step re-lexing everything costs the sum of all prefix lengths.
  // Measured 2026-09-23 on this fixture: 2.38M chars lexed vs 29.3M (12.3x).
  // What remains is the growing last block (a long table or code block is
  // re-lexed while it grows). The ceiling holds that win; lower it, never raise.
  assert.ok(lexed * 10 < fullCost, `lexed ${lexed} chars vs ${fullCost} for full re-splits`);
});
