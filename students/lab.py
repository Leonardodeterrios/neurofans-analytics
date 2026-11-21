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
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lab")

def _env(k, d=""):
    v = os.environ.get(k, d)
    return (v.split("#")[0].strip() if v else d) or d

HERE      = os.path.dirname(os.path.abspath(__file__))
DATA      = _env("LAB_DATA", os.path.join(HERE, "..", "lab-data"))
DB_PATH   = os.path.join(DATA, "lab.db")
GEO_DB    = os.path.join(DATA, "geo-city.mmdb")
ROOT_HOST = _env("ROOT_HOST", "neurofans.ru").lower()
LAB_HOST  = _env("LAB_HOST", "lab." + ROOT_HOST).lower()
LINK_HOST = _env("LINK_HOST", "to." + ROOT_HOST).lower()
TZ        = ZoneInfo(_env("LAB_TZ", "Europe/Moscow"))
PORT      = int(_env("LAB_PORT", "8090"))
GEO_ENABLE         = _env("GEO_ENABLE", "1") == "1"
MAX_MODELS_PER_VIS = int(_env("MAX_MODELS_PER_VISITOR", "3"))   # один человек — максимум 3 моделям за неделю
MAX_HITS_PER_IP    = int(_env("MAX_HITS_PER_IP", "100"))        # и не больше 100 очков с одного IP за неделю
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

def is_admin():
    tok = request.headers.get("X-Admin-Token", "") or request.args.get("token", "")
    return bool(tok) and hmac.compare_digest(tok, ADMIN_TOKEN)

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
    ip = client_ip()
    vid = request.cookies.get("nfv", "")
    set_cookie = not re.fullmatch(r"[a-f0-9]{20}", vid or "")
    if set_cookie:
        vid = hmac.new(SECRET.encode(), f"{ip}|{ua}".encode(), hashlib.sha1).hexdigest()[:20]
    record_click(link, ip, ua, vid, request.cookies.get("nfo", ""))
    if set_cookie:
        resp.set_cookie("nfv", vid, max_age=365 * 86400, httponly=True, secure=is_https(), samesite="Lax", path="/")
    return resp

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
    for m in c.execute("SELECT * FROM models WHERE uid=? ORDER BY archived, id", (uid,)):
        links = [{"id": l["id"], "ch": l["ch"], "ch_name": CHANNELS.get(l["ch"], ("?",))[0], "slug": l["slug"],
                  "url": link_url(l["slug"]), "target": l["target"], "note": l["note"], "archived": l["archived"]}
                 for l in c.execute("SELECT * FROM links WHERE mid=? ORDER BY archived, id", (m["id"],))]
        out.append({"id": m["id"], "name": m["name"], "slug": m["slug"], "target": m["target"],
                    "archived": m["archived"], "hidden": m["hidden"], "links": links})
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
    resp = jsonify({"ok": True, "nick": u["nick"], "models": models,
                    "channels": [{"id": k, "name": v[0]} for k, v in CHANNELS.items()],
                    "link_host": LINK_HOST, "week": week_key(), "week_end": e.isoformat(),
                    "tz": str(TZ), "limits": {"models": MAX_MODELS_PER_USER, "links": MAX_LINKS_PER_MODEL}})
    return resp

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


# ---------- лига недели ----------
def board(c, wk, ch=None):
    sql = ("SELECT k.mid, m.name, m.uid, u.nick, SUM(k.scored) pts, COUNT(DISTINCT k.ch) chs "
           "FROM clicks k JOIN models m ON m.id=k.mid JOIN users u ON u.id=m.uid "
           "WHERE k.wk=? AND k.scored=1 AND m.hidden=0 AND m.archived=0 AND u.blocked=0")
    a = [wk]
    if ch:
        sql += " AND k.ch=?"; a.append(ch)
    sql += " GROUP BY k.mid ORDER BY pts DESC, MIN(k.id) ASC"
    return [dict(r) for r in c.execute(sql, a)]

@app.route("/api/league")
def api_league():
    u = need_user()
    ch = request.args.get("ch") if request.args.get("ch") in CHANNELS else None
    wk, pwk = week_key(), prev_week_key()
    s, e = week_bounds()
    with closing(db()) as c:
        cur = board(c, wk, ch)
        prev = board(c, pwk, ch)
        prevmap = {r["mid"]: r["pts"] for r in prev}
        rows = []
        for i, r in enumerate(cur):
            rows.append({"place": i + 1, "mid": r["mid"], "model": r["name"], "nick": r["nick"], "pts": r["pts"],
                         "prev": prevmap.get(r["mid"], 0), "mine": r["uid"] == u["id"]})
        # «Рывок недели»: рост к прошлой неделе (сглажено +5, чтобы новички с 1→3 не улетали в ×3)
        rush = []
        for r in cur:
            p0 = prevmap.get(r["mid"], 0)
            if r["pts"] >= 10 and r["pts"] > p0:
                rush.append({"mid": r["mid"], "model": r["name"], "nick": r["nick"], "pts": r["pts"], "prev": p0,
                             "x": round((r["pts"] + 5) / (p0 + 5), 2), "mine": r["uid"] == u["id"]})
        rush.sort(key=lambda x: (-x["x"], -x["pts"]))
        winners = [{"place": i + 1, "model": r["name"], "nick": r["nick"], "pts": r["pts"]}
                   for i, r in enumerate(prev[:3])]
        n_models = c.execute("SELECT COUNT(*) FROM models m JOIN users u ON u.id=m.uid "
                             "WHERE m.archived=0 AND m.hidden=0 AND u.blocked=0").fetchone()[0]
    return jsonify({"ok": True, "week": wk, "week_start": s.isoformat(), "week_end": e.isoformat(),
                    "ch": ch or "", "rows": rows[:100], "mine": [x for x in rows if x["mine"]],
                    "rush": rush[:10], "winners": winners, "models_total": n_models,
                    "channels": [{"id": k, "name": v[0]} for k, v in CHANNELS.items()]})


# ---------- админка ----------
def need_admin():
    if not is_admin():
        abort(401)

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
    for i in invites:
        i["url"] = f"https://{LAB_HOST}/?invite={i['code']}"
    return jsonify({"ok": True, "users": users, "invites": invites, "totals": tot, "week": wk})

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

if __name__ == "__main__":
    log.info("NeuroFans Lab: кабинет https://%s, ссылки https://%s/<метка>", LAB_HOST, LINK_HOST)
    log.info("Админка: https://%s/admin?token=<ADMIN_TOKEN из %s>", LAB_HOST, os.path.join(DATA, "admin_token.txt"))
    threading.Thread(target=geo_updater, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
