// The tool names app-web-kit/ask.ts askFromTool raises a card for, read from
// its syntax tree — for scripts/test_go_ahead_guard.py, which requires the
// Host's go-ahead guard (go_ahead_guard.CARD_TOOLS) to know each one, so a new
// branch in askFromTool cannot go unnoticed.
//
// Reads JSON on stdin: {"ts": "<path to typescript.js>", "source": "<ask.ts>"}
// Writes JSON: {"names": [...], "unread": [...]}. Inside askFromTool, every
// comparison of the tool's name (`t.name` or `opName(t)`) with a string is a
// name, and every `SET.has(t.name | opName(t))` adds the members of SET, a
// module-level `new Set([...strings])`. Any other condition that reads the
// tool's name is listed as unread, and the test fails on it.
"use strict";
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const ts = require(input.ts);
const K = ts.SyntaxKind;
const sf = ts.createSourceFile("ask.ts", input.source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const sets = {};
const names = new Set();
const unread = [];
let fn = null;

function stringsOf(arr) {
  return arr.elements.every((e) => e.kind === K.StringLiteral) ? arr.elements.map((e) => e.text) : null;
}

sf.forEachChild((n) => {
  if (n.kind === K.VariableStatement) {
    for (const d of n.declarationList.declarations) {
      const init = d.initializer;
      if (init && init.kind === K.NewExpression && init.expression.getText(sf) === "Set"
          && init.arguments && init.arguments[0] && init.arguments[0].kind === K.ArrayLiteralExpression) {
        const v = stringsOf(init.arguments[0]);
        if (v) sets[d.name.getText(sf)] = v;
      }
    }
  }
  if (n.kind === K.FunctionDeclaration && n.name && n.name.text === "askFromTool") fn = n;
});

function isToolName(e) {
  const t = e.getText(sf).replace(/\s+/g, "");
  return t === "t.name" || t === "opName(t)";
}

function visit(n) {
  if (n.kind === K.BinaryExpression
      && [K.EqualsEqualsEqualsToken, K.EqualsEqualsToken, K.ExclamationEqualsEqualsToken,
          K.ExclamationEqualsToken].includes(n.operatorToken.kind)) {
    const [a, b] = [n.left, n.right];
    if (isToolName(a) && b.kind === K.StringLiteral) { names.add(b.text); return; }
    if (isToolName(b) && a.kind === K.StringLiteral) { names.add(a.text); return; }
  }
  if (n.kind === K.CallExpression && n.expression.kind === K.PropertyAccessExpression
      && n.expression.name.text === "has" && n.arguments.length === 1 && isToolName(n.arguments[0])) {
    const set = sets[n.expression.expression.getText(sf)];
    if (set) { set.forEach((x) => names.add(x)); return; }
    unread.push(n.getText(sf));
    return;
  }
  if (isToolName(n)) {
    // the tool's name read some other way: not understood, so the test fails
    unread.push(n.parent.getText(sf).slice(0, 120));
    return;
  }
  ts.forEachChild(n, visit);
}

if (!fn) {
  unread.push("no askFromTool function in ask.ts");
} else {
  visit(fn.body);
}
process.stdout.write(JSON.stringify({ names: [...names].sort(), unread }));
