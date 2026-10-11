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

function Block({ b, key }) {
  switch (b.tag) {
    case 'hr':
      return <hr key={key} />;
    case 'pre':
      return <pre key={key} className="md-pre"><code>{(b.children[0].children || []).join('')}</code></pre>;
    case 'blockquote':
      return <blockquote key={key} className="md-quote">{b.children.map((c, i) => <Block b={c} key={i} />)}</blockquote>;
    case 'ul':
      return <ul key={key} className="md-ul">{b.children.map((li, i) => <li key={i}><Inline nodes={li.children} /></li>)}</ul>;
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': {
      const Tag = b.tag;
      return <Tag key={key} className="md-h"><Inline nodes={b.children} /></Tag>;
    }
    default:
      return <p key={key} className="md-p"><Inline nodes={b.children} /></p>;
  }
}

export function Markdown({ text }) {
  const blocks = parseBlocks(text == null ? '' : String(text));
  return <>{blocks.map((b, i) => <Block b={b} key={i} />)}</>;
}
