/**
 * J.A.R.V.I.S. <-> Google Docs, Slides, Sheets & Gmail bridge.
 *
 * Runs inside YOUR Google account as an Apps Script web app (free, no API key, no Cloud project).
 * JARVIS sends it small JSON commands; it edits your Docs/Slides/Sheets and sends the emails you confirm,
 * using Google's built-in services.
 * Only requests carrying the secret token below are accepted: keep the deployment URL private.
 *
 * Setup (JARVIS fills in the token and shows these steps in Settings):
 *   1. script.google.com -> New project -> replace everything with this file -> Save
 *   2. Deploy -> New deployment -> type "Web app" -> Execute as: Me, Who has access: Anyone -> Deploy
 *   3. Authorize access when Google asks, then copy the Web app URL into JARVIS.
 */
var JARVIS_TOKEN = '__JARVIS_TOKEN__';
var BRIDGE_VERSION = 3;
var MIME = {
  docs: 'application/vnd.google-apps.document',
  slides: 'application/vnd.google-apps.presentation',
  sheets: 'application/vnd.google-apps.spreadsheet',
};
var MAX_ROWS = 200;
var MAX_TEXT = 20000;

function doPost(e) {
  var req;
  try {
    req = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return reply_({ ok: false, error: 'invalid JSON' });
  }
  if (!JARVIS_TOKEN || JARVIS_TOKEN.indexOf('__') === 0 || req.token !== JARVIS_TOKEN) {
    return reply_({ ok: false, error: 'unauthorized' });
  }
  try {
    var handler = ACTIONS[req.action];
    if (!handler) return reply_({ ok: false, error: 'unknown action: ' + req.action });
    var result = handler(req) || {};
    result.ok = true;
    return reply_(result);
  } catch (err) {
    return reply_({ ok: false, error: String((err && err.message) || err) });
  }
}

function doGet() {
  return reply_({ ok: true, service: 'jarvis-google-bridge', version: BRIDGE_VERSION });
}

var ACTIONS = {
  ping: function () {
    return { service: 'jarvis-google-bridge', version: BRIDGE_VERSION, user: Session.getEffectiveUser().getEmail() };
  },

  list: function (req) {
    return { files: findFiles_(MIME[req.kind] || MIME.docs, req.query || '', 10) };
  },

  // ---------------------------------------------------------------- Docs
  doc_create: function (req) {
    var doc = DocumentApp.create(req.title || 'Untitled document');
    if (req.text) writeLines_(doc.getBody(), req.text, true);
    doc.saveAndClose();
    return describe_(doc);
  },

  doc_read: function (req) {
    var doc = openDoc_(req.document);
    var text = doc.getBody().getText();
    return { title: doc.getName(), url: doc.getUrl(), text: text.slice(0, MAX_TEXT), truncated: text.length > MAX_TEXT };
  },

  doc_append: function (req) {
    var doc = openDoc_(req.document);
    writeLines_(doc.getBody(), req.text || '', false);
    doc.saveAndClose();
    return describe_(doc);
  },

  doc_rewrite: function (req) {
    var doc = openDoc_(req.document);
    var body = doc.getBody();
    body.clear();
    writeLines_(body, req.text || '', true);
    doc.saveAndClose();
    return describe_(doc);
  },

  doc_replace: function (req) {
    if (!req.find) throw new Error('nothing to find');
    var doc = openDoc_(req.document);
    var body = doc.getBody();
    var count = body.getText().split(req.find).length - 1;
    if (count) body.replaceText(escapeRegex_(req.find), req.replace || '');
    doc.saveAndClose();
    var out = describe_(doc);
    out.replaced = count;
    return out;
  },

  // ---------------------------------------------------------------- Slides
  slides_create: function (req) {
    var deck = SlidesApp.create(req.title || 'Untitled presentation');
    var first = deck.getSlides()[0];
    setPlaceholder_(first, [SlidesApp.PlaceholderType.CENTERED_TITLE, SlidesApp.PlaceholderType.TITLE], req.title || '');
    setPlaceholder_(first, [SlidesApp.PlaceholderType.SUBTITLE], req.subtitle || '');
    (req.slides || []).forEach(function (s) { addSlide_(deck, s.title, s.body); });
    deck.saveAndClose();
    return describe_(deck);
  },

  slides_add: function (req) {
    var deck = openDeck_(req.presentation);
    addSlide_(deck, req.title, req.body);
    deck.saveAndClose();
    return describe_(deck);
  },

  slides_delete: function (req) {
    var deck = openDeck_(req.presentation);
    var slide = slideAt_(deck, req.number);
    slide.remove();
    deck.saveAndClose();
    var out = describe_(deck);
    out.slides = deck.getSlides().length;
    return out;
  },

  slides_move: function (req) {
    var deck = openDeck_(req.presentation);
    var slide = slideAt_(deck, req.number);
    var count = deck.getSlides().length;
    var to = Math.max(1, Math.min(count, parseInt(req.to, 10) || count));
    slide.move(to - 1);
    deck.saveAndClose();
    return describe_(deck);
  },

  slides_edit: function (req) {
    var deck = openDeck_(req.presentation);
    var slide = slideAt_(deck, req.number);
    var shapes = textShapes_(slide);
    if (req.title !== undefined && req.title !== '') {
      if (!setPlaceholder_(slide, [SlidesApp.PlaceholderType.TITLE, SlidesApp.PlaceholderType.CENTERED_TITLE], req.title) && shapes[0]) {
        shapes[0].getText().setText(req.title);
      }
    }
    if (req.body !== undefined && req.body !== '') {
      var text = Array.isArray(req.body) ? req.body.join('\n') : String(req.body);
      if (!setPlaceholder_(slide, [SlidesApp.PlaceholderType.BODY, SlidesApp.PlaceholderType.SUBTITLE], text) && shapes[1]) {
        shapes[1].getText().setText(text);
      }
    }
    deck.saveAndClose();
    return describe_(deck);
  },

  slides_replace: function (req) {
    if (!req.find) throw new Error('nothing to find');
    var deck = openDeck_(req.presentation);
    var count = deck.replaceAllText(req.find, req.replace || '');
    deck.saveAndClose();
    var out = describe_(deck);
    out.replaced = count;
    return out;
  },

  // ---------------------------------------------------------------- Gmail
  mail_send: function (req) {
    var to = String(req.to || '').trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+(\s*,\s*[^@\s]+@[^@\s]+\.[^@\s]+)*$/.test(to)) throw new Error('invalid recipient address: ' + to);
    var options = {};
    if (req.cc) options.cc = String(req.cc);
    GmailApp.sendEmail(to, String(req.subject || '(no subject)'), String(req.body || ''), options);
    return { sent: true, to: to };
  },

  /** People you've emailed with whose name matches, most frequent first (Gmail has no contacts API in Apps Script). */
  contact_find: function (req) {
    var name = String(req.name || '').trim();
    if (!name) throw new Error('whose address?');
    var words = name.toLowerCase().split(/\s+/);
    var counts = {}, names = {};
    var threads = GmailApp.search('from:(' + name + ') OR to:(' + name + ')', 0, 25);
    threads.forEach(function (thread) {
      thread.getMessages().slice(-5).forEach(function (msg) {
        [msg.getFrom(), msg.getTo(), msg.getCc()].join(',').split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/).forEach(function (part) {
          var m = part.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/) || part.match(/^\s*()([^\s<>]+@[^\s<>]+)\s*$/);
          if (!m) return;
          var display = m[1].trim(), email = m[2].trim().toLowerCase();
          var hay = (display + ' ' + email).toLowerCase();
          if (!words.every(function (w) { return hay.indexOf(w) >= 0; })) return;
          counts[email] = (counts[email] || 0) + 1;
          if (display) names[email] = display;
        });
      });
    });
    var me = Session.getEffectiveUser().getEmail().toLowerCase();
    var people = Object.keys(counts).filter(function (e) { return e !== me; }).map(function (e) {
      return { email: e, name: names[e] || '', count: counts[e] };
    });
    people.sort(function (a, b) { return b.count - a.count; });
    return { people: people.slice(0, 5) };
  },

  /** Let someone open a file JARVIS emails them a link to. */
  share_file: function (req) {
    var file = DriveApp.getFileById(String(req.file || ''));
    var email = String(req.email || '').trim();
    if (req.role === 'edit') file.addEditor(email); else file.addViewer(email);
    return { shared: true, title: file.getName(), url: file.getUrl() };
  },

  // ---------------------------------------------------------------- Sheets
  sheet_create: function (req) {
    var ss = SpreadsheetApp.create(req.title || 'Untitled spreadsheet');
    var sheet = ss.getSheets()[0];
    var rows = rows_(req.rows);
    if (rows.length) {
      sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
      sheet.getRange(1, 1, 1, rows[0].length).setFontWeight('bold');
      sheet.autoResizeColumns(1, rows[0].length);
    }
    return describe_(ss);
  },

  sheet_append: function (req) {
    var ss = openSheet_(req.spreadsheet);
    var sheet = tab_(ss, req.tab);
    var rows = rows_(req.rows);
    if (!rows.length) throw new Error('no rows to add');
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
    var out = describe_(ss);
    out.rows_added = rows.length;
    return out;
  },

  sheet_write: function (req) {
    var ss = openSheet_(req.spreadsheet);
    var sheet = tab_(ss, req.tab);
    var rows = rows_(req.rows);
    if (!rows.length) throw new Error('no values to write');
    sheet.getRange(req.range || 'A1').offset(0, 0, rows.length, rows[0].length).setValues(rows);
    return describe_(ss);
  },

  sheet_read: function (req) {
    var ss = openSheet_(req.spreadsheet);
    var sheet = tab_(ss, req.tab);
    var range = req.range ? sheet.getRange(req.range) : sheet.getDataRange();
    var values = range.getDisplayValues();
    return {
      title: ss.getName(), url: ss.getUrl(), tab: sheet.getName(),
      tabs: ss.getSheets().map(function (s) { return s.getName(); }),
      rows: values.slice(0, MAX_ROWS), truncated: values.length > MAX_ROWS,
    };
  },

  slides_read: function (req) {
    var deck = openDeck_(req.presentation);
    var slides = deck.getSlides().map(function (slide, i) {
      var parts = slide.getShapes().map(function (shape) {
        try { return shape.getText().asString().trim(); } catch (err) { return ''; }
      }).filter(function (t) { return t; });
      return { number: i + 1, text: parts.join('\n') };
    });
    return { title: deck.getName(), url: deck.getUrl(), slides: slides };
  },
};

// -------------------------------------------------------------------- helpers
function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function describe_(file) {
  return { id: file.getId(), title: file.getName(), url: file.getUrl() };
}

function escapeRegex_(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Accepts a file ID, a docs.google.com URL or (part of) a file name: the most recently edited match wins. */
function resolveId_(ref, mime) {
  ref = String(ref || '').trim();
  if (!ref) throw new Error('which file? (give a name, link or ID)');
  var m = ref.match(/\/d\/([A-Za-z0-9_-]{20,})/);
  if (m) return m[1];
  if (/^[A-Za-z0-9_-]{25,}$/.test(ref)) return ref;
  var found = findFiles_(mime, ref, 1);
  var kind = mime === MIME.slides ? 'presentation' : mime === MIME.sheets ? 'spreadsheet' : 'document';
  if (!found.length) throw new Error('no ' + kind + ' named "' + ref + '"');
  return found[0].id;
}

function findFiles_(mime, query, limit) {
  var q = "mimeType = '" + mime + "' and trashed = false";
  if (query) q += " and title contains '" + String(query).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  var it = DriveApp.searchFiles(q);
  var files = [];
  while (it.hasNext() && files.length < 200) {
    var f = it.next();
    files.push({ id: f.getId(), title: f.getName(), url: f.getUrl(), updated: f.getLastUpdated().getTime() });
  }
  files.sort(function (a, b) { return b.updated - a.updated; });
  return files.slice(0, limit).map(function (f) {
    return { id: f.id, title: f.title, url: f.url, updated: new Date(f.updated).toISOString() };
  });
}

function openDoc_(ref) { return DocumentApp.openById(resolveId_(ref, MIME.docs)); }
function openDeck_(ref) { return SlidesApp.openById(resolveId_(ref, MIME.slides)); }
function openSheet_(ref) { return SpreadsheetApp.openById(resolveId_(ref, MIME.sheets)); }

function tab_(ss, name) {
  if (!name) return ss.getSheets()[0];
  var sheet = ss.getSheetByName(name);
  if (!sheet) throw new Error('no tab named "' + name + '"');
  return sheet;
}

/** Rectangular 2-D array (Sheets rejects ragged rows). */
function rows_(rows) {
  rows = Array.isArray(rows) ? rows.filter(function (r) { return Array.isArray(r); }) : [];
  var width = rows.reduce(function (w, r) { return Math.max(w, r.length); }, 0);
  return rows.map(function (r) {
    var out = r.slice();
    while (out.length < width) out.push('');
    return out;
  });
}

function slideAt_(deck, number) {
  var slides = deck.getSlides();
  var n = parseInt(number, 10);
  if (!n || n < 1 || n > slides.length) throw new Error('slide number must be between 1 and ' + slides.length);
  return slides[n - 1];
}

function textShapes_(slide) {
  return slide.getShapes().filter(function (shape) {
    try { shape.getText(); return true; } catch (err) { return false; }
  });
}

/**
 * Writes Markdown-ish text: "#"/"##"/"###" headings, "- "/"* " bullets, "1. " numbered items,
 * "| a | b |" tables, and **bold** / *italic* inline. Everything else becomes a paragraph.
 */
function writeLines_(body, text, replaceFirst) {
  var lines = String(text).replace(/\r/g, '').split('\n');
  var firstFree = replaceFirst;
  var H = DocumentApp.ParagraphHeading;
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    if (/^\s*\|.*\|\s*$/.test(line)) {  // table block
      var cells = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) {
          cells.push(lines[i].trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return stripMarks_(c.trim()); }));
        }
        i++;
      }
      i--;
      if (cells.length) body.appendTable(rows_(cells));
      continue;
    }
    var heading = null, glyph = null, content = line, m;
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) {
      heading = [H.HEADING1, H.HEADING2, H.HEADING3][m[1].length - 1];
      content = m[2];
    } else if ((m = line.match(/^\s*[-*\u2022]\s+(.*)$/))) {
      glyph = DocumentApp.GlyphType.BULLET; content = m[1];
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      glyph = DocumentApp.GlyphType.NUMBER; content = m[1];
    }
    var rich = inline_(content);
    var para;
    if (glyph) {
      para = body.appendListItem(rich.text);
      para.setGlyphType(glyph);
    } else if (firstFree) {
      para = body.getParagraphs()[0];
      para.setText(rich.text);
    } else {
      para = body.appendParagraph(rich.text);
    }
    firstFree = false;
    if (heading) para.setHeading(heading);
    if (rich.text) {
      var t = para.editAsText();
      rich.spans.forEach(function (s) {
        if (s.end > s.start) (s.bold ? t.setBold(s.start, s.end - 1, true) : t.setItalic(s.start, s.end - 1, true));
      });
    }
  }
}

function stripMarks_(s) { return s.replace(/\*\*(.+?)\*\*/g, '$1').replace(/(^|[^*])\*(?!\s)(.+?)\*/g, '$1$2'); }

/** Removes **bold** / *italic* markers and records where the formatted spans are in the plain text. */
function inline_(content) {
  var out = '', spans = [], re = /\*\*(.+?)\*\*|\*(?!\s)([^*]+?)\*/g, last = 0, m;
  while ((m = re.exec(content))) {
    out += content.slice(last, m.index);
    var inner = m[1] !== undefined ? m[1] : m[2];
    spans.push({ start: out.length, end: out.length + inner.length, bold: m[1] !== undefined });
    out += inner;
    last = re.lastIndex;
  }
  out += content.slice(last);
  return { text: out, spans: spans };
}

function setPlaceholder_(slide, types, text) {
  for (var i = 0; i < types.length; i++) {
    var ph = slide.getPlaceholder(types[i]);
    if (ph) { ph.asShape().getText().setText(text); return true; }
  }
  return false;
}

function addSlide_(deck, title, body) {
  var slide = deck.appendSlide(SlidesApp.PredefinedLayout.TITLE_AND_BODY);
  setPlaceholder_(slide, [SlidesApp.PlaceholderType.TITLE, SlidesApp.PlaceholderType.CENTERED_TITLE], title || '');
  var text = Array.isArray(body) ? body.join('\n') : String(body || '');
  setPlaceholder_(slide, [SlidesApp.PlaceholderType.BODY], text);
  return slide;
}
