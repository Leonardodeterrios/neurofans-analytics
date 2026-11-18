#!/usr/bin/env python3
"""
NeuroFans — веб-дашборд аналитики переходов.
Читает ту же базу data/starts.db, что и бот. Показывает: сколько переходов,
откуда, график по дням, таблицу источников.

Запуск:  pip install -r requirements.txt && python dashboard.py
Открой:  http://localhost:8080  (если задан DASHBOARD_TOKEN → http://localhost:8080/?token=XXX)
"""
import os, sqlite3, datetime as dt
from flask import Flask, jsonify, request, abort, Response
from dotenv import load_dotenv

load_dotenv()
DB_PATH         = os.getenv("DB_PATH", "data/starts.db")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
PORT            = int(os.getenv("DASHBOARD_PORT", "8080"))

app = Flask(__name__)

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def _check_token():
    if DASHBOARD_TOKEN and request.args.get("token", "") != DASHBOARD_TOKEN:
        abort(401)

def get_stats():
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM starts").fetchone()[0]
        uniq  = c.execute("SELECT COUNT(DISTINCT user_id) FROM starts").fetchone()[0]
        sources = [dict(r) for r in c.execute("""
            SELECT source, COUNT(*) clicks, COUNT(DISTINCT user_id) uniq, MAX(ts) last
            FROM starts GROUP BY source ORDER BY clicks DESC""")]
        by_day = [dict(r) for r in c.execute("""
            SELECT substr(ts,1,10) day, COUNT(*) clicks
            FROM starts GROUP BY day ORDER BY day""")]
    return {"total": total, "unique": uniq, "sources": sources, "by_day": by_day}

@app.route("/api/stats")
def api_stats():
    _check_token()
    return jsonify(get_stats())

@app.route("/")
def index():
    _check_token()
    return Response(PAGE, mimetype="text/html")

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
</style></head><body>
<h1>NeuroFans — аналитика переходов</h1>
<p class="sub" id="updated">загрузка…</p>
<div class="kpis">
  <div class="card kpi"><div class="n" id="k_total">—</div><div class="l">Всего переходов</div></div>
  <div class="card kpi"><div class="n" id="k_uniq">—</div><div class="l">Уникальных людей</div></div>
  <div class="card kpi"><div class="n" id="k_src">—</div><div class="l">Активных источников</div></div>
</div>
<div class="grid">
  <div class="card"><b>Переходы по источникам</b><canvas id="bySource" height="220"></canvas></div>
  <div class="card"><b>Переходы по дням</b><canvas id="byDay" height="220"></canvas></div>
</div>
<div class="card" style="margin-top:16px">
  <b>Источники</b>
  <table><thead><tr><th>Источник</th><th>Переходов</th><th>Уникальных</th><th>Последний</th></tr></thead>
  <tbody id="rows"></tbody></table>
</div>
<p class="foot">Обновляется автоматически каждые 30 сек. Метка источника = имя профиля.</p>
<script>
const token = new URLSearchParams(location.search).get('token');
let barChart, dayChart;
function fmt(ts){return ts? ts.replace('T',' ') : '—';}
async function load(){
  const r = await fetch('/api/stats'+(token?('?token='+encodeURIComponent(token)):''));
  if(!r.ok){document.getElementById('updated').textContent='Нет доступа (проверь token)';return;}
  const d = await r.json();
  k_total.textContent=d.total; k_uniq.textContent=d.unique; k_src.textContent=d.sources.length;
  document.getElementById('updated').textContent='Обновлено: '+new Date().toLocaleString('ru-RU');
  // table
  document.getElementById('rows').innerHTML = d.sources.map(s=>
    `<tr><td><span class="dot" style="background:#5b8cff"></span>${s.source||'direct'}</td>
     <td>${s.clicks}</td><td>${s.uniq}</td><td class="muted">${fmt(s.last)}</td></tr>`).join('')
     || '<tr><td colspan=4 class="muted">Пока нет данных</td></tr>';
  // bar: clicks per source
  const sl=d.sources.map(s=>s.source||'direct'), sc=d.sources.map(s=>s.clicks);
  if(barChart)barChart.destroy();
  barChart=new Chart(bySource,{type:'bar',data:{labels:sl,datasets:[{label:'Переходы',data:sc,
    backgroundColor:'#5b8cff'}]},options:{plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8aa0b8'}},y:{ticks:{color:'#8aa0b8'},beginAtZero:true}}}});
  // line: by day
  const dl=d.by_day.map(x=>x.day), dc=d.by_day.map(x=>x.clicks);
  if(dayChart)dayChart.destroy();
  dayChart=new Chart(byDay,{type:'line',data:{labels:dl,datasets:[{label:'Переходы',data:dc,
    borderColor:'#5b8cff',backgroundColor:'rgba(91,140,255,.15)',fill:true,tension:.3}]},
    options:{plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8aa0b8'}},y:{ticks:{color:'#8aa0b8'},beginAtZero:true}}}});
}
load(); setInterval(load,30000);
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
