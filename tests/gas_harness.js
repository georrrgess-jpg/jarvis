// Runs integrations/jarvis_google_bridge.gs against in-memory fakes of Google's Apps Script services.
// Usage: node tests/gas_harness.js <script.gs> <token>  (reads a JSON array of requests on stdin)
'use strict';
const fs = require('fs');
const vm = require('vm');

const [scriptPath, token] = process.argv.slice(2);
const store = { files: [], seq: 0, clock: 1700000000000, sent: [], shares: [] };
// A small mailbox for contact lookups: [from, to, cc]
const MAILBOX = [
  ['"Sarah Connor" <sarah.connor@example.com>', 'tony@example.com', ''],
  ['tony@example.com', 'Sarah Connor <sarah.connor@example.com>', 'Pepper Potts <pepper@stark.com>'],
  ['Sarah Lee <slee@school.edu>', 'tony@example.com', ''],
  ['"Potts, Pepper" <pepper@stark.com>', 'tony@example.com', ''],
  ['Sarah Connor <sarah.connor@example.com>', 'tony@example.com', ''],
];
const newId = () => `id${String(++store.seq).padStart(30, 'x')}`;

class Paragraph {
  constructor(text, list = false) { this.text = text; this.heading = 'NORMAL'; this.list = list; this.glyph = null; this.marks = []; }
  setText(t) { this.text = t; this.marks = []; return this; }
  editAsText() {
    const p = this;
    const mark = (kind) => (start, end, on) => {
      if (start < 0 || end >= p.text.length || end < start) throw new Error(`offset out of range: ${start}-${end}`);
      if (on) p.marks.push({ kind, text: p.text.slice(start, end + 1) });
      return this;
    };
    return { setBold: mark('bold'), setItalic: mark('italic') };
  }
  setHeading(h) { this.heading = h; return this; }
  setGlyphType(g) { this.glyph = g; return this; }
}
class Body {
  constructor() { this.paragraphs = [new Paragraph('')]; this.tables = []; }
  getParagraphs() { return this.paragraphs.filter((p) => !p.list); }
  clear() { this.paragraphs = [new Paragraph('')]; this.tables = []; return this; }
  appendTable(cells) {
    if (!cells.every((r) => r.length === cells[0].length)) throw new Error('ragged table');
    this.tables.push(cells); this.paragraphs.push(new Paragraph(cells.map((r) => r.join('\t')).join('\n'))); return {};
  }
  appendParagraph(t) { const p = new Paragraph(t); this.paragraphs.push(p); return p; }
  appendListItem(t) { const p = new Paragraph(t, true); this.paragraphs.push(p); return p; }
  getText() { return this.paragraphs.map((p) => p.text).join('\n'); }
  replaceText(pattern, repl) { const re = new RegExp(pattern, 'g'); this.paragraphs.forEach((p) => { p.text = p.text.replace(re, repl); }); return this; }
}
class FileBase {
  constructor(name, mime) { this.id = newId(); this.name = name; this.mime = mime; this.updated = (store.clock += 1000); store.files.push(this); }
  getId() { return this.id; }
  getName() { return this.name; }
  getUrl() { const kind = this.mime.includes('document') ? 'document' : this.mime.includes('spreadsheet') ? 'spreadsheets' : 'presentation'; return `https://docs.google.com/${kind}/d/${this.id}/edit`; }
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
  constructor(types, deck) { this.deck = deck; this.ph = {}; types.forEach((t) => { this.ph[t] = new Shape(); }); }
  remove() { this.deck.slides.splice(this.deck.slides.indexOf(this), 1); }
  move(i) { const s = this.deck.slides; s.splice(s.indexOf(this), 1); s.splice(i, 0, this); }
  getPlaceholder(t) { return this.ph[t] || null; }
  getShapes() { return Object.values(this.ph); }
}
class Deck extends FileBase {
  constructor(name) { super(name, 'application/vnd.google-apps.presentation'); this.slides = [new Slide(['CENTERED_TITLE', 'SUBTITLE'], this)]; }
  getSlides() { return this.slides.slice(); }
  appendSlide(layout) { if (layout !== 'TITLE_AND_BODY') throw new Error('bad layout'); const s = new Slide(['TITLE', 'BODY'], this); this.slides.push(s); return s; }
  replaceAllText(find, repl) {
    let n = 0;
    this.slides.forEach((s) => Object.values(s.ph).forEach((sh) => { const parts = sh.text.split(find); n += parts.length - 1; sh.text = parts.join(repl); }));
    return n;
  }
}
const colIndex = (letters) => letters.toUpperCase().split('').reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
class Range {
  constructor(tab, row, col, h, w) {
    if (row < 1 || col < 1 || h < 1 || w < 1) throw new Error(`bad range ${row},${col},${h},${w}`);
    Object.assign(this, { tab, row, col, h, w });
  }
  offset(r, c, h, w) { return new Range(this.tab, this.row + r, this.col + c, h ?? this.h, w ?? this.w); }
  setValues(values) {
    if (values.length !== this.h || values.some((v) => v.length !== this.w)) throw new Error('The number of rows or columns in the data does not match the range');
    values.forEach((r, i) => r.forEach((v, j) => { const row = (this.tab.cells[this.row - 1 + i] ||= []); row[this.col - 1 + j] = v; }));
    return this;
  }
  getDisplayValues() {
    return Array.from({ length: this.h }, (_, i) => Array.from({ length: this.w }, (_, j) => {
      const v = (this.tab.cells[this.row - 1 + i] || [])[this.col - 1 + j]; return v === undefined || v === null ? '' : String(v);
    }));
  }
  setFontWeight(w) { this.tab.bold.push({ row: this.row, w }); return this; }
}
class Tab {
  constructor(name) { this.name = name; this.cells = []; this.bold = []; }
  getName() { return this.name; }
  getLastRow() { return this.cells.length; }
  getRange(a, b, c, d) {
    if (typeof a === 'string') {
      const m = /^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$/i.exec(a);
      if (!m) throw new Error(`Range not found: ${a}`);
      const r1 = +m[2], c1 = colIndex(m[1]);
      return new Range(this, r1, c1, m[3] ? +m[4] - r1 + 1 : 1, m[3] ? colIndex(m[3]) - c1 + 1 : 1);
    }
    return new Range(this, a, b, c ?? 1, d ?? 1);
  }
  getDataRange() { return new Range(this, 1, 1, Math.max(1, this.cells.length), Math.max(1, ...this.cells.map((r) => r.length))); }
  autoResizeColumns() { return this; }
}
class Sheet extends FileBase {
  constructor(name) { super(name, 'application/vnd.google-apps.spreadsheet'); this.tabs = [new Tab('Sheet1')]; }
  getSheets() { return this.tabs.slice(); }
  getSheetByName(n) { return this.tabs.find((t) => t.name === n) || null; }
}
const byId = (id, cls) => { const f = store.files.find((x) => x.id === id && x instanceof cls); if (!f) throw new Error(`No item with the given ID could be found: ${id}`); return f; };

const sandbox = {
  DocumentApp: {
    create: (n) => new Doc(n), openById: (id) => byId(id, Doc),
    ParagraphHeading: { HEADING1: 'HEADING1', HEADING2: 'HEADING2', HEADING3: 'HEADING3', NORMAL: 'NORMAL' }, GlyphType: { BULLET: 'BULLET', NUMBER: 'NUMBER' },
  },
  SlidesApp: {
    create: (n) => new Deck(n), openById: (id) => byId(id, Deck),
    PlaceholderType: { CENTERED_TITLE: 'CENTERED_TITLE', TITLE: 'TITLE', SUBTITLE: 'SUBTITLE', BODY: 'BODY' },
    PredefinedLayout: { TITLE_AND_BODY: 'TITLE_AND_BODY' },
  },
  SpreadsheetApp: { create: (n) => new Sheet(n), openById: (id) => byId(id, Sheet) },
  GmailApp: {
    sendEmail(to, subject, body, options) { store.sent.push({ to, subject, body, options }); },
    search(q) {
      const m = /from:\(([^)]*)\)/.exec(q);
      const words = m[1].toLowerCase().split(/\s+/);
      const hits = MAILBOX.filter((row) => words.every((w) => row.join(' ').toLowerCase().includes(w)));
      return hits.map((row) => ({ getMessages: () => [{ getFrom: () => row[0], getTo: () => row[1], getCc: () => row[2] }] }));
    },
  },
  DriveApp: {
    getFileById(id) {
      const f = store.files.find((x) => x.id === id);
      if (!f) throw new Error(`No item with the given ID could be found: ${id}`);
      return { addViewer: (e) => store.shares.push({ id, e, role: 'view' }), addEditor: (e) => store.shares.push({ id, e, role: 'edit' }),
        getName: () => f.name, getUrl: () => f.getUrl() };
    },
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
const docs = store.files.map((f) => {
  if (f instanceof Doc) {
    return { kind: 'doc', name: f.name, tables: f.body.tables,
      paragraphs: f.body.paragraphs.map((p) => ({ text: p.text, heading: p.heading, list: p.list, ...(p.glyph ? { glyph: p.glyph } : {}), ...(p.marks.length ? { marks: p.marks } : {}) })) };
  }
  if (f instanceof Sheet) return { kind: 'sheet', name: f.name, tabs: f.tabs.map((t) => ({ name: t.name, cells: t.cells, bold: t.bold })) };
  return { kind: 'deck', name: f.name, slides: f.slides.map((s) => Object.fromEntries(Object.entries(s.ph).map(([k, v]) => [k, v.text]))) };
});
process.stdout.write(JSON.stringify({ results, files: docs, sent: store.sent, shares: store.shares }));
