#!/usr/bin/env node
'use strict';

const major = Number(process.versions.node.split('.')[0]);
if (major < 18) {
  process.stderr.write(`cseries needs Node.js 18 or newer, and this is ${process.versions.node}.\n` +
    'Install a current Node.js from https://nodejs.org and run npx cseries again.\n');
  process.exit(1);
}

require('../src/cli').main(process.argv.slice(2)).then((code) => {
  process.exitCode = code;
});
