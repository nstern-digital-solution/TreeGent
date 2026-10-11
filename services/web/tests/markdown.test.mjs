// node --test services/web/tests/markdown.test.mjs
// Zero-dep tests for the parser's safety contract + subset behavior (issue #46).
// Note: run with the system node; the parser is dependency-free by design.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { inlineNodes, parseBlocks } from '../imports/ui/markdown.js';

const VOCAB = new Set(['p','pre','code','b','i','a','br','h1','h2','h3','h4','h5','h6','ul','li','blockquote','hr']);
const walk = (nodes, at) => {
  for (const n of nodes) {
    if (typeof n === 'string') continue;
    assert.ok(VOCAB.has(n.tag), `tag outside vocabulary: ${n.tag}`);
    if (n.attrs) {
      assert.deepEqual(Object.keys(n.attrs).sort(), ['href','rel','target']);
      if (at !== 'skip-href') assert.match(n.attrs.href, /^(https?:|mailto:)/i, `unfiltered href: ${n.attrs.href}`);
      assert.match(n.attrs.rel, /^noopener/);
    }
    walk(n.children || [], 'skip-href');
  }
};
const allBlocks = (src) => { walk(parseBlocks(src)); return parseBlocks(src); };

test('safety: html never becomes markup', () => {
  for (const src of ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
                     '<a href="javascript:alert(1)">click</a>', '<<<>>>'])
    assert.ok(JSON.stringify(allBlocks(src)).includes('<'), `${src}: raw text kept, no html nodes emitted`);
});
test('safety: javascript:/data: hrefs demoted to literal text', () => {
  const j = JSON.stringify(allBlocks('[x](javascript:alert(1))'));
  assert.ok(!j.includes('javascript') || !j.includes('"href"'), 'no anchor node for bad scheme');
  assert.ok(!JSON.stringify(allBlocks('[x](data:text/html,<b>)')).includes('"href"'));
});
test('links: good schemes only, rel hardened', () => {
  const out = JSON.stringify(allBlocks('[ok](https://a.test/x) [no](http://b.test) [m](mailto:a@b.c)'));
  assert.equal((out.match(/"href"/g) || []).length, 3);
  assert.ok(out.includes('noopener'));
});
test('autolink bare urls, trailing period not eaten', () => {
  const b = parseBlocks('see https://example.com/a_b. now')[0];
  const a = b.children.find((n) => n.tag === 'a');
  assert.equal(a.attrs.href, 'https://example.com/a_b');
  assert.equal(b.children[b.children.length - 1], '. now');
});
test('emphasis: **b** and *i*, unbalanced stays text', () => {
  const kids = inlineNodes('**Landed on main** in the *reviewed order** x');
  assert.equal(kids[0].tag, 'b');
  assert.equal(kids[2].tag, 'i');
  assert.ok(inlineNodes('a ** b').every((n) => typeof n === 'string'));
});
test('code: inline terminal; fenced multi-line; unclosed fence ok', () => {
  assert.deepEqual(inlineNodes('use `git **status**` now')[1],
    { tag: 'code', children: ['git **status**'] });
  const p = parseBlocks('```js\nlet x = 1;\nlet y = 2;\n```')[0];
  assert.equal(p.children[0].children[0], 'let x = 1;\nlet y = 2;');
  assert.equal(parseBlocks('```\na\n')[0].children[0].children[0], 'a');
});
test('blocks: headings clamped, lists merged, quotes merged, hr', () => {
  assert.equal(parseBlocks('####### seven')[0].tag, 'p');
  assert.equal(parseBlocks('## Title ##')[0].tag, 'h2');
  assert.equal(parseBlocks('## Title')[0].children[0], 'Title');
  const ul = parseBlocks('- a\n- b\n\nnext')[0];
  assert.equal(ul.tag, 'ul');
  assert.equal(ul.children.length, 2);
  assert.equal(ul.children[0].tag, 'li');
  const q = parseBlocks('> one\n> two')[0];
  assert.equal(q.tag, 'blockquote');
  assert.ok(JSON.stringify(q).includes('one two'));
  assert.equal(parseBlocks('---')[0].tag, 'hr');
});
test('paragraph line breaks become br nodes', () => {
  const p = parseBlocks('l1\nl2');
  assert.equal(p.length, 1);
  assert.ok(p[0].children.some((n) => typeof n === 'object' && n.tag === 'br'));
});
test('CRLF normalized', () => {
  const p = parseBlocks('a\r\nb');
  assert.ok(JSON.stringify(p).includes('"br"'));
});
