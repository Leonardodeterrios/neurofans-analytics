"""Забота об учениках через бот кабинета: план первой недели, просьба об отзыве, переписка с куратором.

1) План первой недели (купил курс и зарегистрировался): 7 коротких сообщений, по одному в день, по урокам.
   Отправляем только в своё окно (сутки дня N от регистрации) — старым ученикам пачкой не пишем.
2) Отзыв: через 14 дней после покупки курса бот просит пару слов и скрин. Ответ сохраняется в reviews,
   ученика спрашиваем кнопками, можно ли показать отзыв на сайте без ника. На сайт ставим только с «да».
3) Любое сообщение ученика боту (не команда) уходит куратору в admin_chat с меткой #u<id>; куратор отвечает
   «ответом» на это сообщение — бот пересылает текст ученику. Так у ученика есть живая связь в одном чате.
Здесь только логика над базой и Notifier; сеть — через N.http / N._send.
"""
import datetime as dt
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, created TEXT, text TEXT,
  photo TEXT, consent INTEGER, hidden INTEGER DEFAULT 0);
"""

CAB = "https://{host}/"
# день → (заголовок, текст для Базового, текст для Продвинутого или None = тот же)
WEEK = {
    1: ("День 1: старт и сервисы",
        "Сегодня уроки 0 и 1: план обучения и оплата нейросетей из России без иностранной карты. "
        "Цель дня простая: открыть все сервисы и закинуть немного денег на баланс.", None),
    2: ("День 2: своя модель",
        "Урок 2: придумай внешность и характер модели и сделай первые 100 фото одного и того же лица. "
        "Не гонись за идеалом, главное, чтобы лицо узнавалось на всех кадрах.", None),
    3: ("День 3: 18+ с живой кожей",
        "Урок 3: откровенные фото без эффекта пластика. Сравни свои кадры с примерами из урока: "
        "текстура кожи, свет, лёгкий беспорядок в кадре. Модель всегда взрослая, 21+.", None),
    4: ("День 4: референсы",
        "Уроки 4 и 5: позы и сцены по референсу, референс плюс промпт. К вечеру у тебя должны получаться "
        "кадры под любой запрос.", None),
    5: ("День 5: кастомы и сеты",
        "Уроки 6 и 8: заказ под фаната и готовый фотосет на продажу. Собери свой первый сет из 8–10 кадров.", None),
    6: ("День 6: закрепляем",
        "Сегодня без новых уроков: доведи до конца сет и выбери 20 лучших кадров модели. Загляни в кабинет, "
        "в Студию: она поможет с паспортом модели и планом постов.",
        "Модуль про Fanvue: профиль, BIO, цены, платные посты и автосообщения. К вечеру у модели должна быть "
        "своя страница."),
    7: ("День 7: итоги недели",
        "Неделя позади: у тебя есть модель, 18+ кадры и первый сет. Дальше по желанию: продажи, видео и трафик "
        "проходят в Продвинутом, доплата есть в кабинете.",
        "Трафик и ссылки: прогрев соцсетей по плану и ссылки из кабинета в био. Поставь ссылку, нажми в кабинете "
        "«Проверить» и смотри, как приходят первые люди."),
}
TAIL = "\n\nЗастрял(а) или есть вопрос? Просто напиши сюда, ответ придёт от куратора."
REVIEW_DAY = 14
REVIEW_ASK = ("Прошло две недели с начала курса. Как успехи? 🙂\n\n"
              "Напиши в ответ пару слов: что получилось, что было сложно. Если есть скрин первых денег или "
              "любимый кадр модели, пришли его тоже. Это поможет нам сделать курс лучше, а новичкам поверить в себя.")


def migrate(c):
    c.executescript(SCHEMA)


def _ts(s):
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def scan(N, c, now):
    """Кладёт в очередь Notifier сообщения плана недели и просьбы об отзыве. Идемпотентно (ключи UNIQUE)."""
    q = 0
    host = N.lab_host
    for u in c.execute("SELECT id, created, course FROM users WHERE blocked=0 AND tg_chat_id IS NOT NULL "
                       "AND course IN ('base','adv')").fetchall():
        t0 = _ts(u["created"])
        if not t0:
            continue
        age_h = (now - t0).total_seconds() / 3600
        for d, (title, base, adv) in WEEK.items():
            start = (d - 1) * 24 + 1                     # день 1 — через час после регистрации
            if start <= age_h < start + 24:             # окна не пересекаются: одно сообщение в день
                text = adv if (u["course"] == "adv" and adv) else base
                q += N.enqueue(c, u["id"], f"care:w{d}", f"📅 {title}\n\n{text}\n{CAB.format(host=host)}{TAIL}", "care", now)
    # отзыв: 14 дней после первой живой покупки курса
    for r in c.execute("SELECT u.id, MIN(p.created) first FROM purchases p JOIN users u ON u.tg_chat_id=p.tg_id "
                       "WHERE p.refunded IS NULL AND u.blocked=0 GROUP BY u.id").fetchall():
        t0 = _ts(r["first"])
        if t0 and REVIEW_DAY * 24 <= (now - t0).total_seconds() / 3600 < REVIEW_DAY * 24 + 72:
            q += N.enqueue(c, r["id"], "care:review", REVIEW_ASK, "care", now)
    return q


def _admin_chat(c):
    r = c.execute("SELECT v FROM kv WHERE k='admin_chat'").fetchone()
    return int(r[0]) if r and str(r[0]).lstrip("-").isdigit() else None


def review_window(c, uid, now):
    """Ученик ответил в течение 7 дней после просьбы об отзыве — сохраняем ответ как отзыв."""
    r = c.execute("SELECT sent_at FROM notify WHERE uid=? AND key='care:review' AND status='sent'", (uid,)).fetchone()
    t = _ts(r[0]) if r else None
    return bool(t and now - t < dt.timedelta(days=7))


def on_message(N, c, msg, now):
    """Не-командное сообщение в бот кабинета. True — обработано здесь."""
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    photo = (msg.get("photo") or [{}])[-1].get("file_id")
    admin = _admin_chat(c)
    # куратор отвечает ученику: reply на пересланное сообщение с меткой #u<id>
    if admin and chat == admin:
        rep = (msg.get("reply_to_message") or {})
        m = re.search(r"#u(\d+)", rep.get("text") or rep.get("caption") or "")
        if not m:
            return False
        u = c.execute("SELECT tg_chat_id, nick FROM users WHERE id=?", (int(m.group(1)),)).fetchone()
        if not (u and u["tg_chat_id"] and text):
            N._send(chat, "Не получилось: ученик отвязал Telegram или сообщение пустое.")
            return True
        r = N._send(u["tg_chat_id"], f"💬 Куратор:\n{text}")
        N._send(chat, f"✅ Отправлено {u['nick']}" if r.get("ok") else "❌ Не дошло: ученик, похоже, заблокировал бота.")
        return True
    u = c.execute("SELECT id, nick, course FROM users WHERE tg_chat_id=? AND blocked=0", (chat,)).fetchone()
    if not u or not (text or photo):
        return False
    rid = None
    if review_window(c, u["id"], now):
        rid = c.execute("INSERT INTO reviews(uid, created, text, photo) VALUES(?,?,?,?)",
                        (u["id"], now.isoformat(timespec="seconds"), text[:3000], photo)).lastrowid
        c.commit()
        N.http("sendMessage", {"chat_id": chat, "text": "Спасибо, это очень ценно! 🙏 Можно показать твой отзыв на сайте "
                               "курса? Без ника и без ссылок, только текст и картинка.",
                               "reply_markup": {"inline_keyboard": [[{"text": "Да, можно", "callback_data": f"rv:{rid}:1"},
                                                                     {"text": "Нет, только вам", "callback_data": f"rv:{rid}:0"}]]}}, 15)
    elif admin:
        N._send(chat, "Передал куратору, ответ придёт сюда же.")
    else:
        return False
    if admin:
        head = ("⭐ Отзыв" if rid else "✉️ Сообщение") + f" от {u['nick']} (тариф {u['course'] or 'нет'}) #u{u['id']}"
        body = f"{head}\n\n{text}" + ("\n\nОтветь на это сообщение — ответ уйдёт ученику." if not rid else "")
        if photo:
            N.http("sendPhoto", {"chat_id": admin, "photo": photo, "caption": body[:1000]}, 20)
        else:
            N._send(admin, body)
    return True


def on_callback(N, c, cb, now):
    data = cb.get("data") or ""
    m = re.fullmatch(r"rv:(\d+):([01])", data)
    chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
    if not m or not chat:
        return False
    rid, yes = int(m.group(1)), m.group(2) == "1"
    u = c.execute("SELECT id FROM users WHERE tg_chat_id=?", (chat,)).fetchone()
    if u:
        c.execute("UPDATE reviews SET consent=? WHERE id=? AND uid=?", (1 if yes else 0, rid, u["id"]))
        c.commit()
    N.http("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": "Спасибо!"}, 10)
    N._send(chat, "Отлично, покажем без ника. Спасибо! ❤️" if yes else "Хорошо, отзыв останется только у нас. Спасибо! ❤️")
    return True


def admin_list(c, limit=50):
    return [dict(r) for r in c.execute(
        "SELECT r.id, r.created, r.text, r.photo IS NOT NULL has_photo, r.consent, r.hidden, u.nick, u.course "
        "FROM reviews r JOIN users u ON u.id=r.uid ORDER BY r.id DESC LIMIT ?", (limit,))]
