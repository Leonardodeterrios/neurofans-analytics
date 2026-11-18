/**
 * Google Apps Script — приёмник переходов для таблицы аналитики.
 *
 * Как подключить:
 * 1. Открой нужную Google-таблицу → Расширения → Apps Script.
 * 2. Вставь этот код, сохрани.
 * 3. Развернуть → Новое развёртывание → тип "Веб-приложение".
 *    "Выполнять от имени": Я. "Доступ": у кого есть ссылка (Anyone).
 * 4. Скопируй URL веб-приложения и вставь в бота как SHEET_WEBHOOK_URL.
 *
 * Каждый заход бота добавляет строку: время | источник(имя профиля) | user_id | username | имя.
 */
function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sh = ss.getSheetByName('starts');
    if (!sh) {
      sh = ss.insertSheet('starts');
      sh.appendRow(['timestamp', 'source', 'user_id', 'username', 'first_name']);
    }
    sh.appendRow([
      new Date(),
      String(data.source || ''),
      String(data.user_id || ''),
      String(data.username || ''),
      String(data.first_name || '')
    ]);
    return ContentService
      .createTextOutput(JSON.stringify({ ok: true }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}

/**
 * Необязательно: сводка переходов по источникам.
 * Создаёт/обновляет лист "by_source" с количеством заходов на каждый аккаунт.
 * Запускается вручную из редактора или по триггеру.
 */
function buildSummary() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var src = ss.getSheetByName('starts');
  if (!src) return;
  var rows = src.getDataRange().getValues();
  var counts = {};
  for (var i = 1; i < rows.length; i++) {
    var s = rows[i][1] || 'direct';
    counts[s] = (counts[s] || 0) + 1;
  }
  var out = ss.getSheetByName('by_source') || ss.insertSheet('by_source');
  out.clear();
  out.appendRow(['source', 'clicks']);
  Object.keys(counts).sort(function(a, b){ return counts[b] - counts[a]; })
    .forEach(function(k){ out.appendRow([k, counts[k]]); });
}
