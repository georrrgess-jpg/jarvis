// Runs integrations/jarvis_google_bridge.gs against in-memory fakes of Google's Apps Script services.
// Usage: node tests/gas_harness.js <script.gs> <token>  (reads a JSON array of requests on stdin)
'use strict';
const fs = require('fs');
const vm = require('vm');

const [scriptPath, token] = process.argv.slice(2);
const store = { files: [], seq: 0, clock: 1700000000000 };
const newId = () => `id${String(++store.seq).padStart(30, 'x')}`;

class Paragraph {
  constructor(text, list = false) { this.text = text; this.heading = 'NORMAL'; this.list = list; this.glyph = null; }
  setText(t) { this.text = t; return this; }
  setHeading(h) { this.heading = h; return this; }
  setGlyphType(g) { this.glyph = g; return this; }
}
class Body {
  constructor() { this.paragraphs = [new Paragraph('')]; }
  getParagraphs() { return this.paragraphs.filter((p) => !p.list); }
  appendParagraph(t) { const p = new Paragraph(t); this.paragraphs.push(p); return p; }
  appendListItem(t) { const p = new Paragraph(t, true); this.paragraphs.push(p); return p; }
  getText() { return this.paragraphs.map((p) => p.text).join('\n'); }
  replaceText(pattern, repl) { const re = new RegExp(pattern, 'g'); this.paragraphs.forEach((p) => { p.text = p.text.replace(re, repl); }); return this; }
}
class FileBase {
  constructor(name, mime) { this.id = newId(); this.name = name; this.mime = mime; this.updated = (store.clock += 1000); store.files.push(this); }
  getId() { return this.id; }
  getName() { return this.name; }
  getUrl() { return `https://docs.google.com/${this.mime.includes('document') ? 'document' : 'presentation'}/d/${this.id}/edit`; }
  saveAndClose() { this.updated = (store.clock += 1000); }
}
class Doc extends FileBase {
  constructor(name) { super(name, 'application/vnd.google-apps.document'); this.body = new Body(); }
  getBody() { return this.body; }
}
class Shape {
  constructor() { this.text = ''; }
  asShape() { return this; }
  getText() { const s = this; return { setText(t) { s.text = t; }, asString() { return s.text; } }; }
}
class Slide {
  constructor(types) { this.ph = {}; types.forEach((t) => { this.ph[t] = new Shape(); }); }
  getPlaceholder(t) { return this.ph[t] || null; }
  getShapes() { return Object.values(this.ph); }
}
class Deck extends FileBase {
  constructor(name) { super(name, 'application/vnd.google-apps.presentation'); this.slides = [new Slide(['CENTERED_TITLE', 'SUBTITLE'])]; }
  getSlides() { return this.slides; }
  appendSlide(layout) { if (layout !== 'TITLE_AND_BODY') throw new Error('bad layout'); const s = new Slide(['TITLE', 'BODY']); this.slides.push(s); return s; }
}
const byId = (id, cls) => { const f = store.files.find((x) => x.id === id && x instanceof cls); if (!f) throw new Error(`No item with the given ID could be found: ${id}`); return f; };

const sandbox = {
  DocumentApp: {
    create: (n) => new Doc(n), openById: (id) => byId(id, Doc),
    ParagraphHeading: { HEADING1: 'HEADING1', HEADING2: 'HEADING2', NORMAL: 'NORMAL' }, GlyphType: { BULLET: 'BULLET' },
  },
  SlidesApp: {
    create: (n) => new Deck(n), openById: (id) => byId(id, Deck),
    PlaceholderType: { CENTERED_TITLE: 'CENTERED_TITLE', TITLE: 'TITLE', SUBTITLE: 'SUBTITLE', BODY: 'BODY' },
    PredefinedLayout: { TITLE_AND_BODY: 'TITLE_AND_BODY' },
  },
  DriveApp: {
    searchFiles(q) {
      const mime = /mimeType = '([^']+)'/.exec(q)[1];
      const title = /title contains '((?:[^'\\]|\\.)*)'/.exec(q);
      const needle = title ? title[1].replace(/\\(.)/g, '$1').toLowerCase() : '';
      const hits = store.files.filter((f) => f.mime === mime && f.name.toLowerCase().includes(needle));
      let i = 0;
      return { hasNext: () => i < hits.length, next: () => { const f = hits[i++]; return { getId: () => f.id, getName: () => f.name, getUrl: () => f.getUrl(), getLastUpdated: () => new Date(f.updated) }; } };
    },
  },
  ContentService: { MimeType: { JSON: 'json' }, createTextOutput: (s) => ({ content: s, setMimeType() { return this; } }) },
  Session: { getEffectiveUser: () => ({ getEmail: () => 'tony@example.com' }) },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8').replace('__JARVIS_TOKEN__', token), sandbox, { filename: scriptPath });

const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = requests.map((req) => JSON.parse(sandbox.doPost({ postData: { contents: JSON.stringify(req) } }).content));
const docs = store.files.map((f) => (f instanceof Doc
  ? { kind: 'doc', name: f.name, paragraphs: f.body.paragraphs.map((p) => ({ text: p.text, heading: p.heading, list: p.list })) }
  : { kind: 'deck', name: f.name, slides: f.slides.map((s) => Object.fromEntries(Object.entries(s.ph).map(([k, v]) => [k, v.text]))) }));
process.stdout.write(JSON.stringify({ results, files: docs }));
