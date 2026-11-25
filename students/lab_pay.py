"""Оплата курса через Tribute → доступ в кабинет.

Поток: ученик платит в Tribute (цифровой товар курса) → Tribute шлёт вебхук → мы записываем покупку
по Telegram ID. В самом товаре выдаётся ссылка t.me/<бот кабинета>?start=buy → ученик жмёт → бот находит
оплату и присылает ЛИЧНУЮ одноразовую ссылку регистрации (ник и пароль ученик придумывает сам, пароли
в Telegram не пересылаем). При регистрации по такой ссылке Telegram привязывается сам, тариф курса ставится сам.

Тарифы курса (не путать с тарифами ИИ Тест/Базовый/Pro): base — Базовый, adv — Продвинутый.
Товар up — доплата с Базового до Продвинутого. Настройка: TRIBUTE_COURSE="111:base,222:adv,333:up".
Здесь только функции над базой — без сети. Отправку сообщений делают lab.py и lab_notify.py.
"""
import secrets

KINDS = ("base", "adv", "up")
RANK = {None: 0, "": 0, "base": 1, "adv": 2}
COURSE_NAME = {"base": "Базовый", "adv": "Продвинутый"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases(ref TEXT PRIMARY KEY, tg_id INTEGER, product_id INTEGER, kind TEXT,
  created TEXT, refunded TEXT);
CREATE INDEX IF NOT EXISTS ix_purchases_tg ON purchases(tg_id);
"""
USER_COLS = (("course", "TEXT"), ("course_admin", "TEXT"))
INVITE_COLS = (("tg_id", "INTEGER"),)


def migrate(c):
    c.executescript(SCHEMA)
    for table, cols in (("users", USER_COLS), ("invites", INVITE_COLS)):
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols:
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def parse_course(s):
    """TRIBUTE_COURSE="111:base, 222:adv,333:up" → {111: "base", 222: "adv", 333: "up"}."""
    out = {}
    for part in (s or "").split(","):
        if ":" in part:
            a, b = (x.strip() for x in part.split(":", 1))
            if a.isdigit() and b in KINDS:
                out[int(a)] = b
    return out


def best(*tiers):
    return max(tiers, key=lambda t: RANK.get(t, 0)) or None


# ---------- покупки ----------
def record(c, tg, product_id, kind, ref, now_iso):
    """Записать покупку. Повтор того же ref (Tribute шлёт вебхук ещё раз) — ничего не меняет."""
    cur = c.execute("INSERT OR IGNORE INTO purchases(ref,tg_id,product_id,kind,created) VALUES(?,?,?,?,?)",
                    (str(ref), int(tg), int(product_id), kind, now_iso))
    return bool(cur.rowcount)


def refund(c, tg, product_id, ref, now_iso, course_ids):
    """Возврат: по purchase_id, а если его нет — последняя живая покупка этого товара (или любого товара курса)."""
    row = c.execute("SELECT ref FROM purchases WHERE ref=? AND refunded IS NULL", (str(ref),)).fetchone() if ref else None
    if not row and tg:
        ids = [int(product_id)] if product_id and int(product_id) in course_ids else list(course_ids)
        if ids:
            q = ",".join("?" * len(ids))
            row = c.execute(f"SELECT ref FROM purchases WHERE tg_id=? AND refunded IS NULL AND product_id IN ({q}) "
                            "ORDER BY created DESC LIMIT 1", (int(tg), *ids)).fetchone()
    if not row:
        return False
    c.execute("UPDATE purchases SET refunded=? WHERE ref=?", (now_iso, row[0]))
    return True


def tier_by_tg(c, tg, floor=None):
    """Тариф курса по живым (без возврата) покупкам. Доплата up поднимает до Продвинутого только поверх Базового."""
    kinds = {r[0] for r in c.execute("SELECT kind FROM purchases WHERE tg_id=? AND refunded IS NULL", (int(tg),))} if tg else set()
    t = "adv" if "adv" in kinds else "base" if "base" in kinds else None
    if "up" in kinds and best(t, floor) == "base":
        t = "adv"
    return t


# ---------- ученики ----------
def user_by_tg(c, tg):
    return c.execute("SELECT * FROM users WHERE tg_chat_id=? ORDER BY id LIMIT 1", (int(tg),)).fetchone() if tg else None


def sync_user(c, uid):
    """Пересчитать тариф курса ученика: максимум из покупок (по привязанному Telegram) и выданного куратором."""
    u = c.execute("SELECT tg_chat_id, course_admin FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return None
    t = best(tier_by_tg(c, u["tg_chat_id"], u["course_admin"]), u["course_admin"])
    c.execute("UPDATE users SET course=? WHERE id=?", (t, uid))
    return t


def invite_for(c, tg, now_iso):
    """Личная одноразовая ссылка регистрации для этого Telegram: та же, пока ею не воспользовались."""
    row = c.execute("SELECT code FROM invites WHERE tg_id=? AND uses<max_uses ORDER BY created DESC LIMIT 1",
                    (int(tg),)).fetchone()
    if row:
        return row[0]
    while True:
        code = "NF-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        if not c.execute("SELECT 1 FROM invites WHERE code=?", (code,)).fetchone():
            break
    c.execute("INSERT INTO invites(code,note,created,max_uses,tg_id) VALUES(?,?,?,1,?)",
              (code, "оплата Tribute", now_iso, int(tg)))
    return code


def on_register(c, uid, invite_row):
    """Регистрация по личной ссылке после оплаты: привязать Telegram и поставить тариф курса."""
    tg = invite_row["tg_id"] if "tg_id" in invite_row.keys() else None
    if not tg:
        return None
    c.execute("UPDATE users SET tg_chat_id=NULL WHERE tg_chat_id=? AND id<>?", (int(tg), uid))
    c.execute("UPDATE users SET tg_chat_id=?, tg_mode=COALESCE(tg_mode,'all') WHERE id=?", (int(tg), uid))
    return sync_user(c, uid)


def welcome(c, tg, lab_host, now_iso):
    """Что ответить в боте на «/start buy» (и что отправить сразу после оплаты). None — оплаты нет."""
    u = user_by_tg(c, tg)
    if u:
        if not tier_by_tg(c, tg, u["course_admin"]):      # без оплаты (тариф только от куратора) — не «оплата получена»
            return None
        t = sync_user(c, u["id"])
        return (f"✅ Оплата получена, доступ открыт: тариф «{COURSE_NAME[t]}».\n\n"
                f"Твой кабинет — {u['nick']}. Вход: https://{lab_host}/")
    t = tier_by_tg(c, tg)
    if not t:
        return None
    code = invite_for(c, tg, now_iso)
    return (f"✅ Оплата получена, тариф «{COURSE_NAME[t]}». Добро пожаловать в NeuroFans!\n\n"
            f"Твоя личная ссылка для входа в кабинет ученика (работает один раз, никому не пересылай):\n"
            f"https://{lab_host}/?invite={code}\n\n"
            "Открой её, придумай ник и пароль — и ты внутри: уроки, аналитика ссылок и Студия. "
            "Уведомления сюда, в этот чат, подключатся сами.\n\n"
            "Уже есть кабинет? Не регистрируйся заново: войди в него и нажми меню → «Уведомления в Telegram» "
            "→ «Подключить», тариф подтянется сам.")
