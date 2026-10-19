<!-- Benchmark fixture: one long markdown reply replayed through the chat renderer.
     Verbatim public text from the READMEs of streamdown (Apache-2.0), marked, unified
     and enhanced-resolve (MIT), so the benchmark input can be published. Chosen to
     contain no footnote syntax: Streamdown renders any message containing a footnote
     reference as ONE block, a separate case this fixture does not measure. -->

# Streamdown

A drop-in replacement for react-markdown, designed for AI-powered streaming.

[![npm version](https://img.shields.io/npm/v/streamdown)](https://www.npmjs.com/package/streamdown)

## Overview

Formatting Markdown is easy, but when you tokenize and stream it, new challenges arise. Streamdown is built specifically to handle the unique requirements of streaming Markdown content from AI models, providing seamless formatting even with incomplete or unterminated Markdown blocks.

Streamdown powers the [AI Elements Message](https://ai-sdk.dev/elements/components/message) component but can be installed as a standalone package for your own streaming needs.

## Features

- 🚀 **Drop-in replacement** for `react-markdown`
- 🔄 **Streaming-optimized** - Handles incomplete Markdown gracefully
- 🎨 **Unterminated block parsing** - Build with `remend` for better streaming quality
- 📊 **GitHub Flavored Markdown** - Tables, task lists, and strikethrough support
- 🔢 **Math rendering** - LaTeX equations via KaTeX
- 📈 **Mermaid diagrams** - Render Mermaid diagrams as code blocks with a button to render them
- 🎯 **Code syntax highlighting** - Beautiful code blocks with Shiki
- 🛡️ **Security-first** - Built with `rehype-harden` for safe rendering
- ⚡ **Performance optimized** - Memoized rendering for efficient updates

## Installation

```bash
npm i streamdown
```

Then, update your Tailwind `globals.css` to include the following so that Tailwind can detect the utility classes used by Streamdown.

```css
@source "../node_modules/streamdown/dist/*.js";
```

The path must be relative from your CSS file to the `node_modules` folder containing `streamdown`. In a standard Next.js project where `globals.css` lives in `app/`, the default path above should work.

If you install optional Streamdown plugins, add their matching `@source` lines from the relevant plugin docs (`/docs/plugins/code`, `/docs/plugins/cjk`, `/docs/plugins/math`, `/docs/plugins/mermaid`). Only include plugin `@source` entries for packages that are actually installed.

Example:

```css
@source "../node_modules/@streamdown/code/dist/*.js";
```


### Monorepo setup

In a monorepo (npm workspaces, Turbo, pnpm, etc.), dependencies are typically hoisted to the root `node_modules`. You need to adjust the relative path to point there:

```
monorepo/
├── node_modules/streamdown/  ← hoisted here
├── apps/
│   └── web/
│       └── app/
│           └── globals.css   ← your CSS file
```

```css
/* apps/web/app/globals.css → 3 levels up to reach root node_modules */
@source "../../../node_modules/streamdown/dist/*.js";
```

Adjust the number of `../` segments based on where your CSS file lives relative to the root `node_modules`. If you install Streamdown plugins, add their respective `@source` entries only when those packages are installed.

### CSS Custom Properties (Design Tokens)

Streamdown components are built using shadcn/ui's design system and rely on CSS custom properties for colors, border radius, and spacing. Without these variables defined, components may render with missing backgrounds, borders, or incorrect spacing.

If you are already using shadcn/ui, these variables are set up automatically. If not, add the following minimal set to your global CSS:

```css
:root {
  --background: oklch(1 0 0);
  --foreground: oklch(0.145 0 0);
  --card: oklch(1 0 0);
  --card-foreground: oklch(0.145 0 0);
  --muted: oklch(0.97 0 0);
  --muted-foreground: oklch(0.556 0 0);
  --border: oklch(0.922 0 0);
  --input: oklch(0.922 0 0);
  --primary: oklch(0.205 0 0);
  --primary-foreground: oklch(0.985 0 0);
  --radius: 0.625rem;
}

.dark {
  --background: oklch(0.145 0 0);
  --foreground: oklch(0.985 0 0);
  --card: oklch(0.205 0 0);
  --card-foreground: oklch(0.985 0 0);
  --muted: oklch(0.269 0 0);
  --muted-foreground: oklch(0.708 0 0);
  --border: oklch(0.269 0 0);
  --input: oklch(0.269 0 0);
  --primary: oklch(0.985 0 0);
  --primary-foreground: oklch(0.205 0 0);
  --radius: 0.625rem;
}
```

You can also use the shadcn/ui [theme generator](https://ui.shadcn.com/themes) to create a custom palette and copy the generated CSS variables directly into your project.

## Usage

Here's how you can use Streamdown in your React application with the AI SDK:

```tsx
import { useChat } from "@ai-sdk/react";
import { Streamdown } from "streamdown";
import { code } from "@streamdown/code";
import { mermaid } from "@streamdown/mermaid";
import { math } from "@streamdown/math";
import { cjk } from "@streamdown/cjk";
import "katex/dist/katex.min.css";
import "streamdown/styles.css";

export default function Chat() {
  const { messages, status } = useChat();

  return (
    <div>
      {messages.map(message => (
        <div key={message.id}>
          {message.role === 'user' ? 'User: ' : 'AI: '}
          {message.parts.map((part, index) =>
            part.type === 'text' ? (
              <Streamdown
                key={index}
                animated
                plugins={{ code, mermaid, math, cjk }}
                isAnimating={status === 'streaming'}
              >
                {part.text}
              </Streamdown>
            ) : null,
          )}
        </div>
      ))}
    </div>
  );
}
```

For more info, see the [documentation](https://streamdown.ai/docs).


<a href="https://marked.js.org">
  <img width="60px" height="60px" src="https://marked.js.org/img/logo-black.svg" align="right" />
</a>

# Marked

[![npm](https://badgen.net/npm/v/marked)](https://www.npmjs.com/package/marked)
[![install size](https://badgen.net/packagephobia/install/marked)](https://packagephobia.now.sh/result?p=marked)
[![downloads](https://badgen.net/npm/dt/marked)](https://www.npmjs.com/package/marked)
[![github actions](https://github.com/markedjs/marked/workflows/Tests/badge.svg)](https://github.com/markedjs/marked/actions)
[![snyk](https://snyk.io/test/npm/marked/badge.svg)](https://snyk.io/test/npm/marked)

- ⚡ built for speed
- ⬇️ low-level compiler for parsing markdown without caching or blocking for long periods of time
- ⚖️ light-weight while implementing all markdown features from the supported flavors & specifications
- 🌐 works in a browser, on a server, or from a command line interface (CLI)

## Demo

Check out the [demo page](https://marked.js.org/demo/) to see Marked in action ⛹️

## Docs

Our [documentation pages](https://marked.js.org) are also rendered using marked 💯

Also read about:

* [Options](https://marked.js.org/using_advanced)
* [Extensibility](https://marked.js.org/using_pro)

## Compatibility

**Node.js:** Only [current and LTS](https://nodejs.org/en/about/releases/) Node.js versions are supported. End of life Node.js versions may become incompatible with Marked at any point in time.

**Browser:** [Baseline Widely Available](https://developer.mozilla.org/en-US/docs/Glossary/Baseline/Compatibility)

## Installation

**CLI:**

```sh
npm install -g marked
```

**In-browser:**

```sh
npm install marked
```

## Usage

### Warning: 🚨 Marked does not [sanitize](https://marked.js.org/using_advanced#options) the output HTML. Please use a sanitize library, like [DOMPurify](https://github.com/cure53/DOMPurify) (recommended), [sanitize-html](https://github.com/apostrophecms/sanitize-html) or [insane](https://github.com/bevacqua/insane) on the *output* HTML! 🚨

```
DOMPurify.sanitize(marked.parse(`<img src="x" onerror="alert('not happening')">`));
```

**CLI**

``` bash
# Example with stdin input
$ marked -o hello.html
hello world
^D
$ cat hello.html
<p>hello world</p>
```

```bash
# Print all options
$ marked --help
```

**Browser**

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Marked in the browser</title>
</head>
<body>
  <div id="content"></div>
  <script src="https://cdn.jsdelivr.net/npm/marked/lib/marked.umd.js"></script>
  <script>
    document.getElementById('content').innerHTML =
      marked.parse('# Marked in the browser\n\nRendered by **marked**.');
  </script>
</body>
</html>
```
or import esm module

```html
<script type="module">
  import { marked } from "https://cdn.jsdelivr.net/npm/marked/lib/marked.esm.js";
  document.getElementById('content').innerHTML =
    marked.parse('# Marked in the browser\n\nRendered by **marked**.');
</script>
```

## License

Copyright (c) 2018+, MarkedJS. (MIT License)
Copyright (c) 2011-2018, Christopher Jeffrey. (MIT License)


# [![unified][logo]][site]

[![Build][build-badge]][build]
[![Coverage][coverage-badge]][coverage]
[![Downloads][downloads-badge]][downloads]
[![Size][size-badge]][size]
[![Sponsors][sponsors-badge]][collective]
[![Backers][backers-badge]][collective]
[![Chat][chat-badge]][chat]

**unified** lets you inspect and transform content with plugins.

## Contents

* [What is this?](#what-is-this)
* [When should I use this?](#when-should-i-use-this)
* [Install](#install)
* [Use](#use)
* [Overview](#overview)
* [API](#api)
  * [`processor()`](#processor)
  * [`processor.compiler`](#processorcompiler)
  * [`processor.data([key[, value]])`](#processordatakey-value)
  * [`processor.freeze()`](#processorfreeze)
  * [`processor.parse(file)`](#processorparsefile)
  * [`processor.parser`](#processorparser)
  * [`processor.process(file[, done])`](#processorprocessfile-done)
  * [`processor.processSync(file)`](#processorprocesssyncfile)
  * [`processor.run(tree[, file][, done])`](#processorruntree-file-done)
  * [`processor.runSync(tree[, file])`](#processorrunsynctree-file)
  * [`processor.stringify(tree[, file])`](#processorstringifytree-file)
  * [`processor.use(plugin[, options])`](#processoruseplugin-options)
  * [`CompileResultMap`](#compileresultmap)
  * [`CompileResults`](#compileresults)
  * [`Compiler`](#compiler)
  * [`Data`](#data)
  * [`Parser`](#parser)
  * [`Pluggable`](#pluggable)
  * [`PluggableList`](#pluggablelist)
  * [`Plugin`](#plugin)
  * [`PluginTuple`](#plugintuple)
  * [`Preset`](#preset)
  * [`ProcessCallback`](#processcallback)
  * [`Processor`](#processor-1)
  * [`RunCallback`](#runcallback)
  * [`Settings`](#settings)
  * [`TransformCallback`](#transformcallback)
  * [`Transformer`](#transformer)
* [Types](#types)
* [Compatibility](#compatibility)
* [Contribute](#contribute)
* [Sponsor](#sponsor)
* [Acknowledgments](#acknowledgments)
* [License](#license)

## What is this?

unified is two things:

* **unified** is a collective of 500+ free and open source packages that work
  with content as structured data (ASTs)
* `unified` (this project) is the core package, used in 1.3m+ projects on GH,
  to process content with plugins

Several ecosystems are built on unified around different kinds of content.
Notably, [remark][] (markdown), [rehype][] (HTML), and [retext][] (natural
language).
These ecosystems can be connected together.

* for more about us, see [`unifiedjs.com`][site]
* for updates, see [@unifiedjs][twitter] on Twitter
* for questions, see [support][]
* to help, see [contribute][] and [sponsor][] below

## When should I use this?

In some cases, you are already using unified.
For example, it’s used in MDX, Gatsby, Docusaurus, etc.
In those cases, you don’t need to add `unified` yourself but you can include
plugins into those projects.

But the real fun (for some) is to get your hands dirty and work with syntax
trees and build with it yourself.
You can create those projects, or things like Prettier, or your own site
generator.
You can connect utilities together and make your own plugins that check for
problems and transform from one thing to another.

When you are dealing with one type of content (such as markdown), you can use
the main package of that ecosystem instead (so `remark`).
When you are dealing with different kinds of content (such as markdown and
HTML), it’s recommended to use `unified` itself, and pick and choose the plugins
you need.

## Install

This package is [ESM only][esm].
In Node.js (version 16+), install with [npm][]:

```sh
npm install unified
```

In Deno with [`esm.sh`][esmsh]:

```js
import {unified} from 'https://esm.sh/unified@11'
```

In browsers with [`esm.sh`][esmsh]:

```html
<script type="module">
  import {unified} from 'https://esm.sh/unified@11?bundle'
</script>
```

## Use

```js
import rehypeDocument from 'rehype-document'
import rehypeFormat from 'rehype-format'
import rehypeStringify from 'rehype-stringify'
import remarkParse from 'remark-parse'
import remarkRehype from 'remark-rehype'
import {unified} from 'unified'
import {reporter} from 'vfile-reporter'

const file = await unified()
  .use(remarkParse)
  .use(remarkRehype)
  .use(rehypeDocument, {title: '👋🌍'})
  .use(rehypeFormat)
  .use(rehypeStringify)
  .process('# Hello world!')

console.error(reporter(file))
console.log(String(file))
```

Yields:

```txt
no issues found
```

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>👋🌍</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
  </head>
  <body>
    <h1>Hello world!</h1>
  </body>
</html>
```

<!-- Old name: -->

<a name="description"></a>

## Overview

`unified` is an interface for processing content with syntax trees.
Syntax trees are a representation of content understandable to programs.
Those programs, called *[plugins][api-plugin]*, take these trees and inspect and
modify them.
To get to the syntax tree from text, there is a *[parser][api-parser]*.
To get from that back to text, there is a *[compiler][api-compiler]*.
This is the *[process][api-process]* of a *processor*.

```ascii
| ........................ process ........................... |
| .......... parse ... | ... run ... | ... stringify ..........|

          +--------+                     +----------+
Input ->- | Parser | ->- Syntax Tree ->- | Compiler | ->- Output
          +--------+          |          +----------+
                              X
                              |
                       +--------------+
                       | Transformers |
                       +--------------+
```

###### Processors

Processors process content.
On its own, `unified` (the root processor) doesn’t work.
It needs to be configured with plugins to work.
For example:

```js
const processor = unified()
  .use(remarkParse)
  .use(remarkRehype)
  .use(rehypeDocument, {title: '👋🌍'})
  .use(rehypeFormat)
  .use(rehypeStringify)
```

That processor can do different things.
It can:

* …parse markdown (`parse`)
* …turn parsed markdown into HTML and format the HTML (`run`)
* …compile HTML (`stringify`)
* …do all of the above (`process`)

Every processor implements another processor.
To create a processor, call another processor.
The new processor is configured to work the same as its ancestor.
But when the descendant processor is configured in the future it does not affect
the ancestral processor.

When processors are exposed from a module (for example, `unified` itself) they
should not be configured directly, as that would change their behavior for all
module users.
Those processors are *[frozen][api-freeze]* and they should be called to create
a new processor before they are used.

###### File

When processing a document, metadata is gathered about that document.
[`vfile`][vfile] is the file format that stores data, metadata, and messages
about files for unified and plugins.

There are several [utilities][vfile-utilities] for working with these files.

###### Syntax tree

The syntax trees used in unified are [unist][] nodes.
A tree represents a whole document and each [node][] is a plain JavaScript
object with a `type` field.
The semantics of nodes and the format of syntax trees is defined by other
projects:

* [esast][] — JavaScript
* [hast][] — HTML
* [mdast][] — markdown
* [nlcst][] — natural language
* [xast][] — XML

There are many utilities for working with trees listed in each aforementioned
project and maintained in the [`syntax-tree`][syntax-tree] organization.
These utilities are a level lower than unified itself and are building blocks
that can be used to make plugins.

<!-- Old name: -->

<a name="list-of-processors"></a>

###### Ecosystems

Around each syntax tree is an ecosystem that focusses on that particular kind
of content.
At their core, they parse text to a tree and compile that tree back to text.
They also provide plugins that work with the syntax tree, without requiring
that the end user has knowledge about that tree.

* [rehype][] (hast) — HTML
* [remark][] (mdast) — markdown
* [retext][] (nlcst) — natural language

<a name="list-of-plugins"></a>

###### Plugins

Each aforementioned ecosystem comes with a large set of plugins that you can
pick and choose from to do all kinds of things.

* [List of remark plugins][remark-plugins] ·
  [`remarkjs/awesome-remark`][awesome-remark] ·
  [`remark-plugin` topic][topic-remark-plugin]
* [List of rehype plugins][rehype-plugins] ·
  [`rehypejs/awesome-rehype`][awesome-rehype] ·
  [`rehype-plugin` topic][topic-rehype-plugin]
* [List of retext plugins][retext-plugins] ·
  [`retextjs/awesome-retext`][awesome-retext] ·
  [`retext-plugin` topic][topic-retext-plugin]

There are also a few plugins that work in any ecosystem:

* [`unified-diff`](https://github.com/unifiedjs/unified-diff)
  — ignore unrelated messages in GitHub Actions and Travis
* [`unified-infer-git-meta`](https://github.com/unifiedjs/unified-infer-git-meta)
  — infer metadata of a document from Git
* [`unified-message-control`](https://github.com/unifiedjs/unified-message-control)
  — enable, disable, and ignore messages from content

###### Configuration

Processors are configured with [plugins][api-plugin] or with the
[`data`][api-data] method.
Most plugins also accept configuration through options.
See each plugin’s readme for more info.

###### Integrations

unified can integrate with the file system through
[`unified-engine`][unified-engine].
CLI apps can be created with [`unified-args`][unified-args], Gulp plugins with
[`unified-engine-gulp`][unified-engine-gulp], and language servers with
[`unified-language-server`][unified-language-server].
A streaming interface can be created with [`unified-stream`][unified-stream].

###### Programming interface

The [API][] provided by `unified` allows multiple files to be processed and
gives access to metadata (such as lint messages):

```js
import rehypeStringify from 'rehype-stringify'
import remarkParse from 'remark-parse'
import remarkPresetLintMarkdownStyleGuide from 'remark-preset-lint-markdown-style-guide'
import remarkRehype from 'remark-rehype'
import remarkRetext from 'remark-retext'
import retextEnglish from 'retext-english'
import retextEquality from 'retext-equality'
import {unified} from 'unified'
import {reporter} from 'vfile-reporter'

const file = await unified()
  .use(remarkParse)
  .use(remarkPresetLintMarkdownStyleGuide)
  .use(remarkRetext, unified().use(retextEnglish).use(retextEquality))
  .use(remarkRehype)
  .use(rehypeStringify)
  .process('*Emphasis* and _stress_, you guys!')

console.error(reporter(file))
console.log(String(file))
```

Yields:

```txt
1:16-1:24 warning Emphasis should use `*` as a marker                                 emphasis-marker remark-lint
1:30-1:34 warning `guys` may be insensitive, use `people`, `persons`, `folks` instead gals-man        retext-equality

⚠ 2 warnings
```

```html
<p><em>Emphasis</em> and <em>stress</em>, you guys!</p>
```

<!-- Old name: -->

<a name="processing-between-syntaxes"></a>

###### Transforming between ecosystems

Ecosystems can be combined in two modes.

**Bridge** mode transforms the tree from one format (*origin*) to another
(*destination*).
A different processor runs on the destination tree.
Afterwards, the original processor continues with the origin tree.

**Mutate** mode also transforms the syntax tree from one format to another.
But the original processor continues transforming the destination tree.

In the previous example (“Programming interface”), `remark-retext` is used in
bridge mode: the origin syntax tree is kept after retext is done; whereas
`remark-rehype` is used in mutate mode: it sets a new syntax tree and discards
the origin tree.

The following plugins lets you combine ecosystems:

* [`remark-retext`][remark-retext] — turn markdown into natural language
* [`remark-rehype`][remark-rehype] — turn markdown into HTML
* [`rehype-retext`][rehype-retext] — turn HTML into natural language
* [`rehype-remark`][rehype-remark] — turn HTML into markdown

## API

This package exports the identifier `unified` (the root `processor`).
There is no default export.

### `processor()`

Create a new processor.

###### Returns

New *[unfrozen][api-freeze]* processor ([`processor`][api-processor]).

This processor is configured to work the same as its ancestor.
When the descendant processor is configured in the future it does not affect
the ancestral processor.

###### Example

This example shows how a new processor can be created (from `remark`) and linked
to **stdin**(4) and **stdout**(4).

```js
import process from 'node:process'
import concatStream from 'concat-stream'
import {remark} from 'remark'

process.stdin.pipe(
  concatStream(function (buf) {
    process.stdout.write(String(remark().processSync(buf)))
  })
)
```

### `processor.compiler`

Compiler to use ([`Compiler`][api-compiler], optional).

### `processor.data([key[, value]])`

Configure the processor with info available to all plugins.
Information is stored in an object.

Typically, options can be given to a specific plugin, but sometimes it makes
sense to have information shared with several plugins.
For example, a list of HTML elements that are self-closing, which is needed
during all [phases][overview].

> 👉 **Note**: setting information cannot occur on *[frozen][api-freeze]*
> processors.
> Call the processor first to create a new unfrozen processor.

> 👉 **Note**: to register custom data in TypeScript, augment the
> [`Data`][api-data] interface.

###### Signatures

* `processor = processor.data(key, value)`
* `processor = processor.data(dataset)`
* `value = processor.data(key)`
* `dataset = processor.data()`

###### Parameters

* `key` ([`keyof Data`][api-data], optional) — field to get
* `value` ([`Data[key]`][api-data]) — value to set
* `values` ([`Data`][api-data]) — values to set

###### Returns

The current processor when setting ([`processor`][api-processor]), the value at
`key` when getting ([`Data[key]`][api-data]), or the entire dataset when
getting without key ([`Data`][api-data]).

###### Example

This example show how to get and set info:

```js
import {unified} from 'unified'

const processor = unified().data('alpha', 'bravo')

processor.data('alpha') // => 'bravo'

processor.data() // => {alpha: 'bravo'}

processor.data({charlie: 'delta'})

processor.data() // => {charlie: 'delta'}
```

### `processor.freeze()`

Freeze a processor.

Frozen processors are meant to be extended and not to be configured directly.

When a processor is frozen it cannot be unfrozen.
New processors working the same way can be created by calling the processor.

It’s possible to freeze processors explicitly by calling `.freeze()`.
Processors freeze automatically when `.parse()`, `.run()`, `.runSync()`,
`.stringify()`, `.process()`, or `.processSync()` are called.

###### Returns

The current processor ([`processor`][api-processor]).

###### Example

This example, `index.js`, shows how `rehype` prevents extensions to itself:

```js
import rehypeParse from 'rehype-parse'
import rehypeStringify from 'rehype-stringify'
import {unified} from 'unified'

export const rehype = unified().use(rehypeParse).use(rehypeStringify).freeze()
```

That processor can be used and configured like so:

```js
import {rehype} from 'rehype'
import rehypeFormat from 'rehype-format'
// …

rehype()
  .use(rehypeFormat)
  // …
```

A similar looking example is broken as operates on the frozen interface.
If this behavior was allowed it would result in unexpected behavior so an error
is thrown.
**This is not valid**:

```js
import {rehype} from 'rehype'
import rehypeFormat from 'rehype-format'
// …

rehype
  .use(rehypeFormat)
  // …
```

Yields:

```txt
~/node_modules/unified/index.js:426
    throw new Error(
    ^

Error: Cannot call `use` on a frozen processor.
Create a new processor first, by calling it: use `processor()` instead of `processor`.
    at assertUnfrozen (~/node_modules/unified/index.js:426:11)
    at Function.use (~/node_modules/unified/index.js:165:5)
    …
```

### `processor.parse(file)`

Parse text to a syntax tree.

> 👉 **Note**: `parse` freezes the processor if not already
> *[frozen][api-freeze]*.

> 👉 **Note**: `parse` performs the [parse phase][overview], not the run phase
> or other phases.

###### Parameters

* `file` ([`Compatible`][vfile-compatible]) — file to parse; typically
  `string` or [`VFile`][vfile]; any value accepted as `x` in `new VFile(x)`

###### Returns

Syntax tree representing `file` ([`Node`][node]).

###### Example

This example shows how `parse` can be used to create a tree from a file.

```js
import remarkParse from 'remark-parse'
import {unified} from 'unified'

const tree = unified().use(remarkParse).parse('# Hello world!')

console.log(tree)
```

Yields:

```js
{
  type: 'root',
  children: [
    {type: 'heading', depth: 1, children: [Array], position: [Object]}
  ],
  position: {
    start: {line: 1, column: 1, offset: 0},
    end: {line: 1, column: 15, offset: 14}
  }
}
```

### `processor.parser`

Parser to use ([`Parser`][api-parser], optional).

### `processor.process(file[, done])`

Process the given file as configured on the processor.

> 👉 **Note**: `process` freezes the processor if not already
> *[frozen][api-freeze]*.

> 👉 **Note**: `process` performs the [parse, run, and stringify
> phases][overview].

###### Signatures

* `processor.process(file, done)`
* `Promise<VFile> = processor.process(file?)`

###### Parameters

* `file` ([`Compatible`][vfile-compatible], optional) — file; typically
  `string` or [`VFile`][vfile]; any value accepted as `x` in `new VFile(x)`
* `done` ([`ProcessCallback`][api-process-callback], optional) — callback

###### Returns

Nothing if `done` is given (`undefined`).
Otherwise a promise, rejected with a fatal error or resolved with the
processed file ([`Promise<VFile>`][vfile]).

The parsed, transformed, and compiled value is available at `file.value` (see
note).

> 👉 **Note**: unified typically compiles by serializing: most
> compilers return `string` (or `Uint8Array`).
> Some compilers, such as the one configured with
> [`rehype-react`][rehype-react], return other values (in this case, a React
> tree).
> If you’re using a compiler that doesn’t serialize, expect different result
> values.
>
> To register custom results in TypeScript, add them to
> [`CompileResultMap`][api-compile-result-map].

###### Example

This example shows how `process` can be used to process a file:

```js
import rehypeDocument from 'rehype-document'
import rehypeFormat from 'rehype-format'
import rehypeStringify from 'rehype-stringify'
import remarkParse from 'remark-parse'
import remarkRehype from 'remark-rehype'
import {unified} from 'unified'

const file = await unified()
  .use(remarkParse)
  .use(remarkRehype)
  .use(rehypeDocument, {title: '👋🌍'})
  .use(rehypeFormat)
  .use(rehypeStringify)
  .process('# Hello world!')

console.log(String(file))
```

Yields:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>👋🌍</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
  </head>
  <body>
    <h1>Hello world!</h1>
  </body>
</html>
```

### `processor.processSync(file)`

Process the given file as configured on the processor.

An error is thrown if asynchronous transforms are configured.

> 👉 **Note**: `processSync` freezes the processor if not already
> *[frozen][api-freeze]*.

> 👉 **Note**: `processSync` performs the [parse, run, and stringify
> phases][overview].

###### Parameters

* `file` ([`Compatible`][vfile-compatible], optional) — file; typically
  `string` or [`VFile`][vfile]; any value accepted as `x` in `new VFile(x)`

###### Returns

The processed file ([`VFile`][vfile]).

The parsed, transformed, and compiled value is available at `file.value` (see
note).

> 👉 **Note**: unified typically compiles by serializing: most
> compilers return `string` (or `Uint8Array`).
> Some compilers, such as the one configured with
> [`rehype-react`][rehype-react], return other values (in this case, a React
> tree).
> If you’re using a compiler that doesn’t serialize, expect different result
> values.
>
> To register custom results in TypeScript, add them to
> [`CompileResultMap`][api-compile-result-map].

###### Example

This example shows how `processSync` can be used to process a file, if all
transformers are synchronous.

```js
import rehypeDocument from 'rehype-document'
import rehypeFormat from 'rehype-format'
import rehypeStringify from 'rehype-stringify'
import remarkParse from 'remark-parse'
import remarkRehype from 'remark-rehype'
import {unified} from 'unified'

const processor = unified()
  .use(remarkParse)
  .use(remarkRehype)
  .use(rehypeDocument, {title: '👋🌍'})
  .use(rehypeFormat)
  .use(rehypeStringify)

console.log(String(processor.processSync('# Hello world!')))
```

Yields:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>👋🌍</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
  </head>
  <body>
    <h1>Hello world!</h1>
  </body>
</html>
```

### `processor.run(tree[, file][, done])`

Run *[transformers][api-transformer]* on a syntax tree.

> 👉 **Note**: `run` freezes the processor if not already
> *[frozen][api-freeze]*.

> 👉 **Note**: `run` performs the [run phase][overview], not other phases.

###### Signatures

* `processor.run(tree, done)`
* `processor.run(tree, file, done)`
* `Promise<Node> = processor.run(tree, file?)`

###### Parameters

* `tree` ([`Node`][node]) — tree to transform and inspect
* `file` ([`Compatible`][vfile-compatible], optional) — file associated
  with `node`; any value accepted as `x` in `new VFile(x)`
* `done` ([`RunCallback`][api-run-callback], optional) — callback

###### Returns

Nothing if `done` is given (`undefined`).
Otherwise, a promise rejected with a fatal error or resolved with the
transformed tree ([`Promise<Node>`][node]).

###### Example

#### Resolver Options

| Field                    | Default                     | Description                                                                                                                                                                                                                                                                                 |
| ------------------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| alias                    | []                          | A list of module alias configurations or an object which maps key to value                                                                                                                                                                                                                  |
| aliasFields              | []                          | A list of alias fields in description files                                                                                                                                                                                                                                                 |
| extensionAlias           | {}                          | An object which maps extension to extension aliases                                                                                                                                                                                                                                         |
| extensionAliasForExports | false                       | Also apply `extensionAlias` to paths resolved through the package.json `exports` field. Off by default (Node.js-aligned)                                                                                                                                                                    |
| cachePredicate           | function() { return true }; | A function which decides whether a request should be cached or not. An object is passed to the function with `path` and `request` properties.                                                                                                                                               |
| cacheWithContext         | true                        | If unsafe cache is enabled, includes `request.context` in the cache key                                                                                                                                                                                                                     |
| conditionNames           | []                          | A list of exports field condition names                                                                                                                                                                                                                                                     |
| descriptionFiles         | ["package.json"]            | A list of description files to read from                                                                                                                                                                                                                                                    |
| enforceExtension         | false                       | Enforce that a extension from extensions must be used                                                                                                                                                                                                                                       |
| exportsFields            | ["exports"]                 | A list of exports fields in description files                                                                                                                                                                                                                                               |
| extensions               | [".js", ".json", ".node"]   | A list of extensions which should be tried for files                                                                                                                                                                                                                                        |
| fallback                 | []                          | Same as `alias`, but only used if default resolving fails                                                                                                                                                                                                                                   |
| fileSystem               |                             | The file system which should be used                                                                                                                                                                                                                                                        |
| fullySpecified           | false                       | Request passed to resolve is already fully specified and extensions or main files are not resolved for it (they are still resolved for internal requests)                                                                                                                                   |
| mainFields               | ["main"]                    | A list of main fields in description files                                                                                                                                                                                                                                                  |
| mainFiles                | ["index"]                   | A list of main files in directories                                                                                                                                                                                                                                                         |
| modules                  | ["node_modules"]            | A list of directories to resolve modules from, can be absolute path or folder name                                                                                                                                                                                                          |
| plugins                  | []                          | A list of additional resolve plugins which should be applied                                                                                                                                                                                                                                |
| resolver                 | undefined                   | A prepared Resolver to which the plugins are attached                                                                                                                                                                                                                                       |
| resolveToContext         | false                       | Resolve to a context instead of a file                                                                                                                                                                                                                                                      |
| preferRelative           | false                       | Prefer to resolve module requests as relative request and fallback to resolving as module                                                                                                                                                                                                   |
| preferAbsolute           | false                       | Prefer to resolve server-relative urls as absolute paths before falling back to resolve in roots                                                                                                                                                                                            |
| restrictions             | []                          | A list of resolve restrictions                                                                                                                                                                                                                                                              |
| roots                    | []                          | A list of root paths                                                                                                                                                                                                                                                                        |
| symlinks                 | true                        | Whether to resolve symlinks to their symlinked location                                                                                                                                                                                                                                     |
| tsconfig                 | false                       | TypeScript config for paths mapping. Can be `false` (disabled), `true` (use default `tsconfig.json`), a string path to `tsconfig.json`, or an object with `configFile`, `references`, and `baseUrl` options. Supports JSONC format (comments and trailing commas) like TypeScript compiler. |
| tsconfig.configFile      | tsconfig.json               | Path to the tsconfig.json file                                                                                                                                                                                                                                                              |
| tsconfig.references      | []                          | Project references. `'auto'` to load from tsconfig, or an array of paths to referenced projects                                                                                                                                                                                             |
| tsconfig.baseUrl         | undefined                   | Override baseUrl from tsconfig.json. If provided, this value will be used instead of the baseUrl in the tsconfig file                                                                                                                                                                       |
| unsafeCache              | false                       | Use this cache object to unsafely cache the successful requests                                                                                                                                                                                                                             |

End of benchmark reply.
