const AUDIT_SHEET_NAME = 'Creative Change Audit';
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
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  let auditSheet = spreadsheet.getSheetByName(AUDIT_SHEET_NAME);
  if (!auditSheet) {
    auditSheet = spreadsheet.insertSheet(AUDIT_SHEET_NAME);
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
  );
  if (!alreadyInstalled) {
    ScriptApp.newTrigger('recordCreativeChange')
      .forSpreadsheet(spreadsheet)
      .onEdit()
      .create();
  }
}

function recordCreativeChange(event) {
  if (!event || !event.range) return;

  const range = event.range;
  const sheet = range.getSheet();
  if (sheet.getName() === AUDIT_SHEET_NAME) return;

  const spreadsheet = event.source || sheet.getParent();
  const auditSheet = spreadsheet.getSheetByName(AUDIT_SHEET_NAME);
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
  const lock = LockService.getDocumentLock();
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
    if (excessRows > 1) {
      auditSheet.deleteRows(2, excessRows);
    }
  } finally {
    lock.releaseLock();
  }
}

function getEditorEmail(event) {
  try {
    return event.user ? event.user.getEmail() : '';
  } catch (error) {
    console.warn('Could not determine the editor identity:', error);
    return '';
  }
}
