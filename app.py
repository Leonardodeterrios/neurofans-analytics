#!/usr/bin/env python3
"""
NeuroFans — аналитика переходов на уровне ССЫЛКИ + дашборд + уведомления.

Схема (ссылку в био ставим НАШУ, считающую, а не прямую на бота):

  ссылка в био:  http://СЕРВЕР/go?source=<метка_аккаунта>
       клик  ─►  наш сервер фиксирует переход (метка + IP)  ─►  302 redirect
                 ─►  https://t.me/<BOT_USERNAME>?start=<метка>  ─►  PuzzleBot (воронка)

Ловим КАЖДЫЙ клик, даже если человек не нажал «Старт». Воронка на PuzzleBot
не трогается вообще. Токен бота используем только чтобы слать тебе уведомления
и сводки (sendMessage) — polling не запускаем, конфликта с PuzzleBot нет.

Запуск:  python app.py   (слушает $DASHBOARD_PORT, по умолчанию 8080)
"""
import os, io, csv, re, sqlite3, threading, time, logging, datetime as dt
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from flask import Flask, jsonify, request, abort, redirect, Response
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN          = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME       = os.getenv("BOT_USERNAME", "AccessGrantedGuide_bot").strip().lstrip("@")
ADMIN_CHAT_ID      = os.getenv("ADMIN_CHAT_ID", "").strip()
DB_PATH            = os.getenv("DB_PATH", "data/clicks.db")
DASHBOARD_TOKEN    = os.getenv("DASHBOARD_TOKEN", "").strip()   # пароль на дашборд (?token=...)
PORT               = int(os.getenv("DASHBOARD_PORT", "8080"))
SHEET_WEBHOOK_URL  = os.getenv("SHEET_WEBHOOK_URL", "").strip()
NOTIFY_CLICKS      = os.getenv("NOTIFY_CLICKS", "1").strip() not in ("0", "false", "no", "")
DAILY_SUMMARY_TIME = os.getenv("DAILY_SUMMARY_TIME", "").strip()  # "09:00", пусто = выкл
SUMMARY_TZ         = os.getenv("SUMMARY_TZ", "UTC").strip()
SILENCE_HOURS      = int(os.getenv("SILENCE_HOURS", "24"))
SILENCE_CHECK_MIN  = int(os.getenv("SILENCE_CHECK_MIN", "60"))
SILENCE_MIN_CLICKS = int(os.getenv("SILENCE_MIN_CLICKS", "3"))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# не считаем предпросмотрщики ссылок (телега/соцсети дёргают ссылку при публикации)
BOT_UA = re.compile(r"bot|crawler|spider|preview|facebookexternalhit|telegrambot|"
                    r"whatsapp|vkshare|slackbot|skypeuripreview|discordbot|twitterbot", re.I)
SAFE_START = re.compile(r"[^A-Za-z0-9_-]")   # допустимые символы для telegram ?start=

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neurofans")
app = Flask(__name__)


# ---------- база ----------
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS clicks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, source TEXT, ip TEXT, ua TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, source TEXT, kind TEXT)""")

def record_click(source, ip, ua) -> bool:
    """Пишет клик. Возвращает True, если IP раньше не встречался (похоже на нового человека)."""
    with _conn() as c:
        is_new = True
        if ip:
            seen = c.execute("SELECT 1 FROM clicks WHERE ip=? LIMIT 1", (ip,)).fetchone()
            is_new = seen is None
        c.execute("INSERT INTO clicks(ts,source,ip,ua) VALUES(?,?,?,?)",
                  (dt.datetime.utcnow().isoformat(timespec="seconds"), source, ip, ua))
    return is_new

def stats_text() -> str:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        uniq  = c.execute("SELECT COUNT(DISTINCT ip) FROM clicks").fetchone()[0]
        rows  = c.execute("""SELECT source, COUNT(*) n, COUNT(DISTINCT ip) u
                             FROM clicks GROUP BY source ORDER BY n DESC""").fetchall()
    if not rows:
        return "📊 Пока нет переходов."
    lines = [f"📊 Всего переходов: {total}  |  уникальных (по IP): {uniq}", ""]
    lines += [f"• {r[0] or 'direct'}: {r[1]} переходов ({r[2]} уник.)" for r in rows]
    return "\n".join(lines)

def csv_bytes() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "source", "ip", "ua"])
    with _conn() as c:
        for row in c.execute("SELECT ts,source,ip,ua FROM clicks ORDER BY id"):
            w.writerow(list(row))
    return buf.getvalue().encode("utf-8-sig")

def get_stats():
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        uniq  = c.execute("SELECT COUNT(DISTINCT ip) FROM clicks").fetchone()[0]
        sources = [dict(r) for r in c.execute("""
            SELECT source, COUNT(*) clicks, COUNT(DISTINCT ip) uniq, MAX(ts) last
            FROM clicks GROUP BY source ORDER BY clicks DESC""")]
        by_day = [dict(r) for r in c.execute("""
            SELECT substr(ts,1,10) day, COUNT(*) clicks
            FROM clicks GROUP BY day ORDER BY day""")]
    return {"total": total, "unique": uniq, "sources": sources, "by_day": by_day}


# ---------- Telegram (только отправка) ----------
def tg_send(text, chat_id=None):
    cid = chat_id or ADMIN_CHAT_ID
    if not (BOT_TOKEN and cid):
        return
    try:
        httpx.post(f"{TG_API}/sendMessage",
                   json={"chat_id": int(cid), "text": text, "disable_web_page_preview": True},
                   timeout=10)
    except Exception as e:
        log.error("tg send failed: %s", e)

def notify_admin(source, is_new):
    if not (ADMIN_CHAT_ID and NOTIFY_CLICKS):
        return
    flag = "🆕 новый" if is_new else "↩️ повторный"
    tg_send(f"🔔 Переход по ссылке ({flag})\nАккаунт: {source}")

def push_sheet(source, ip, ua):
    if not SHEET_WEBHOOK_URL:
        return
    try:
        httpx.post(SHEET_WEBHOOK_URL,
                   json={"source": source, "ip": ip, "ua": ua}, timeout=10)
    except Exception as e:
        log.error("sheet push failed: %s", e)


# ---------- алерты «тишина по источнику» ----------
def record_alert(source, kind):
    with _conn() as c:
        c.execute("INSERT INTO alerts(ts,source,kind) VALUES(?,?,?)",
                  (dt.datetime.utcnow().isoformat(timespec="seconds"), source, kind))

def silent_sources():
    now = dt.datetime.utcnow()
    threshold = now - dt.timedelta(hours=SILENCE_HOURS)
    out = []
    with _conn() as c:
        rows = c.execute("""SELECT source, MAX(ts) last, COUNT(*) total
                            FROM clicks GROUP BY source""").fetchall()
        for source, last, total in rows:
            if total < SILENCE_MIN_CLICKS or not last:
                continue
            try:
                last_dt = dt.datetime.fromisoformat(last)
            except ValueError:
                continue
            if last_dt >= threshold:
                continue
            la = c.execute("SELECT MAX(ts) FROM alerts WHERE source=? AND kind='silence'",
                           (source,)).fetchone()[0]
            if la and dt.datetime.fromisoformat(la) >= last_dt:
                continue
            hours = (now - last_dt).total_seconds() / 3600
            out.append((source or "direct", last, total, hours))
    return out

def run_silence_check():
    if not ADMIN_CHAT_ID:
        return
    for source, last, total, hours in silent_sources():
        tg_send(f"🔕 Тишина по источнику: {source}\n"
                f"Нет переходов уже ~{int(hours)} ч (последний: {last} UTC).\n"
                f"Всего по нему было {total}. Проверь ссылку в био / статус аккаунта.")
        record_alert(source, "silence")


# ---------- фоновый планировщик ----------
def scheduler_loop():
    last_silence = 0.0
    last_summary_date = None
    while True:
        now = time.time()
        if now - last_silence >= SILENCE_CHECK_MIN * 60:
            last_silence = now
            try:
                run_silence_check()
            except Exception as e:
                log.error("silence check failed: %s", e)
        if DAILY_SUMMARY_TIME and ADMIN_CHAT_ID:
            try:
                tz = ZoneInfo(SUMMARY_TZ)
                nowtz = dt.datetime.now(tz)
                hh, mm = (int(x) for x in DAILY_SUMMARY_TIME.split(":"))
                if nowtz.hour == hh and nowtz.minute == mm and last_summary_date != nowtz.date():
                    last_summary_date = nowtz.date()
                    tg_send("🗓 Ежедневная сводка\n\n" + stats_text())
            except Exception as e:
                log.error("daily summary failed: %s", e)
        time.sleep(30)


# ---------- веб ----------
def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""

def _check_dashboard():
    if DASHBOARD_TOKEN and request.args.get("token", "") != DASHBOARD_TOKEN:
        abort(401)

@app.route("/go")
def go():
    """Публичная ссылка-считалка для био. Фиксирует переход и перекидывает в бота."""
    source = (request.values.get("source") or request.values.get("s") or "direct").strip() or "direct"
    ua = (request.headers.get("User-Agent", "") or "")[:300]
    # предпросмотрщики ссылок не считаем, но всё равно перекидываем
    if not BOT_UA.search(ua):
        is_new = record_click(source, _client_ip(), ua)
        notify_admin(source, is_new)
        push_sheet(source, _client_ip(), ua)
        log.info("click source=%s new=%s", source, is_new)
    payload = SAFE_START.sub("", source)[:64]
    target = f"https://t.me/{BOT_USERNAME}" + (f"?start={quote(payload)}" if payload else "")
    return redirect(target, code=302)

@app.route("/api/stats")
def api_stats():
    _check_dashboard()
    return jsonify(get_stats())

@app.route("/export")
def export():
    _check_dashboard()
    return Response(csv_bytes(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=clicks.csv"})

@app.route("/")
def index():
    _check_dashboard()
    return Response(PAGE, mimetype="text/html")

@app.route("/healthz")
def healthz():
    return "ok"

PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NeuroFans — аналитика переходов</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{--bg:#0f1420;--card:#19202e;--line:#263142;--txt:#e7edf5;--mut:#8aa0b8;--acc:#5b8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:24px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 20px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.kpi .n{font-size:30px;font-weight:700}.kpi .l{color:var(--mut);font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;margin-top:6px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:14px}
th{color:var(--mut);font-weight:600}.muted{color:var(--mut)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.foot{color:var(--mut);font-size:12px;margin-top:16px}
a{color:var(--acc)}
</style></head><body>
<h1>NeuroFans — аналитика переходов</h1>
<p class="sub" id="updated">загрузка…</p>
<div class="kpis">
  <div class="card kpi"><div class="n" id="k_total">—</div><div class="l">Всего переходов</div></div>
  <div class="card kpi"><div class="n" id="k_uniq">—</div><div class="l">Уникальных (по IP)</div></div>
  <div class="card kpi"><div class="n" id="k_src">—</div><div class="l">Активных аккаунтов</div></div>
</div>
<div class="grid">
  <div class="card"><b>Переходы по аккаунтам</b><canvas id="bySource" height="220"></canvas></div>
  <div class="card"><b>Переходы по дням</b><canvas id="byDay" height="220"></canvas></div>
</div>
<div class="card" style="margin-top:16px">
  <b>Аккаунты</b> &nbsp; <a id="csv" href="#">скачать CSV</a>
  <table><thead><tr><th>Аккаунт</th><th>Переходов</th><th>Уникальных</th><th>Последний</th></tr></thead>
  <tbody id="rows"></tbody></table>
</div>
<p class="foot">Обновляется автоматически каждые 30 сек. Метка = имя аккаунта из ссылки в био.</p>
<script>
const token = new URLSearchParams(location.search).get('token');
const q = token?('?token='+encodeURIComponent(token)):'';
document.getElementById('csv').href='/export'+q;
let barChart, dayChart;
function fmt(ts){return ts? ts.replace('T',' ') : '—';}
async function load(){
  const r = await fetch('/api/stats'+q);
  if(!r.ok){document.getElementById('updated').textContent='Нет доступа (проверь token)';return;}
  const d = await r.json();
  k_total.textContent=d.total; k_uniq.textContent=d.unique; k_src.textContent=d.sources.length;
  document.getElementById('updated').textContent='Обновлено: '+new Date().toLocaleString('ru-RU');
  document.getElementById('rows').innerHTML = d.sources.map(s=>
    `<tr><td><span class="dot" style="background:#5b8cff"></span>${s.source||'direct'}</td>
     <td>${s.clicks}</td><td>${s.uniq}</td><td class="muted">${fmt(s.last)}</td></tr>`).join('')
     || '<tr><td colspan=4 class="muted">Пока нет данных</td></tr>';
  const sl=d.sources.map(s=>s.source||'direct'), sc=d.sources.map(s=>s.clicks);
  if(barChart)barChart.destroy();
  barChart=new Chart(bySource,{type:'bar',data:{labels:sl,datasets:[{label:'Переходы',data:sc,
    backgroundColor:'#5b8cff'}]},options:{plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8aa0b8'}},y:{ticks:{color:'#8aa0b8'},beginAtZero:true}}}});
  const dl=d.by_day.map(x=>x.day), dc=d.by_day.map(x=>x.clicks);
  if(dayChart)dayChart.destroy();
  dayChart=new Chart(byDay,{type:'line',data:{labels:dl,datasets:[{label:'Переходы',data:dc,
    borderColor:'#5b8cff',backgroundColor:'rgba(91,140,255,.15)',fill:true,tension:.3}]},
    options:{plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8aa0b8'}},y:{ticks:{color:'#8aa0b8'},beginAtZero:true}}}});
}
load(); setInterval(load,30000);
</script></body></html>"""


def main():
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("NeuroFans click-tracker on :%s (bot=@%s, admin=%s, daily=%s)",
             PORT, BOT_USERNAME, bool(ADMIN_CHAT_ID), DAILY_SUMMARY_TIME or "off")
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

if __name__ == "__main__":
    main()
