#!/usr/bin/env python3
"""
NeuroFans — партнёрская аналитика переходов (две команды) + геймифицированный дашборд.

Схема (ссылку в био партнёра ставим НАШУ, считающую):
  http://СЕРВЕР/go?source=<метка_партнёра>
     клик ─► фиксируем переход (метка + IP) ─► 302 redirect ─► t.me/<BOT>?start=<метка> ─► PuzzleBot

Команды определяются по префиксу метки:
  leo*  → Команда Лёни     kate* → Команда Кати     (настраивается через .env)

Дашборд: две «копилки» команд, лидер дня, таблица-лидерборд с медалями, графики.
Каждый день в заданное время — итоги и победитель в Telegram.
polling не запускаем (используем токен только для sendMessage) — конфликта с PuzzleBot нет.
"""
import os, io, csv, re, hmac, sqlite3, threading, time, logging, datetime as dt
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from flask import Flask, jsonify, request, abort, redirect, Response
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN          = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME       = os.getenv("BOT_USERNAME", "AccessGrantedGuide_bot").strip().lstrip("@")
ADMIN_CHAT_ID      = os.getenv("ADMIN_CHAT_ID", "").strip()   # один ID или несколько через запятую
ADMIN_IDS          = [x.strip() for x in ADMIN_CHAT_ID.replace(";", ",").split(",") if x.strip()]
DB_PATH            = os.getenv("DB_PATH", "data/clicks.db")
DASHBOARD_TOKEN    = os.getenv("DASHBOARD_TOKEN", "").strip()
PORT               = int(os.getenv("DASHBOARD_PORT", "8080"))
SHEET_WEBHOOK_URL  = os.getenv("SHEET_WEBHOOK_URL", "").strip()
NOTIFY_CLICKS      = os.getenv("NOTIFY_CLICKS", "1").strip() not in ("0", "false", "no", "")
DAILY_SUMMARY_TIME = os.getenv("DAILY_SUMMARY_TIME", "").strip()   # "21:00", пусто = выкл
SUMMARY_TZ         = os.getenv("SUMMARY_TZ", "Asia/Bangkok").strip()
SILENCE_HOURS      = int(os.getenv("SILENCE_HOURS", "24"))
SILENCE_CHECK_MIN  = int(os.getenv("SILENCE_CHECK_MIN", "60"))
SILENCE_MIN_CLICKS = int(os.getenv("SILENCE_MIN_CLICKS", "3"))

# ---- команды (две «копилки») ----
TEAM_A_NAME   = os.getenv("TEAM_A_NAME", "Команда Лёни").strip()
TEAM_A_PREFIX = os.getenv("TEAM_A_PREFIX", "leo").strip().lower()
TEAM_A_COLOR  = os.getenv("TEAM_A_COLOR", "#4f8cff").strip()
TEAM_B_NAME   = os.getenv("TEAM_B_NAME", "Команда Кати").strip()
TEAM_B_PREFIX = os.getenv("TEAM_B_PREFIX", "kate").strip().lower()
TEAM_B_COLOR  = os.getenv("TEAM_B_COLOR", "#ff5c8a").strip()
TEAMS = {"A": (TEAM_A_NAME, TEAM_A_PREFIX, TEAM_A_COLOR),
         "B": (TEAM_B_NAME, TEAM_B_PREFIX, TEAM_B_COLOR)}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_UA = re.compile(r"bot|crawler|spider|preview|facebookexternalhit|telegrambot|"
                    r"whatsapp|vkshare|slackbot|skypeuripreview|discordbot|twitterbot", re.I)
SAFE_START = re.compile(r"[^A-Za-z0-9_-]")    # допустимое для telegram ?start=
SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]")   # допустимое для метки партнёра (защита от мусора/XSS)
PUBLIC_TOKEN = os.getenv("PUBLIC_TOKEN", "").strip()   # пароль «только арена» для партнёров
ARENA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arena.html")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neurofans")
app = Flask(__name__)


def team_of(source):
    s = (source or "").lower()
    if TEAM_A_PREFIX and s.startswith(TEAM_A_PREFIX): return "A"
    if TEAM_B_PREFIX and s.startswith(TEAM_B_PREFIX): return "B"
    return None


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
    with _conn() as c:
        is_new = True
        if ip:
            is_new = c.execute("SELECT 1 FROM clicks WHERE ip=? LIMIT 1", (ip,)).fetchone() is None
        c.execute("INSERT INTO clicks(ts,source,ip,ua) VALUES(?,?,?,?)",
                  (dt.datetime.utcnow().isoformat(timespec="seconds"), source, ip, ua))
    return is_new

def _round_hm():
    if DAILY_SUMMARY_TIME:
        try:
            hh, mm = (int(x) for x in DAILY_SUMMARY_TIME.split(":"))
            return hh, mm
        except ValueError:
            pass
    return 0, 0

def round_bounds(at=None):
    """Раунд = от гонга до гонга (DAILY_SUMMARY_TIME в SUMMARY_TZ). Возвращает (start, end)."""
    tz = ZoneInfo(SUMMARY_TZ)
    at = (at or dt.datetime.now(tz)).astimezone(tz)
    hh, mm = _round_hm()
    end = at.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end <= at:
        end += dt.timedelta(days=1)
    return end - dt.timedelta(days=1), end

def _utc_naive(d):
    return d.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

def _utc_z(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def get_stats(at=None):
    """«today» во всех полях = текущий раунд (от прошлого гонга до следующего)."""
    start, end = round_bounds(at)
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        uniq  = c.execute("SELECT COUNT(DISTINCT ip) FROM clicks").fetchone()[0]
        rows = c.execute("""SELECT source, COUNT(*) clicks, COUNT(DISTINCT ip) uniq, MAX(ts) last
                            FROM clicks GROUP BY source""").fetchall()
        today_rows = dict(c.execute(
            "SELECT source, COUNT(*) FROM clicks WHERE ts>=? AND ts<? GROUP BY source",
            (_utc_naive(start), _utc_naive(end))).fetchall())
        by_day = [dict(r) for r in c.execute(
            "SELECT substr(ts,1,10) day, COUNT(*) clicks FROM clicks GROUP BY day ORDER BY day")]
    partners = []
    for r in rows:
        src = r["source"]
        partners.append({"source": src, "clicks": r["clicks"], "uniq": r["uniq"],
                         "last": r["last"], "today": today_rows.get(src, 0), "team": team_of(src)})
    partners.sort(key=lambda p: (-p["clicks"], p["source"] or ""))
    teams = []
    for key, (name, _prefix, color) in TEAMS.items():
        mem = [p for p in partners if p["team"] == key]
        teams.append({"key": key, "name": name, "color": color,
                      "clicks": sum(p["clicks"] for p in mem),
                      "today": sum(p["today"] for p in mem),
                      "partners": len(mem)})
    lt = max((p for p in partners if p["team"]), key=lambda p: p["today"], default=None)
    leader_today = ({"source": lt["source"], "team": lt["team"], "clicks": lt["today"]}
                    if lt and lt["today"] > 0 else None)
    return {"total": total, "unique": uniq, "teams": teams, "partners": partners,
            "leader_today": leader_today, "by_day": by_day,
            "round_start": _utc_z(start), "round_end": _utc_z(end),
            "server_now": _utc_z(dt.datetime.now(dt.timezone.utc))}

def stats_text() -> str:
    st = get_stats()
    if st["total"] == 0:
        return "📊 Пока нет переходов."
    a, b = st["teams"]
    lines = [f"📊 Всего переходов: {st['total']}  |  уник.: {st['unique']}", "",
             f"🔵 {a['name']}: {a['clicks']} (в раунде {a['today']})",
             f"🩷 {b['name']}: {b['clicks']} (в раунде {b['today']})", "", "Топ партнёров:"]
    for i, p in enumerate(st["partners"][:10], 1):
        lines.append(f"{i}. {p['source']}: {p['clicks']} (в раунде {p['today']})")
    return "\n".join(lines)

def _plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14: return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many

def daily_results_text() -> str:
    # гонг только что прозвучал — берём раунд, который закончился
    at = dt.datetime.now(ZoneInfo(SUMMARY_TZ)) - dt.timedelta(minutes=2)
    st = get_stats(at)
    _start, end = round_bounds(at)
    a, b = st["teams"]
    lines = [f"🥊 Гонг! Итоги раунда {end.strftime('%d.%m.%Y')}", "",
             f"🔵 {a['name']}: {a['today']} {_plural(a['today'],'удар','удара','ударов')} за раунд (всего {a['clicks']})",
             f"🩷 {b['name']}: {b['today']} {_plural(b['today'],'удар','удара','ударов')} за раунд (всего {b['clicks']})", ""]
    if a["today"] != b["today"]:
        win = a if a["today"] > b["today"] else b
        lines.append(f"👑 Раунд выиграла {win['name']}! 🎉")
    elif a["today"] == 0:
        lines.append("В этом раунде ударов не было.")
    else:
        lines.append("🤝 Ничья!")
    lt = st["leader_today"]
    if lt:
        lines.append(f"🥇 Лучший боец раунда: {lt['source']} ({lt['clicks']} {_plural(lt['clicks'],'удар','удара','ударов')})")
    lines.append("\nНовый раунд уже начался — до следующего гонга 24 часа 💪")
    return "\n".join(lines)

def csv_bytes() -> bytes:
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["ts", "source", "team", "ip", "ua"])
    with _conn() as c:
        for ts, source, ip, ua in c.execute("SELECT ts,source,ip,ua FROM clicks ORDER BY id"):
            t = TEAMS[team_of(source)][0] if team_of(source) else ""
            w.writerow([ts, source, t, ip, ua])
    return buf.getvalue().encode("utf-8-sig")


# ---------- Telegram (только отправка) ----------
def tg_send(text, chat_id):
    if not (BOT_TOKEN and chat_id):
        return
    try:
        httpx.post(f"{TG_API}/sendMessage",
                   json={"chat_id": int(chat_id), "text": text, "disable_web_page_preview": True},
                   timeout=10)
    except Exception as e:
        log.error("tg send to %s failed: %s", chat_id, e)

def notify_all(text):
    """Шлёт сообщение всем админам из ADMIN_IDS (тебе и Кате)."""
    for cid in ADMIN_IDS:
        tg_send(text, cid)

def notify_admin(source, is_new):
    if not (ADMIN_IDS and NOTIFY_CLICKS):
        return
    flag = "🆕 новый" if is_new else "↩️ повторный"
    tk = team_of(source)
    tname = TEAMS[tk][0] if tk else "без команды"
    notify_all(f"🔔 Переход ({flag})\nПартнёр: {source}\nКоманда: {tname}")

def push_sheet(source, ip, ua):
    if not SHEET_WEBHOOK_URL:
        return
    try:
        httpx.post(SHEET_WEBHOOK_URL, json={"source": source, "ip": ip, "ua": ua}, timeout=10)
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
        rows = c.execute("SELECT source, MAX(ts) last, COUNT(*) total FROM clicks GROUP BY source").fetchall()
        for source, last, total in rows:
            if total < SILENCE_MIN_CLICKS or not last:
                continue
            try:
                last_dt = dt.datetime.fromisoformat(last)
            except ValueError:
                continue
            if last_dt >= threshold:
                continue
            la = c.execute("SELECT MAX(ts) FROM alerts WHERE source=? AND kind='silence'", (source,)).fetchone()[0]
            if la and dt.datetime.fromisoformat(la) >= last_dt:
                continue
            out.append((source or "direct", last, total, (now - last_dt).total_seconds() / 3600))
    return out

def run_silence_check():
    if not ADMIN_IDS:
        return
    for source, last, total, hours in silent_sources():
        notify_all(f"🔕 Тишина у партнёра: {source}\n"
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
        if DAILY_SUMMARY_TIME and ADMIN_IDS:
            try:
                tz = ZoneInfo(SUMMARY_TZ)
                nowtz = dt.datetime.now(tz)
                hh, mm = (int(x) for x in DAILY_SUMMARY_TIME.split(":"))
                if nowtz.hour == hh and nowtz.minute == mm and last_summary_date != nowtz.date():
                    last_summary_date = nowtz.date()
                    notify_all(daily_results_text())
            except Exception as e:
                log.error("daily summary failed: %s", e)
        time.sleep(30)


# ---------- веб ----------
def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "")

def _role():
    """admin — полный доступ (дашборд, CSV); public — только арена/рейтинг (для партнёров)."""
    tok = request.args.get("token", "")
    if not DASHBOARD_TOKEN or hmac.compare_digest(tok, DASHBOARD_TOKEN):
        return "admin"
    if PUBLIC_TOKEN and hmac.compare_digest(tok, PUBLIC_TOKEN):
        return "public"
    return None

def _need(admin=False):
    r = _role()
    if r is None or (admin and r != "admin"):
        abort(401)
    return r

@app.route("/go")
def go():
    raw = (request.values.get("source") or request.values.get("s") or "").strip()
    source = SAFE_LABEL.sub("", raw)[:64] or "direct"   # метка = только безопасные символы
    ua = (request.headers.get("User-Agent", "") or "")[:300]
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
    role = _need()
    st = get_stats()
    st["role"] = role
    return jsonify(st)

@app.route("/api/feed")
def api_feed():
    """Лента ударов для арены. Без since — только last_id (историю не проигрываем)."""
    _need()
    since = request.args.get("since")
    with _conn() as c:
        last = c.execute("SELECT COALESCE(MAX(id),0) FROM clicks").fetchone()[0]
        items = []
        if since is not None:
            try:
                s = int(since)
            except ValueError:
                s = last
            rows = c.execute("SELECT id, ts, source FROM clicks WHERE id>? ORDER BY id LIMIT 300",
                             (s,)).fetchall()
            items = [{"id": r["id"], "ts": r["ts"], "source": r["source"],
                      "team": team_of(r["source"])} for r in rows]
    return jsonify(last_id=last, items=items)

@app.route("/arena")
def arena():
    _need()
    try:
        with open(ARENA_PATH, encoding="utf-8") as f:
            html = f.read()
    except OSError:
        html = "<h1>arena.html не найден</h1>"
    return Response(html, mimetype="text/html")

@app.route("/export")
def export():
    _need(admin=True)
    return Response(csv_bytes(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=clicks.csv"})

@app.route("/")
def index():
    _need(admin=True)
    return Response(PAGE, mimetype="text/html")

@app.route("/healthz")
def healthz():
    return "ok"

PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NeuroFans — соревнование команд</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{--bg:#0d1117;--card:#161c26;--line:#232c3a;--txt:#e9eef5;--mut:#8aa0b8;
--a:#4f8cff;--b:#ff5c8a;--gold:#ffcf4a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:22px;max-width:1100px;margin:0 auto}
h1{font-size:22px;margin:0 0 2px}.sub{color:var(--mut);margin:0 0 18px;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}
.teams{display:grid;grid-template-columns:1fr auto 1fr;gap:14px;align-items:stretch;margin-bottom:14px}
.team{border-radius:16px;padding:18px;position:relative;overflow:hidden}
.team.a{background:linear-gradient(135deg,rgba(79,140,255,.18),rgba(79,140,255,.05));border:1px solid rgba(79,140,255,.4)}
.team.b{background:linear-gradient(135deg,rgba(255,92,138,.18),rgba(255,92,138,.05));border:1px solid rgba(255,92,138,.4)}
.team .nm{font-size:15px;color:var(--mut);display:flex;align-items:center;gap:6px}
.team .big{font-size:46px;font-weight:800;line-height:1.05;margin:6px 0 0}
.team .tod{font-size:13px;margin-top:4px}
.team .pc{font-size:12px;color:var(--mut);margin-top:8px}
.crown{position:absolute;top:12px;right:14px;font-size:24px}
.vs{display:flex;align-items:center;justify-content:center;font-weight:800;color:var(--mut);font-size:18px}
.bar{height:12px;border-radius:8px;background:var(--b);overflow:hidden;margin:4px 0 16px}
.bar>i{display:block;height:100%;background:var(--a)}
.banner{background:linear-gradient(135deg,rgba(255,207,74,.18),rgba(255,207,74,.04));
border:1px solid rgba(255,207,74,.45);border-radius:14px;padding:14px 16px;margin-bottom:16px;font-size:15px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi .n{font-size:26px;font-weight:700}.kpi .l{color:var(--mut);font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
@media(max-width:820px){.grid{grid-template-columns:1fr}.teams{grid-template-columns:1fr}.vs{display:none}}
table{width:100%;border-collapse:collapse;margin-top:8px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:14px}
th{color:var(--mut);font-weight:600}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px}
.badge.a{background:rgba(79,140,255,.2);color:#9cc0ff}
.badge.b{background:rgba(255,92,138,.2);color:#ffb0c7}
.rk{width:34px;font-weight:700}.mut{color:var(--mut)}
.foot{color:var(--mut);font-size:12px;margin-top:14px}a{color:var(--a)}
</style></head><body>
<h1>🏆 NeuroFans — соревнование команд &nbsp;<a id="arenaLink" href="/arena" style="font-size:14px">🥊 Открыть арену</a></h1>
<p class="sub" id="updated">загрузка…</p>

<div class="teams" id="teams"></div>
<div class="bar" id="bar"><i id="barA" style="width:50%"></i></div>
<div class="banner" id="banner">—</div>

<div class="kpis">
  <div class="card kpi"><div class="n" id="k_total">—</div><div class="l">Всего переходов</div></div>
  <div class="card kpi"><div class="n" id="k_uniq">—</div><div class="l">Уникальных (по IP)</div></div>
  <div class="card kpi"><div class="n" id="k_act">—</div><div class="l">Активных партнёров</div></div>
</div>

<div class="grid">
  <div class="card"><b>Переходы по дням</b><canvas id="byDay" height="200"></canvas></div>
  <div class="card"><b>Топ-10 партнёров</b><canvas id="byTop" height="200"></canvas></div>
</div>

<div class="card">
  <b>Лидерборд партнёров</b> &nbsp; <a id="csv" href="#">скачать CSV</a>
  <table><thead><tr><th class="rk">#</th><th>Партнёр</th><th>Команда</th>
    <th>Всего</th><th>Раунд</th><th>Уник.</th><th>Последний</th></tr></thead>
  <tbody id="rows"></tbody></table>
</div>
<p class="foot">Обновляется каждые 30 сек. Метка = ссылка партнёра в био. 🔵 — команда A, 🩷 — команда B.</p>

<script>
const token=new URLSearchParams(location.search).get('token');
const q=token?('?token='+encodeURIComponent(token)):'';
document.getElementById('csv').href='/export'+q;
document.getElementById('arenaLink').href='/arena'+q;
let dayChart,topChart;
const medals=['🥇','🥈','🥉'];
function fmt(ts){return ts?ts.replace('T',' '):'—';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function load(){
  const r=await fetch('/api/stats'+q);
  if(!r.ok){updated.textContent='Нет доступа (проверь token)';return;}
  const d=await r.json();
  const A=d.teams[0], B=d.teams[1];
  k_total.textContent=d.total;k_uniq.textContent=d.unique;k_act.textContent=d.partners.length;
  updated.textContent='Обновлено: '+new Date().toLocaleString('ru-RU');
  const lead = A.clicks===B.clicks?null:(A.clicks>B.clicks?'A':'B');
  document.getElementById('teams').innerHTML=`
    <div class="team a">${lead==='A'?'<div class="crown">👑</div>':''}
      <div class="nm">🔵 ${esc(A.name)}</div><div class="big">${A.clicks}</div>
      <div class="tod">за раунд: <b>${A.today}</b></div><div class="pc">${A.partners} партнёров</div></div>
    <div class="vs">VS</div>
    <div class="team b">${lead==='B'?'<div class="crown">👑</div>':''}
      <div class="nm">🩷 ${esc(B.name)}</div><div class="big">${B.clicks}</div>
      <div class="tod">за раунд: <b>${B.today}</b></div><div class="pc">${B.partners} партнёров</div></div>`;
  const sum=A.clicks+B.clicks; document.getElementById('barA').style.width=(sum?(A.clicks/sum*100):50)+'%';
  const lt=d.leader_today;
  document.getElementById('banner').innerHTML = lt
    ? `🔥 Лидер раунда: <b>${esc(lt.source)}</b> — ${lt.clicks} переходов `+
      (lt.team?`<span class="badge ${lt.team.toLowerCase()}">${esc(lt.team==='A'?A.name:B.name)}</span>`:'')
    : '😴 В этом раунде переходов ещё не было — кто будет первым?';
  // таблица
  document.getElementById('rows').innerHTML=d.partners.map((p,i)=>{
    const tb=p.team?`<span class="badge ${p.team.toLowerCase()}">${esc(p.team==='A'?A.name:B.name)}</span>`:'<span class="mut">—</span>';
    return `<tr><td class="rk">${medals[i]||(i+1)}</td><td>${esc(p.source||'direct')}</td><td>${tb}</td>
      <td><b>${p.clicks}</b></td><td>${p.today}</td><td>${p.uniq}</td><td class="mut">${fmt(p.last)}</td></tr>`;
  }).join('')||'<tr><td colspan=7 class="mut">Пока нет данных</td></tr>';
  // графики
  const dl=d.by_day.map(x=>x.day),dc=d.by_day.map(x=>x.clicks);
  if(dayChart)dayChart.destroy();
  dayChart=new Chart(byDay,{type:'line',data:{labels:dl,datasets:[{data:dc,borderColor:'#4f8cff',
    backgroundColor:'rgba(79,140,255,.15)',fill:true,tension:.3}]},
    options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8aa0b8'}},y:{ticks:{color:'#8aa0b8'},beginAtZero:true}}}});
  const tp=d.partners.slice(0,10);
  if(topChart)topChart.destroy();
  topChart=new Chart(byTop,{type:'bar',data:{labels:tp.map(p=>p.source),datasets:[{data:tp.map(p=>p.clicks),
    backgroundColor:tp.map(p=>p.team==='B'?'#ff5c8a':(p.team==='A'?'#4f8cff':'#8aa0b8'))}]},
    options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8aa0b8'},beginAtZero:true},y:{ticks:{color:'#8aa0b8'}}}}});
}
load();setInterval(load,30000);
</script></body></html>"""


def main():
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("NeuroFans teams-tracker on :%s (bot=@%s, admins=%d, daily=%s, teams=%s/%s)",
             PORT, BOT_USERNAME, len(ADMIN_IDS), DAILY_SUMMARY_TIME or "off",
             TEAM_A_PREFIX, TEAM_B_PREFIX)
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

if __name__ == "__main__":
    main()
