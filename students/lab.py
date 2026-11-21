"""NeuroFans Lab — кабинет учеников (отдельный сервис, своя база, свой контейнер).

Ученик по инвайту заводит кабинет на lab.<домен>, добавляет модели (куда вести: Fanvue,
Telegram, сайт…) и получает ссылки на каждый канал вида https://to.<домен>/<метка>.
Клик по ссылке → запись в SQLite → 302 на ссылку модели. Боты и превью не считаются,
но получают тот же редирект. Лига недели: очки = уникальные люди на модель за неделю.

Запуск локально:  LAB_DATA=/tmp/lab python3 students/lab.py  → http://127.0.0.1:8090
"""
import os, re, json, time, gzip, hmac, sqlite3, secrets, hashlib, ipaddress, logging, threading
import datetime as dt
import urllib.request
from contextlib import closing
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, redirect, abort, Response, send_from_directory

import lab_game as game
import lab_ops as ops
import lab_core
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lab")

def _env(k, d=""):
    v = os.environ.get(k, d)
    return (v.split("#")[0].strip() if v else d) or d

HERE      = os.path.dirname(os.path.abspath(__file__))
DATA      = _env("LAB_DATA", os.path.join(HERE, "..", "lab-data"))
DB_PATH   = os.path.join(DATA, "lab.db")
BACKUP_DIR = os.path.join(DATA, "backups")
GEO_DB    = os.path.join(DATA, "geo-city.mmdb")
ROOT_HOST = _env("ROOT_HOST", "neurofans.ru").lower()
LAB_HOST  = _env("LAB_HOST", "lab." + ROOT_HOST).lower()
LINK_HOST = _env("LINK_HOST", "to." + ROOT_HOST).lower()
TZ        = ZoneInfo(_env("LAB_TZ", "Europe/Moscow"))
PORT      = int(_env("LAB_PORT", "8090"))
GEO_ENABLE         = _env("GEO_ENABLE", "1") == "1"
MAX_MODELS_PER_VIS = int(_env("MAX_MODELS_PER_VISITOR", "3"))   # один человек — максимум 3 моделям за неделю
MAX_HITS_PER_IP    = int(_env("MAX_HITS_PER_IP", "100"))        # и не больше 100 очков с одного IP за неделю
LAB_BOT_TOKEN = _env("LAB_BOT_TOKEN")              # отдельный бот кабинета (не PuzzleBot); пусто — уведомления выключены
LAB_BOT_USERNAME = _env("LAB_BOT_USERNAME")
MAX_MODELS_PER_USER = 10
MAX_LINKS_PER_MODEL = 20
STATIC_DIR = next((p for p in (os.path.join(HERE, "static"), os.path.join(HERE, "..", "static"))
                   if os.path.isdir(p)), os.path.join(HERE, "static"))
PAGE_PATH  = os.path.join(HERE, "lab.html")

os.makedirs(DATA, exist_ok=True)

def _secret_file(name, gen):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(gen())
        os.chmod(p, 0o600)
    return open(p).read().strip()

SECRET      = _secret_file("secret.key", lambda: secrets.token_hex(32))
ADMIN_TOKEN = _env("LAB_ADMIN_TOKEN") or _secret_file("admin_token.txt", lambda: secrets.token_urlsafe(18))

# Каналы трафика: код → (название, суффикс метки)
CHANNELS = {
    "threads":   ("Threads", "t"),
    "instagram": ("Instagram", "i"),
    "tiktok":    ("TikTok", "tt"),
    "youtube":   ("YouTube", "y"),
    "telegram":  ("Telegram", "tg"),
    "x":         ("X / Twitter", "x"),
    "reddit":    ("Reddit", "r"),
    "other":     ("Другое", "o"),
}
RESERVED = {"api", "admin", "static", "r", "robots.txt", "favicon.ico", "lab", "to", "www", "login"}

BOT_UA = re.compile(
    r"(?<!cu)bot\b|(?<!cu)bot/|crawler|spider|preview|facebookexternalhit|facebookcatalog|meta-externalagent|"
    r"telegrambot|whatsapp|vkshare|slackbot|skypeuripreview|discordbot|twitterbot|embedly|quora link|"
    r"headless|python-requests|python-urllib|aiohttp|httpx|curl/|wget|go-http-client|okhttp|axios|"
    r"node-fetch|scrapy|libwww|java/|phantomjs|puppeteer|playwright|lighthouse|pingdom|uptimerobot|"
    r"statuscake|monitor", re.I)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
NICK_RE = re.compile(r"^[\w.\-]{3,24}$", re.U)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


# ---------- время и недели ----------
def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def local(t=None):
    return (t or now_utc()).astimezone(TZ)

def week_key(t=None):
    y, w, _ = local(t).isocalendar()
    return f"{y}-W{w:02d}"

def week_bounds(t=None):
    l = local(t)
    start = (l - dt.timedelta(days=l.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=7)

def prev_week_key(t=None):
    return week_key((t or now_utc()) - dt.timedelta(days=7))


# ---------- база ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, nick TEXT UNIQUE COLLATE NOCASE,
  pw TEXT, created TEXT, invite TEXT, blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY, note TEXT, created TEXT,
  uses INTEGER DEFAULT 0, max_uses INTEGER DEFAULT 1, used_by TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions(tok TEXT PRIMARY KEY, uid INTEGER, created TEXT, seen TEXT);
CREATE TABLE IF NOT EXISTS models(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT,
  slug TEXT UNIQUE COLLATE NOCASE, target TEXT, created TEXT, archived INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS links(id INTEGER PRIMARY KEY AUTOINCREMENT, mid INTEGER, uid INTEGER, ch TEXT,
  slug TEXT UNIQUE COLLATE NOCASE, target TEXT DEFAULT '', note TEXT DEFAULT '', created TEXT, archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS clicks(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, wk TEXT, day TEXT, hr INTEGER,
  lid INTEGER, mid INTEGER, uid INTEGER, ch TEXT, vid TEXT, iph TEXT, dev TEXT, app TEXT,
  city TEXT, cc TEXT, lat REAL, lon REAL, scored INTEGER, why TEXT);
CREATE TABLE IF NOT EXISTS own_vids(vid TEXT PRIMARY KEY, uid INTEGER, seen TEXT);
CREATE TABLE IF NOT EXISTS link_checks(id INTEGER PRIMARY KEY AUTOINCREMENT, lid INTEGER, uid INTEGER, ts TEXT,
  dev TEXT, app TEXT);
CREATE TABLE IF NOT EXISTS badge_log(mid INTEGER, badge TEXT, earned TEXT, PRIMARY KEY(mid, badge));
CREATE TABLE IF NOT EXISTS notify(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, key TEXT, kind TEXT, text TEXT,
  created TEXT, sent_at TEXT, status TEXT, UNIQUE(uid, key));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS conv(mid INTEGER, wk TEXT, subs INTEGER, updated TEXT, PRIMARY KEY(mid, wk));
CREATE INDEX IF NOT EXISTS ix_notify_status ON notify(status, id);
CREATE INDEX IF NOT EXISTS ix_checks_lid ON link_checks(lid);
CREATE INDEX IF NOT EXISTS ix_clicks_mid_wk ON clicks(mid, wk);
CREATE INDEX IF NOT EXISTS ix_clicks_uid_ts ON clicks(uid, ts);
CREATE INDEX IF NOT EXISTS ix_clicks_wk_vid ON clicks(wk, vid);
CREATE INDEX IF NOT EXISTS ix_clicks_wk_iph ON clicks(wk, iph);
CREATE INDEX IF NOT EXISTS ix_clicks_lid ON clicks(lid);
CREATE INDEX IF NOT EXISTS ix_links_mid ON links(mid);
CREATE INDEX IF NOT EXISTS ix_models_uid ON models(uid);
"""
_wlock = threading.Lock()

def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    with closing(db()) as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        for col, ddl in (("tg_chat_id", "INTEGER"), ("tg_mode", "TEXT DEFAULT 'all'"),
                         ("tg_code", "TEXT"), ("tg_code_exp", "TEXT"), ("public_ok", "INTEGER DEFAULT 0")):
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        c.commit()

def iso(t=None):
    return (t or now_utc()).isoformat(timespec="seconds")


# ---------- утилиты ----------
TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a","b","v","g","d","e","e","zh","z","i","y","k","l","m","n","o","p","r","s","t","u","f","h","ts","ch","sh","sch","","y","","e","yu","ya"]))

def slugify(s, n=20):
    s = "".join(TRANSLIT.get(ch, ch) for ch in (s or "").lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:n].strip("-") or "model"

def clean_target(u):
    u = (u or "").strip()
    if len(u) > 500 or re.search(r"\s", u):
        return None
    if not re.match(r"^https?://", u, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", u, re.I):
            u = "https://" + u                    # «fanvue.com/anna» → https://fanvue.com/anna
        else:
            return None
    p = urlsplit(u)
    host = (p.hostname or "").lower()
    if not host or "." not in host or host == LINK_HOST:
        return None
    return u

def client_ip():
    """X-Real-IP — только от нашего caddy (внутренний адрес), иначе адрес соединения."""
    ra = (request.remote_addr or "").strip()
    try:
        private = ipaddress.ip_address(ra).is_private
    except ValueError:
        private = False
    if private:
        v = request.headers.get("X-Real-IP", "").strip()
        if v:
            return v
    return ra

def ip_hash(ip):
    return hmac.new(SECRET.encode(), (ip or "").encode(), hashlib.sha256).hexdigest()[:24]

def parse_ua(ua):
    u = ua.lower()
    dev = "iOS" if re.search(r"iphone|ipad|ipod", u) else "Android" if "android" in u else "Desktop"
    if "barcelona" in u or "threads" in u: a = "Threads"
    elif "instagram" in u: a = "Instagram"
    elif re.search(r"musical_ly|bytedancewebview|tiktok|trill", u): a = "TikTok"
    elif "telegram" in u: a = "Telegram"
    elif re.search(r"fban|fbav|fb_iab", u): a = "Facebook"
    elif "twitter" in u: a = "X"
    elif "reddit" in u: a = "Reddit"
    elif "youtube" in u: a = "YouTube"
    else: a = "Браузер"
    return dev, a

def is_https():
    return request.headers.get("X-Forwarded-Proto", "") == "https" or request.is_secure or \
        request.host.split(":")[0].endswith(ROOT_HOST)

def cookie_domain():
    h = request.host.split(":")[0].lower()
    return "." + ROOT_HOST if h == ROOT_HOST or h.endswith("." + ROOT_HOST) else None

_rl = {}
def rate_limited(key, limit=20, window=900):
    t = time.time()
    lst = [x for x in _rl.get(key, []) if t - x < window]
    lst.append(t)
    _rl[key] = lst
    if len(_rl) > 5000:
        for k in list(_rl)[:2500]:
            _rl.pop(k, None)
    return len(lst) > limit


# ---------- гео (DB-IP Lite, CC BY 4.0): только город и координаты 0,1° ----------
_geo = {"r": None, "tried": 0.0}

def geo_lookup(ip):
    if not GEO_ENABLE or not os.path.exists(GEO_DB) or not ip:
        return None
    if _geo["r"] is None and time.time() - _geo["tried"] > 60:
        _geo["tried"] = time.time()
        try:
            import maxminddb
            _geo["r"] = maxminddb.open_database(GEO_DB)
        except Exception as e:
            log.warning("geo open failed: %s", e)
    r = _geo["r"]
    if not r:
        return None
    try:
        d = r.get(ip) or {}
    except Exception:
        return None
    loc = d.get("location") or {}
    if loc.get("latitude") is None:
        return None
    names = (d.get("city") or {}).get("names") or {}
    return {"city": (names.get("ru") or names.get("en") or "")[:60],
            "cc": ((d.get("country") or {}).get("iso_code") or "")[:2],
            "lat": round(float(loc["latitude"]), 1), "lon": round(float(loc["longitude"]), 1)}

def geo_updater():
    while GEO_ENABLE:
        try:
            fresh = os.path.exists(GEO_DB) and time.time() - os.path.getmtime(GEO_DB) < 35 * 86400
            if not fresh:
                today = dt.date.today()
                for m in (today, today.replace(day=1) - dt.timedelta(days=1)):
                    url = f"https://download.db-ip.com/free/dbip-city-lite-{m:%Y-%m}.mmdb.gz"
                    tmp = GEO_DB + ".tmp"
                    try:
                        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as f:
                            d = gzip.decompressobj(16 + 15)
                            while True:
                                chunk = resp.read(1 << 16)
                                if not chunk:
                                    break
                                f.write(d.decompress(chunk))
                            f.write(d.flush())
                    except Exception:
                        continue
                    os.replace(tmp, GEO_DB)
                    _geo["r"] = None; _geo["tried"] = 0.0
                    log.info("geo db updated from %s", url)
                    break
        except Exception as e:
            log.warning("geo update failed: %s", e)
        time.sleep(86400)


# ---------- сессии ----------
def current_user():
    tok = request.cookies.get("nfs", "")
    if not tok or len(tok) > 80:
        return None
    with closing(db()) as c:
        r = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.uid WHERE s.tok=?", (tok,)).fetchone()
        if r and not r["blocked"]:
            c.execute("UPDATE sessions SET seen=? WHERE tok=?", (iso(), tok)); c.commit()
            return r
    return None

def need_user():
    u = current_user()
    if not u:
        abort(401)
    return u

ADMIN_UID = -1                 # сессии админа лежат в той же таблице sessions с uid=-1

def is_admin():
    """Админ = заголовок X-Admin-Token (для скриптов/тестов) или сессия после входа по паролю.
    Токен в адресной строке больше не принимается: он оседал в истории браузера и логах."""
    tok = request.headers.get("X-Admin-Token", "")
    if tok and hmac.compare_digest(tok, ADMIN_TOKEN):
        return True
    st = request.cookies.get("nfa", "")
    if not st or len(st) > 80:
        return False
    with closing(db()) as c:
        r = c.execute("SELECT created FROM sessions WHERE tok=? AND uid=?", (st, ADMIN_UID)).fetchone()
    return bool(r) and r[0] >= iso(now_utc() - dt.timedelta(days=7))

def kv_get(c, k, d=None):
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else d

def kv_set(c, k, v):
    c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v)))

def need_json():
    """Защита от подделки запросов с чужих сайтов: изменения — только JSON-запросом с нашего адреса."""
    if not request.is_json:
        abort(415)
    org = request.headers.get("Origin", "")
    if org and urlsplit(org).netloc.lower() != request.host.lower():
        abort(403)
    return request.get_json(silent=True) or {}

def start_session(resp, uid):
    tok = secrets.token_urlsafe(32)
    with closing(db()) as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, uid, iso(), iso())); c.commit()
    resp.set_cookie("nfs", tok, max_age=60 * 86400, httponly=True, secure=is_https(), samesite="Lax", path="/")
    mark_owner(resp, uid)
    return resp

def mark_owner(resp, uid):
    """Метка «это браузер ученика» на весь домен: его собственные клики по своим ссылкам не идут в очки."""
    ov = request.cookies.get("nfo", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", ov or ""):
        ov = secrets.token_urlsafe(16)
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO own_vids VALUES(?,?,?)", (ov, uid, iso())); c.commit()
    resp.set_cookie("nfo", ov, max_age=365 * 86400, httponly=True, secure=is_https(), samesite="Lax",
                    path="/", domain=cookie_domain())
    return resp

def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


# ---------- редирект по ссылке ----------
def page_msg(title, text, code=404):
    html = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><body style='margin:0;min-height:100vh;display:grid;place-items:center;"
            f"background:#07060d;color:#e9e6ff;font:16px system-ui'><div style='text-align:center;padding:24px'>"
            f"<div style='font-size:42px'>🔗</div><h2 style='margin:.4em 0'>{title}</h2>"
            f"<p style='opacity:.7'>{text}</p></div>")
    return Response(html, code, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})

def record_click(link, ip, ua, vid, owner_vid):
    t = now_utc()
    wk = week_key(t); l = local(t)
    iph = ip_hash(ip)
    dev, ap = parse_ua(ua)
    g = geo_lookup(ip) or {}
    with _wlock, closing(db()) as c:
        why = ""
        if owner_vid and c.execute("SELECT 1 FROM own_vids WHERE vid=?", (owner_vid,)).fetchone():
            why = "own"
        elif c.execute("SELECT 1 FROM clicks WHERE wk=? AND mid=? AND vid=? AND scored=1",
                       (wk, link["mid"], vid)).fetchone():
            why = "repeat"
        elif c.execute("SELECT COUNT(DISTINCT mid) FROM clicks WHERE wk=? AND vid=? AND scored=1",
                       (wk, vid)).fetchone()[0] >= MAX_MODELS_PER_VIS:
            why = "cap_vis"
        elif c.execute("SELECT COUNT(*) FROM clicks WHERE wk=? AND iph=? AND scored=1",
                       (wk, iph)).fetchone()[0] >= MAX_HITS_PER_IP:
            why = "cap_ip"
        scored = 0 if why else 1
        cur = c.execute(
            "INSERT INTO clicks(ts,wk,day,hr,lid,mid,uid,ch,vid,iph,dev,app,city,cc,lat,lon,scored,why) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(t), wk, l.date().isoformat(), l.hour, link["id"], link["mid"], link["uid"], link["ch"], vid,
             iph, dev, ap, g.get("city", ""), g.get("cc", ""), g.get("lat"), g.get("lon"), scored, why))
        c.commit()
        return cur.lastrowid, scored

def follow(slug):
    slug = (slug or "").lower()[:40]
    if not SLUG_RE.match(slug):
        return page_msg("Ссылка не найдена", "Проверь адрес ссылки.")
    with closing(db()) as c:
        link = c.execute(
            "SELECT l.*, m.target AS mtarget, u.blocked FROM links l JOIN models m ON m.id=l.mid "
            "JOIN users u ON u.id=l.uid WHERE l.slug=?", (slug,)).fetchone()
    if not link:
        return page_msg("Ссылка не найдена", "Проверь адрес ссылки.")
    if link["blocked"]:
        return page_msg("Ссылка отключена", "Эта ссылка больше не работает.", 410)
    target = link["target"] or link["mtarget"]
    ua = (request.headers.get("User-Agent", "") or "")[:300]
    resp = redirect(target, code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    if not ua.strip() or BOT_UA.search(ua) or request.method == "HEAD":
        return resp                       # не человек: не считаем, но ведём туда же
    if request.args.get("check") and is_owner_browser(link["uid"]):
        dev, ap = parse_ua(ua)                # тест-клик владельца: в клики и очки не попадает
        with _wlock, closing(db()) as c:
            c.execute("INSERT INTO link_checks(lid,uid,ts,dev,app) VALUES(?,?,?,?,?)",
                      (link["id"], link["uid"], iso(), dev, ap))
            c.commit()
        return resp
    ip = client_ip()
    vid = request.cookies.get("nfv", "")
    set_cookie = not re.fullmatch(r"[a-f0-9]{20}", vid or "")
    if set_cookie:
        vid = hmac.new(SECRET.encode(), f"{ip}|{ua}".encode(), hashlib.sha1).hexdigest()[:20]
    record_click(link, ip, ua, vid, request.cookies.get("nfo", ""))
    if set_cookie:
        resp.set_cookie("nfv", vid, max_age=365 * 86400, httponly=True, secure=is_https(), samesite="Lax", path="/")
    return resp

def is_owner_browser(uid):
    ov = request.cookies.get("nfo", "")
    if not ov:
        return False
    with closing(db()) as c:
        r = c.execute("SELECT uid FROM own_vids WHERE vid=?", (ov,)).fetchone()
    return bool(r) and r[0] == uid

def link_host_request():
    return request.host.split(":")[0].lower() == LINK_HOST

@app.before_request
def _route_link_host():
    if link_host_request():
        p = request.path.strip("/")
        if not p:
            return redirect(f"https://{ROOT_HOST}/", 302)
        if p in ("robots.txt",):
            return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")
        if "/" in p or p == "favicon.ico":
            return page_msg("Ссылка не найдена", "Проверь адрес ссылки.")
        return follow(p)
    h = request.host.split(":")[0].lower()
    if h in (ROOT_HOST, "www." + ROOT_HOST):
        return redirect(f"https://{LAB_HOST}{request.full_path if request.query_string else request.path}", 302)

@app.route("/r/<slug>")
def follow_any_host(slug):
    return follow(slug)


# ---------- страницы ----------
def _page():
    with open(PAGE_PATH, encoding="utf-8") as f:
        html = f.read()
    return Response(html, headers={"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache",
                                   "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"})

@app.route("/")
def index():
    resp = _page()
    u = current_user()
    if u:
        mark_owner(resp, u["id"])
    return resp

@app.route("/admin")
def admin_page():
    return _page()

@app.route("/static/fonts/<path:fn>")
def fonts(fn):
    resp = send_from_directory(os.path.join(STATIC_DIR, "fonts"), fn)
    resp.headers["Cache-Control"] = "public, max-age=2592000"
    return resp

@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

_icons = {}

@app.route("/icon-<int:size>.png")
def app_icon(size):
    if size not in (180, 192, 512):
        abort(404)
    if size not in _icons:
        _icons[size] = ops.app_icon(size, os.path.join(STATIC_DIR, "fonts"))
    return Response(_icons[size], headers={"Content-Type": "image/png", "Cache-Control": "public, max-age=604800"})

@app.route("/manifest.webmanifest")
def manifest():
    m = {"name": "NeuroFans Lab", "short_name": "NF Lab", "lang": "ru", "start_url": "/?app=1", "scope": "/",
         "display": "standalone", "background_color": "#06050b", "theme_color": "#06050b",
         "description": "Ссылки моделей, живые переходы и лига недели",
         "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                   {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                   {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    return Response(json.dumps(m, ensure_ascii=False), headers={"Content-Type": "application/manifest+json"})

SW_JS = r"""const C='nflab-v1';
self.addEventListener('install',e=>{self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))));self.clients.claim()});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin)return;
 if(u.pathname.startsWith('/static/fonts/')||/^\/icon-\d+\.png$/.test(u.pathname)){e.respondWith(caches.open(C).then(c=>c.match(e.request).then(r=>r||fetch(e.request).then(n=>{c.put(e.request,n.clone());return n}))));return}
 if(u.pathname==='/'){e.respondWith(fetch(e.request).then(n=>{caches.open(C).then(c=>c.put('/',n.clone()));return n}).catch(()=>caches.match('/')))}});"""

@app.route("/sw.js")
def service_worker():
    return Response(SW_JS, headers={"Content-Type": "application/javascript", "Cache-Control": "no-cache",
                                    "Service-Worker-Allowed": "/"})

@app.route("/healthz")
def healthz():
    return "ok"


# ---------- вход ----------
@app.route("/api/register", methods=["POST"])
def api_register():
    d = need_json()
    if rate_limited("reg:" + client_ip(), 15):
        return err("Слишком много попыток. Подожди 15 минут.", 429)
    code = (d.get("invite") or "").strip().upper()
    nick = (d.get("nick") or "").strip()
    pw = d.get("password") or ""
    if not NICK_RE.match(nick):
        return err("Ник: 3–24 символа — буквы, цифры, точка, _ или -.")
    if len(pw) < 8 or len(pw) > 200:
        return err("Пароль — минимум 8 символов.")
    with _wlock, closing(db()) as c:
        inv = c.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
        if not inv or inv["uses"] >= inv["max_uses"]:
            return err("Инвайт-код не найден или уже использован.")
        if c.execute("SELECT 1 FROM users WHERE nick=?", (nick,)).fetchone():
            return err("Такой ник уже занят.")
        cur = c.execute("INSERT INTO users(nick,pw,created,invite) VALUES(?,?,?,?)",
                        (nick, generate_password_hash(pw), iso(), code))
        used = ",".join(x for x in [inv["used_by"], nick] if x)
        c.execute("UPDATE invites SET uses=uses+1, used_by=? WHERE code=?", (used, code))
        c.commit()
        uid = cur.lastrowid
    return start_session(jsonify({"ok": True}), uid)

@app.route("/api/login", methods=["POST"])
def api_login():
    d = need_json()
    if rate_limited("login:" + client_ip(), 20):
        return err("Слишком много попыток. Подожди 15 минут.", 429)
    nick = (d.get("nick") or "").strip()
    with closing(db()) as c:
        u = c.execute("SELECT * FROM users WHERE nick=?", (nick,)).fetchone()
    if not u or not check_password_hash(u["pw"], d.get("password") or ""):
        return err("Неверный ник или пароль.", 401)
    if u["blocked"]:
        return err("Кабинет отключён. Напиши куратору.", 403)
    return start_session(jsonify({"ok": True}), u["id"])

@app.route("/api/logout", methods=["POST"])
def api_logout():
    need_json()
    tok = request.cookies.get("nfs", "")
    with closing(db()) as c:
        c.execute("DELETE FROM sessions WHERE tok=?", (tok,)); c.commit()
    resp = jsonify({"ok": True})
    resp.delete_cookie("nfs", path="/")
    return resp

@app.route("/api/password", methods=["POST"])
def api_password():
    u = need_user(); d = need_json()
    if not check_password_hash(u["pw"], d.get("old") or ""):
        return err("Старый пароль не подходит.")
    new = d.get("new") or ""
    if len(new) < 8:
        return err("Новый пароль — минимум 8 символов.")
    with closing(db()) as c:
        c.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(new), u["id"]))
        c.execute("DELETE FROM sessions WHERE uid=? AND tok<>?", (u["id"], request.cookies.get("nfs", "")))
        c.commit()
    return jsonify({"ok": True})


# ---------- модели и ссылки ----------
def link_url(slug):
    return f"https://{LINK_HOST}/{slug}"

def models_of(c, uid):
    out = []
    checks = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT lid, MAX(ts), app FROM link_checks WHERE uid=? GROUP BY lid", (uid,))}
    firsts = {r[0]: r[1] for r in c.execute(
        "SELECT lid, MIN(ts) FROM clicks WHERE uid=? AND scored=1 GROUP BY lid", (uid,))}
    ppl = {r[0]: r[1] or 0 for r in c.execute("SELECT mid, SUM(scored) FROM clicks WHERE uid=? GROUP BY mid", (uid,))}
    for m in c.execute("SELECT * FROM models WHERE uid=? ORDER BY archived, id", (uid,)):
        links = [{"id": l["id"], "ch": l["ch"], "ch_name": CHANNELS.get(l["ch"], ("?",))[0], "slug": l["slug"],
                  "url": link_url(l["slug"]), "target": l["target"], "note": l["note"], "archived": l["archived"],
                  "checked_at": checks.get(l["id"], (None, None))[0], "checked_app": checks.get(l["id"], (None, None))[1],
                  "first_at": firsts.get(l["id"])}
                 for l in c.execute("SELECT * FROM links WHERE mid=? ORDER BY archived, id", (m["id"],))]
        out.append({"id": m["id"], "name": m["name"], "slug": m["slug"], "target": m["target"],
                    "archived": m["archived"], "hidden": m["hidden"], "links": links,
                    "rank": game.rank_of(ppl.get(m["id"], 0))})
    return out

def unique_slug(c, table, base):
    s, i = base, 2
    while s in RESERVED or c.execute(f"SELECT 1 FROM {table} WHERE slug=?", (s,)).fetchone():
        s = f"{base}{i}"; i += 1
    return s

@app.route("/api/me")
def api_me():
    u = need_user()
    s, e = week_bounds()
    with closing(db()) as c:
        models = models_of(c, u["id"])
        onb = onboarding(c, u, models)
    resp = jsonify({"ok": True, "nick": u["nick"], "models": models, "onboarding": onb,
                    "public_ok": bool(u["public_ok"]), "tg_linked": bool(u["tg_chat_id"]),
                    "channels": [{"id": k, "name": v[0]} for k, v in CHANNELS.items()],
                    "link_host": LINK_HOST, "week": week_key(), "week_end": e.isoformat(),
                    "tz": str(TZ), "limits": {"models": MAX_MODELS_PER_USER, "links": MAX_LINKS_PER_MODEL}})
    return resp

def onboarding(c, u, models):
    """Чек-лист первого дня: шаги считаются по реальным данным, руками ничего отмечать не надо."""
    links = [l for m in models for l in m["links"]]
    steps = [
        {"id": "model", "title": "Добавь модель", "hint": "имя и куда вести: Fanvue, Telegram или сайт", "done": bool(models)},
        {"id": "link", "title": "Возьми ссылку под канал", "hint": "своя ссылка на Threads, TikTok, Instagram…", "done": bool(links)},
        {"id": "check", "title": "Проверь ссылку", "hint": "кнопка «Проверить» — в статистику не попадёт",
         "done": any(l["checked_at"] for l in links)},
        {"id": "person", "title": "Поставь в био и получи первого человека", "hint": "клики появятся в обзоре сами",
         "done": any(l["first_at"] for l in links)},
        {"id": "tg", "title": "Подключи уведомления", "hint": "узнаешь о первом человеке и местах в лиге первым",
         "done": bool(u["tg_chat_id"])},
    ]
    done = sum(s["done"] for s in steps)
    return {"steps": steps, "done": done, "total": len(steps),
            "hidden": kv_get(c, f"onb_hide:{u['id']}") == "1" or done == len(steps)}

@app.route("/api/onboarding/hide", methods=["POST"])
def api_onb_hide():
    u = need_user(); need_json()
    with closing(db()) as c:
        kv_set(c, f"onb_hide:{u['id']}", 1); c.commit()
    return jsonify({"ok": True})

@app.route("/api/privacy", methods=["POST"])
def api_privacy():
    u = need_user(); d = need_json()
    with closing(db()) as c:
        c.execute("UPDATE users SET public_ok=? WHERE id=?", (1 if d.get("public_ok") else 0, u["id"])); c.commit()
    return jsonify({"ok": True, "public_ok": bool(d.get("public_ok"))})


# ---------- сравнение ссылок ----------
@app.route("/api/compare")
def api_compare():
    u = need_user()
    try:
        ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.strip()][:6]
    except ValueError:
        return err("Неверный список ссылок.")
    wk, pwk = week_key(), prev_week_key()
    today = local().date()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    out = []
    with closing(db()) as c:
        for lid in ids:
            l = c.execute("SELECT l.*, m.name mname FROM links l JOIN models m ON m.id=l.mid "
                          "WHERE l.id=? AND l.uid=?", (lid, u["id"])).fetchone()
            if not l:
                continue
            one = lambda sql, a: c.execute(sql, a).fetchone()[0] or 0
            series = {r[0]: r[1] or 0 for r in c.execute(
                "SELECT day, SUM(scored) FROM clicks WHERE lid=? AND day>=? GROUP BY day", (lid, days[0]))}
            out.append({"id": lid, "slug": l["slug"], "ch": l["ch"], "ch_name": CHANNELS.get(l["ch"], ("?",))[0],
                        "model": l["mname"], "note": l["note"],
                        "week": one("SELECT SUM(scored) FROM clicks WHERE lid=? AND wk=?", (lid, wk)),
                        "prev": one("SELECT SUM(scored) FROM clicks WHERE lid=? AND wk=?", (lid, pwk)),
                        "clicks_week": one("SELECT COUNT(*) FROM clicks WHERE lid=? AND wk=?", (lid, wk)),
                        "all": one("SELECT SUM(scored) FROM clicks WHERE lid=?", (lid,)),
                        "days": [series.get(d_, 0) for d_ in days]})
    return jsonify({"ok": True, "links": out, "days": days})


# ---------- своя конверсия (видит только ученик) ----------
@app.route("/api/conv", methods=["GET", "POST"])
def api_conv():
    u = need_user()
    with _wlock, closing(db()) as c:
        if request.method == "POST":
            d = need_json()
            try:
                mid, subs = int(d.get("mid") or 0), int(d.get("subs"))
            except (TypeError, ValueError):
                return err("Укажи число подписчиков.")
            if not 0 <= subs <= 100000:
                return err("Число подписчиков — от 0 до 100 000.")
            own_model(c, u["id"], mid)
            wk = d.get("wk") if d.get("wk") in (week_key(), prev_week_key()) else week_key()
            c.execute("INSERT OR REPLACE INTO conv(mid, wk, subs, updated) VALUES(?,?,?,?)", (mid, wk, subs, iso()))
            c.commit()
        try:
            mid = int(request.args.get("mid") or (request.get_json(silent=True) or {}).get("mid") or 0)
        except ValueError:
            mid = 0
        own_model(c, u["id"], mid)
        weeks = [week_key(now_utc() - dt.timedelta(days=7 * i)) for i in range(7, -1, -1)]
        ppl = {r[0]: r[1] or 0 for r in c.execute(
            "SELECT wk, SUM(scored) FROM clicks WHERE mid=? AND wk>=? GROUP BY wk", (mid, weeks[0]))}
        subs = {r[0]: r[1] for r in c.execute("SELECT wk, subs FROM conv WHERE mid=? AND wk>=?", (mid, weeks[0]))}
    rows = [{"wk": w, "n": int(w.split("-W")[1]), "people": ppl.get(w, 0), "subs": subs.get(w),
             "rate": round(subs[w] / ppl[w] * 100, 1) if subs.get(w) is not None and ppl.get(w) else None}
            for w in weeks]
    return jsonify({"ok": True, "mid": mid, "weeks": rows, "week": week_key(), "prev_week": prev_week_key()})

@app.route("/api/models", methods=["POST"])
def api_model_add():
    u = need_user(); d = need_json()
    name = (d.get("name") or "").strip()[:40]
    target = clean_target(d.get("target"))
    if not name:
        return err("Укажи имя модели.")
    if not target:
        return err("Ссылка, куда вести, должна быть вида https://… (Fanvue, Telegram, сайт).")
    with _wlock, closing(db()) as c:
        if c.execute("SELECT COUNT(*) FROM models WHERE uid=? AND archived=0", (u["id"],)).fetchone()[0] >= MAX_MODELS_PER_USER:
            return err(f"Максимум {MAX_MODELS_PER_USER} моделей.")
        slug = unique_slug(c, "models", slugify(name))
        cur = c.execute("INSERT INTO models(uid,name,slug,target,created) VALUES(?,?,?,?,?)",
                        (u["id"], name, slug, target, iso()))
        mid = cur.lastrowid
        chs = [x for x in (d.get("channels") or []) if x in CHANNELS][:8]
        for ch in chs:
            ls = unique_slug(c, "links", f"{slug}-{CHANNELS[ch][1]}")
            c.execute("INSERT INTO links(mid,uid,ch,slug,created) VALUES(?,?,?,?,?)", (mid, u["id"], ch, ls, iso()))
        c.commit()
        return jsonify({"ok": True, "id": mid, "models": models_of(c, u["id"])})

def own_model(c, uid, mid):
    m = c.execute("SELECT * FROM models WHERE id=? AND uid=?", (mid, uid)).fetchone()
    if not m:
        abort(404)
    return m

@app.route("/api/models/<int:mid>", methods=["PATCH"])
def api_model_edit(mid):
    u = need_user(); d = need_json()
    with _wlock, closing(db()) as c:
        own_model(c, u["id"], mid)
        if "name" in d:
            name = (d.get("name") or "").strip()[:40]
            if not name:
                return err("Имя не может быть пустым.")
            c.execute("UPDATE models SET name=? WHERE id=?", (name, mid))
        if "target" in d:
            t = clean_target(d.get("target"))
            if not t:
                return err("Ссылка должна быть вида https://…")
            c.execute("UPDATE models SET target=? WHERE id=?", (t, mid))
        for k in ("archived", "hidden"):
            if k in d:
                c.execute(f"UPDATE models SET {k}=? WHERE id=?", (1 if d[k] else 0, mid))
        c.commit()
        return jsonify({"ok": True, "models": models_of(c, u["id"])})

@app.route("/api/links", methods=["POST"])
def api_link_add():
    u = need_user(); d = need_json()
    ch = d.get("ch") if d.get("ch") in CHANNELS else None
    if not ch:
        return err("Выбери канал.")
    with _wlock, closing(db()) as c:
        try:
            mid = int(d.get("mid") or 0)
        except (TypeError, ValueError):
            return err("Нет такой модели.")
        m = own_model(c, u["id"], mid)
        if c.execute("SELECT COUNT(*) FROM links WHERE mid=?", (mid,)).fetchone()[0] >= MAX_LINKS_PER_MODEL:
            return err(f"Максимум {MAX_LINKS_PER_MODEL} ссылок на модель.")
        want = (d.get("slug") or "").strip().lower()
        if want:
            if not SLUG_RE.match(want) or want in RESERVED:
                return err("Метка: латиница, цифры и дефис, 2–40 символов.")
            if c.execute("SELECT 1 FROM links WHERE slug=?", (want,)).fetchone():
                return err("Такая метка уже занята — придумай другую.")
            slug = want
        else:
            slug = unique_slug(c, "links", f"{m['slug']}-{CHANNELS[ch][1]}")
        target = ""
        if d.get("target"):
            target = clean_target(d.get("target"))
            if not target:
                return err("Ссылка должна быть вида https://…")
        note = (d.get("note") or "").strip()[:60]
        c.execute("INSERT INTO links(mid,uid,ch,slug,target,note,created) VALUES(?,?,?,?,?,?,?)",
                  (mid, u["id"], ch, slug, target, note, iso()))
        c.commit()
        return jsonify({"ok": True, "models": models_of(c, u["id"])})

@app.route("/api/links/<int:lid>", methods=["PATCH"])
def api_link_edit(lid):
    u = need_user(); d = need_json()
    with _wlock, closing(db()) as c:
        if not c.execute("SELECT 1 FROM links WHERE id=? AND uid=?", (lid, u["id"])).fetchone():
            abort(404)
        if "target" in d:
            t = clean_target(d["target"]) if d["target"] else ""
            if t is None:
                return err("Ссылка должна быть вида https://…")
            c.execute("UPDATE links SET target=? WHERE id=?", (t, lid))
        if "note" in d:
            c.execute("UPDATE links SET note=? WHERE id=?", ((d["note"] or "").strip()[:60], lid))
        if "archived" in d:
            c.execute("UPDATE links SET archived=? WHERE id=?", (1 if d["archived"] else 0, lid))
        c.commit()
        return jsonify({"ok": True, "models": models_of(c, u["id"])})


# ---------- статистика ----------
def _scope(c, uid, mid):
    if mid and mid != "all":
        try:
            m = own_model(c, uid, int(mid))
        except ValueError:
            abort(404)
        return "mid=?", (m["id"],)
    return "uid=?", (uid,)

@app.route("/api/stats")
def api_stats():
    u = need_user()
    try:
        days = max(7, min(60, int(request.args.get("days", "14") or 14)))
    except ValueError:
        days = 14
    with closing(db()) as c:
        where, args = _scope(c, u["id"], request.args.get("mid", "all"))
        today = local().date()
        since_day = (today - dt.timedelta(days=days - 1)).isoformat()
        wk, pwk = week_key(), prev_week_key()
        q = lambda sql, a=(): c.execute(sql, args + tuple(a)).fetchall()
        one = lambda sql, a=(): c.execute(sql, args + tuple(a)).fetchone()[0] or 0
        series = {r["day"]: (r["n"], r["p"]) for r in q(
            f"SELECT day, COUNT(*) n, SUM(scored) p FROM clicks WHERE {where} AND day>=? GROUP BY day", (since_day,))}
        daylist = [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        hours = [0] * 24
        for r in q(f"SELECT hr, COUNT(*) n FROM clicks WHERE {where} AND day>=? GROUP BY hr", (since_day,)):
            hours[r["hr"]] = r["n"]
        ch = [{"ch": r["ch"], "name": CHANNELS.get(r["ch"], ("?",))[0], "clicks": r["n"], "people": r["p"] or 0}
              for r in q(f"SELECT ch, COUNT(*) n, SUM(scored) p FROM clicks WHERE {where} AND day>=? "
                         f"GROUP BY ch ORDER BY n DESC", (since_day,))]
        per_link = {r["lid"]: {"clicks": r["n"], "people": r["p"] or 0, "today": r["t"] or 0, "last": r["last"]}
                    for r in c.execute(f"SELECT lid, COUNT(*) n, SUM(scored) p, SUM(day=?) t, MAX(ts) last "
                                       f"FROM clicks WHERE {where} GROUP BY lid", (today.isoformat(),) + args)}
        cities = [{"city": r["city"], "cc": r["cc"], "n": r["n"]} for r in q(
            f"SELECT city, cc, COUNT(*) n FROM clicks WHERE {where} AND day>=? AND city<>'' "
            f"GROUP BY city, cc ORDER BY n DESC LIMIT 12", (since_day,))]
        countries = [{"cc": r["cc"], "n": r["n"]} for r in q(
            f"SELECT cc, COUNT(*) n FROM clicks WHERE {where} AND day>=? AND cc<>'' "
            f"GROUP BY cc ORDER BY n DESC LIMIT 8", (since_day,))]
        devs = [{"k": r["dev"], "n": r["n"]} for r in q(
            f"SELECT dev, COUNT(*) n FROM clicks WHERE {where} AND day>=? GROUP BY dev ORDER BY n DESC", (since_day,))]
        apps = [{"k": r["app"], "n": r["n"]} for r in q(
            f"SELECT app, COUNT(*) n FROM clicks WHERE {where} AND day>=? GROUP BY app ORDER BY n DESC", (since_day,))]
        recent = [{"ts": r["ts"], "ch": r["ch"], "slug": r["slug"], "model": r["mname"], "city": r["city"],
                   "cc": r["cc"], "dev": r["dev"], "app": r["app"], "scored": r["scored"], "why": r["why"]}
                  for r in c.execute(
                      f"SELECT k.*, l.slug, m.name mname FROM clicks k JOIN links l ON l.id=k.lid "
                      f"JOIN models m ON m.id=k.mid WHERE k.{where} ORDER BY k.ts DESC, k.id DESC LIMIT 40", args)]
        points = [{"lat": r["lat"], "lon": r["lon"], "n": r["n"], "city": r["city"]} for r in q(
            f"SELECT lat, lon, city, COUNT(*) n FROM clicks WHERE {where} AND day>=? AND lat IS NOT NULL "
            f"GROUP BY lat, lon ORDER BY n DESC LIMIT 200", (since_day,))]
        tot = {
            "today_clicks": one(f"SELECT COUNT(*) FROM clicks WHERE {where} AND day=?", (today.isoformat(),)),
            "today_people": one(f"SELECT SUM(scored) FROM clicks WHERE {where} AND day=?", (today.isoformat(),)),
            "week_people":  one(f"SELECT SUM(scored) FROM clicks WHERE {where} AND wk=?", (wk,)),
            "prev_week_people": one(f"SELECT SUM(scored) FROM clicks WHERE {where} AND wk=?", (pwk,)),
            "week_clicks":  one(f"SELECT COUNT(*) FROM clicks WHERE {where} AND wk=?", (wk,)),
            "all_clicks":   one(f"SELECT COUNT(*) FROM clicks WHERE {where}"),
            "all_people":   one(f"SELECT SUM(scored) FROM clicks WHERE {where}"),
            "own_week":     one(f"SELECT COUNT(*) FROM clicks WHERE {where} AND wk=? AND why='own'", (wk,)),
            "period_clicks": sum(v[0] for v in series.values()),
            "period_people": sum((v[1] or 0) for v in series.values()),
        }
    return jsonify({"ok": True, "days": [{"day": d_, "clicks": series.get(d_, (0, 0))[0],
                                          "people": series.get(d_, (0, 0))[1] or 0} for d_ in daylist],
                    "hours": hours, "channels": ch, "links": per_link, "cities": cities, "countries": countries,
                    "devices": devs, "apps": apps, "recent": recent, "points": points, "totals": tot})


@app.route("/api/core")
def api_core():
    """NF Core: пульс за час, всплески/провалы по ссылкам и каналам, прогноз недели, гео за сутки."""
    u = need_user()
    with closing(db()) as c:
        where, args = _scope(c, u["id"], request.args.get("mid", "all"))
        s, e = week_bounds()
        mids = [(r["id"], r["name"]) for r in c.execute(
            "SELECT id, name FROM models WHERE uid=? AND archived=0 ORDER BY id", (u["id"],))]
        if request.args.get("mid", "all") != "all":
            mids = [m for m in mids if str(m[0]) == request.args.get("mid")]
        out = lab_core.core(c, where, args, now_utc(), week_key(), s, e, game.divisions(c, week_key()),
                            game.rank_of, game.RANKS, mids)
        names = {r["id"]: (r["slug"], r["ch"], r["mname"]) for r in c.execute(
            "SELECT l.id, l.slug, l.ch, m.name mname FROM links l JOIN models m ON m.id=l.mid WHERE l.uid=?",
            (u["id"],))}
    for sgl in out["sig_links"]:
        sl, ch, mn = names.get(sgl["key"], ("?", "other", ""))
        sgl.update(slug=sl, ch=ch, model=mn, name=CHANNELS.get(ch, ("?",))[0])
    for sgc in out["sig_ch"]:
        sgc.update(ch=sgc["key"], name=CHANNELS.get(sgc["key"], ("?",))[0])
    return jsonify({"ok": True, **out})


# ---------- лига недели ----------
board = game.board

@app.route("/api/league")
def api_league():
    u = need_user()
    ch = request.args.get("ch") if request.args.get("ch") in CHANNELS else None
    div = request.args.get("div") if request.args.get("div") in game.DIV_IDS else None
    wk, pwk = week_key(), prev_week_key()
    s, e = week_bounds()
    with closing(db()) as c:
        cur = board(c, wk, ch)
        prev = board(c, pwk, ch)
        divs = game.divisions(c, wk)
        mine_div = next((divs.get(r["mid"]) for r in cur if r["uid"] == u["id"]), None)
        if mine_div is None:
            own = [r[0] for r in c.execute("SELECT id FROM models WHERE uid=? AND archived=0", (u["id"],))]
            mine_div = divs.get(own[0], "start") if own else "start"
        if div:
            cur = [r for r in cur if divs.get(r["mid"], "start") == div]
            prev = [r for r in prev if divs.get(r["mid"], "start") == div]
        prevmap = {r["mid"]: r["pts"] for r in prev}
        allp = game.people_all(c)
        rk = lambda mid: game.rank_of(allp.get(mid, 0))["icon"]
        rl = lambda mid: game.rank_of(allp.get(mid, 0))["lvl"]
        rows = []
        for i, r in enumerate(cur):
            rows.append({"place": i + 1, "mid": r["mid"], "model": r["name"], "nick": r["nick"], "pts": r["pts"],
                         "prev": prevmap.get(r["mid"], 0), "mine": r["uid"] == u["id"], "rank": rk(r["mid"]),
                         "lvl": rl(r["mid"]), "div": divs.get(r["mid"], "start")})
        # «Рывок недели»: рост к прошлой неделе (сглажено +5, чтобы новички с 1→3 не улетали в ×3)
        rush = [{"mid": r["mid"], "model": r["name"], "nick": r["nick"], "pts": r["pts"], "prev": r["prev"],
                 "x": r["x"], "mine": r["uid"] == u["id"], "rank": rk(r["mid"]), "lvl": rl(r["mid"])}
                for r in game.rush_rows(cur, prev)]
        winners = [{"place": i + 1, "model": r["name"], "nick": r["nick"], "pts": r["pts"]}
                   for i, r in enumerate(prev[:3])]
        vis = [r[0] for r in c.execute("SELECT m.id FROM models m JOIN users u ON u.id=m.uid "
                                       "WHERE m.archived=0 AND m.hidden=0 AND u.blocked=0")]
        n_models = len(vis)
        div_total = sum(1 for m in vis if divs.get(m, "start") == div) if div else n_models
    return jsonify({"ok": True, "week": wk, "week_start": s.isoformat(), "week_end": e.isoformat(),
                    "ch": ch or "", "rows": rows[:100], "mine": [x for x in rows if x["mine"]],
                    "rush": rush[:10], "winners": winners, "models_total": n_models, "div_total": div_total,
                    "div": div or "", "my_div": mine_div,
                    "divs": [{"id": d[0], "name": d[1], "range": d[2]} for d in game.DIVS],
                    "channels": [{"id": k, "name": v[0]} for k, v in CHANNELS.items()]})


# ---------- ранги и ачивки ----------
@app.route("/api/achievements")
def api_achievements():
    u = need_user()
    wk, pwk = week_key(), prev_week_key()
    with _wlock, closing(db()) as c:
        game.sync_badges(c, pwk, week_key(now_utc() - dt.timedelta(days=14)), iso())
        allp = game.people_all(c)
        out = []
        for m in c.execute("SELECT id, name FROM models WHERE uid=? AND archived=0 ORDER BY id", (u["id"],)).fetchall():
            out.append({"mid": m["id"], "name": m["name"], "rank": game.rank_of(allp.get(m["id"], 0)),
                        "badges": game.badges_of(c, m["id"]), "week": game.model_week(c, m["id"], wk, pwk)})
    return jsonify({"ok": True, "models": out, "defs": game.badge_defs(),
                    "ranks": [{"at": a, "name": n, "icon": i} for a, n, i in game.RANKS]})


# ---------- карточка «Моя неделя» ----------
@app.route("/api/card.png")
def api_card():
    u = need_user()
    try:
        mid = int(request.args.get("mid", "0"))
    except ValueError:
        abort(404)
    final = request.args.get("kind") == "last"
    t = now_utc() - dt.timedelta(days=7) if final else now_utc()
    wk, pwk = week_key(t), week_key(t - dt.timedelta(days=7))
    with _wlock, closing(db()) as c:
        m = own_model(c, u["id"], mid)
        s = game.model_week(c, mid, wk, pwk)
        allp = game.people_all(c, [mid])
        badges = game.badges_of(c, mid)
        kv_set(c, f"card_u:{u['id']}", iso())
        k = "cards:" + week_key()
        c.execute("INSERT INTO kv(k,v) VALUES(?, '1') ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER)+1", (k,))
        c.commit()
    import lab_card
    png = lab_card.card({
        "model": m["name"], "nick": u["nick"] if request.args.get("nick") == "1" else None,
        "week_no": int(wk.split("-W")[1]), "final": final, "pts": s["pts"],
        "place": s["place"] if not m["hidden"] else None, "total": s["total"], "rush_x": s["rush_x"],
        "top_ch_name": CHANNELS.get(s["top_ch"], (None,))[0] if s["top_ch"] else None,
        "rank": game.rank_of(allp.get(mid, 0)), "badges": badges,
    }, os.path.join(STATIC_DIR, "fonts"))
    name = f"neurofans-{m['slug']}-{wk}.png"
    return Response(png, headers={"Content-Type": "image/png", "Cache-Control": "no-store",
                                  "Content-Disposition": f'inline; filename="{name}"'})


# ---------- Telegram-уведомления ----------
NOTIFIER = None

def notifier():
    global NOTIFIER
    if NOTIFIER is None:
        import lab_notify
        NOTIFIER = lab_notify.Notifier(db, TZ, week_key, LAB_BOT_TOKEN, LAB_HOST, LAB_BOT_USERNAME,
                                       db_path=DB_PATH, backup_dir=BACKUP_DIR)
    return NOTIFIER

@app.route("/api/tg")
def api_tg():
    u = need_user()
    return jsonify({"ok": True, "enabled": notifier().enabled, "linked": bool(u["tg_chat_id"]),
                    "mode": u["tg_mode"] or "all"})

@app.route("/api/tg/link", methods=["POST"])
def api_tg_link():
    u = need_user(); need_json()
    n = notifier()
    if not n.enabled:
        return err("Уведомления пока не включены — бот кабинета ещё не подключён.")
    name = n.username()
    if not name:
        return err("Бот кабинета не отвечает. Попробуй позже.", 503)
    with closing(db()) as c:
        code = n.new_link_code(c, u["id"], now_utc())
    return jsonify({"ok": True, "url": f"https://t.me/{name}?start={code}"})

@app.route("/api/tg/mode", methods=["POST"])
def api_tg_mode():
    u = need_user(); d = need_json()
    mode = d.get("mode") if d.get("mode") in ("all", "digest", "off") else None
    if not mode:
        return err("Неизвестный режим.")
    with closing(db()) as c:
        c.execute("UPDATE users SET tg_mode=? WHERE id=?", (mode, u["id"])); c.commit()
    return jsonify({"ok": True, "mode": mode})

@app.route("/api/tg/unlink", methods=["POST"])
def api_tg_unlink():
    u = need_user(); need_json()
    with closing(db()) as c:
        c.execute("UPDATE users SET tg_chat_id=NULL WHERE id=?", (u["id"],)); c.commit()
    return jsonify({"ok": True})


# ---------- «Пульс лиги»: только агрегаты, для сайта курса и продающего бота ----------
_pulse = {"t": 0.0, "v": None}

@app.route("/api/public/pulse")
def api_pulse():
    if time.time() - _pulse["t"] > 300 or _pulse["v"] is None:
        wk = week_key()
        with closing(db()) as c:
            one = lambda sql, a=(): c.execute(sql, a).fetchone()[0] or 0
            models_active = one("SELECT COUNT(DISTINCT mid) FROM clicks WHERE wk=? AND scored=1", (wk,))
            v = {"ok": True, "week": int(wk.split("-W")[1]), "ready": models_active >= 3}
            if v["ready"]:                      # меньше 3 моделей — по сумме можно вычислить чужие цифры
                v.update({
                    "people_week": one("SELECT SUM(scored) FROM clicks WHERE wk=?", (wk,)),
                    "people_all": one("SELECT SUM(scored) FROM clicks"),
                    "models_active": models_active,
                    "students": one("SELECT COUNT(*) FROM users WHERE blocked=0"),
                    "countries_week": one("SELECT COUNT(DISTINCT cc) FROM clicks WHERE wk=? AND scored=1 AND cc<>''", (wk,)),
                    "bots_filtered_note": "боты и превью соцсетей не считаются",
                })
            v["updated"] = iso()
        _pulse.update(t=time.time(), v=v)
    resp = jsonify(_pulse["v"])
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# ---------- админка ----------
def need_admin():
    if not is_admin():
        abort(401)

@app.route("/api/admin/state")
def api_admin_state():
    with closing(db()) as c:
        has_pw = bool(kv_get(c, "admin_pw"))
    return jsonify({"ok": True, "has_password": has_pw, "logged": is_admin()})

def _admin_session(resp):
    tok = secrets.token_urlsafe(32)
    with closing(db()) as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, ADMIN_UID, iso(), iso())); c.commit()
    resp.set_cookie("nfa", tok, max_age=7 * 86400, httponly=True, secure=is_https(), samesite="Strict", path="/")
    return resp

@app.route("/api/admin/setup", methods=["POST"])
def api_admin_setup():
    """Задать (или сбросить) пароль админки. Нужен токен из lab-data/admin_token.txt — он остаётся ключом восстановления."""
    d = need_json()
    if rate_limited("adm:" + client_ip(), 10):
        return err("Слишком много попыток. Подожди 15 минут.", 429)
    tok = (d.get("token") or "").strip()
    if not tok or not hmac.compare_digest(tok, ADMIN_TOKEN):
        return err("Токен не подходит. Возьми его на сервере: cat lab-data/admin_token.txt", 403)
    pw = d.get("password") or ""
    if len(pw) < 10:
        return err("Пароль админки — минимум 10 символов.")
    with closing(db()) as c:
        kv_set(c, "admin_pw", generate_password_hash(pw))
        c.execute("DELETE FROM sessions WHERE uid=?", (ADMIN_UID,))
        c.commit()
    return _admin_session(jsonify({"ok": True}))

@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    d = need_json()
    if rate_limited("adm:" + client_ip(), 10):
        return err("Слишком много попыток. Подожди 15 минут.", 429)
    with closing(db()) as c:
        h = kv_get(c, "admin_pw")
    if not h or not check_password_hash(h, d.get("password") or ""):
        return err("Неверный пароль.", 401)
    return _admin_session(jsonify({"ok": True}))

@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    need_json()
    with closing(db()) as c:
        c.execute("DELETE FROM sessions WHERE tok=? AND uid=?", (request.cookies.get("nfa", ""), ADMIN_UID)); c.commit()
    resp = jsonify({"ok": True})
    resp.delete_cookie("nfa", path="/")
    return resp

@app.route("/api/admin/backup")
def api_admin_backup():
    need_admin()
    path = ops.make_backup(DB_PATH, BACKUP_DIR, local().strftime("%Y-%m-%d-%H%M"))
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, headers={"Content-Type": "application/gzip", "Cache-Control": "no-store",
                                   "Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'})

@app.route("/api/admin/tg/link", methods=["POST"])
def api_admin_tg_link():
    need_admin(); need_json()
    n = notifier()
    if not n.enabled or not n.username():
        return err("Сначала подключи бота кабинета (LAB_BOT_TOKEN в .env).")
    code = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    with closing(db()) as c:
        kv_set(c, "admin_tg_code", code)
        kv_set(c, "admin_tg_exp", iso(now_utc() + dt.timedelta(minutes=30)))
        c.commit()
    return jsonify({"ok": True, "url": f"https://t.me/{n.username()}?start=adm{code}"})

@app.route("/api/admin/weekly_post")
def api_admin_weekly_post():
    need_admin()
    wk = week_key() if request.args.get("wk") == "cur" else prev_week_key()
    before = week_key(dt.datetime.combine(game.week_monday(wk) - dt.timedelta(days=3), dt.time(12), tzinfo=TZ))
    with closing(db()) as c:
        text = ops.weekly_post(c, wk, before, LAB_HOST)
    return jsonify({"ok": True, "text": text, "wk": wk})

@app.route("/api/admin/overview")
def api_admin_overview():
    need_admin()
    wk = week_key()
    with closing(db()) as c:
        users = [dict(r) for r in c.execute(
            "SELECT u.id, u.nick, u.created, u.blocked, u.invite, "
            "(SELECT COUNT(*) FROM models m WHERE m.uid=u.id AND m.archived=0) models, "
            "(SELECT COUNT(*) FROM links l WHERE l.uid=u.id AND l.archived=0) links, "
            "(SELECT COALESCE(SUM(scored),0) FROM clicks k WHERE k.uid=u.id AND k.wk=?) week_people, "
            "(SELECT COUNT(*) FROM clicks k WHERE k.uid=u.id) all_clicks, "
            "(SELECT MAX(seen) FROM sessions s WHERE s.uid=u.id) seen "
            "FROM users u ORDER BY week_people DESC, u.id", (wk,))]
        invites = [dict(r) for r in c.execute("SELECT * FROM invites ORDER BY created DESC LIMIT 300")]
        tot = {"users": len(users), "week_people": sum(x["week_people"] for x in users),
               "week_clicks": c.execute("SELECT COUNT(*) FROM clicks WHERE wk=?", (wk,)).fetchone()[0],
               "models": c.execute("SELECT COUNT(*) FROM models WHERE archived=0").fetchone()[0]}
        one = lambda sql, a=(): c.execute(sql, a).fetchone()[0] or 0
        funnel = {
            "users": len(users),
            "with_link": one("SELECT COUNT(DISTINCT uid) FROM links"),
            "with_link_24h": one("SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM links l WHERE l.uid=u.id "
                                 "AND datetime(l.created)<=datetime(u.created,'+1 day'))"),
            "with_person": one("SELECT COUNT(DISTINCT uid) FROM clicks WHERE scored=1"),
            "with_person_7d": one("SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM clicks k WHERE k.uid=u.id "
                                  "AND k.scored=1 AND datetime(k.ts)<=datetime(u.created,'+7 day'))"),
            "active_week": one("SELECT COUNT(DISTINCT uid) FROM clicks WHERE wk=? AND scored=1", (wk,)),
            "tg_linked": one("SELECT COUNT(*) FROM users WHERE tg_chat_id IS NOT NULL"),
            "cards_week": int((c.execute("SELECT v FROM kv WHERE k=?", ("cards:" + wk,)).fetchone() or ["0"])[0]),
            "notify_sent_week": one("SELECT COUNT(*) FROM notify WHERE status='sent' AND sent_at>=?",
                                    (week_bounds()[0].astimezone(dt.timezone.utc).isoformat(timespec="seconds"),)),
            "checks": one("SELECT COUNT(DISTINCT lid) FROM link_checks"),
        }
        stuck = ops.stuck_students(c, now_utc())
        backups = {"last": os.path.basename(ops.latest_backup(BACKUP_DIR) or "") or None,
                   "admin_tg": bool(kv_get(c, "admin_chat"))}
        consent = one("SELECT COUNT(*) FROM users WHERE public_ok=1")
    for i in invites:
        i["url"] = f"https://{LAB_HOST}/?invite={i['code']}"
    return jsonify({"ok": True, "users": users, "invites": invites, "totals": tot, "week": wk, "funnel": funnel,
                    "tg_enabled": bool(LAB_BOT_TOKEN), "stuck": stuck, "backups": backups, "consent": consent})

@app.route("/api/admin/invites", methods=["POST"])
def api_admin_invites():
    need_admin(); d = need_json()
    n = max(1, min(100, int(d.get("count") or 1)))
    mx = max(1, min(1000, int(d.get("max_uses") or 1)))
    note = (d.get("note") or "").strip()[:80]
    codes = []
    with _wlock, closing(db()) as c:
        for _ in range(n):
            code = "NF-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            c.execute("INSERT INTO invites(code,note,created,max_uses) VALUES(?,?,?,?)", (code, note, iso(), mx))
            codes.append({"code": code, "url": f"https://{LAB_HOST}/?invite={code}"})
        c.commit()
    return jsonify({"ok": True, "codes": codes})

@app.route("/api/admin/invites/<code>", methods=["DELETE"])
def api_admin_invite_del(code):
    need_admin(); need_json()
    with closing(db()) as c:
        c.execute("UPDATE invites SET max_uses=uses WHERE code=?", (code.upper(),)); c.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/users/<int:uid>", methods=["PATCH"])
def api_admin_user(uid):
    need_admin(); d = need_json()
    with closing(db()) as c:
        if "blocked" in d:
            c.execute("UPDATE users SET blocked=? WHERE id=?", (1 if d["blocked"] else 0, uid))
            if d["blocked"]:
                c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
        if d.get("reset_password"):
            pw = secrets.token_urlsafe(9)
            c.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(pw), uid))
            c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
            c.commit()
            return jsonify({"ok": True, "password": pw})
        c.commit()
    return jsonify({"ok": True})


@app.errorhandler(401)
def _e401(e):
    return err("Нужно войти.", 401)

@app.errorhandler(404)
def _e404(e):
    if request.path.startswith("/api/"):
        return err("Не найдено.", 404)
    return page_msg("Страница не найдена", "Проверь адрес.")

@app.errorhandler(415)
def _e415(e):
    return err("Нужен JSON-запрос.", 415)


init_db()

_bg_started = False

def start_background():
    """Фоновые потоки: геобаза и уведомитель (он же делает ачивки и резервные копии). Один раз на процесс."""
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    threading.Thread(target=geo_updater, daemon=True).start()
    threading.Thread(target=notifier().run, args=(now_utc,), daemon=True).start()

if _env("LAB_BG", "0") == "1":      # под gunicorn (1 воркер): потоки стартуют при импорте
    start_background()

if __name__ == "__main__":
    log.info("NeuroFans Lab: кабинет https://%s, ссылки https://%s/<метка>", LAB_HOST, LINK_HOST)
    log.info("Админка: https://%s/admin?token=<ADMIN_TOKEN из %s>", LAB_HOST, os.path.join(DATA, "admin_token.txt"))
    start_background()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
