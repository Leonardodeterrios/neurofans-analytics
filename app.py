#!/usr/bin/env python3
"""
NeuroFans — партнёрский трекер (две команды) + арена + сверка «клик → старт в боте».

Ссылки партнёров (метка = партнёр, путь = канал трафика):
  https://ДОМЕН/t/<метка>   — Threads
  https://ДОМЕН/v/<метка>   — видео (Shorts / Reels / TikTok)
  https://ДОМЕН/go/<метка>  — без канала  (старый вид /go?source=<метка> тоже работает)

Клик → пишем переход → 302 → t.me/<BOT>?start=<метка>__<id клика> → воронка в PuzzleBot.
PuzzleBot на /start шлёт GET /bot/start?p=<start-параметр>&uid=<telegram id>&key=<INGEST_TOKEN>.

Честный подсчёт силы:
  • боты и превью-краулеры не попадают в счётчик (их перекидываем туда же, что и людей);
  • удар засчитывается 1 раз на человека на партнёра за раунд;
  • один человек засчитывается максимум MAX_PARTNERS_PER_VISITOR партнёрам за раунд,
    с одного IP — не больше MAX_HITS_PER_IP ударов за раунд (защита от «распыления»).
Сила = засчитанные удары + START_POWER × новые люди, дошедшие до бота («апперкот»).
polling не запускаем: токен бота нужен только для уведомлений (sendMessage), с PuzzleBot не конфликтует.
"""
import os, io, csv, re, hmac, hashlib, sqlite3, threading, time, logging, datetime as dt
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from flask import Flask, jsonify, request, abort, redirect, Response
from dotenv import load_dotenv

load_dotenv()

def _env(name, default=""):
    """Значение из .env без хвостового комментария «  # ...» (цвета вида #19d3c5 не трогаем)."""
    return re.split(r"\s+#", os.getenv(name, default), maxsplit=1)[0].strip()

def _int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default

def _flag(name, default="1"):
    return _env(name, default).lower() not in ("0", "false", "no", "")

def _str(name, default=""):
    return _env(name, default)

BOT_TOKEN          = _str("BOT_TOKEN")
BOT_USERNAME       = _str("BOT_USERNAME", "AccessGrantedGuide_bot").lstrip("@")
ADMIN_CHAT_ID      = _str("ADMIN_CHAT_ID")      # один ID или несколько через запятую
ADMIN_IDS          = [x.strip() for x in ADMIN_CHAT_ID.replace(";", ",").split(",") if x.strip()]
DB_PATH            = _str("DB_PATH", "data/clicks.db")
DASHBOARD_TOKEN    = _str("DASHBOARD_TOKEN")    # полный доступ (дашборд, CSV)
PUBLIC_TOKEN       = _str("PUBLIC_TOKEN")       # только арена/рейтинг — для партнёров
INGEST_TOKEN       = _str("INGEST_TOKEN")       # ключ, с которым PuzzleBot шлёт /bot/start
PORT               = _int("DASHBOARD_PORT", 8080)
SHEET_WEBHOOK_URL  = _str("SHEET_WEBHOOK_URL")
NOTIFY_CLICKS      = _flag("NOTIFY_CLICKS", "1")
NOTIFY_STARTS      = _flag("NOTIFY_STARTS", "1")
DAILY_SUMMARY_TIME = _str("DAILY_SUMMARY_TIME")
SUMMARY_TZ         = _str("SUMMARY_TZ", "Asia/Bangkok") or "Asia/Bangkok"
SILENCE_HOURS      = _int("SILENCE_HOURS", 24)
SILENCE_CHECK_MIN  = _int("SILENCE_CHECK_MIN", 60)
SILENCE_MIN_CLICKS = _int("SILENCE_MIN_CLICKS", 3)
START_POWER        = _int("START_POWER", 3)
MAX_PARTNERS_PER_VISITOR = _int("MAX_PARTNERS_PER_VISITOR", 3)
MAX_HITS_PER_IP    = _int("MAX_HITS_PER_IP", 30)

TEAM_A_NAME   = _str("TEAM_A_NAME", "Команда Лёни")
TEAM_A_PREFIX = _str("TEAM_A_PREFIX", "leo").lower()
TEAM_A_COLOR  = _str("TEAM_A_COLOR", "#19d3c5")
TEAM_B_NAME   = _str("TEAM_B_NAME", "Команда Кати")
TEAM_B_PREFIX = _str("TEAM_B_PREFIX", "kate").lower()
TEAM_B_COLOR  = _str("TEAM_B_COLOR", "#ff4f8b")
TEAMS = {"A": (TEAM_A_NAME, TEAM_A_PREFIX, TEAM_A_COLOR),
         "B": (TEAM_B_NAME, TEAM_B_PREFIX, TEAM_B_COLOR)}
TEAM_EMOJI = {"A": "🌊", "B": "🌺"}
CHANNEL_NAMES = {"threads": "Threads", "video": "Видео (Shorts/Reels/TikTok)", "": "Без канала"}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# не люди: превью-краулеры соцсетей и мессенджеров, сканеры, скрипты, «безголовые» браузеры
BOT_UA = re.compile(
    r"bot\b|bot/|crawler|spider|preview|facebookexternalhit|facebookcatalog|meta-externalagent|"
    r"telegrambot|whatsapp|vkshare|slackbot|skypeuripreview|discordbot|twitterbot|embedly|quora link|"
    r"headless|python-requests|python-urllib|aiohttp|httpx|curl/|wget|go-http-client|okhttp|axios|"
    r"node-fetch|scrapy|libwww|java/|phantomjs|puppeteer|playwright|lighthouse|pingdom|uptimerobot|"
    r"statuscake|monitor", re.I)
SAFE_START = re.compile(r"[^A-Za-z0-9_-]")
SAFE_LABEL = re.compile(r"[^A-Za-z0-9_-]")
ARENA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arena.html")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neurofans")
app = Flask(__name__)


def team_of(source):
    s = (source or "").lower()
    if TEAM_A_PREFIX and s.startswith(TEAM_A_PREFIX): return "A"
    if TEAM_B_PREFIX and s.startswith(TEAM_B_PREFIX): return "B"
    return None

def clean_label(raw):
    s = SAFE_LABEL.sub("", raw or "")
    s = re.sub(r"_{2,}", "_", s).strip("_-")
    return s[:40] or "direct"

def b36(n):
    a, s = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while True:
        n, r = divmod(n, 36)
        s = a[r] + s
        if n == 0:
            return s

def parse_payload(raw):
    """'leo07__1a2b' -> ('leo07', 1a2b->int); 'leo07' -> ('leo07', None)."""
    raw = (raw or "").strip()
    if raw.lower().startswith("/start"):
        raw = raw[6:].strip()
    label_part, _, cid_part = raw.partition("__")
    cid = None
    if cid_part:
        try:
            cid = int(SAFE_START.sub("", cid_part)[:12], 36)
        except ValueError:
            cid = None
    return clean_label(label_part), cid


# ---------- раунды ----------
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

def _now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


# ---------- база ----------
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS clicks(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, ip TEXT, ua TEXT)""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(clicks)")}
        for col, ddl in (("vid", "TEXT"), ("scored", "INTEGER DEFAULT 1"),
                         ("rnd", "TEXT"), ("ch", "TEXT DEFAULT ''")):
            if col not in cols:
                c.execute(f"ALTER TABLE clicks ADD COLUMN {col} {ddl}")
        c.execute("""CREATE TABLE IF NOT EXISTS starts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, label TEXT, click_id INTEGER,
            tg_uid INTEGER, ch TEXT, scored INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, kind TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_src_vid ON clicks(source, vid, rnd)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_vid ON clicks(vid, rnd)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_ip ON clicks(ip, rnd)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_ts ON clicks(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_starts_uid ON starts(tg_uid)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_starts_ts ON starts(ts)")

def record_click(source, ch, ip, ua, vid):
    """Пишет клик. Возвращает (id клика, засчитан ли удар, новый ли человек)."""
    _start, end = round_bounds()
    rnd = _utc_z(end)
    with _conn() as c:
        dup = c.execute("SELECT 1 FROM clicks WHERE source=? AND vid=? AND rnd=? AND scored=1 LIMIT 1",
                        (source, vid, rnd)).fetchone()
        spread = c.execute("SELECT COUNT(DISTINCT source) FROM clicks WHERE vid=? AND rnd=? AND scored=1",
                           (vid, rnd)).fetchone()[0]
        ip_hits = c.execute("SELECT COUNT(*) FROM clicks WHERE ip=? AND rnd=? AND scored=1",
                            (ip, rnd)).fetchone()[0] if ip else 0
        scored = 0 if (dup or spread >= MAX_PARTNERS_PER_VISITOR or ip_hits >= MAX_HITS_PER_IP
                       or source == "direct") else 1
        new_vis = c.execute("SELECT 1 FROM clicks WHERE vid=? LIMIT 1", (vid,)).fetchone() is None
        cur = c.execute("INSERT INTO clicks(ts,source,ip,ua,vid,scored,rnd,ch) VALUES(?,?,?,?,?,?,?,?)",
                        (_now_iso(), source, ip, ua, vid, scored, rnd, ch))
        return cur.lastrowid, bool(scored), new_vis

def record_start(raw_payload, uid):
    """Старт бота из PuzzleBot. Засчитывается («апперкот»), если человек впервые пришёл в бота."""
    label, cid = parse_payload(raw_payload)
    ch, click_ok = "", False
    with _conn() as c:
        if cid is not None:
            row = c.execute("SELECT source, ch, scored FROM clicks WHERE id=?", (cid,)).fetchone()
            if row is None or row["source"] != label:
                cid = None
            else:
                ch, click_ok = row["ch"] or "", bool(row["scored"])
        # апперкот только за человека, который пришёл через засчитанный клик партнёра:
        # один старт на клик и один на Telegram-аккаунт. «Голые» t.me/...?start=метка не считаются.
        used = cid is not None and c.execute(
            "SELECT 1 FROM starts WHERE click_id=? AND scored=1 LIMIT 1", (cid,)).fetchone() is not None
        seen_uid = bool(uid) and c.execute(
            "SELECT 1 FROM starts WHERE tg_uid=? LIMIT 1", (uid,)).fetchone() is not None
        scored = 1 if (click_ok and not used and not seen_uid and label != "direct") else 0
        c.execute("INSERT INTO starts(ts,label,click_id,tg_uid,ch,scored) VALUES(?,?,?,?,?,?)",
                  (_now_iso(), label, cid, uid or None, ch, scored))
    return {"label": label, "click_id": cid, "ch": ch, "scored": bool(scored)}


# ---------- статистика ----------
def _conv(starts, hits):
    return round(starts / hits * 100, 1) if hits else None

def get_stats(at=None):
    """«today» во всех полях = текущий раунд. Сила = удары + START_POWER × старты."""
    start, end = round_bounds(at)
    s0, e0 = _utc_naive(start), _utc_naive(end)
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        hits_total = c.execute("SELECT COALESCE(SUM(scored),0) FROM clicks").fetchone()[0]
        uniq = c.execute("SELECT COUNT(DISTINCT COALESCE(vid, ip)) FROM clicks").fetchone()[0]
        starts_total = c.execute("SELECT COALESCE(SUM(scored),0) FROM starts").fetchone()[0]
        src = {r["source"]: r for r in c.execute(
            """SELECT source, COUNT(*) clicks, COALESCE(SUM(scored),0) hits,
                      COUNT(DISTINCT COALESCE(vid, ip)) uniq, MAX(ts) last
               FROM clicks GROUP BY source""")}
        hits_r = {r[0]: r[1] or 0 for r in c.execute(
            "SELECT source, SUM(scored) FROM clicks WHERE ts>=? AND ts<? GROUP BY source", (s0, e0))}
        st_all = {r[0]: r[1] or 0 for r in c.execute("SELECT label, SUM(scored) FROM starts GROUP BY label")}
        st_r = {r[0]: r[1] or 0 for r in c.execute(
            "SELECT label, SUM(scored) FROM starts WHERE ts>=? AND ts<? GROUP BY label", (s0, e0))}
        by_day = [{"day": r[0], "clicks": r[1] or 0} for r in c.execute(
            "SELECT substr(ts,1,10) day, SUM(scored) FROM clicks GROUP BY day ORDER BY day")]
        ch_clicks = {r[0]: (r[1], r[2] or 0) for r in c.execute(
            "SELECT COALESCE(ch,''), COUNT(*), SUM(scored) FROM clicks GROUP BY 1")}
        ch_starts = {r[0]: r[1] or 0 for r in c.execute(
            "SELECT COALESCE(ch,''), SUM(scored) FROM starts GROUP BY 1")}
    partners = []
    for label in (set(src) | set(st_all)) - {"direct"}:
        r = src.get(label)
        clicks, hits, uq, last = (r["clicks"], r["hits"], r["uniq"], r["last"]) if r else (0, 0, 0, None)
        st, hr, sr = st_all.get(label, 0), hits_r.get(label, 0), st_r.get(label, 0)
        partners.append({"source": label, "team": team_of(label), "clicks": clicks, "hits": hits,
                         "uniq": uq, "starts": st, "last": last,
                         "hits_today": hr, "starts_today": sr,
                         "today": hr + START_POWER * sr, "power": hits + START_POWER * st,
                         "conv": _conv(st, hits)})
    partners.sort(key=lambda p: (-p["power"], -p["clicks"], p["source"] or ""))
    teams = []
    for key, (name, _prefix, color) in TEAMS.items():
        mem = [p for p in partners if p["team"] == key]
        hits = sum(p["hits"] for p in mem); st = sum(p["starts"] for p in mem)
        teams.append({"key": key, "name": name, "color": color, "emoji": TEAM_EMOJI[key],
                      "clicks": sum(p["power"] for p in mem),          # сила за всё время
                      "today": sum(p["today"] for p in mem),           # сила за раунд
                      "hits": hits, "starts": st, "conv": _conv(st, hits),
                      "hits_today": sum(p["hits_today"] for p in mem),
                      "starts_today": sum(p["starts_today"] for p in mem),
                      "partners": len(mem)})
    lt = max((p for p in partners if p["team"]), key=lambda p: p["today"], default=None)
    leader_today = ({"source": lt["source"], "team": lt["team"], "clicks": lt["today"]}
                    if lt and lt["today"] > 0 else None)
    channels = []
    for ch in sorted(set(ch_clicks) | set(ch_starts), key=lambda x: (x == "", x)):
        cl, ht = ch_clicks.get(ch, (0, 0)); st = ch_starts.get(ch, 0)
        channels.append({"ch": ch, "name": CHANNEL_NAMES.get(ch, ch), "clicks": cl, "hits": ht,
                         "starts": st, "conv": _conv(st, ht)})
    return {"total": total, "hits": hits_total, "unique": uniq, "starts": starts_total,
            "conv": _conv(starts_total, hits_total), "start_power": START_POWER,
            "teams": teams, "partners": partners, "leader_today": leader_today,
            "by_day": by_day, "channels": channels,
            "round_start": _utc_z(start), "round_end": _utc_z(end),
            "server_now": _utc_z(dt.datetime.now(dt.timezone.utc))}

def _pct(v):
    return "—" if v is None else f"{v:g}%"

def _plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14: return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many

def stats_text() -> str:
    st = get_stats()
    if st["total"] == 0 and st["starts"] == 0:
        return "📊 Пока нет переходов."
    a, b = st["teams"]
    lines = [f"📊 Кликов: {st['total']} · засчитано ударов: {st['hits']} · в бот: {st['starts']} "
             f"(доходимость {_pct(st['conv'])})", "",
             f"🌊 {a['name']}: сила {a['clicks']} (в раунде {a['today']}), доходимость {_pct(a['conv'])}",
             f"🌺 {b['name']}: сила {b['clicks']} (в раунде {b['today']}), доходимость {_pct(b['conv'])}",
             "", "Топ партнёров:"]
    for i, p in enumerate(st["partners"][:10], 1):
        lines.append(f"{i}. {p['source']}: сила {p['power']} (удары {p['hits']}, в бот {p['starts']}, {_pct(p['conv'])})")
    return "\n".join(lines)

def daily_results_text() -> str:
    at = dt.datetime.now(ZoneInfo(SUMMARY_TZ)) - dt.timedelta(minutes=2)   # раунд, который только что закончился
    st = get_stats(at)
    _start, end = round_bounds(at)
    a, b = st["teams"]
    def line(t):
        conv = _conv(t["starts_today"], t["hits_today"])
        return (f"{t['emoji']} {t['name']}: сила {t['today']} — {t['hits_today']} "
                f"{_plural(t['hits_today'], 'удар', 'удара', 'ударов')} + {t['starts_today']} "
                f"{_plural(t['starts_today'], 'апперкот', 'апперкота', 'апперкотов')}, доходимость {_pct(conv)}")
    lines = [f"🥊 Гонг! Итоги раунда {end.strftime('%d.%m.%Y')}", "", line(a), line(b), ""]
    if a["today"] != b["today"]:
        win = a if a["today"] > b["today"] else b
        lines.append(f"👑 Раунд выиграла {win['name']}! 🎉")
    elif a["today"] == 0:
        lines.append("В этом раунде ударов не было.")
    else:
        lines.append("🤝 Ничья!")
    lt = st["leader_today"]
    if lt:
        lines.append(f"🥇 Лучший боец раунда: {lt['source']} (сила {lt['clicks']})")
    lines.append("\nНовый раунд уже начался — до следующего гонга 24 часа 💪")
    return "\n".join(lines)

def csv_bytes() -> bytes:
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["ts", "source", "team", "channel", "scored", "ip", "ua", "visitor"])
    with _conn() as c:
        for r in c.execute("SELECT ts,source,ch,scored,ip,ua,vid FROM clicks ORDER BY id"):
            tk = team_of(r["source"])
            w.writerow([r["ts"], r["source"], TEAMS[tk][0] if tk else "", r["ch"] or "", r["scored"],
                        r["ip"], r["ua"], r["vid"] or ""])
    return buf.getvalue().encode("utf-8-sig")

def starts_csv_bytes() -> bytes:
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["ts", "label", "team", "channel", "click_id", "telegram_id", "scored"])
    with _conn() as c:
        for r in c.execute("SELECT ts,label,ch,click_id,tg_uid,scored FROM starts ORDER BY id"):
            tk = team_of(r["label"])
            w.writerow([r["ts"], r["label"], TEAMS[tk][0] if tk else "", r["ch"] or "",
                        r["click_id"] or "", r["tg_uid"] or "", r["scored"]])
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
    for cid in ADMIN_IDS:
        tg_send(text, cid)

def _team_label(source):
    tk = team_of(source)
    return f"{TEAM_EMOJI[tk]} {TEAMS[tk][0]}" if tk else "без команды"

def notify_hit(source, ch, is_new):
    if not (ADMIN_IDS and NOTIFY_CLICKS):
        return
    flag = "🆕 новый человек" if is_new else "↩️ уже заходил раньше"
    chs = f"\nКанал: {CHANNEL_NAMES.get(ch, ch)}" if ch else ""
    notify_all(f"🥊 Удар! ({flag})\nПартнёр: {source}\nКоманда: {_team_label(source)}{chs}")

def notify_start(label, ch):
    if not (ADMIN_IDS and NOTIFY_STARTS):
        return
    chs = f"\nКанал: {CHANNEL_NAMES.get(ch, ch)}" if ch else ""
    notify_all(f"💥 Апперкот! +{START_POWER} к силе\n{label} привёл нового человека в бота\n"
               f"Команда: {_team_label(label)}{chs}")

def push_sheet(payload):
    if not SHEET_WEBHOOK_URL:
        return
    try:
        httpx.post(SHEET_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        log.error("sheet push failed: %s", e)


# ---------- алерты «тишина по партнёру» ----------
def record_alert(source, kind):
    with _conn() as c:
        c.execute("INSERT INTO alerts(ts,source,kind) VALUES(?,?,?)", (_now_iso(), source, kind))

def silent_sources():
    now = dt.datetime.utcnow()
    threshold = now - dt.timedelta(hours=SILENCE_HOURS)
    out = []
    with _conn() as c:
        rows = c.execute("SELECT source, MAX(ts) last, COUNT(*) total FROM clicks "
                         "WHERE scored=1 GROUP BY source").fetchall()
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
                   f"Нет засчитанных ударов уже ~{int(hours)} ч (последний: {last} UTC).\n"
                   f"Всего было {total}. Проверь ссылку в био / статус аккаунта.")
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
                nowtz = dt.datetime.now(ZoneInfo(SUMMARY_TZ))
                hh, mm = _round_hm()
                if nowtz.hour == hh and nowtz.minute == mm and last_summary_date != nowtz.date():
                    last_summary_date = nowtz.date()
                    notify_all(daily_results_text())
            except Exception as e:
                log.error("daily summary failed: %s", e)
        time.sleep(30)


# ---------- веб ----------
def _client_ip():
    for h in ("CF-Connecting-IP", "X-Real-IP"):
        v = request.headers.get(h, "")
        if v:
            return v.strip()
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "")

def _is_https():
    return (request.headers.get("X-Forwarded-Proto", "") == "https"
            or '"https"' in request.headers.get("CF-Visitor", "")
            or bool(getattr(request, "is_secure", False)))

def _visitor(ip, ua):
    """ID человека: cookie, а если её нет — отпечаток IP+браузер. Возвращает (id, нужно ли ставить cookie)."""
    v = request.cookies.get("nf_vid", "") if hasattr(request, "cookies") else ""
    if v and re.fullmatch(r"[a-f0-9]{16,40}", v):
        return v, False
    return hashlib.sha1(f"{ip}|{ua}".encode()).hexdigest()[:20], True

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

def _track_and_redirect(raw, ch):
    source = clean_label(raw)
    ua = (request.headers.get("User-Agent", "") or "")[:300]
    payload = SAFE_START.sub("", source)[:40] if source != "direct" else ""
    plain = f"https://t.me/{BOT_USERNAME}" + (f"?start={quote(payload)}" if payload else "")
    if not ua.strip() or BOT_UA.search(ua):
        resp = redirect(plain, code=302)          # не человек: в счётчик не пишем, ведём туда же
        resp.headers["Cache-Control"] = "no-store"
        return resp
    ip = _client_ip()
    vid, set_cookie = _visitor(ip, ua)
    cid, scored, new_vis = record_click(source, ch, ip, ua, vid)
    if scored:
        notify_hit(source, ch, new_vis)
        push_sheet({"type": "hit", "source": source, "channel": ch, "visitor": vid})
    log.info("click source=%s ch=%s scored=%s", source, ch or "-", scored)
    target = f"https://t.me/{BOT_USERNAME}" + (f"?start={quote(payload + '__' + b36(cid))}" if payload else "")
    resp = redirect(target, code=302)
    resp.headers["Cache-Control"] = "no-store"
    if set_cookie:
        resp.set_cookie("nf_vid", vid, max_age=31536000, path="/", samesite="Lax",
                        secure=_is_https(), httponly=True)
    return resp

@app.route("/go")
def go():
    return _track_and_redirect(request.values.get("source") or request.values.get("s") or "", "")

@app.route("/go/<label>")
def go_label(label):
    return _track_and_redirect(label, "")

@app.route("/t/<label>")
def go_threads(label):
    return _track_and_redirect(label, "threads")

@app.route("/v/<label>")
def go_video(label):
    return _track_and_redirect(label, "video")

@app.route("/bot/start", methods=["GET", "POST"])
def bot_start():
    """Вызывает PuzzleBot на /start. Ключ обязателен, иначе апперкоты можно было бы подделать."""
    if not INGEST_TOKEN or not hmac.compare_digest(request.values.get("key", ""), INGEST_TOKEN):
        abort(403)
    raw = (request.values.get("p") or request.values.get("start") or request.values.get("payload") or "")
    try:
        uid = int(re.sub(r"\D", "", request.values.get("uid") or request.values.get("user_id") or "") or 0)
    except ValueError:
        uid = 0
    res = record_start(raw, uid)
    if res["scored"]:
        notify_start(res["label"], res["ch"])
        push_sheet({"type": "start", "source": res["label"], "channel": res["ch"]})
    log.info("start label=%s cid=%s scored=%s", res["label"], res["click_id"], res["scored"])
    return jsonify(ok=True, **res)

@app.route("/api/stats")
def api_stats():
    role = _need()
    st = get_stats()
    st["role"] = role
    return jsonify(st)

@app.route("/api/feed")
def api_feed():
    """Лента для арены: засчитанные удары и апперкоты. Без since — только курсоры (историю не проигрываем)."""
    _need()
    sc = request.args.get("since_c", request.args.get("since"))
    ss = request.args.get("since_s")
    with _conn() as c:
        last_c = c.execute("SELECT COALESCE(MAX(id),0) FROM clicks").fetchone()[0]
        last_s = c.execute("SELECT COALESCE(MAX(id),0) FROM starts").fetchone()[0]
        items = []
        def _i(v, d):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d
        if sc is not None:
            for r in c.execute("SELECT id, ts, source FROM clicks WHERE id>? AND scored=1 ORDER BY id LIMIT 300",
                               (_i(sc, last_c),)):
                items.append({"kind": "hit", "id": r["id"], "ts": r["ts"], "source": r["source"],
                              "team": team_of(r["source"])})
        if ss is not None:
            for r in c.execute("SELECT id, ts, label FROM starts WHERE id>? AND scored=1 ORDER BY id LIMIT 300",
                               (_i(ss, last_s),)):
                items.append({"kind": "start", "id": r["id"], "ts": r["ts"], "source": r["label"],
                              "team": team_of(r["label"])})
    items.sort(key=lambda x: x["ts"])
    return jsonify(last_c=last_c, last_s=last_s, last_id=last_c, items=items)

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

@app.route("/export/starts")
def export_starts():
    _need(admin=True)
    return Response(starts_csv_bytes(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=starts.csv"})

@app.route("/")
def index():
    _need(admin=True)
    return Response(PAGE, mimetype="text/html")

@app.route("/healthz")
def healthz():
    return "ok"

PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NeuroFans — дашборд партнёров</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{--card:rgba(10,38,44,.6);--line:rgba(255,255,255,.09);--txt:#fff7ec;--mut:#a9c2bf;--a:#19d3c5;--b:#ff4f8b;--gold:#ffd166}
*{box-sizing:border-box}body{margin:0;color:var(--txt);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:22px;max-width:1150px;margin:0 auto;
background:radial-gradient(900px 500px at 10% -10%,rgba(255,122,89,.25),transparent 60%),radial-gradient(900px 600px at 100% 0%,rgba(255,79,139,.2),transparent 60%),linear-gradient(180deg,#0b2430,#04120f) fixed}
h1{font-size:22px;margin:0 0 2px}.sub{color:var(--mut);margin:0 0 18px;font-size:13px}
a{color:var(--a)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px}
.teams{display:grid;grid-template-columns:1fr auto 1fr;gap:14px;margin-bottom:12px}
.team{border-radius:16px;padding:18px;position:relative}
.team.a{background:linear-gradient(135deg,rgba(25,211,197,.2),rgba(25,211,197,.05));border:1px solid rgba(25,211,197,.45)}
.team.b{background:linear-gradient(135deg,rgba(255,79,139,.2),rgba(255,79,139,.05));border:1px solid rgba(255,79,139,.45)}
.team .nm{font-size:15px;color:var(--mut)}.team .big{font-size:44px;font-weight:800;line-height:1.05;margin:4px 0}
.team .row{font-size:13px;color:#d8ece9}.team .row b{color:#fff}
.crown{position:absolute;top:12px;right:14px;font-size:24px}
.vs{display:flex;align-items:center;justify-content:center;font-weight:800;color:var(--mut);font-size:18px}
.bar{height:12px;border-radius:8px;background:var(--b);overflow:hidden;margin:0 0 14px}.bar>i{display:block;height:100%;background:var(--a)}
.banner{background:linear-gradient(135deg,rgba(255,209,102,.18),rgba(255,209,102,.04));border:1px solid rgba(255,209,102,.45);border-radius:14px;padding:12px 16px;margin-bottom:14px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
.kpi .n{font-size:26px;font-weight:700}.kpi .l{color:var(--mut);font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:820px){.grid,.teams{grid-template-columns:1fr}.vs{display:none}.kpis{grid-template-columns:1fr 1fr}}
table{width:100%;border-collapse:collapse;margin-top:8px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);font-size:14px;white-space:nowrap}
th{color:var(--mut);font-weight:600}
.tw{overflow-x:auto}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px}
.badge.a{background:rgba(25,211,197,.18);color:#8ff7ec}.badge.b{background:rgba(255,79,139,.2);color:#ffc2d6}
.cv{font-weight:700}.cv.hi{color:#7dffb0}.cv.mid{color:var(--gold)}.cv.lo{color:#ff9a9a}
.rk{width:34px;font-weight:700}.mut{color:var(--mut)}
.foot{color:var(--mut);font-size:12px;margin-top:14px}
</style></head><body>
<h1>🏆 NeuroFans — соревнование команд &nbsp;<a id="arenaLink" href="/arena" style="font-size:14px">🥊 Открыть арену</a></h1>
<p class="sub" id="updated">загрузка…</p>
<div class="teams" id="teams"></div>
<div class="bar"><i id="barA" style="width:50%"></i></div>
<div class="banner" id="banner">—</div>
<div class="kpis">
  <div class="card kpi"><div class="n" id="k_total">—</div><div class="l">Кликов всего</div></div>
  <div class="card kpi"><div class="n" id="k_hits">—</div><div class="l">Засчитано ударов (уник. люди)</div></div>
  <div class="card kpi"><div class="n" id="k_starts">—</div><div class="l">Новых людей в боте</div></div>
  <div class="card kpi"><div class="n" id="k_conv">—</div><div class="l">Доходимость клик → бот</div></div>
</div>
<div class="grid">
  <div class="card"><b>Удары по дням</b><canvas id="byDay" height="200"></canvas></div>
  <div class="card"><b>Топ-10 по силе</b><canvas id="byTop" height="200"></canvas></div>
</div>
<div class="card" style="margin-bottom:14px">
  <b>Каналы трафика</b>
  <div class="tw"><table><thead><tr><th>Канал</th><th>Клики</th><th>Удары</th><th>В бот</th><th>Доходимость</th></tr></thead><tbody id="chRows"></tbody></table></div>
</div>
<div class="card">
  <b>Лидерборд партнёров</b> &nbsp; <a id="csv" href="#">CSV кликов</a> · <a id="csvS" href="#">CSV стартов</a>
  <div class="tw"><table><thead><tr><th class="rk">#</th><th>Партнёр</th><th>Команда</th><th>Сила</th><th>Удары</th><th>В бот</th>
    <th>Доходимость</th><th>Раунд</th><th>Клики</th><th>Последний</th></tr></thead>
  <tbody id="rows"></tbody></table></div>
</div>
<p class="foot">Обновляется каждые 30 сек. Удар = уникальный человек по ссылке партнёра (1 раз за раунд). Апперкот = человек дошёл до бота (+<span id="sp">3</span> к силе).
Доходимость = сколько людей из засчитанных ударов нажали «Старт» в боте.</p>
<script>
const token=new URLSearchParams(location.search).get('token');
const q=token?('?token='+encodeURIComponent(token)):'';
document.getElementById('csv').href='/export'+q;document.getElementById('csvS').href='/export/starts'+q;
document.getElementById('arenaLink').href='/arena'+q;
let dayChart,topChart;const medals=['🥇','🥈','🥉'];
function fmt(ts){return ts?ts.replace('T',' '):'—';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function cv(v){if(v==null)return '<span class="mut">—</span>';const c=v>=25?'hi':(v>=10?'mid':'lo');return `<span class="cv ${c}">${v}%</span>`;}
function tb(t,A,B){return t?`<span class="badge ${t.toLowerCase()}">${esc(t==='A'?A.name:B.name)}</span>`:'<span class="mut">—</span>';}
async function load(){
  const r=await fetch('/api/stats'+q);
  if(!r.ok){updated.textContent='Нет доступа (проверь token)';return;}
  const d=await r.json();const A=d.teams[0],B=d.teams[1];
  sp.textContent=d.start_power;
  k_total.textContent=d.total;k_hits.textContent=d.hits;k_starts.textContent=d.starts;k_conv.innerHTML=cv(d.conv);
  updated.textContent='Обновлено: '+new Date().toLocaleString('ru-RU');
  const lead=A.clicks===B.clicks?null:(A.clicks>B.clicks?'A':'B');
  const card=(t,cls,emo)=>`<div class="team ${cls}">${lead===t.key?'<div class="crown">👑</div>':''}
    <div class="nm">${emo} ${esc(t.name)} · ${t.partners} партнёров</div><div class="big">${t.clicks}</div>
    <div class="row">сила за раунд: <b>${t.today}</b> · удары: <b>${t.hits}</b> · в бот: <b>${t.starts}</b> · доходимость: ${cv(t.conv)}</div></div>`;
  document.getElementById('teams').innerHTML=card(A,'a','🌊')+'<div class="vs">VS</div>'+card(B,'b','🌺');
  const sum=A.clicks+B.clicks;document.getElementById('barA').style.width=(sum?(A.clicks/sum*100):50)+'%';
  const lt=d.leader_today;
  document.getElementById('banner').innerHTML=lt?`🔥 Лидер раунда: <b>${esc(lt.source)}</b> — сила ${lt.clicks} `+tb(lt.team,A,B)
    :'😴 В этом раунде ударов ещё не было — кто будет первым?';
  document.getElementById('chRows').innerHTML=(d.channels||[]).map(c=>`<tr><td>${esc(c.name)}</td><td>${c.clicks}</td><td>${c.hits}</td><td>${c.starts}</td><td>${cv(c.conv)}</td></tr>`).join('')
    ||'<tr><td colspan=5 class="mut">Пока нет данных</td></tr>';
  document.getElementById('rows').innerHTML=d.partners.map((p,i)=>`<tr><td class="rk">${medals[i]||(i+1)}</td><td>${esc(p.source)}</td><td>${tb(p.team,A,B)}</td>
    <td><b>${p.power}</b></td><td>${p.hits}</td><td>${p.starts}</td><td>${cv(p.conv)}</td><td>${p.today}</td><td class="mut">${p.clicks}</td><td class="mut">${fmt(p.last)}</td></tr>`).join('')
    ||'<tr><td colspan=10 class="mut">Пока нет данных</td></tr>';
  if(window.Chart){
    const dl=d.by_day.map(x=>x.day),dc=d.by_day.map(x=>x.clicks);
    if(dayChart)dayChart.destroy();
    dayChart=new Chart(byDay,{type:'line',data:{labels:dl,datasets:[{data:dc,borderColor:'#19d3c5',backgroundColor:'rgba(25,211,197,.15)',fill:true,tension:.3}]},
      options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#a9c2bf'}},y:{ticks:{color:'#a9c2bf'},beginAtZero:true}}}});
    const tp=d.partners.slice(0,10);
    if(topChart)topChart.destroy();
    topChart=new Chart(byTop,{type:'bar',data:{labels:tp.map(p=>p.source),datasets:[{data:tp.map(p=>p.power),
      backgroundColor:tp.map(p=>p.team==='B'?'#ff4f8b':(p.team==='A'?'#19d3c5':'#8aa0b8'))}]},
      options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#a9c2bf'},beginAtZero:true},y:{ticks:{color:'#a9c2bf'}}}}});
  }
}
load();setInterval(load,30000);
</script></body></html>"""


def main():
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("NeuroFans on :%s (bot=@%s, admins=%d, daily=%s, teams=%s/%s, starts=%s)",
             PORT, BOT_USERNAME, len(ADMIN_IDS), DAILY_SUMMARY_TIME or "off",
             TEAM_A_PREFIX, TEAM_B_PREFIX, "on" if INGEST_TOKEN else "off")
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

if __name__ == "__main__":
    main()
