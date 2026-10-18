const AUDIT_SHEET_NAME = 'Daily Report';
const AUDIT_SPREADSHEET_ID = '1iDQoBTDHsrfavBA4iWrcgrUKIt7NfWty28s2PkMvF18';
const AUDIT_API_TOKEN_PROPERTY = 'AUDIT_API_TOKEN';
const AUDIT_SOURCE_SPREADSHEET_PROPERTY = 'AUDIT_SOURCE_SPREADSHEET_ID';
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

function setupCreativeChangeAudit() {
  const sourceSpreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!sourceSpreadsheet) {
    throw new Error('Open the source spreadsheet before running audit setup.');
  }
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
  PropertiesService.getScriptProperties().setProperty(
    AUDIT_SOURCE_SPREADSHEET_PROPERTY,
    sourceSpreadsheet.getId()
  );
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

  const headerInfo = findAuditHeaders_(sheet);
  const editor = getEditorEmail(event);
  const editValues = range.getDisplayValues();
  const editIsSingleCell = range.getNumRows() === 1 && range.getNumColumns() === 1;
  const statusColumn = headerInfo.statusColumn;
  const statusIsIncluded = statusColumn !== null
    && range.getColumn() <= statusColumn
    && range.getLastColumn() >= statusColumn;
  const auditRows = [];

  if (!statusIsIncluded || !editIsSingleCell) {
    const editedRow = range.getRow();
    let creativeName = '';
    if (headerInfo.creativeColumn !== null) {
      creativeName = sheet.getRange(editedRow, headerInfo.creativeColumn).getDisplayValue();
      if (!creativeName && range.getColumn() <= headerInfo.creativeColumn
        && range.getLastColumn() >= headerInfo.creativeColumn) {
        creativeName = event.oldValue || '';
      }
    }
    const columnName = editIsSingleCell
      ? headerInfo.headers[range.getColumn() - 1] || range.getA1Notation()
      : range.getA1Notation();
    auditRows.push([
      Utilities.getUuid(),
      new Date(),
      editor || 'Editor identity unavailable',
      sheet.getName(),
      creativeName,
      range.getA1Notation(),
      columnName,
      editIsSingleCell ? (event.oldValue || '') : '[Previous values unavailable for multi-cell edits]',
      JSON.stringify(editValues)
    ]);
  }

  if (statusIsIncluded) {
    const firstStatusRow = range.getRow();
    const lastStatusRow = range.getLastRow();
    const statusOffset = statusColumn - range.getColumn();
    const creativeNames = headerInfo.creativeColumn === null
      ? []
      : sheet.getRange(
        firstStatusRow,
        headerInfo.creativeColumn,
        lastStatusRow - firstStatusRow + 1,
        1
      ).getDisplayValues().map(row => row[0]);
    for (let row = firstStatusRow; row <= lastStatusRow; row++) {
      const creativeName = creativeNames[row - firstStatusRow] || '';
      const statusValue = editValues[row - firstStatusRow][statusOffset];
      const isSingleStatusCell = editIsSingleCell;
      auditRows.push([
        Utilities.getUuid(),
        new Date(),
        editor || 'Editor identity unavailable',
        sheet.getName(),
        creativeName,
        `${auditColumnName_(statusColumn)}${row}`,
        headerInfo.headers[statusColumn - 1] || 'Status',
        isSingleStatusCell ? (event.oldValue || '') : '[Previous values unavailable for multi-cell edits]',
        JSON.stringify([[statusValue]])
      ]);
    }
  }

  if (!auditRows.length) return;
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const firstRow = auditSheet.getLastRow() + 1;
    auditSheet.getRange(firstRow, 1, auditRows.length, AUDIT_HEADERS.length).setValues(auditRows);
  } finally {
    lock.releaseLock();
  }
}

function auditColumnName_(columnIndex) {
  let column = columnIndex;
  let name = '';
  while (column > 0) {
    const remainder = (column - 1) % 26;
    name = String.fromCharCode(65 + remainder) + name;
    column = Math.floor((column - 1) / 26);
  }
  return name;
}

function findAuditHeaders_(sheet) {
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (!lastRow || !lastColumn) {
    return { row: 1, headers: [], creativeColumn: null, statusColumn: null };
  }
  const rowsToCheck = Math.min(lastRow, 25);
  const values = sheet.getRange(1, 1, rowsToCheck, lastColumn).getDisplayValues();
  for (let rowIndex = 0; rowIndex < values.length; rowIndex++) {
    const headers = values[rowIndex];
    const creativeColumnIndex = headers.findIndex(header => /creative/i.test(header));
    const statusColumnIndex = headers.findIndex(header => /status/i.test(header));
    if (creativeColumnIndex !== -1 && statusColumnIndex !== -1) {
      return {
        row: rowIndex + 1,
        headers,
        creativeColumn: creativeColumnIndex + 1,
        statusColumn: statusColumnIndex + 1
      };
    }
  }
  return {
    row: 1,
    headers: values[0] || [],
    creativeColumn: null,
    statusColumn: null
  };
}

function isAuditDoneStatus_(value) {
  const status = String(value || '').trim().toUpperCase();
  return status.includes('DONE') || status === 'Y';
}

function getCurrentDoneCreatives_() {
  const sourceSpreadsheetId = PropertiesService.getScriptProperties()
    .getProperty(AUDIT_SOURCE_SPREADSHEET_PROPERTY);
  if (!sourceSpreadsheetId) {
    throw new Error('Run setupCreativeChangeAudit() to connect the source spreadsheet.');
  }

  const sourceSpreadsheet = SpreadsheetApp.openById(sourceSpreadsheetId);
  const creatives = [];
  for (const sheet of sourceSpreadsheet.getSheets()) {
    if (sheet.getName().trim().toLowerCase() === AUDIT_SHEET_NAME.toLowerCase()) continue;
    const headerInfo = findAuditHeaders_(sheet);
    if (headerInfo.creativeColumn === null || headerInfo.statusColumn === null) continue;

    const lastRow = sheet.getLastRow();
    if (lastRow <= headerInfo.row) continue;
    const rowCount = lastRow - headerInfo.row;
    const values = sheet
      .getRange(headerInfo.row + 1, 1, rowCount, sheet.getLastColumn())
      .getDisplayValues();
    for (let offset = 0; offset < values.length; offset++) {
      const row = values[offset];
      const creative = String(row[headerInfo.creativeColumn - 1] || '').trim();
      const status = row[headerInfo.statusColumn - 1];
      if (!creative || !isAuditDoneStatus_(status)) continue;
      const sheetRow = headerInfo.row + offset + 1;
      creatives.push({
        sheet: sheet.getName(),
        creative,
        range: `${auditColumnName_(headerInfo.statusColumn)}${sheetRow}`
      });
    }
  }
  return creatives;
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

    const currentDone = getCurrentDoneCreatives_();
    const currentDoneKeys = new Set(
      currentDone.map(item => `${item.sheet}\u0000${item.range}`)
    );
    const auditSpreadsheet = SpreadsheetApp.openById(AUDIT_SPREADSHEET_ID);
    const auditSheet = auditSpreadsheet.getSheetByName(AUDIT_SHEET_NAME);
    if (!auditSheet || auditSheet.getLastRow() < 1) {
      return jsonResponse_({
        success: true,
        tracking_enabled: false,
        events: [],
        current_done: currentDone
      });
    }

    const rows = auditSheet.getDataRange().getValues();
    const headers = rows[0].map(value => String(value || '').trim().toLowerCase());
    const columns = Object.fromEntries(headers.map((header, index) => [header, index]));
    if (AUDIT_HEADERS.some(header => columns[header.toLowerCase()] === undefined)) {
      return jsonResponse_({ success: false, error: 'The Daily Report header row is invalid.' });
    }

    const timezone = auditSpreadsheet.getSpreadsheetTimeZone();
    const latestEventsByCell = new Map();
    for (const row of rows.slice(1)) {
      const timestamp = row[columns.timestamp];
      if (!(timestamp instanceof Date) || isNaN(timestamp.getTime())) continue;
      const sheetName = String(row[columns.sheet] || '');
      const range = String(row[columns.range] || '');
      const column = String(row[columns.column] || '');
      if (!/status/i.test(column) || !currentDoneKeys.has(`${sheetName}\u0000${range}`)) continue;
      latestEventsByCell.set(`${sheetName}\u0000${range}`, {
        id: String(row[columns['event id']] || ''),
        timestamp: Utilities.formatDate(timestamp, timezone, "yyyy-MM-dd'T'HH:mm:ss"),
        editor: String(row[columns.editor] || ''),
        sheet: sheetName,
        et: String(row[columns['et / creative']] || ''),
        range,
        column,
        old_value: String(row[columns['old value']] || ''),
        new_value: String(row[columns['new value']] || '')
      });
    }
    return jsonResponse_({
      success: true,
      tracking_enabled: true,
      events: Array.from(latestEventsByCell.values()),
      current_done: currentDone
    });
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
