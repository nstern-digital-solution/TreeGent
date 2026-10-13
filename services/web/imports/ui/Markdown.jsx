import React from 'react';
import { parseBlocks } from './markdown.js';

// Renders chat text as safe-subset markdown (issue #46).
//
// React escapes all string children — that IS the sanitization. The parser
// (markdown.js) can only emit the fixed vocabulary; there is no path from a
// message to an attribute other than the scheme-filtered href. No
// dangerouslySetInnerHTML anywhere.

function Inline({ nodes }) {
  return (nodes || []).map((n, i) => {
    if (typeof n === 'string') return n;
    const kids = <Inline nodes={n.children} />;
    switch (n.tag) {
      case 'code': return <code key={i}>{kids}</code>;
      case 'b':    return <strong key={i}>{kids}</strong>;
      case 'i':    return <em key={i}>{kids}</em>;
      case 'a':    return <a key={i} href={n.attrs.href}
                             rel={n.attrs.rel} target={n.attrs.target}>{kids}</a>;
      case 'br':   return <br key={i} />;
      default:     return null; // unreachable for parser output: fail closed
    }
  });
}

function Block({ b, pk }) {
  switch (b.tag) {
    case 'hr':
      return <hr key={pk} />;
    case 'pre':
      return <pre key={pk} className="md-pre"><code>{(b.children[0].children || []).join('')}</code></pre>;
    case 'blockquote':
      return <blockquote key={pk} className="md-quote">{b.children.map((c, i) => <Block b={c} pk={i} key={i} />)}</blockquote>;
    case 'ul':
      return <ul key={pk} className="md-ul">{b.children.map((li, i) => <li key={i}><Inline nodes={li.children} /></li>)}</ul>;
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': {
      const Tag = b.tag;
      return <Tag key={pk} className="md-h"><Inline nodes={b.children} /></Tag>;
    }
    default:
      return <p key={pk} className="md-p"><Inline nodes={b.children} /></p>;
  }
}

function MarkdownBase({ text }) {
  const blocks = parseBlocks(text == null ? '' : String(text));
  return <>{blocks.map((b, i) => <Block b={b} pk={i} key={i} />)}</>;
}

// memo: chat lists re-render on every new message; identical text never
// re-parses (review nit: parse cost is O(message), memo is cheap insurance).
export const Markdown = React.memo(MarkdownBase);
