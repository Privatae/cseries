// Every text a literal expression in TypeScript / TSX / JavaScript can produce,
// for scripts/test_platform_texts_ask_nothing.py (the React apps and the
// system apps' own JS are held to the same rule as the Python texts).
//
// Reads JSON on stdin: {"ts": "<path to typescript.js>", "files": [{"path", "source"}]}
// Writes JSON: [{"path", "items": [[line, text]], "unread": [[line, text]]}]
//
// The same composition as the Python side: a `+` chain is joined, BOTH
// branches of a conditional are taken, a template literal keeps its literal
// parts with its holes as X, `[...].join(sep)` is joined, and a JSX element's
// children are read as one sentence — an expression in it is X, an inline tag
// (<b>, <code>, <a> …) is read through, a block tag breaks the sentence and is
// read on its own. Left out: module specifiers, literal types, console calls.
// A literal no text read is reported as unread, so none can hide.
"use strict";
const fs = require("fs");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const ts = require(input.ts);
const CAP = 32;
const INLINE = new Set(["b", "i", "em", "strong", "code", "span", "a", "kbd", "small", "mark", "u", "s"]);
const K = ts.SyntaxKind;

function product(a, b) {
  const out = [];
  for (const x of a) {
    for (const y of b) {
      out.push(x + y);
      if (out.length >= CAP) return out;
    }
  }
  return out;
}

function tagName(el) {
  const open = el.kind === K.JsxElement ? el.openingElement : el;
  return open.tagName ? open.tagName.getText() : "";
}

function jsxChildren(el) {
  // [alternatives] for an element's or fragment's children, or null when
  // nothing in them is text.
  let parts = [""];
  let any = false;
  for (const c of el.children) {
    if (c.kind === K.JsxText) {
      if (c.containsOnlyTriviaWhiteSpaces) continue;
      parts = product(parts, [c.text.replace(/\s+/g, " ")]);
      any = true;
    } else if (c.kind === K.JsxExpression) {
      if (!c.expression) continue;
      const a = alts(c.expression);
      if (a) any = true;
      parts = product(parts, a || ["X"]);
    } else if ((c.kind === K.JsxElement || c.kind === K.JsxSelfClosingElement)
               && INLINE.has(tagName(c))) {
      const a = c.kind === K.JsxElement ? jsxChildren(c) : null;
      if (a) any = true;
      parts = product(parts, a || ["X"]);
    } else {
      parts = product(parts, ["\n"]);      // a block element: the sentence breaks
    }
  }
  return any ? parts.map((p) => p.replace(/[ \t]+/g, " ").trim()) : null;
}

function alts(n) {
  switch (n.kind) {
    case K.StringLiteral:
    case K.NoSubstitutionTemplateLiteral:
      return [n.text];
    case K.TemplateExpression:
      return [n.head.text + n.templateSpans.map((s) => "X" + s.literal.text).join("")];
    case K.ParenthesizedExpression:
    case K.AsExpression:
    case K.NonNullExpression:
    case K.TypeAssertionExpression:
      return alts(n.expression);
    case K.BinaryExpression: {
      if (n.operatorToken.kind !== K.PlusToken) return null;
      const a = alts(n.left), b = alts(n.right);
      if (!a && !b) return null;
      return product(a || ["X"], b || ["X"]);
    }
    case K.ConditionalExpression: {
      const a = alts(n.whenTrue), b = alts(n.whenFalse);
      if (!a && !b) return null;
      return (a || ["X"]).concat(b || ["X"]).slice(0, CAP);
    }
    case K.CallExpression: {
      const f = n.expression;
      if (f.kind === K.PropertyAccessExpression && f.name.text === "join"
          && f.expression.kind === K.ArrayLiteralExpression) {
        const elems = f.expression.elements.map((e) => alts(e));
        if (!elems.some(Boolean)) return null;
        const sep = n.arguments.length ? (alts(n.arguments[0]) || ["X"])[0] : ",";
        let parts = [""];
        elems.forEach((e, i) => {
          parts = product(parts, (e || ["X"]).map((x) => (i ? sep : "") + x));
        });
        return parts;
      }
      return null;
    }
    case K.JsxElement:
    case K.JsxFragment:
      return jsxChildren(n);
    default:
      return null;
  }
}

function folded(n) {
  // The nodes whose words alts(n) folded in.
  const out = [n];
  const add = (c) => { if (c && alts(c) !== null) out.push(...folded(c)); };
  switch (n.kind) {
    case K.ParenthesizedExpression:
    case K.AsExpression:
    case K.NonNullExpression:
    case K.TypeAssertionExpression:
      add(n.expression);
      break;
    case K.BinaryExpression:
      add(n.left); add(n.right);
      break;
    case K.ConditionalExpression:
      add(n.whenTrue); add(n.whenFalse);
      break;
    case K.CallExpression:
      n.expression.expression.elements.forEach(add);
      if (n.arguments.length) add(n.arguments[0]);
      break;
    case K.JsxElement:
    case K.JsxFragment:
      for (const c of n.children) {
        if (c.kind === K.JsxText) out.push(c);
        else if (c.kind === K.JsxExpression && c.expression) add(c.expression);
        else if (c.kind === K.JsxElement && INLINE.has(tagName(c))) add(c);
      }
      break;
    default:
      break;
  }
  return out;
}

const LITERAL = new Set([K.StringLiteral, K.NoSubstitutionTemplateLiteral, K.TemplateExpression]);

function skipped(n) {
  const p = n.parent;
  if (!p) return false;
  if ((p.kind === K.ImportDeclaration || p.kind === K.ExportDeclaration) && p.moduleSpecifier === n) return true;
  if (p.kind === K.ExternalModuleReference || p.kind === K.LiteralType || p.kind === K.ImportType) return true;
  if (p.kind === K.CallExpression && p.arguments.includes(n)) {
    const f = p.expression;
    if (f.kind === K.ImportKeyword) return true;
    if (f.kind === K.Identifier && f.text === "require") return true;
    if (f.kind === K.PropertyAccessExpression && f.expression.kind === K.Identifier
        && f.expression.text === "console") return true;
  }
  return false;
}

const result = [];
for (const file of input.files) {
  const kind = file.path.endsWith(".tsx") ? ts.ScriptKind.TSX
    : file.path.endsWith(".ts") ? ts.ScriptKind.TS
    : ts.ScriptKind.JSX;           // plain JS may carry JSX; JSX mode reads both
  const sf = ts.createSourceFile(file.path, file.source, ts.ScriptTarget.Latest, true, kind);
  const seen = new Set();
  const skip = new Set();
  const items = [];
  const lineOf = (n) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;
  const visit = (n) => {
    if (LITERAL.has(n.kind) && skipped(n)) skip.add(n);
    if (!seen.has(n) && !skip.has(n)) {
      const a = alts(n);
      if (a) {
        for (const c of folded(n)) seen.add(c);
        for (const t of a) items.push([lineOf(n), t]);
      }
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  const unread = [];
  const check = (n) => {
    if ((LITERAL.has(n.kind) || (n.kind === K.JsxText && !n.containsOnlyTriviaWhiteSpaces))
        && !seen.has(n) && !skip.has(n)) {
      unread.push([lineOf(n), n.getText(sf).slice(0, 120)]);
    }
    ts.forEachChild(n, check);
  };
  check(sf);
  result.push({ path: file.path, items, unread });
}
process.stdout.write(JSON.stringify(result));
