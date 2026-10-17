const AUDIT_SHEET_NAME = 'Daily Report';
const AUDIT_SPREADSHEET_ID = '1iDQoBTDHsrfavBA4iWrcgrUKIt7NfWty28s2PkMvF18';
const AUDIT_API_TOKEN_PROPERTY = 'AUDIT_API_TOKEN';
const AUDIT_HEADERS = [
  'Event ID',
  'Timestamp',
  'Editor',
  'Sheet',
  'ET / Creative',
  'Range',
  'Column',
  'Old Value',
  'New Value'
];
const AUDIT_MAX_ROWS = 5000;

function setupCreativeChangeAudit() {
  const sourceSpreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  const auditSpreadsheet = SpreadsheetApp.openById(AUDIT_SPREADSHEET_ID);
  let auditSheet = auditSpreadsheet.getSheetByName(AUDIT_SHEET_NAME);
  if (!auditSheet) {
    auditSheet = auditSpreadsheet.insertSheet(AUDIT_SHEET_NAME);
  }

  if (auditSheet.getLastRow() === 0) {
    auditSheet.appendRow(AUDIT_HEADERS);
    auditSheet.setFrozenRows(1);
  } else {
    const existingHeaders = auditSheet
      .getRange(1, 1, 1, AUDIT_HEADERS.length)
      .getDisplayValues()[0];
    if (AUDIT_HEADERS.some((header, index) => existingHeaders[index] !== header)) {
      throw new Error(`The "${AUDIT_SHEET_NAME}" tab exists but its headers do not match. Rename or repair the tab, then run setup again.`);
    }
  }

  const alreadyInstalled = ScriptApp.getProjectTriggers().some(
    trigger => trigger.getHandlerFunction() === 'recordCreativeChange'
      && trigger.getEventType() === ScriptApp.EventType.ON_EDIT
      && trigger.getTriggerSourceId() === sourceSpreadsheet.getId()
  );
  if (!alreadyInstalled) {
    ScriptApp.newTrigger('recordCreativeChange')
      .forSpreadsheet(sourceSpreadsheet)
      .onEdit()
      .create();
  }
}

function generateAuditApiToken() {
  const token = `${Utilities.getUuid().replace(/-/g, '')}${Utilities.getUuid().replace(/-/g, '')}`;
  PropertiesService.getScriptProperties().setProperty(AUDIT_API_TOKEN_PROPERTY, token);
  Logger.log('Copy this one-time token into the app server environment variable AUDIT_API_TOKEN:');
  Logger.log(token);
  return token;
}

function recordCreativeChange(event) {
  if (!event || !event.range) return;

  const range = event.range;
  const sheet = range.getSheet();
  if (sheet.getName().trim().toLowerCase() === AUDIT_SHEET_NAME.toLowerCase()) return;

  const auditSpreadsheet = SpreadsheetApp.openById(AUDIT_SPREADSHEET_ID);
  const auditSheet = auditSpreadsheet.getSheetByName(AUDIT_SHEET_NAME);
  if (!auditSheet) return;

  const editedRow = range.getRow();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
  const creativeColumn = headers.findIndex(header => /creative/i.test(header)) + 1;
  let creativeName = '';
  if (creativeColumn > 0) {
    creativeName = sheet.getRange(editedRow, creativeColumn).getDisplayValue();
    if (!creativeName && range.getColumn() <= creativeColumn
      && range.getLastColumn() >= creativeColumn) {
      creativeName = event.oldValue || '';
    }
  }

  const columnName = range.getNumColumns() === 1
    ? (headers[range.getColumn() - 1] || range.getA1Notation())
    : range.getA1Notation();
  const editor = getEditorEmail(event);
  const oldValue = range.getNumRows() === 1 && range.getNumColumns() === 1
    ? (event.oldValue || '')
    : '[Previous values unavailable for multi-cell edits]';
  const newValue = JSON.stringify(range.getDisplayValues());
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    auditSheet.appendRow([
      Utilities.getUuid(),
      new Date(),
      editor || 'Editor identity unavailable',
      sheet.getName(),
      creativeName,
      range.getA1Notation(),
      columnName,
      oldValue,
      newValue
    ]);

    const excessRows = auditSheet.getLastRow() - AUDIT_MAX_ROWS;
    if (excessRows > 0) {
      auditSheet.deleteRows(2, excessRows);
    }
  } finally {
    lock.releaseLock();
  }
}

function doPost(event) {
  try {
    const payload = JSON.parse(event?.postData?.contents || '{}');
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      return jsonResponse_({ success: false, error: 'Invalid request.' });
    }
    const expectedToken = PropertiesService.getScriptProperties()
      .getProperty(AUDIT_API_TOKEN_PROPERTY) || '';
    if (!expectedToken || !payload.token || !constantTimeEquals_(String(payload.token), expectedToken)) {
      return jsonResponse_({ success: false, error: 'Unauthorized.' });
    }
    if (payload.action !== 'get_daily_report') {
      return jsonResponse_({ success: false, error: 'Unsupported action.' });
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(payload.date || ''))) {
      return jsonResponse_({ success: false, error: 'A valid report date is required.' });
    }

    const auditSpreadsheet = SpreadsheetApp.openById(AUDIT_SPREADSHEET_ID);
    const auditSheet = auditSpreadsheet.getSheetByName(AUDIT_SHEET_NAME);
    if (!auditSheet || auditSheet.getLastRow() < 1) {
      return jsonResponse_({ success: true, tracking_enabled: false, events: [] });
    }

    const rows = auditSheet.getDataRange().getValues();
    const headers = rows[0].map(value => String(value || '').trim().toLowerCase());
    const columns = Object.fromEntries(headers.map((header, index) => [header, index]));
    if (AUDIT_HEADERS.some(header => columns[header.toLowerCase()] === undefined)) {
      return jsonResponse_({ success: false, error: 'The Daily Report header row is invalid.' });
    }

    const timezone = auditSpreadsheet.getSpreadsheetTimeZone();
    const events = [];
    for (const row of rows.slice(1)) {
      const timestamp = row[columns.timestamp];
      if (!(timestamp instanceof Date) || isNaN(timestamp.getTime())) continue;
      if (Utilities.formatDate(timestamp, timezone, 'yyyy-MM-dd') !== payload.date) continue;

      events.push({
        id: String(row[columns['event id']] || ''),
        timestamp: Utilities.formatDate(timestamp, timezone, "yyyy-MM-dd'T'HH:mm:ss"),
        editor: String(row[columns.editor] || ''),
        sheet: String(row[columns.sheet] || ''),
        et: String(row[columns['et / creative']] || ''),
        range: String(row[columns.range] || ''),
        column: String(row[columns.column] || ''),
        old_value: String(row[columns['old value']] || ''),
        new_value: String(row[columns['new value']] || '')
      });
    }
    return jsonResponse_({ success: true, tracking_enabled: true, events: events.slice(-5000) });
  } catch (error) {
    console.error('Daily Report API failed:', error);
    return jsonResponse_({ success: false, error: 'Could not read the Daily Report.' });
  }
}

function constantTimeEquals_(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index++) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function jsonResponse_(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function getEditorEmail(event) {
  try {
    return event.user ? event.user.getEmail() : '';
  } catch (error) {
    console.warn('Could not determine the editor identity:', error);
    return '';
  }
}
