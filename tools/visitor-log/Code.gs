/**
 * Visitor log for the XMTrading X Mention Monitor dashboard.
 * Paste into a Google Apps Script project bound to a Google Sheet (see README.md).
 *
 * The dashboard POSTs {email, kind, ua, lang, page} (kind = "signin" | "visit").
 * One row per e-mail is kept on the "Visitors" sheet; every event is also appended to "Log".
 */
var ALLOWED_DOMAINS = ['xmtrading.com', 'xm.com', 'trading-point.com', 'tradingpoint.com'];
var VISITOR_HEADERS = ['Email', 'Domain', 'First seen (UTC)', 'Last seen (UTC)', 'Visits', 'Last user agent', 'Last language'];
var LOG_HEADERS = ['Time (UTC)', 'Email', 'Event', 'Language', 'User agent', 'Page'];

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    var d = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var email = String(d.email || '').trim().toLowerCase();
    var m = /^[a-z0-9._%+'-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)$/.exec(email);
    if (!m || email.length > 254 || ALLOWED_DOMAINS.indexOf(m[1]) < 0) return reply_('rejected');

    var kind = d.kind === 'signin' ? 'signin' : 'visit';
    var ua = String(d.ua || '').slice(0, 200);
    var lang = d.lang === 'ja' ? 'ja' : 'en';
    var page = String(d.page || '').slice(0, 200);
    var now = new Date();

    lock.waitLock(10000);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var vis = sheet_(ss, 'Visitors', VISITOR_HEADERS);
    var log = sheet_(ss, 'Log', LOG_HEADERS);

    var row = findRow_(vis, email);
    if (row) {
      var visits = Number(vis.getRange(row, 5).getValue()) || 0;
      vis.getRange(row, 4, 1, 4).setValues([[now, visits + 1, ua, lang]]);
    } else {
      vis.appendRow([email, m[1], now, now, 1, ua, lang]);
    }
    log.appendRow([now, email, kind, lang, ua, page]);
    return reply_('ok');
  } catch (err) {
    return reply_('error');
  } finally {
    try { lock.releaseLock(); } catch (x) { /* not held */ }
  }
}

function doGet() { return reply_('ok'); }

function sheet_(ss, name, headers) {
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(headers);
    sh.setFrozenRows(1);
    sh.getRange(1, 1, 1, headers.length).setFontWeight('bold');
  }
  return sh;
}

function findRow_(sh, email) {
  var last = sh.getLastRow();
  if (last < 2) return 0;
  var emails = sh.getRange(2, 1, last - 1, 1).getValues();
  for (var i = 0; i < emails.length; i++) if (emails[i][0] === email) return i + 2;
  return 0;
}

function reply_(status) {
  return ContentService.createTextOutput(JSON.stringify({ status: status })).setMimeType(ContentService.MimeType.JSON);
}
