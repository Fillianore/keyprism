#!/usr/bin/env node
/** i18n key-completeness guard (Phase 3.6 D1 regression guard).
 *
 *  Scans every `t('key')` / `t("key")` usage and every
 *  `t(`prefix${…}`)` template usage under frontend/src, plus every
 *  data-i18n / data-i18n-title / data-i18n-aria attribute in
 *  frontend/index.html, and FAILS when:
 *    - a used key is missing from the EN dict, or
 *    - a used key is missing from the ZH dict, or
 *    - a template prefix has no matching key in one of the dicts, or
 *    - the two dicts do not define the exact same key set.
 *
 *  Wired as `npm run check:i18n` and enforced in the CI frontend job.
 *  The dicts are imported directly from src/i18n.js — a single source
 *  of truth, no duplicated parsing.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';
import { dict } from '../src/i18n.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = join(root, 'src');
const langs = Object.keys(dict);

// ---- 1. dict parity: en and zh must define the same key set ----------
const keySets = Object.fromEntries(
  langs.map((l) => [l, new Set(Object.keys(dict[l]))])
);
const problems = [];
for (const l of langs) {
  for (const k of keySets[l]) {
    for (const other of langs) {
      if (other !== l && !keySets[other].has(k)) {
        problems.push(`dict "${l}" has key "${k}" missing from "${other}"`);
      }
    }
  }
}

// ---- 2. collect every used key ----------------------------------------
function walk(dir, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (p.endsWith('.js')) out.push(p);
  }
  return out;
}

const used = new Map(); // key or "prefix:p" -> [location]
const note = (key, file, line) => {
  if (!key) return;
  if (!used.has(key)) used.set(key, []);
  used.get(key).push(`${relative(root, file)}:${line}`);
};

/** Blank comments so a doc comment mentioning t('…') is not mistaken
 *  for a usage (same naive approach as the pytest source contracts). */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:'"\\])\/\/[^\n]*/g, '$1');
}

for (const f of walk(srcDir)) {
  stripComments(readFileSync(f, 'utf8')).split('\n').forEach((line, i) => {
    for (const m of line.matchAll(/\bt\(\s*'([^']+)'/g)) note(m[1], f, i + 1);
    for (const m of line.matchAll(/\bt\(\s*"([^"]+)"/g)) note(m[1], f, i + 1);
    // template keys: t(`prefix${expr}`) — remember the static prefix
    for (const m of line.matchAll(/\bt\(\s*`([^`$]*)\$\{/g)) {
      note(`prefix:${m[1]}`, f, i + 1);
    }
  });
}

const html = readFileSync(join(root, 'index.html'), 'utf8');
for (const m of html.matchAll(/data-i18n(?:-title|-aria)?="([^"]+)"/g)) {
  note(m[1], join(root, 'index.html'), 1);
}

// ---- 3. verify every used key against BOTH dicts ----------------------
for (const [key, where] of used) {
  if (key.startsWith('prefix:')) {
    const p = key.slice('prefix:'.length);
    for (const l of langs) {
      const hit = [...keySets[l]].some((k) => k.startsWith(p));
      if (!hit) {
        problems.push(
          `no "${l}" key matches pattern \`${p}\${…}\` (used at ${where.join(', ')})`
        );
      }
    }
  } else {
    for (const l of langs) {
      if (!keySets[l].has(key)) {
        problems.push(
          `missing "${l}" key "${key}" (used at ${where.join(', ')})`
        );
      }
    }
  }
}

if (problems.length) {
  console.error('check:i18n FAILED:');
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(
  `check:i18n OK: ${used.size} used keys, ` +
    `${keySets.en.size} en / ${keySets.zh.size} zh dict keys, dicts in sync`
);
