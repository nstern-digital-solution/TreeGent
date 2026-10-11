// Safe-subset markdown parser for web chat (issue #46).
//
// Why: chat renders plain text, but real traffic (GitHub bug digests, PR
// land notes) is heavily markdown and shows up as literal ** ## ` walls.
//
// Design constraints (deliberate, do not regress):
//  * NO new npm dependencies, NO dangerouslySetInnerHTML. This package has
//    no CI/build of its own; a vendored dependency-free renderer is one
//    screen to audit and cannot be supply-chained.
//  * XSS-safety is STRUCTURAL, not filter-based: the parser emits only the
//    fixed vocabulary below and NEVER produces raw html nodes. The React
//    mapper escapes every string child; the only attribute the parser can
//    produce is href, scheme-filtered to http(s)/mailto. Vocabulary:
//    strings plus {tag: p|pre|code|b|i|a|br|h1..h6|ul|li|blockquote|hr}.
//  * Supported subset (chat-sized, not a CommonMark claim): fenced ```code,
//    `inline`, **bold**, *italic*, [text](url) + bare http(s) autolinks,
//    ATX headings #..######, "- " bullets (one level), "> " quotes, ---
//    rules, plain line breaks. Anything else renders as text: a miss is
//    cosmetic, never a hole.

const HREF_OK = /^(https?:|mailto:)/i;

export function inlineNodes(text) {
  // Returns an array of nodes: strings, or {tag, attrs?, children}.
  const out = [];
  let i = 0;
  let buf = '';
  const flush = () => { if (buf) { out.push(buf); buf = ''; } };

  while (i < text.length) {
    const c = text[i];

    // [label](url) — scheme-filtered; bad urls stay as literal text.
    if (c === '[') {
      const close = text.indexOf('](', i);
      if (close !== -1) {
        const end = text.indexOf(')', close + 2);
        if (end !== -1) {
          const label = text.slice(i + 1, close);
          const url = text.slice(close + 2, end).trim();
          if (label && HREF_OK.test(url)) {
            flush();
            out.push({ tag: 'a', attrs: { href: url, rel: 'noopener noreferrer nofollow', target: '_blank' }, children: inlineNodes(label) });
            i = end + 1;
            continue;
          }
        }
      }
      buf += c; i += 1; continue;
    }

    // bare http(s) autolink (trailing punctuation trimmed)
    if (c === 'h' || c === 'H') {
      const m = /^(https?:\/\/[^\s<>"')\]]+[^\s<>"'.,;:!?)\]])/i.exec(text.slice(i));
      if (m) {
        flush();
        out.push({ tag: 'a', attrs: { href: m[1], rel: 'noopener noreferrer nofollow', target: '_blank' }, children: [m[1]] });
        i += m[1].length;
        continue;
      }
    }

    // `code` — code content is terminal: no inline parsing inside.
    if (c === '`') {
      const end = text.indexOf('`', i + 1);
      if (end !== -1) {
        flush();
        out.push({ tag: 'code', children: [text.slice(i + 1, end)] });
        i = end + 1;
        continue;
      }
    }

    // **bold**
    if (c === '*' && text[i + 1] === '*') {
      const end = text.indexOf('**', i + 2);
      if (end !== -1) {
        flush();
        out.push({ tag: 'b', children: inlineNodes(text.slice(i + 2, end)) });
        i = end + 2;
        continue;
      }
    }

    // *italic* (single star, non-empty content)
    if (c === '*' && text[i + 1] !== '*') {
      const end = text.indexOf('*', i + 1);
      if (end > i + 1) {
        flush();
        out.push({ tag: 'i', children: inlineNodes(text.slice(i + 1, end)) });
        i = end + 1;
        continue;
      }
    }

    buf += c; i += 1;
  }
  flush();
  return out;
}

export function parseBlocks(src) {
  // Block pass: fenced code, headings, rules, bullets, quotes, paragraphs.
  const lines = String(src).replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (line.trim() === '') { i += 1; continue; }

    // fenced code: ``` ... ``` (unclosed fence runs to end of message)
    if (/^\s*```/.test(line)) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { body.push(lines[i]); i += 1; }
      i += 1; // skip closing fence if present
      while (body.length && body[body.length - 1].trim() === '') body.pop(); // trailing blank lines are not code
      blocks.push({ tag: 'pre', children: [{ tag: 'code', children: [body.join('\n')] }] });
      continue;
    }

    // horizontal rule
    if (/^\s*(---|___|\*\*\*)\s*$/.test(line)) { blocks.push({ tag: 'hr' }); i += 1; continue; }

    // ATX heading
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      blocks.push({ tag: 'h' + h[1].length, children: inlineNodes(h[2].replace(/\s+#+\s*$/, '')) });
      i += 1; continue;
    }

    // blockquote: consecutive '>' lines merge, soft-wrapped with a space
    if (/^\s*>/.test(line)) {
      const inner = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) { inner.push(lines[i].replace(/^\s*>\s?/, '')); i += 1; }
      blocks.push({ tag: 'blockquote', children: [{ tag: 'p', children: inlineNodes(inner.join(' ')) }] });
      continue;
    }

    // unordered list: consecutive bullet lines, single level
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push({ tag: 'li', children: inlineNodes(lines[i].replace(/^\s*[-*+]\s+/, '')) });
        i += 1;
      }
      blocks.push({ tag: 'ul', children: items });
      continue;
    }

    // paragraph: each source '\n' becomes a hard <br> node so line breaks
    // survive React rendering without CSS white-space hacks.
    const para = [];
    while (i < lines.length && lines[i].trim() !== ''
           && !/^\s*(```|>|[-*+]\s|#{1,6}\s|---|___|\*\*\*)/.test(lines[i])) {
      para.push(lines[i]); i += 1;
    }
    const kids = [];
    para.forEach((ln, idx) => {
      if (idx) kids.push({ tag: 'br' });
      kids.push(...inlineNodes(ln));
    });
    blocks.push({ tag: 'p', children: kids });
  }
  return blocks;
}
