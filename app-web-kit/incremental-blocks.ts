// Incremental block splitting for a streamed markdown reply.
//
// Streamdown renders a message as a list of blocks, one memoized component per
// block, and gets that list from parseMarkdownIntoBlocks — which lexes the WHOLE
// text every time the text changes. While a reply streams, that is once per
// update, so a long reply costs O(length) per chunk and O(length²) overall.
//
// A reply only grows at the end, and a markdown block far behind the end cannot
// change when text is appended to it. So this keeps every block except the last
// few from the previous call and re-splits only from there. Streamdown accepts
// the result through its `parseMarkdownIntoBlocksFn` prop.
//
// Streamdown closes unfinished syntax at the end of the text before splitting
// (an open ``` or ** gets its closer), so consecutive inputs are not strict
// extensions of each other. What is required is only that the text covered by
// the kept blocks is unchanged, and that is checked on every call.
//
// Any other input — an edited or shorter message, CR line endings the lexer
// would normalize, footnotes (which Streamdown renders as one block because
// they refer across the whole message) — takes the full split, exactly as
// before. scripts/perf and test/incremental-blocks.test.ts pin the result
// against the full split at every chunk of real replies.

type Split = (markdown: string) => string[];

// Blocks re-split on each call. Appending text can re-type the block before it
// (a `---` line turns the paragraph above into a heading; a `|---|` row turns it
// into a table) and blank lines are blocks of their own, so the last three are
// redone; everything before them is kept as-is.
const RESPLIT_TAIL = 3;

// Same tests Streamdown's splitter applies before lexing (footnote reference or
// definition anywhere → the whole message is one block).
const FOOTNOTE_REF = /\[\^[\w-]{1,200}\](?!:)/;
const FOOTNOTE_DEF = /\[\^[\w-]{1,200}\]:/;

export function createIncrementalSplit(split: Split): Split {
  let prevText = "";
  let prevBlocks: string[] = [];
  // False when the previous blocks do not add up to the previous text, so their
  // offsets cannot be trusted.
  let prevExact = false;

  const full = (text: string): string[] => {
    const blocks = split(text);
    let len = 0;
    for (const b of blocks) len += b.length;
    prevText = text;
    prevBlocks = blocks;
    prevExact = len === text.length;
    return blocks;
  };

  return (text: string): string[] => {
    const keep = prevBlocks.length - RESPLIT_TAIL;
    if (!prevExact || keep <= 0 || text.includes("\r")
        || FOOTNOTE_REF.test(text) || FOOTNOTE_DEF.test(text)) {
      return full(text);
    }
    let offset = 0;
    for (let i = 0; i < keep; i++) offset += prevBlocks[i].length;
    if (text.length < offset || !text.startsWith(prevText.slice(0, offset))) {
      return full(text);
    }
    const tail = split(text.slice(offset));
    let len = offset;
    for (const b of tail) len += b.length;
    const blocks = prevBlocks.slice(0, keep).concat(tail);
    prevText = text;
    prevBlocks = blocks;
    prevExact = len === text.length;
    return blocks;
  };
}
