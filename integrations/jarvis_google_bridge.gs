/**
 * J.A.R.V.I.S. <-> Google Docs & Slides bridge.
 *
 * Runs inside YOUR Google account as an Apps Script web app (free, no API key, no Cloud project).
 * JARVIS sends it small JSON commands; it edits your Docs/Slides with Google's built-in services.
 * Only requests carrying the secret token below are accepted: keep the deployment URL private.
 *
 * Setup (JARVIS fills in the token and shows these steps in Settings):
 *   1. script.google.com -> New project -> replace everything with this file -> Save
 *   2. Deploy -> New deployment -> type "Web app" -> Execute as: Me, Who has access: Anyone -> Deploy
 *   3. Authorize access when Google asks, then copy the Web app URL into JARVIS.
 */
var JARVIS_TOKEN = '__JARVIS_TOKEN__';
var MIME = {
  docs: 'application/vnd.google-apps.document',
  slides: 'application/vnd.google-apps.presentation',
};
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
  return reply_({ ok: true, service: 'jarvis-google-bridge', version: 1 });
}

var ACTIONS = {
  ping: function () {
    return { service: 'jarvis-google-bridge', version: 1, user: Session.getEffectiveUser().getEmail() };
  },

  list: function (req) {
    return { files: findFiles_(req.kind === 'slides' ? MIME.slides : MIME.docs, req.query || '', 10) };
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
  if (!found.length) throw new Error('no ' + (mime === MIME.slides ? 'presentation' : 'document') + ' named "' + ref + '"');
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

/** Writes text line by line: "# " / "## " headings, "- " or "* " bullets, everything else paragraphs. */
function writeLines_(body, text, replaceFirst) {
  var lines = String(text).replace(/\r/g, '').split('\n');
  var firstUsed = !replaceFirst;
  lines.forEach(function (line) {
    var heading = null, bullet = false, content = line;
    if (/^##\s+/.test(line)) { heading = DocumentApp.ParagraphHeading.HEADING2; content = line.replace(/^##\s+/, ''); }
    else if (/^#\s+/.test(line)) { heading = DocumentApp.ParagraphHeading.HEADING1; content = line.replace(/^#\s+/, ''); }
    else if (/^\s*[-*]\s+/.test(line)) { bullet = true; content = line.replace(/^\s*[-*]\s+/, ''); }
    var para;
    if (!firstUsed && !bullet) {
      para = body.getParagraphs()[0];
      para.setText(content);
      firstUsed = true;
    } else if (bullet) {
      para = body.appendListItem(content);
      para.setGlyphType(DocumentApp.GlyphType.BULLET);
      firstUsed = true;
    } else {
      para = body.appendParagraph(content);
    }
    if (heading) para.setHeading(heading);
  });
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
