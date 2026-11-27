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
import os, io, csv, re, json, gzip, zlib, hmac, ipaddress, hashlib, sqlite3, threading, time, logging, datetime as dt
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
NOTIFY_ACTIONS     = _flag("NOTIFY_ACTIONS", "0")   # уведомлять о каждом действии в боте (по умолчанию нет — их много)
DAILY_SUMMARY_TIME = _str("DAILY_SUMMARY_TIME")
SUMMARY_TZ         = _str("SUMMARY_TZ", "Asia/Bangkok") or "Asia/Bangkok"
SILENCE_HOURS      = _int("SILENCE_HOURS", 24)
SILENCE_CHECK_MIN  = _int("SILENCE_CHECK_MIN", 60)
SILENCE_MIN_CLICKS = _int("SILENCE_MIN_CLICKS", 3)
START_POWER        = _int("START_POWER", 3)
TRUST_CF           = _flag("TRUST_CF", "0")   # 1 = сайт за Cloudflare, берём IP из CF-Connecting-IP
MAX_PARTNERS_PER_VISITOR = _int("MAX_PARTNERS_PER_VISITOR", 3)
MAX_HITS_PER_IP    = _int("MAX_HITS_PER_IP", 100)
MAX_HITS_PER_NET   = _int("MAX_HITS_PER_NET", 10)   # с одной подсети /24 (IPv6 — /48) за раунд: прокси-сканеры идут блоками
BASE_URL           = _str("BASE_URL").rstrip("/")
PER_TEAM           = _int("PER_TEAM", 50)
PARTNER_SECRET     = _str("PARTNER_SECRET")     # подпись личных ссылок партнёров (/p/<метка>?k=...)
GEO_ENABLE         = _flag("GEO", "1")          # города кликов по базе DB-IP Lite (скачивается сама)
GEO_DB             = _str("GEO_DB") or os.path.join(os.path.dirname(DB_PATH) or ".", "geo-city.mmdb")
ASN_DB             = _str("ASN_DB") or os.path.join(os.path.dirname(DB_PATH) or ".", "geo-asn.mmdb")
REQUIRE_MOBILE     = _flag("REQUIRE_MOBILE", "1")  # удар только с телефона/из приложения (сканеры ходят с «компьютера»)
BLOCK_HOSTING      = _flag("BLOCK_HOSTING", "1")   # удар не засчитывается с IP дата-центров/хостингов

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
    r"(?<!cu)bot\b|(?<!cu)bot/|crawler|spider|preview|facebookexternalhit|facebookcatalog|meta-externalagent|"
    r"telegrambot|whatsapp|vkshare|slackbot|skypeuripreview|discordbot|twitterbot|embedly|quora link|"
    r"headless|python-requests|python-urllib|aiohttp|httpx|curl/|wget|go-http-client|okhttp|axios|"
    r"node-fetch|scrapy|libwww|java/|phantomjs|puppeteer|playwright|lighthouse|pingdom|uptimerobot|"
    r"statuscake|monitor", re.I)
# телефон или встроенный браузер приложения: живые люди из био Threads/Instagram/TikTok/YouTube приходят так
MOBILE_UA = re.compile(r"Mobile|Android|iPhone|iPod|iPad|Instagram|Barcelona|FBAN|FBAV|musical_ly|"
                       r"BytedanceWebview|TikTok|YouTube", re.I)
# владельцы IP-сетей дата-центров и хостингов (по названию сети в DB-IP ASN Lite). Не включаем
# Cloudflare/Akamai Technologies/Fastly — через них ходит iCloud Private Relay (живые айфоны).
HOSTING_ORG = re.compile(
    r"amazon|\baws\b|google cloud|google llc|microsoft|azure|digitalocean|hetzner|\bovh|linode|"
    r"akamai connected cloud|vultr|choopa|oracle|alibaba|tencent|ucloud|contabo|leaseweb|scaleway|"
    r"online s\.?a\.?s|m247|datacamp|cdn77|hostinger|ionos|hostwinds|colocrossing|quadranet|psychz|"
    r"servers\.com|g-?core|kamatera|upcloud|zenlayer|interserver|nforce|worldstream|serverion|equinix|"
    r"frantech|ponynet|buyvm|racknerd|dedipath|tzulo|hivelocity|limestone|liquid web|rackspace|"
    r"ikoula|timeweb|selectel|aeza|hostkey|vdsina|firstbyte|stark industries|pq hosting|cloudvider|"
    r"tencent|huawei cloud|byteplus|hosting|data ?cent(er|re)|datacenter|\bvps\b|dedicated|colocation|"
    r"\bcloud\b|server", re.I)
IOS_VER = re.compile(r"(?:iPhone|CPU) OS (\d+)_(\d+)(?:_(\d+))?")
SAFARI_VER = re.compile(r"Version/(\d+)(?:\.(\d+))?")
NOT_SAFARI = re.compile(r"CriOS|FxiOS|EdgiOS|OPiOS|YaBrowser|Instagram|Barcelona|FBAN|FBAV|musical_ly|Bytedance|"
                        r"TikTok|YouTube|GSA/|Line/|Telegram", re.I)

def forged_ua(ua):
    """Строка браузера, которую не отправляет ни один настоящий айфон (так притворяются сканеры):
    версия iOS с нулевым третьим числом (26_3_0 — у Apple всегда 26_3) или Safari, чья версия
    не совпадает с версией iOS (iOS 16.6.1 → Version/16.6). С iOS 26 Safari пишет старую версию
    системы (OS 18_6 … Version/26.0) — это настоящий айфон, его не трогаем."""
    m = IOS_VER.search(ua or "")
    if not m:
        return False
    if m.group(3) == "0":
        return True
    v = SAFARI_VER.search(ua)
    if v and not NOT_SAFARI.search(ua) and "Safari" in ua:
        os_maj, os_min, sv_maj = int(m.group(1)), int(m.group(2)), int(v.group(1))
        if sv_maj < os_maj:
            return True
        if sv_maj == os_maj and v.group(2) is not None and int(v.group(2)) != os_min:
            return True
    return False

def ip_net(ip):
    """Подсеть адреса: /24 для IPv4, /48 для IPv6."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip or ""
    return str(ipaddress.ip_network(f"{ip}/{24 if a.version == 4 else 48}", strict=False))

SAFE_START = re.compile(r"[^A-Za-z0-9_-]")
SAFE_LABEL = re.compile(r"[^A-Za-z0-9_-]")
HERE = os.path.dirname(os.path.abspath(__file__))
ARENA_PATH = os.path.join(HERE, "arena.html")
PARTNER_PATH = os.path.join(HERE, "partner.html")

# Ранги по силе за всё время и ачивки (значки рядом с меткой партнёра)
# Целевые действия в боте (кнопки PuzzleBot → /bot/action): код → (очки, подпись для ленты)
ACTIONS = {
    "examples": (8, "смотрит примеры генераций"),
    "guide":    (3, "взял бесплатный гайд"),
    "prompts":  (3, "открыл базу промтов"),
    "sim":      (4, "в симуляторе чаттера"),
    "channel":  (4, "в Telegram-канале"),
    "reviews":  (5, "читает отзывы учеников"),
    "site":     (8, "перешёл на сайт"),
    # ближе к покупке — больше очков (согласовано с Лёней 22.11.2026)
    "program":  (10, "смотрит программу клуба"),
    "intent":   (12, "хочет такие же результаты"),
    "checkout": (20, "открыл оплату Продвинутого"),
    "checkout_base": (15, "открыл оплату Базового"),
    # настоящая оплата клуба — приходит вебхуком от Tribute, кнопкой её не получить (не в ACT_LINKS)
    "paid":     (1000, "оплатил курс"),
}
# Кнопки воронки в боте — это ссылки (мини-приложения ai-creators.ru, канал), PuzzleBot при нажатии
# ничего не шлёт. Поэтому кнопка ведёт на наш переходник /a/<код>: он засчитывает действие и сразу
# открывает настоящую страницу. Куда вести — только отсюда (никаких адресов из ссылки: не открытый редирект).
ACT_LINKS = {
    "examples": "https://gallery.lukaflow.shop/",          # галерея 18+ на зарубежном сервере (27.11.2026)
    "guide":    "https://ai-creators.ru/guide-model",
    "prompts":  "https://ai-creators.ru/prompts",
    "sim":      "https://ai-creators.ru/simulator",
    "channel":  "https://t.me/+X-AXokWU0zswZTFi",
    "reviews":  "https://t.me/+X-AXokWU0zswZTFi",
    "site":     "https://new.neurofans.ru/",
    "program":  "https://new.neurofans.ru/#price",
    "intent":   "https://new.neurofans.ru/#price",
    "checkout": "https://t.me/tribute/app?startapp=hLUB",       # Продвинутый 14 900 ₽
    "checkout_base": "https://t.me/tribute/app?startapp=hLUy",  # Базовый 3 900 ₽
}
for _kv in (os.getenv("ACT_LINKS") or "").split(","):        # переопределение в .env: ACT_LINKS=site=https://…,guide=https://…
    _k, _, _v = _kv.strip().partition("=")
    if _k in ACTIONS and _v.startswith("https://"):
        ACT_LINKS[_k] = _v
# Tribute: вебхук об оплате подписан HMAC-SHA256 тела на API-ключе (заголовок trbt-signature).
TRIBUTE_KEY = (os.getenv("TRIBUTE_API_KEY") or "").strip()
TRIBUTE_CLUB_IDS = {x.strip() for x in (os.getenv("TRIBUTE_CLUB_IDS") or "").split(",") if x.strip()}  # пусто — любая покупка
TRIBUTE_FORWARD = (os.getenv("TRIBUTE_FORWARD", "http://lab:8090/api/tribute/webhook") or "").strip()  # копия вебхука кабинету (Pro)
PAID_EVENTS = ("new_subscription", "new_digital_product", "new_donation")

def tribute_sig_ok(raw, sig, key=None):
    key = TRIBUTE_KEY if key is None else key
    if not key or not sig:
        return False
    import base64
    mac = hmac.new(key.encode(), raw, hashlib.sha256)
    sig = sig.strip()
    return hmac.compare_digest(sig, mac.hexdigest()) or hmac.compare_digest(sig, base64.b64encode(mac.digest()).decode())

def tribute_event(ev):
    """Событие Tribute → {"paid": uid} / {"refund": uid} / {}. Клуб — по TRIBUTE_CLUB_IDS (если задан)."""
    name, p = str(ev.get("name") or ""), ev.get("payload") or {}
    try:
        uid = int(p.get("telegram_user_id") or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return {}
    if "refund" in name.lower():
        return {"refund": uid}
    if name not in PAID_EVENTS:
        return {}
    ids = {str(p.get(k)) for k in ("product_id", "subscription_id", "channel_id", "donation_request_id") if p.get(k) is not None}
    if TRIBUTE_CLUB_IDS and not (ids & TRIBUTE_CLUB_IDS):
        return {}
    return {"paid": uid}

def remove_paid(uid):
    with _conn() as c:
        n = c.execute("DELETE FROM actions WHERE tg_uid=? AND act='paid'", (uid,)).rowcount
    _stats_cache.clear()
    return n

RANKS = [(0, "Новичок"), (10, "Боец"), (30, "Воин"), (75, "Мастер"), (150, "Чемпион"), (300, "Легенда")]
BADGES = {
    "first_blood": ("🩸", "Первая кровь", "первый удар этого раунда"),
    "champ":       ("👑", "Чемпион раунда", "лучший боец прошлого раунда"),
    "belt":        ("🥋", "Пояс недели", "больше всех силы за 7 раундов"),
    "ko":          ("⚡", "Нокаутёр", "10+ апперкотов"),
    "sniper":      ("🎯", "Снайпер", "доходимость 50%+ при 10+ ударах"),
    "hundred":     ("💯", "Сотня", "100+ ударов"),
    "combo":       ("🔥", "Комбо ×5", "5 ударов за 10 минут в этом раунде"),
    "heavy":       ("💥", "Тяжёлая артиллерия", "2+ действия в боте от +8 (сайт, примеры, программа, оплата)"),
    "globe":       ("🌍", "Кругосветка", "удары из 5+ разных городов за раунд"),
    "funnel":      ("🧭", "Проводник", "люди прошли 4+ разных шага в боте"),
    "rush":        ("🚀", "Рывок", "сила за раунд ≥10 и вдвое больше прошлого раунда"),
    "marathon":    ("🏃", "Марафонец", "сила в 5 раундах подряд"),
    "night":       ("🌙", "Ночной дозор", "удар между 00:00 и 05:00 в этом раунде"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neurofans")
app = Flask(__name__)
if hasattr(app, "config"):
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 7 * 86400   # шрифты и статика кэшируются неделю


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
    """'leo07-2s' -> ('leo07', 100, 'leo07-2s'); старый вид 'leo07__2s' тоже понимаем.
    Третье значение — вся строка как метка: на случай, если «-» был частью самой метки."""
    raw = (raw or "").strip()
    if raw.lower().startswith("/start"):
        raw = raw[6:].strip()
    if "__" in raw:
        label_part, _, cid_part = raw.partition("__")
    else:
        label_part, _, cid_part = raw.rpartition("-")
        if not label_part:
            label_part, cid_part = raw, ""
    cid = None
    if cid_part and re.fullmatch(r"[0-9a-zA-Z]{1,12}", cid_part):
        cid = int(cid_part, 36)
    return clean_label(label_part), cid, clean_label(raw)


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
                         ("rnd", "TEXT"), ("ch", "TEXT DEFAULT ''"),
                         ("city", "TEXT"), ("cc", "TEXT"), ("lat", "REAL"), ("lon", "REAL"),
                         ("why", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE clicks ADD COLUMN {col} {ddl}")
        c.execute("""CREATE TABLE IF NOT EXISTS starts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, label TEXT, click_id INTEGER,
            tg_uid INTEGER, ch TEXT, scored INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tg_uid INTEGER, act TEXT, label TEXT,
            points INTEGER, start_id INTEGER, UNIQUE(tg_uid, act))""")
        c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, kind TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_src_vid ON clicks(source, vid, rnd)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_vid ON clicks(vid, rnd)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_ip ON clicks(ip, rnd)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_clicks_ts ON clicks(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_starts_uid ON starts(tg_uid)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_starts_ts ON starts(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_actions_ts ON actions(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_actions_label ON actions(label)")

def record_click(source, ch, ip, ua, vid, geo=None, why=""):
    """Пишет клик. Возвращает (id клика, засчитан ли удар, новый ли человек).
    why — почему клик не может быть ударом в принципе ("desktop" / "hosting"), "" — может."""
    _stats_cache.clear()
    g = geo or {}
    _start, end = round_bounds()
    rnd = _utc_z(end)
    with _conn() as c:
        dup = c.execute("SELECT 1 FROM clicks WHERE source=? AND vid=? AND rnd=? AND scored=1 LIMIT 1",
                        (source, vid, rnd)).fetchone()
        spread = c.execute("SELECT COUNT(DISTINCT source) FROM clicks WHERE vid=? AND rnd=? AND scored=1",
                           (vid, rnd)).fetchone()[0]
        ip_hits = c.execute("SELECT COUNT(*) FROM clicks WHERE ip=? AND rnd=? AND scored=1",
                            (ip, rnd)).fetchone()[0] if ip else 0
        net_hits = 0
        if ip and MAX_HITS_PER_NET:
            net = ip_net(ip)
            for (oip,) in c.execute("SELECT ip FROM clicks WHERE rnd=? AND scored=1 AND ip LIKE ?",
                                    (rnd, net.split("/")[0].rsplit("." if ":" not in net else ":", 1)[0] + "%")):
                if ip_net(oip) == net:
                    net_hits += 1
        scored = 0 if (why or dup or spread >= MAX_PARTNERS_PER_VISITOR or ip_hits >= MAX_HITS_PER_IP
                       or (MAX_HITS_PER_NET and net_hits >= MAX_HITS_PER_NET) or source == "direct") else 1
        new_vis = c.execute("SELECT 1 FROM clicks WHERE vid=? LIMIT 1", (vid,)).fetchone() is None
        cur = c.execute("INSERT INTO clicks(ts,source,ip,ua,vid,scored,rnd,ch,city,cc,lat,lon,why) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_now_iso(), source, ip, ua, vid, scored, rnd, ch,
                         g.get("city"), g.get("cc"), g.get("lat"), g.get("lon"), why or None))
        return cur.lastrowid, bool(scored), new_vis

def record_start(raw_payload, uid):
    """Старт бота из PuzzleBot. Засчитывается («апперкот»), если человек впервые пришёл в бота."""
    _stats_cache.clear()
    label, cid, whole = parse_payload(raw_payload)
    ch, click_ok = "", False
    with _conn() as c:
        if cid is not None:
            row = c.execute("SELECT source, ch, scored, why FROM clicks WHERE id=?", (cid,)).fetchone()
            if row is None or row["source"] != label:
                cid, label = None, whole       # не наш id клика — считаем всю строку меткой
            else:
                # клик с компьютера — не удар, но если человек дошёл до бота (живой Telegram) — апперкот даём.
                # Клики с хостингов и поддельных браузеров апперкот не дают.
                ch, click_ok = row["ch"] or "", bool(row["scored"]) or row["why"] == "desktop"
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


def record_action(uid, act):
    """Кнопка в боте. Очки идут партнёру, который привёл этого человека (его засчитанный старт).
    Один tg_uid получает очки за каждое действие один раз за всё время. Неизвестный uid — тихо игнорируем."""
    act = (act or "").strip().lower()
    if act not in ACTIONS:
        return {"ok": False, "reason": "unknown_act"}
    if not uid:
        return {"ok": False, "reason": "no_uid"}
    with _conn() as c:
        st = c.execute("SELECT id, label FROM starts WHERE tg_uid=? AND scored=1 ORDER BY id LIMIT 1", (uid,)).fetchone()
        if st is None or not team_of(st["label"]):
            return {"ok": False, "reason": "uid_not_attributed"}
        pts = ACTIONS[act][0]
        cur = c.execute("INSERT OR IGNORE INTO actions(ts,tg_uid,act,label,points,start_id) VALUES(?,?,?,?,?,?)",
                        (_now_iso(), uid, act, st["label"], pts, st["id"]))
        if cur.rowcount == 0:
            return {"ok": False, "reason": "duplicate", "label": st["label"], "act": act}
    _stats_cache.clear()
    return {"ok": True, "label": st["label"], "act": act, "points": pts}


# ---------- статистика ----------
def _conv(starts, hits):
    # апперкот бывает и без удара (клик с компьютера, человек дошёл до бота) — доходимость не больше 100%
    return min(100.0, round(starts / hits * 100, 1)) if hits else None

_stats_cache = {}

def _anchor():
    """Эпоха (сек) гонга 1 января 2020 — от неё нумеруются раунды: индекс = (t - anchor) // сутки."""
    hh, mm = _round_hm()
    return int(dt.datetime(2020, 1, 1, hh, mm, tzinfo=ZoneInfo(SUMMARY_TZ)).timestamp())

def round_idx(d):
    return (int(d.timestamp()) - _anchor()) // 86400

def round_end_of(idx):
    return dt.datetime.fromtimestamp(_anchor() + (idx + 1) * 86400, ZoneInfo(SUMMARY_TZ))

def round_history(c):
    """{метка: {индекс раунда: [удары, апперкоты]}} по всем раундам."""
    A, h = _anchor(), {}
    for lab, r, n in c.execute("SELECT source, (CAST(strftime('%s', ts) AS INTEGER) - ?) / 86400, SUM(scored) "
                               "FROM clicks WHERE scored=1 GROUP BY 1, 2", (A,)):
        h.setdefault(lab, {}).setdefault(r, [0, 0, 0])[0] += n or 0
    for lab, r, n in c.execute("SELECT label, (CAST(strftime('%s', ts) AS INTEGER) - ?) / 86400, SUM(scored) "
                               "FROM starts WHERE scored=1 GROUP BY 1, 2", (A,)):
        h.setdefault(lab, {}).setdefault(r, [0, 0, 0])[1] += n or 0
    for lab, r, n in c.execute("SELECT label, (CAST(strftime('%s', ts) AS INTEGER) - ?) / 86400, SUM(points) "
                               "FROM actions GROUP BY 1, 2", (A,)):
        h.setdefault(lab, {}).setdefault(r, [0, 0, 0])[2] += n or 0
    h.pop("direct", None)
    return h

def _pw(v):
    return v[0] + START_POWER * v[1] + (v[2] if len(v) > 2 else 0)

def round_summary(hist, idx):
    """Итоги одного раунда: сила команд, удары/апперкоты, топ-3 партнёров."""
    teams = {k: {"key": k, "name": n, "color": col, "power": 0, "hits": 0, "starts": 0, "actions": 0}
             for k, (n, _p, col) in TEAMS.items()}
    rows = []
    for lab, rr in hist.items():
        v = rr.get(idx)
        tk = team_of(lab)
        if not v or not tk:
            continue
        t = teams[tk]; t["power"] += _pw(v); t["hits"] += v[0]; t["starts"] += v[1]; t["actions"] += v[2]
        rows.append({"source": lab, "team": tk, "power": _pw(v), "hits": v[0], "starts": v[1], "actions": v[2]})
    rows.sort(key=lambda x: (-x["power"], x["source"]))
    a, b = teams["A"], teams["B"]
    win = None if a["power"] == b["power"] else ("A" if a["power"] > b["power"] else "B")
    return {"idx": idx, "end": _utc_z(round_end_of(idx)), "date": round_end_of(idx).strftime("%d.%m.%Y"),
            "teams": [a, b], "winner": win, "top": rows[:3], "all": rows}

def rank_of(power):
    i = 0
    for j, (thr, _n) in enumerate(RANKS):
        if power >= thr:
            i = j
    nxt = RANKS[i + 1][0] if i + 1 < len(RANKS) else None
    return i, RANKS[i][1], nxt

def get_stats(at=None):
    """Кэш на 2 секунды: арену одновременно смотрят много партнёров. Сбрасывается при каждом клике/старте."""
    if at is not None:
        return _get_stats(at)
    hit = _stats_cache.get("v")
    if hit and time.time() - hit[0] < 2:
        return hit[1]
    v = _get_stats()
    _stats_cache["v"] = (time.time(), v)
    return v

def _get_stats(at=None):
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
        act_all = {r[0]: r[1] or 0 for r in c.execute("SELECT label, SUM(points) FROM actions GROUP BY label")}
        act_r = {r[0]: r[1] or 0 for r in c.execute(
            "SELECT label, SUM(points) FROM actions WHERE ts>=? AND ts<? GROUP BY label", (s0, e0))}
        act_n = c.execute("SELECT COUNT(*), COALESCE(SUM(points),0) FROM actions").fetchone()
        heavy_all = {r[0]: r[1] for r in c.execute("SELECT label, COUNT(*) FROM actions WHERE points>=8 GROUP BY label")}
        heavy_r = {r[0]: r[1] for r in c.execute(
            "SELECT label, COUNT(*) FROM actions WHERE points>=8 AND ts>=? AND ts<? GROUP BY label", (s0, e0))}
        acts_kinds = {r[0]: r[1] for r in c.execute("SELECT label, COUNT(DISTINCT act) FROM actions GROUP BY label")}
        # удары раунда по времени и городам — для «Комбо», «Кругосветки», «Ночного дозора» и «горячей формы»
        rr = {}
        for r in c.execute("SELECT source, ts, COALESCE(city,'') FROM clicks WHERE scored=1 AND ts>=? AND ts<? ORDER BY id", (s0, e0)):
            rr.setdefault(r[0], []).append((r[1], r[2]))
        fb = c.execute("SELECT source FROM clicks WHERE scored=1 AND source!='direct' AND ts>=? AND ts<? "
                       "ORDER BY id LIMIT 1", (s0, e0)).fetchone()
        hist = round_history(c)
    cur = round_idx(start)
    first = min((r for rr in hist.values() for r in rr), default=cur)
    prev = round_summary(hist, cur - 1)
    week = {lab: sum(_pw(v) for r, v in rr.items() if cur - 6 <= r <= cur) for lab, rr in hist.items()}
    belt = max(week.items(), key=lambda kv: (kv[1], kv[0]), default=(None, 0))
    partners = []
    for label in (set(src) | set(st_all) | set(act_all)) - {"direct"}:
        r = src.get(label)
        clicks, hits, uq, last = (r["clicks"], r["hits"], r["uniq"], r["last"]) if r else (0, 0, 0, None)
        st, hr, sr = st_all.get(label, 0), hits_r.get(label, 0), st_r.get(label, 0)
        ap, apr = act_all.get(label, 0), act_r.get(label, 0)
        partners.append({"source": label, "team": team_of(label), "clicks": clicks, "hits": hits,
                         "uniq": uq, "starts": st, "last": last,
                         "hits_today": hr, "starts_today": sr,
                         "act_points": ap, "act_points_today": apr,
                         "today": hr + START_POWER * sr + apr, "power": hits + START_POWER * st + ap,
                         "conv": _conv(st, hits)})
    tz = ZoneInfo(SUMMARY_TZ)
    hot_from = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)).replace(tzinfo=None).isoformat(timespec="seconds")
    def _combo(ts_list, n=5, win=600):
        t = [dt.datetime.fromisoformat(x).timestamp() for x in ts_list]
        return any(t[i + n - 1] - t[i] <= win for i in range(len(t) - n + 1))
    for p in partners:
        rows = rr.get(p["source"], [])
        p["heavy"], p["heavy_today"] = heavy_all.get(p["source"], 0), heavy_r.get(p["source"], 0)
        p["cities_today"] = len({ci for _, ci in rows if ci})
        p["hot"] = sum(1 for ts, _ in rows if ts >= hot_from)
        ri, rn, nxt = rank_of(p["power"])
        p["rank"], p["rank_name"], p["rank_next"] = ri, rn, nxt
        b = []
        if fb and fb[0] == p["source"]: b.append("first_blood")
        if prev["top"] and prev["top"][0]["source"] == p["source"]: b.append("champ")
        if belt[0] == p["source"] and belt[1] > 0: b.append("belt")
        if p["starts"] >= 10: b.append("ko")
        if p["hits"] >= 10 and (p["conv"] or 0) >= 50: b.append("sniper")
        if p["hits"] >= 100: b.append("hundred")
        if len(rows) >= 5 and _combo([ts for ts, _ in rows]): b.append("combo")
        if p["heavy"] >= 2: b.append("heavy")
        if p["cities_today"] >= 5: b.append("globe")
        if acts_kinds.get(p["source"], 0) >= 4: b.append("funnel")
        h = hist.get(p["source"], {})
        if p["today"] >= 10 and p["today"] >= 2 * _pw(h.get(cur - 1, [0, 0, 0])): b.append("rush")
        k, r0 = 0, cur if _pw(h.get(cur, [0, 0, 0])) > 0 else cur - 1
        while _pw(h.get(r0 - k, [0, 0, 0])) > 0: k += 1
        if k >= 5: b.append("marathon")
        if any(dt.datetime.fromisoformat(ts).replace(tzinfo=dt.timezone.utc).astimezone(tz).hour < 5 for ts, _ in rows): b.append("night")
        p["badges"] = b
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
                      "act_points": sum(p["act_points"] for p in mem),
                      "act_points_today": sum(p["act_points_today"] for p in mem),
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
            "actions": act_n[0], "action_points": act_n[1],
            "action_defs": {k: {"points": v[0], "text": v[1]} for k, v in ACTIONS.items()},
            "round_no": cur - first + 1, "prev_round": {k: v for k, v in prev.items() if k != "all"},
            "ranks": [{"min": t, "name": n} for t, n in RANKS],
            "badge_defs": {k: {"icon": i, "name": n, "desc": d} for k, (i, n, d) in BADGES.items()},
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
                f"{_plural(t['starts_today'], 'апперкот', 'апперкота', 'апперкотов')}"
                + (f" + {t['act_points_today']} за действия в боте" if t.get("act_points_today") else "")
                + f", доходимость {_pct(conv)}")
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


# ---------- гео: город клика (DB-IP Lite, CC BY 4.0) ----------
_geo = {"r": None, "tried": 0.0}

def _geo_reader():
    if not GEO_ENABLE or not os.path.exists(GEO_DB):
        return None
    if _geo["r"] is None and time.time() - _geo["tried"] > 60:
        _geo["tried"] = time.time()
        try:
            import maxminddb
            _geo["r"] = maxminddb.open_database(GEO_DB)
        except Exception as e:
            log.warning("geo db open failed: %s", e)
    return _geo["r"]

def geo_lookup(ip):
    """Только город и округлённые координаты (точность ~10 км) — для «пульсов» на арене."""
    r = _geo_reader()
    if not r or not ip:
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

# ---------- сеть IP: дата-центр/хостинг? (DB-IP ASN Lite, CC BY 4.0) ----------
_asn = {"r": None, "tried": 0.0}

def _asn_reader():
    if not os.path.exists(ASN_DB):
        return None
    if _asn["r"] is None and time.time() - _asn["tried"] > 60:
        _asn["tried"] = time.time()
        try:
            import maxminddb
            _asn["r"] = maxminddb.open_database(ASN_DB)
        except Exception as e:
            log.warning("asn db open failed: %s", e)
    return _asn["r"]

def asn_org(ip):
    r = _asn_reader()
    if not r or not ip:
        return None
    try:
        d = r.get(ip) or {}
    except Exception:
        return None
    return (d.get("autonomous_system_organization") or "").strip() or None

def not_human_reason(ip, ua):
    """Почему клик не может быть ударом: "desktop" — не телефон и не приложение (так ходят сканеры),
    "hosting" — IP дата-центра, "forged" — поддельная строка браузера. "" — может (дальше обычные лимиты). Редирект от этого не меняется."""
    if REQUIRE_MOBILE and not MOBILE_UA.search(ua or ""):
        return "desktop"
    if forged_ua(ua):
        return "forged"
    if BLOCK_HOSTING:
        org = asn_org(ip)
        if org and HOSTING_ORG.search(org):
            return "hosting"
    return ""

def rescore_all():
    """Пересчитать «засчитан ли» для всех кликов и стартов по текущим правилам (клики в базе остаются).
    Повторяет логику record_click/record_start по порядку id."""
    _stats_cache.clear()
    with _conn() as c:
        pairs, spread, ipn, netn, newsc, why_of = set(), {}, {}, {}, {}, {}
        for r in c.execute("SELECT id, source, ip, ua, vid, rnd FROM clicks ORDER BY id").fetchall():
            rnd = r["rnd"] or ""
            why = not_human_reason(r["ip"], r["ua"])
            key_s = (rnd, r["vid"])
            ok = not why and r["source"] != "direct" \
                and (rnd, r["source"], r["vid"]) not in pairs \
                and len(spread.get(key_s, ())) < MAX_PARTNERS_PER_VISITOR \
                and (not r["ip"] or ipn.get((rnd, r["ip"]), 0) < MAX_HITS_PER_IP) \
                and (not r["ip"] or not MAX_HITS_PER_NET or netn.get((rnd, ip_net(r["ip"])), 0) < MAX_HITS_PER_NET)
            if ok:
                pairs.add((rnd, r["source"], r["vid"]))
                spread.setdefault(key_s, set()).add(r["source"])
                if r["ip"]:
                    ipn[(rnd, r["ip"])] = ipn.get((rnd, r["ip"]), 0) + 1
                    k = (rnd, ip_net(r["ip"]))
                    netn[k] = netn.get(k, 0) + 1
            newsc[r["id"]], why_of[r["id"]] = int(ok), (why or None)
        old = {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, scored, why FROM clicks")}
        ch_clicks = [(newsc[i], why_of[i], i) for i in newsc if old.get(i) != (newsc[i], why_of[i])]
        c.executemany("UPDATE clicks SET scored=?, why=? WHERE id=?", ch_clicks)
        used, seen, ch_starts = set(), set(), []
        for r in c.execute("SELECT id, label, click_id, tg_uid, scored FROM starts ORDER BY id").fetchall():
            cid, uid = r["click_id"], r["tg_uid"]
            sc = int(bool(cid is not None and (newsc.get(cid) or why_of.get(cid) == "desktop") and cid not in used
                          and not (uid and uid in seen) and r["label"] != "direct"))
            if sc:
                used.add(cid)
            if uid:
                seen.add(uid)
            if sc != (r["scored"] or 0):
                ch_starts.append((sc, r["id"]))
        c.executemany("UPDATE starts SET scored=? WHERE id=?", ch_starts)
        hits = sum(newsc.values())
    _stats_cache.clear()
    log.info("rescore: clicks changed %d, starts changed %d, hits now %d", len(ch_clicks), len(ch_starts), hits)
    return {"clicks_changed": len(ch_clicks), "starts_changed": len(ch_starts), "hits": hits}

def _download_mmdb(kind, path):
    """kind: city | asn. Свежая база DB-IP Lite за этот или прошлый месяц."""
    today = dt.date.today()
    for m in (today, today.replace(day=1) - dt.timedelta(days=1)):
        url = f"https://download.db-ip.com/free/dbip-{kind}-lite-{m:%Y-%m}.mmdb.gz"
        tmp = path + ".tmp"
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as resp:
            if resp.status_code != 200:
                continue
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip-поток
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(d.decompress(chunk))
                f.write(d.flush())
        os.replace(tmp, path)
        log.info("%s db updated from %s", kind, url)
        return True
    return False

def _stale(path):
    return not (os.path.exists(path) and time.time() - os.path.getmtime(path) < 35 * 86400)

def geo_updater():
    """Раз в сутки проверяет базы городов и сетей; если базы нет или ей больше 35 дней — скачивает свежую.
    Один раз после появления базы сетей пересчитывает старые клики по фильтру сканеров (meta rescore_v2 — при смене правил поднять номер)."""
    while True:
        try:
            if GEO_ENABLE and _stale(GEO_DB) and _download_mmdb("city", GEO_DB):
                _geo["r"] = None; _geo["tried"] = 0.0
        except Exception as e:
            log.warning("geo db update failed: %s", e)
        try:
            if BLOCK_HOSTING and _stale(ASN_DB) and _download_mmdb("asn", ASN_DB):
                _asn["r"] = None; _asn["tried"] = 0.0
        except Exception as e:
            log.warning("asn db update failed: %s", e)
        try:
            if (not BLOCK_HOSTING or os.path.exists(ASN_DB)):
                with _conn() as c:
                    done = c.execute("SELECT v FROM meta WHERE k='rescore_v2'").fetchone()
                if not done:
                    res = rescore_all()
                    with _conn() as c:
                        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('rescore_v2',?)", (json.dumps(res),))
        except Exception as e:
            log.warning("rescore failed: %s", e)
        time.sleep(86400)


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

def tg_photo(png, caption, chat_id):
    if not (BOT_TOKEN and chat_id and png):
        return
    try:
        httpx.post(f"{TG_API}/sendPhoto", data={"chat_id": str(chat_id), "caption": caption[:1000]},
                   files={"photo": ("round.png", png, "image/png")}, timeout=30)
    except Exception as e:
        log.error("tg photo to %s failed: %s", chat_id, e)

def round_card_png(which="last", label=None):
    """Карточка итогов раунда 9:16 (Pillow). which: last — закончившийся раунд, current — идущий."""
    try:
        import cards
    except Exception as e:
        log.warning("cards unavailable: %s", e)
        return None
    with _conn() as c:
        hist = round_history(c)
    start, _end = round_bounds()
    idx = round_idx(start) - (1 if which == "last" else 0)
    first = min((r for rr in hist.values() for r in rr), default=idx)
    summ = round_summary(hist, idx)
    me = None
    if label:
        rows = summ["all"]
        pos = next((i for i, r in enumerate(rows) if r["source"] == label), None)
        me = {"source": label, "team": team_of(label),
              "place": (pos + 1) if pos is not None else None, "of": len(rows),
              "power": rows[pos]["power"] if pos is not None else 0}
    return cards.story(summ, round_no=idx - first + 1, live=(which != "last"), me=me,
                       start_power=START_POWER, fonts=os.path.join(HERE, "static", "fonts"))

# Отправка в Telegram/таблицу — только в фоне. Сервер в РФ, api.telegram.org оттуда отвечает медленно
# (до таймаута 10 с на каждого админа), и раньше человек ждал редирект в бота ~30 секунд.
import queue as _queue
_bg_q = _queue.Queue(maxsize=2000)
_bg_started = threading.Event()

def _bg_worker():
    while True:
        fn, args = _bg_q.get()
        try:
            fn(*args)
        except Exception as e:
            log.error("background task %s failed: %s", getattr(fn, "__name__", fn), e)

def bg(fn, *args):
    """Выполнить fn(*args) в фоновом потоке, не задерживая ответ. Очередь переполнена — пропускаем."""
    if not _bg_started.is_set():
        _bg_started.set()
        threading.Thread(target=_bg_worker, daemon=True, name="bg-notify").start()
    try:
        _bg_q.put_nowait((fn, args))
    except _queue.Full:
        log.warning("background queue full, dropped %s", getattr(fn, "__name__", fn))

def _notify_all_now(text):
    for cid in ADMIN_IDS:
        tg_send(text, cid)

def notify_all(text):
    bg(_notify_all_now, text)

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

def notify_action(label, act, pts):
    if act == "paid" and ADMIN_IDS:            # оплату сообщаем всегда
        return notify_all(f"💰 ОПЛАТА КЛУБА! Привёл {label} (+{pts})\nКоманда: {_team_label(label)}")
    if not (ADMIN_IDS and NOTIFY_ACTIONS):
        return
    notify_all(f"⚡ {label}: человек {ACTIONS[act][1]} (+{pts})\nКоманда: {_team_label(label)}")

def _push_sheet_now(payload):
    try:
        httpx.post(SHEET_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        log.error("sheet push failed: %s", e)

def push_sheet(payload):
    if not SHEET_WEBHOOK_URL:
        return
    bg(_push_sheet_now, payload)


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
                    png = round_card_png("last")
                    for cid in ADMIN_IDS:
                        tg_photo(png, "Карточка итогов раунда — можно выложить в сторис", cid)
            except Exception as e:
                log.error("daily summary failed: %s", e)
        time.sleep(30)


# ---------- веб ----------
def _from_local_proxy():
    """Запрос пришёл от нашего caddy (внутренняя сеть docker), а не напрямую из интернета."""
    try:
        return ipaddress.ip_address((request.remote_addr or "").strip()).is_private
    except ValueError:
        return False

def _client_ip():
    """Настоящий IP посетителя. Заголовки прокси подделываются кем угодно, поэтому верим:
    X-Real-IP — только от нашего caddy (он перезаписывает заголовок адресом соединения),
    CF-Connecting-IP — только при TRUST_CF=1; иначе — адрес TCP-соединения."""
    if TRUST_CF:
        v = request.headers.get("CF-Connecting-IP", "")
        if v:
            return v.strip()
    if _from_local_proxy():
        v = request.headers.get("X-Real-IP", "")
        if v:
            return v.strip()
    return request.remote_addr or ""

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
    lab, k = request.args.get("p", ""), request.args.get("k", "")
    if lab and k and hmac.compare_digest(k, partner_key(clean_label(lab))):
        return "public"                 # личная ссылка партнёра открывает и арену
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
    cid, scored, new_vis = record_click(source, ch, ip, ua, vid, geo_lookup(ip), not_human_reason(ip, ua))
    if scored:
        notify_hit(source, ch, new_vis)
        push_sheet({"type": "hit", "source": source, "channel": ch, "visitor": vid})
    log.info("click source=%s ch=%s scored=%s", source, ch or "-", scored)
    target = f"https://t.me/{BOT_USERNAME}" + (f"?start={quote(payload + '-' + b36(cid))}" if payload else "")
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

def _tribute_forward(raw, sig):
    try:
        httpx.post(TRIBUTE_FORWARD, content=raw, headers={"trbt-signature": sig, "content-type": "application/json"}, timeout=10)
    except Exception as e:
        log.info("tribute forward failed: %s", e)

@app.route("/tribute/webhook", methods=["POST"])
def tribute_webhook():
    """Оплата клуба в Tribute → +1000 партнёру, который привёл этого человека (его засчитанный старт).
    Один человек — одна оплата за всё время; возврат — очки снимаются. Копия вебхука уходит кабинету (Pro)."""
    raw, sig, ev = request.get_data(), request.headers.get("trbt-signature", ""), {}
    if not tribute_sig_ok(raw, sig):
        abort(401)
    if TRIBUTE_FORWARD:
        bg(_tribute_forward, raw, sig)
    try:
        ev = json.loads(raw or b"{}")
        what = tribute_event(ev)
        if "paid" in what:
            res = record_action(what["paid"], "paid")
            if res.get("ok"):
                notify_action(res["label"], res["act"], res["points"])
        elif "refund" in what:
            res = {"ok": True, "refunded": remove_paid(what["refund"])}
        else:
            res = {"ok": True, "skip": ev.get("name")}
    except Exception as e:
        log.error("tribute webhook failed: %s", e)
        res = {"ok": False, "reason": "error"}
    log.info("tribute %s → %s", ev.get("name") if isinstance(ev, dict) else "?", res)
    return jsonify(**res)

@app.route("/bot/action", methods=["GET", "POST"])
def bot_action():
    """Кнопка в боте (PuzzleBot «Отправить запрос»): ?act=<код>&uid=<telegram id>&key=<INGEST_TOKEN>.
    Всегда отвечает 200 (кроме неверного ключа), чтобы PuzzleBot не ретраил и не ломал воронку."""
    if not INGEST_TOKEN or not hmac.compare_digest(request.values.get("key", ""), INGEST_TOKEN):
        abort(403)
    try:
        uid = int(re.sub(r"\D", "", request.values.get("uid") or request.values.get("user_id") or "") or 0)
    except ValueError:
        uid = 0
    try:
        res = record_action(uid, request.values.get("act") or request.values.get("a") or "")
    except Exception as e:                      # никаких 500 для PuzzleBot
        log.error("action failed: %s", e)
        res = {"ok": False, "reason": "error"}
    if res.get("ok"):
        notify_action(res["label"], res["act"], res["points"])
    log.info("action uid=%s act=%s ok=%s %s", uid, res.get("act"), res.get("ok"), res.get("reason", ""))
    return jsonify(**res)

def tg_webapp_uid(init_data, max_age=86400):
    """Telegram ID из initData мини-приложения, если подпись верна (HMAC на токене бота, как в документации
    Telegram WebApp). Подделать без токена бота нельзя. Неверно/нет токена/старше суток — 0."""
    if not (BOT_TOKEN and init_data):
        return 0
    try:
        from urllib.parse import parse_qsl
        d = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        got = d.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(got, want):
            return 0
        if max_age and time.time() - int(d.get("auth_date", "0")) > max_age:
            return 0
        return int((json.loads(d.get("user") or "{}")).get("id") or 0)
    except Exception:
        return 0

_ACT_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<meta http-equiv="refresh" content="4;url=__URL__"><title>Открываем…</title>
<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0d14;color:#9aa4b2;font:15px system-ui,sans-serif}</style>
</head><body>Открываем…<script>
(function(){var to=__JSURL__,done=false;function go(){if(!done){done=true;location.replace(to)}}
var h=new URLSearchParams(location.hash.slice(1)),q=new URLSearchParams(location.search);
var b=new URLSearchParams();b.set("init",h.get("tgWebAppData")||"");b.set("u",q.get("u")||"");
try{fetch(location.pathname,{method:"POST",body:b,keepalive:true,credentials:"omit"}).then(go,go)}catch(e){go()}
setTimeout(go,1500)})();
</script></body></html>"""

@app.route("/a/<act>", methods=["GET", "POST"])
def act_link(act):
    """Переходник для кнопок-ссылок бота. GET — страница, которая отправляет кто открыл и сразу ведёт дальше;
    POST — засчитать действие. Человек = подписанный initData мини-приложения (надёжно), иначе ?u=<id>
    из ссылки (для кнопок «в браузере»; засчитывается только тем, у кого есть засчитанный старт)."""
    act = (act or "").strip().lower()
    to = ACT_LINKS.get(act)
    if not to:
        abort(404)
    if request.method == "GET":
        html = _ACT_PAGE.replace("__JSURL__", json.dumps(to)).replace("__URL__", to.replace('"', "%22"))
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp
    uid, how = tg_webapp_uid(request.values.get("init", "")), "webapp"
    if not uid:
        how = "link"
        try:
            uid = int(re.sub(r"\D", "", request.values.get("u") or "")[:15] or 0)
        except ValueError:
            uid = 0
    try:
        res = record_action(uid, act) if uid else {"ok": False, "reason": "no_uid"}
    except Exception as e:
        log.error("act link failed: %s", e)
        res = {"ok": False, "reason": "error"}
    if res.get("ok"):
        notify_action(res["label"], res["act"], res["points"])
    log.info("act link act=%s via=%s ok=%s %s", act, how, res.get("ok"), res.get("reason", ""))
    return jsonify(ok=bool(res.get("ok")))

@app.route("/api/stats")
def api_stats():
    role = _need()
    st = get_stats()
    st["role"] = role
    return jsonify(st)

def _int_or(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d

def feed_since(c, sc, ss):
    """Новые засчитанные удары (id > sc) и апперкоты (id > ss). Возвращает (items, новый sc, новый ss)."""
    items = []
    if sc is not None:
        for r in c.execute("SELECT id, ts, source, city, cc, lat, lon FROM clicks "
                           "WHERE id>? AND scored=1 ORDER BY id LIMIT 300", (sc,)):
            items.append({"kind": "hit", "id": r["id"], "ts": r["ts"], "source": r["source"],
                          "team": team_of(r["source"]), "city": r["city"] or "", "cc": r["cc"] or "",
                          "lat": r["lat"], "lon": r["lon"]})
            sc = max(sc, r["id"])
    if ss is not None:
        for r in c.execute("SELECT s.id, s.ts, s.label, k.city, k.cc, k.lat, k.lon FROM starts s "
                           "LEFT JOIN clicks k ON k.id = s.click_id "
                           "WHERE s.id>? AND s.scored=1 ORDER BY s.id LIMIT 300", (ss,)):
            items.append({"kind": "start", "id": r["id"], "ts": r["ts"], "source": r["label"],
                          "team": team_of(r["label"]), "city": r["city"] or "", "cc": r["cc"] or "",
                          "lat": r["lat"], "lon": r["lon"]})
            ss = max(ss, r["id"])
    items.sort(key=lambda x: x["ts"])
    return items, sc, ss

def actions_since(c, sa):
    """Действия в боте (id > sa) — тоже идут в ленту: kind = код действия, points = очки."""
    items = []
    for r in c.execute("SELECT a.id, a.ts, a.act, a.label, a.points, k.city, k.cc, k.lat, k.lon FROM actions a "
                       "LEFT JOIN starts s ON s.id = a.start_id LEFT JOIN clicks k ON k.id = s.click_id "
                       "WHERE a.id>? ORDER BY a.id LIMIT 300", (sa,)):
        items.append({"kind": r["act"], "id": r["id"], "ts": r["ts"], "source": r["label"], "team": team_of(r["label"]),
                      "points": r["points"], "text": ACTIONS.get(r["act"], (0, ""))[1],
                      "city": r["city"] or "", "cc": r["cc"] or "", "lat": r["lat"], "lon": r["lon"]})
        sa = max(sa, r["id"])
    return items, sa

def _cursors(c):
    return (c.execute("SELECT COALESCE(MAX(id),0) FROM clicks").fetchone()[0],
            c.execute("SELECT COALESCE(MAX(id),0) FROM starts").fetchone()[0])

def _cursor_a(c):
    return c.execute("SELECT COALESCE(MAX(id),0) FROM actions").fetchone()[0]

@app.route("/api/feed")
def api_feed():
    """Лента для арены: засчитанные удары и апперкоты. Без since — только курсоры (историю не проигрываем)."""
    _need()
    sc = request.args.get("since_c", request.args.get("since"))
    ss = request.args.get("since_s")
    sa = request.args.get("since_a")
    with _conn() as c:
        last_c, last_s = _cursors(c)
        last_a = _cursor_a(c)
        items = []
        if sc is not None or ss is not None:
            items, _a, _b = feed_since(c, _int_or(sc, last_c) if sc is not None else None,
                                       _int_or(ss, last_s) if ss is not None else None)
        if sa is not None:
            items += actions_since(c, _int_or(sa, last_a))[0]
            items.sort(key=lambda x: x["ts"])
    return jsonify(last_c=last_c, last_s=last_s, last_a=last_a, last_id=last_c, items=items)

@app.route("/api/stream")
def api_stream():
    """Живая лента (Server-Sent Events): удар долетает до арены за ~1 секунду.
    Соединение живёт 5 минут, браузер сам переподключается с Last-Event-ID (без повторов)."""
    _need()
    sc, ss, sa = request.args.get("since_c"), request.args.get("since_s"), request.args.get("since_a")
    last_id = (request.headers.get("Last-Event-ID", "") or "")
    if re.fullmatch(r"\d+:\d+(:\d+)?", last_id):
        parts = last_id.split(":")
        sc, ss = parts[0], parts[1]
        sa = parts[2] if len(parts) > 2 else sa

    def gen(sc=sc, ss=ss, sa=sa):
        with _conn() as c:
            lc, ls = _cursors(c)
            la = _cursor_a(c)
        sc, ss, sa = _int_or(sc, lc), _int_or(ss, ls), _int_or(sa, la)
        yield f"retry: 2000\nid: {sc}:{ss}:{sa}\ndata: {json.dumps({'hello': 1, 'last_c': sc, 'last_s': ss, 'last_a': sa})}\n\n"
        t0 = beat = time.time()
        while time.time() - t0 < 300:
            with _conn() as c:
                items, sc, ss = feed_since(c, sc, ss)
                acts, sa = actions_since(c, sa)
            if acts:
                items = sorted(items + acts, key=lambda x: x["ts"])
            if items:
                beat = time.time()
                yield f"id: {sc}:{ss}:{sa}\ndata: {json.dumps({'items': items, 'last_c': sc, 'last_s': ss, 'last_a': sa})}\n\n"
            elif time.time() - beat > 15:
                beat = time.time()
                yield ": ping\n\n"
            time.sleep(1)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- личные страницы партнёров ----------
def _partner_secret():
    return (PARTNER_SECRET or DASHBOARD_TOKEN or "neurofans").encode()

def partner_key(label):
    return hmac.new(_partner_secret(), b"p:" + label.encode(), hashlib.sha256).hexdigest()[:12]

def _base_url():
    if BASE_URL:
        return BASE_URL
    try:
        return request.host_url.rstrip("/")
    except Exception:
        return ""

def partner_link(label):
    return f"{_base_url()}/p/{label}?k={partner_key(label)}"

def _partner_auth(label):
    """Админ/публичный токен или личный ключ ?k= этого партнёра."""
    r = _role()
    if r:
        return r
    if hmac.compare_digest(request.args.get("k", ""), partner_key(label)):
        return "partner"
    abort(401)

def all_labels():
    return [f"{pref}{i:02d}" for _k, (_n, pref, _c) in TEAMS.items() for i in range(1, PER_TEAM + 1)]

@app.route("/api/partner/<label>")
def api_partner(label):
    label = clean_label(label)
    role = _partner_auth(label)
    tk = team_of(label)
    if not tk:
        abort(404)
    st = get_stats()
    plist = [p for p in st["partners"] if p["team"]]
    me = next((p for p in plist if p["source"] == label), None)
    if me is None:
        ri, rn, nxt = rank_of(0)
        me = {"source": label, "team": tk, "clicks": 0, "hits": 0, "uniq": 0, "starts": 0, "last": None,
              "hits_today": 0, "starts_today": 0, "today": 0, "power": 0, "conv": None,
              "rank": ri, "rank_name": rn, "rank_next": nxt, "badges": []}
        plist = plist + [me]
    order = sorted(plist, key=lambda p: (-p["today"], -p["power"], p["source"]))
    place = order.index(me) + 1
    ahead = order[place - 2] if place > 1 else None
    team_order = [p for p in order if p["team"] == tk]
    with _conn() as c:
        hist = round_history(c).get(label, {})
        chans = {r[0] or "": [r[1] or 0, 0] for r in c.execute(
            "SELECT ch, SUM(scored) FROM clicks WHERE source=? GROUP BY ch", (label,))}
        for ch, n in c.execute("SELECT ch, SUM(scored) FROM starts WHERE label=? GROUP BY ch", (label,)):
            chans.setdefault(ch or "", [0, 0])[1] = n or 0
    start, _e = round_bounds()
    cur = round_idx(start)
    days = [{"idx": i, "date": round_end_of(i).strftime("%d.%m"), "power": _pw(hist.get(i, [0, 0])),
             "hits": hist.get(i, [0, 0])[0], "starts": hist.get(i, [0, 0])[1],
             "actions": (hist.get(i) or [0, 0, 0])[2]} for i in range(cur - 13, cur + 1)]
    with _conn() as c:
        acts = [{"act": r[0], "text": ACTIONS.get(r[0], (0, ""))[1], "count": r[1], "points": r[2] or 0}
                for r in c.execute("SELECT act, COUNT(*), SUM(points) FROM actions WHERE label=? GROUP BY act ORDER BY 3 DESC", (label,))]
    channels = [{"ch": k, "name": CHANNEL_NAMES.get(k, k), "hits": v[0], "starts": v[1],
                 "power": v[0] + START_POWER * v[1], "conv": _conv(v[1], v[0])} for k, v in chans.items()]
    channels.sort(key=lambda x: -x["power"])
    best = channels[0]["name"] if channels and channels[0]["power"] > 0 else None
    team = next(t for t in st["teams"] if t["key"] == tk)
    return jsonify(role=role, me=me, team=team, teams=st["teams"], place=place, of=len(order),
                   team_place=team_order.index(me) + 1, team_of=len(team_order),
                   ahead=({"source": ahead["source"], "today": ahead["today"],
                           "gap": ahead["today"] - me["today"] + 1} if ahead else None),
                   days=days, channels=channels, best_channel=best, actions=acts,
                   links={"threads": f"{_base_url()}/t/{label}", "video": f"{_base_url()}/v/{label}"},
                   ranks=st["ranks"], badge_defs=st["badge_defs"], start_power=START_POWER,
                   round_end=st["round_end"], round_no=st["round_no"], server_now=st["server_now"])

def _page(path, og_title, og_desc, og_img):
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return Response("<h1>страница не найдена</h1>", mimetype="text/html", status=404)
    e = lambda v: (v or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    meta = (f'<meta property="og:type" content="website"><meta property="og:site_name" content="NeuroFans Arena">'
            f'<meta property="og:title" content="{e(og_title)}"><meta property="og:description" content="{e(og_desc)}">'
            f'<meta property="og:image" content="{e(og_img)}"><meta property="og:image:width" content="1200">'
            f'<meta property="og:image:height" content="630"><meta name="twitter:card" content="summary_large_image">')
    # no-cache: после выкатки браузер всегда берёт свежую арену/страницу
    return Response(html.replace("<!--OG-->", meta), mimetype="text/html", headers={"Cache-Control": "no-cache"})

def _score_line():
    a, b = get_stats()["teams"]
    return f"{a['name']} {a['today']} : {b['today']} {b['name']}"

@app.route("/p/<label>")
def partner_page(label):
    label = clean_label(label)
    _partner_auth(label)
    if not team_of(label):
        abort(404)
    q = f"k={partner_key(label)}"
    return _page(PARTNER_PATH, f"{label} — личный кабинет бойца", _score_line(),
                 f"{_base_url()}/og.png?label={label}&{q}")

@app.route("/og.png")
def og_png():
    """Превью ссылки (Telegram и др.): текущий счёт боя картинкой 1200×630."""
    label = clean_label(request.args.get("label", "")) if request.args.get("label") else None
    if label:
        _partner_auth(label)
    else:
        _need()
    try:
        import cards
        png = cards.og(get_stats(), fonts=os.path.join(HERE, "static", "fonts"), label=label)
    except Exception as e:
        log.warning("og render failed: %s", e)
        abort(503)
    return Response(png, mimetype="image/png", headers={"Cache-Control": "public, max-age=60"})

@app.route("/story.png")
def story_png():
    label = clean_label(request.args.get("label", "")) if request.args.get("label") else None
    if label:
        _partner_auth(label)
    else:
        _need()
    png = round_card_png("current" if request.args.get("round") == "current" else "last", label)
    if not png:
        abort(503)
    dl = {"Content-Disposition": "inline; filename=neurofans-round.png", "Cache-Control": "no-store"}
    return Response(png, mimetype="image/png", headers=dl)

@app.route("/export/partner_links")
def export_partner_links():
    _need(admin=True)
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["team", "label", "personal_page", "threads_link", "video_link"])
    for lab in all_labels():
        w.writerow([TEAMS[team_of(lab)][0], lab, partner_link(lab), f"{_base_url()}/t/{lab}", f"{_base_url()}/v/{lab}"])
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=partner_pages.csv"})

@app.route("/arena")
def arena():
    _need()
    qs = "&".join(f"{k}={quote(request.args.get(k, ''))}" for k in ("token", "p", "k") if request.args.get(k))
    return _page(ARENA_PATH, "NeuroFans Arena — бой команд в прямом эфире", _score_line(),
                 f"{_base_url()}/og.png?{qs}")

@app.route("/export")
def export():
    _need(admin=True)
    return Response(csv_bytes(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=clicks.csv"})

@app.route("/export/actions")
def export_actions():
    _need(admin=True)
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["ts", "label", "team", "action", "points", "telegram_id"])
    with _conn() as c:
        for r in c.execute("SELECT ts,label,act,points,tg_uid FROM actions ORDER BY id"):
            tk = team_of(r["label"])
            w.writerow([r["ts"], r["label"], TEAMS[tk][0] if tk else "", r["act"], r["points"], r["tg_uid"]])
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=actions.csv"})

@app.route("/admin/rescore")
def admin_rescore():
    """Пересчитать удары по текущим правилам (после смены фильтров). Только админ."""
    _need(admin=True)
    return jsonify(rescore_all())

@app.route("/export/starts")
def export_starts():
    _need(admin=True)
    return Response(starts_csv_bytes(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=starts.csv"})

@app.route("/")
def index():
    _need(admin=True)
    return Response(PAGE, mimetype="text/html")

# ── Воронка бота (27.11.2026): из стартов за период — сколько дошли до каждого шага ──────────────
FUNNEL_STEPS = [("start", "Старт в боте"), ("examples", "Примеры"), ("sim", "Симулятор"), ("site", "Сайт"),
                ("program", "Программа"), ("intent", "Хочу результат"), ("pay_open", "Открыл оплату"),
                ("paid", "Оплатил")]
CH_NAMES = {"t": "Threads", "v": "Видео (Shorts/Reels/TikTok)", "": "Без канала"}

def funnel(days=7, by="ch"):
    """Когорта = люди, впервые стартовавшие за последние `days` дней (0 — за всё время).
    Для каждого — какие шаги сделал (за всё время после старта). Плюс клики → старт по каналу."""
    since = ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).replace(tzinfo=None)
             .isoformat(timespec="seconds")) if days else "0"
    with _conn() as c:
        first = {}
        for r in c.execute("SELECT tg_uid, MIN(ts) ts, label, ch FROM starts WHERE tg_uid IS NOT NULL "
                           "GROUP BY tg_uid HAVING MIN(ts)>=?", (since,)):
            first[r["tg_uid"]] = (r["label"] or "", r["ch"] or "")
        acts = {}
        for r in c.execute("SELECT tg_uid, act FROM actions WHERE tg_uid IN (SELECT tg_uid FROM starts WHERE ts>=?)", (since,)):
            a = "pay_open" if r["act"] in ("checkout", "checkout_base") else r["act"]
            acts.setdefault(r["tg_uid"], set()).add(a)
        clicks = {}
        for r in c.execute("SELECT ch, COUNT(DISTINCT vid) n FROM clicks WHERE ts>=? AND (scored=1 OR why='desktop') GROUP BY ch", (since,)):
            clicks[r["ch"] or ""] = r["n"]
    def key(uid):
        lab, ch = first[uid]
        if by == "label": return lab or "—"
        if by == "team": return TEAMS[team_of(lab)][0] if team_of(lab) else "—"
        return CH_NAMES.get(ch, ch or "Без канала")
    groups = {"Все": list(first)}
    for uid in first:
        groups.setdefault(key(uid), []).append(uid)
    rows = []
    for g, uids in groups.items():
        n = len(uids)
        steps = {"start": n}
        for k, _ in FUNNEL_STEPS[1:]:
            steps[k] = sum(1 for u in uids if k in acts.get(u, ()))
        row = {"group": g, "steps": steps}
        if by == "ch":
            cl = sum(clicks.values()) if g == "Все" else next((v for ch, v in clicks.items() if CH_NAMES.get(ch, ch) == g), None)
            row["clicks"] = cl
        rows.append(row)
    rows.sort(key=lambda r: (r["group"] != "Все", -r["steps"]["start"]))
    return {"days": days, "by": by, "steps": FUNNEL_STEPS, "rows": rows}

@app.route("/api/funnel")
def api_funnel():
    _need(admin=True)
    try:
        days = max(0, min(365, int(request.args.get("days", 7))))
    except ValueError:
        days = 7
    by = request.args.get("by", "ch")
    return jsonify(funnel(days, by if by in ("ch", "label", "team") else "ch"))

@app.route("/funnel")
def funnel_page():
    _need(admin=True)
    return Response(FUNNEL_PAGE, mimetype="text/html")

FUNNEL_PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Воронка бота</title><style>
body{margin:0;background:#04050c;color:#e9edf8;font:15px/1.5 system-ui,-apple-system,sans-serif;padding:20px}
h1{font-size:22px;margin:0 0 4px}p.s{color:#8a93ae;margin:0 0 16px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.bar button{background:#0a0e1c;border:1px solid #283255;color:#c4cade;border-radius:10px;padding:8px 12px;cursor:pointer}
.bar button.on{border-color:#2ee6d6;color:#fff;background:rgba(46,230,214,.12)}
.wrap{overflow-x:auto}table{border-collapse:collapse;min-width:760px;width:100%}th,td{padding:9px 10px;border-bottom:1px solid #1b2238;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:#8a93ae;font-weight:600;font-size:13px}td b{font-size:16px}td i{display:block;font-style:normal;font-size:12px;color:#8a93ae}
td.lo i{color:#ff7d6e}td.hi i{color:#2ee6d6}tr.all td{background:rgba(46,230,214,.05)}.note{color:#8a93ae;font-size:13px;margin-top:14px;max-width:760px}
</style></head><body><h1>Воронка бота</h1><p class="s">Люди, впервые нажавшие «Старт» за период, и сколько из них дошли до каждого шага. Процент под числом: от старта (и от прошлого шага).</p>
<div class="bar" id="d"></div><div class="bar" id="b"></div><div class="wrap"><table id="t"></table></div>
<p class="note">«Открыл оплату» — нажал любую кнопку оплаты (Базовый или Продвинутый). «Оплатил» — пришёл вебхук Tribute. Клики — уникальные люди по ссылкам за период (с телефона, без сканеров). Шаги считаются один раз на человека.</p>
<script>
const Q=new URLSearchParams(location.search),tok=Q.get("token")||"";let days=+(Q.get("days")||7),by=Q.get("by")||"ch";
const D=[[1,"Сутки"],[7,"7 дней"],[30,"30 дней"],[0,"Всё время"]],B=[["ch","По каналам"],["team","По командам"],["label","По партнёрам"]];
function bar(id,list,cur,set){document.getElementById(id).innerHTML=list.map(([v,n])=>`<button class="${v==cur?'on':''}" data-v="${v}">${n}</button>`).join("");
 document.getElementById(id).onclick=e=>{const b=e.target.closest("button");if(b){set(b.dataset.v);load()}}}
const pct=(a,b)=>b?Math.round(a*1000/b)/10+"%":"—";
async function load(){bar("d",D,days,v=>days=+v);bar("b",B,by,v=>by=v);
 const r=await fetch(`/api/funnel?token=${encodeURIComponent(tok)}&days=${days}&by=${by}`);const j=await r.json();
 const hasC=by==="ch";let h=`<tr><th>${by==="label"?"Партнёр":by==="team"?"Команда":"Канал"}</th>${hasC?"<th>Клики</th>":""}`+j.steps.map(s=>`<th>${s[1]}</th>`).join("")+"</tr>";
 for(const row of j.rows){const st=row.steps,n=st.start;h+=`<tr class="${row.group==="Все"?"all":""}"><td>${row.group}</td>`;
  if(hasC)h+=`<td><b>${row.clicks??"—"}</b><i>→ старт ${row.clicks?pct(n,row.clicks):"—"}</i></td>`;
  let prev=n;j.steps.forEach(([k],i)=>{const v=st[k];const p=n?v/n:0;const cls=i&&n>=10?(p<.05?"lo":p>.3?"hi":""):"";
   h+=`<td class="${cls}"><b>${v}</b><i>${i?pct(v,n)+" ("+pct(v,prev)+")":""}</i></td>`;prev=v||prev});h+="</tr>"}
 document.getElementById("t").innerHTML=h}
load();
</script></body></html>"""

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
  <b>Лидерборд партнёров</b> &nbsp; <a id="csv" href="#">CSV кликов</a> · <a id="csvS" href="#">CSV стартов</a> · <a id="csvP" href="#">Личные страницы партнёров (CSV)</a> · <a id="story" href="#" target="_blank">Карточка раунда</a> · <a id="arenaL" href="#" target="_blank">Арена</a>
  <div class="tw"><table><thead><tr><th class="rk">#</th><th>Партнёр</th><th>Команда</th><th>Сила</th><th>Удары</th><th>В бот</th>
    <th>Доходимость</th><th>Раунд</th><th>Клики</th><th>Последний</th></tr></thead>
  <tbody id="rows"></tbody></table></div>
</div>
<p class="foot">Обновляется каждые 30 сек. Удар = уникальный человек по ссылке партнёра (1 раз за раунд). Апперкот = человек дошёл до бота (+<span id="sp">3</span> к силе).
Доходимость = сколько людей из засчитанных ударов нажали «Старт» в боте.</p>
<script>
const token=new URLSearchParams(location.search).get('token');
const q=token?('?token='+encodeURIComponent(token)):'';
document.getElementById('csv').href='/export'+q;document.getElementById('csvS').href='/export/starts'+q;document.getElementById('csvP').href='/export/partner_links'+q;document.getElementById('story').href='/story.png'+q;document.getElementById('arenaL').href='/arena'+q;
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
  document.getElementById('rows').innerHTML=d.partners.map((p,i)=>`<tr><td class="rk">${medals[i]||(i+1)}</td><td>${p.team?`<a href="/p/${encodeURIComponent(p.source)}${q}" target="_blank">${esc(p.source)}</a>`:esc(p.source)}</td><td>${tb(p.team,A,B)}</td>
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
    threading.Thread(target=geo_updater, daemon=True).start()
    log.info("NeuroFans on :%s (bot=@%s, admins=%d, daily=%s, teams=%s/%s, starts=%s)",
             PORT, BOT_USERNAME, len(ADMIN_IDS), DAILY_SUMMARY_TIME or "off",
             TEAM_A_PREFIX, TEAM_B_PREFIX, "on" if INGEST_TOKEN else "off")
    app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)

if __name__ == "__main__":
    main()
