#!/usr/bin/env python3
"""
NeuroFans — Telegram-бот с аналитикой источников.

Заход по ссылке  https://t.me/<BOT_USERNAME>?start=<имя_профиля>:
  • пишется в локальную базу (data/starts.db),
  • уходит в Google-таблицу (если задан SHEET_WEBHOOK_URL),
  • шлёт тебе уведомление в Telegram (если задан ADMIN_CHAT_ID).

Возможности:
  • инлайн-меню с кнопками (/start, /menu)
  • уведомления о каждом переходе (новый/повторный)
  • /stats — сводка по аккаунтам (админ)
  • /export — выгрузка всех переходов в CSV (админ)
  • ежедневная авто-сводка в заданное время (DAILY_SUMMARY_TIME)
  • /myid — узнать свой chat_id

Установка:  pip install -r requirements.txt
Запуск:     python neurofans_bot.py
"""
import os, io, csv, logging, sqlite3, datetime as dt
from zoneinfo import ZoneInfo
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, ContextTypes)

load_dotenv()
BOT_TOKEN          = os.environ["BOT_TOKEN"]
BOT_USERNAME       = os.getenv("BOT_USERNAME", "NeuroFansBot")
SHEET_WEBHOOK_URL  = os.getenv("SHEET_WEBHOOK_URL", "").strip()
ADMIN_CHAT_ID      = os.getenv("ADMIN_CHAT_ID", "").strip()
DB_PATH            = os.getenv("DB_PATH", "data/starts.db")
DAILY_SUMMARY_TIME = os.getenv("DAILY_SUMMARY_TIME", "").strip()   # напр. "09:00", пусто = выкл
SUMMARY_TZ         = os.getenv("SUMMARY_TZ", "UTC").strip()
# алерты «тишина по источнику»
SILENCE_HOURS      = int(os.getenv("SILENCE_HOURS", "24"))         # нет переходов столько часов -> алерт
SILENCE_CHECK_MIN  = int(os.getenv("SILENCE_CHECK_MIN", "60"))     # как часто проверять, минут
SILENCE_MIN_CLICKS = int(os.getenv("SILENCE_MIN_CLICKS", "3"))     # алерт только для источников с таким историческим минимумом

WELCOME_TEXT = (
    "Привет! Это обучающий бот проекта по созданию и ведению AI-моделей.\n\n"
    "Здесь — бесплатный гайд и материалы: как создать свою AI-модель и монетизировать её "
    "на Fanvue (с обязательной пометкой ИИ по правилам площадки).\n\n"
    "Важно: это инструмент и обучение, а не гарантия дохода — результат зависит от тебя.\n\n"
    "Выбирай кнопкой ниже 👇"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neurofans")


# ---------- локальная база ----------
def _conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS starts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, source TEXT, user_id INTEGER, username TEXT, first_name TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, source TEXT, kind TEXT)""")

def record_start(source, user) -> bool:
    with _conn() as c:
        seen = c.execute("SELECT 1 FROM starts WHERE user_id=? LIMIT 1", (user.id,)).fetchone()
        is_new = seen is None
        c.execute("INSERT INTO starts(ts,source,user_id,username,first_name) VALUES(?,?,?,?,?)",
                  (dt.datetime.utcnow().isoformat(timespec="seconds"),
                   source, user.id, user.username or "", user.first_name or ""))
    return is_new

def stats_text() -> str:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM starts").fetchone()[0]
        uniq  = c.execute("SELECT COUNT(DISTINCT user_id) FROM starts").fetchone()[0]
        rows  = c.execute("""SELECT source, COUNT(*) n, COUNT(DISTINCT user_id) u
                             FROM starts GROUP BY source ORDER BY n DESC""").fetchall()
    if not rows:
        return "📊 Пока нет переходов."
    lines = [f"📊 Всего заходов: {total}  |  уникальных: {uniq}", ""]
    lines += [f"• {s or 'direct'}: {n} заходов ({u} уник.)" for s, n, u in rows]
    return "\n".join(lines)

def csv_bytes() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "source", "user_id", "username", "first_name"])
    with _conn() as c:
        for row in c.execute("SELECT ts,source,user_id,username,first_name FROM starts ORDER BY id"):
            w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")   # utf-8-sig — чтобы Excel не ломал кириллицу


# ---------- меню ----------
def is_admin(chat_id) -> bool:
    return bool(ADMIN_CHAT_ID) and str(chat_id) == ADMIN_CHAT_ID

def menu_markup(chat_id) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📘 Бесплатный гайд", callback_data="guide"),
         InlineKeyboardButton("ℹ️ О продукте", callback_data="about")],
    ]
    if is_admin(chat_id):
        rows.append([InlineKeyboardButton("📊 Статистика", callback_data="stats"),
                     InlineKeyboardButton("📥 CSV", callback_data="csv")])
    return InlineKeyboardMarkup(rows)


# ---------- внешние приёмники ----------
async def push_sheet(source, user):
    if not SHEET_WEBHOOK_URL:
        return
    data = {"source": source, "user_id": user.id,
            "username": user.username or "", "first_name": user.first_name or ""}
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.post(SHEET_WEBHOOK_URL, json=data)
            log.info("sheet source=%s user=%s status=%s", source, user.id, r.status_code)
    except Exception as e:
        log.error("sheet push failed: %s", e)

async def notify_admin(context, source, user, is_new):
    if not ADMIN_CHAT_ID:
        return
    flag = "🆕 новый" if is_new else "↩️ повторный"
    who = f"@{user.username}" if user.username else (user.first_name or "—")
    txt = f"🔔 Переход в бота ({flag})\nИсточник: {source}\nКто: {who} (id {user.id})"
    try:
        await context.bot.send_message(int(ADMIN_CHAT_ID), txt)
    except Exception as e:
        log.error("admin notify failed: %s", e)

async def send_csv(bot, chat_id):
    doc = InputFile(io.BytesIO(csv_bytes()), filename="starts.csv")
    await bot.send_document(chat_id, document=doc, caption="Выгрузка переходов")


# ---------- хэндлеры ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = context.args[0] if context.args else "direct"
    user = update.effective_user
    is_new = record_start(source, user)
    await push_sheet(source, user)
    await notify_admin(context, source, user, is_new)
    await update.message.reply_text(WELCOME_TEXT, reply_markup=menu_markup(update.effective_chat.id))

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Меню:", reply_markup=menu_markup(update.effective_chat.id))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat_id = q.message.chat.id
    if data == "guide":
        await q.message.reply_text("Здесь ссылка/файл бесплатного гайда. (заполни)")
    elif data == "about":
        await q.message.reply_text("Честное описание продукта без обещаний сумм. (заполни)")
    elif data == "stats":
        if is_admin(chat_id):
            await q.message.reply_text(stats_text())
        else:
            await q.message.reply_text("Доступно только администратору.")
    elif data == "csv":
        if is_admin(chat_id):
            await send_csv(context.bot, chat_id)
        else:
            await q.message.reply_text("Доступно только администратору.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    await update.message.reply_text(stats_text())

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    await send_csv(context.bot, update.effective_chat.id)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Твой chat_id: {update.effective_chat.id}\n"
        f"Впиши его в .env как ADMIN_CHAT_ID, чтобы получать уведомления и видеть /stats.")

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Здесь ссылка/файл бесплатного гайда. (заполни)")

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Честное описание продукта без обещаний сумм. (заполни)")


# ---------- алерты «тишина по источнику» ----------
def silent_sources():
    """Источники, которые замолчали дольше SILENCE_HOURS и по которым ещё не алертили
    с момента последнего перехода. Возвращает [(source, last_click, total, hours_silent)]."""
    now = dt.datetime.utcnow()
    threshold = now - dt.timedelta(hours=SILENCE_HOURS)
    out = []
    with _conn() as c:
        rows = c.execute("""SELECT source, MAX(ts) last, COUNT(*) total
                            FROM starts GROUP BY source""").fetchall()
        for source, last, total in rows:
            if total < SILENCE_MIN_CLICKS or not last:
                continue
            try:
                last_dt = dt.datetime.fromisoformat(last)
            except ValueError:
                continue
            if last_dt >= threshold:
                continue  # ещё активен
            # уже алертили по этой тишине? (алерт позже последнего перехода)
            la = c.execute("""SELECT MAX(ts) FROM alerts WHERE source=? AND kind='silence'""",
                           (source,)).fetchone()[0]
            if la and dt.datetime.fromisoformat(la) >= last_dt:
                continue  # уже предупреждали, нового перехода не было
            hours = (now - last_dt).total_seconds() / 3600
            out.append((source or "direct", last, total, hours))
    return out

def record_alert(source, kind):
    with _conn() as c:
        c.execute("INSERT INTO alerts(ts,source,kind) VALUES(?,?,?)",
                  (dt.datetime.utcnow().isoformat(timespec="seconds"), source, kind))

async def silence_check_job(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_CHAT_ID:
        return
    for source, last, total, hours in silent_sources():
        txt = (f"🔕 Тишина по источнику: {source}\n"
               f"Нет переходов уже ~{int(hours)} ч (последний: {last} UTC).\n"
               f"Всего по нему было {total}. Проверь ссылку в био / статус аккаунта.")
        try:
            await context.bot.send_message(int(ADMIN_CHAT_ID), txt)
            record_alert(source, "silence")
        except Exception as e:
            log.error("silence alert failed for %s: %s", source, e)

def schedule_silence(app):
    if not ADMIN_CHAT_ID:
        return
    if app.job_queue is None:
        log.warning("JobQueue недоступен — алерты тишины выключены")
        return
    app.job_queue.run_repeating(silence_check_job, interval=SILENCE_CHECK_MIN * 60,
                                first=60)
    log.info("silence alerts on: >%dh, check every %dmin", SILENCE_HOURS, SILENCE_CHECK_MIN)


# ---------- ежедневная авто-сводка ----------
async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_CHAT_ID:
        return
    await context.bot.send_message(int(ADMIN_CHAT_ID), "🗓 Ежедневная сводка\n\n" + stats_text())

def schedule_daily(app):
    if not (DAILY_SUMMARY_TIME and ADMIN_CHAT_ID):
        return
    if app.job_queue is None:
        log.warning("JobQueue недоступен — установи 'python-telegram-bot[job-queue]' для авто-сводки")
        return
    try:
        hh, mm = (int(x) for x in DAILY_SUMMARY_TIME.split(":"))
        tz = ZoneInfo(SUMMARY_TZ)
        app.job_queue.run_daily(daily_summary_job, time=dt.time(hh, mm, tzinfo=tz))
        log.info("daily summary scheduled at %02d:%02d %s", hh, mm, SUMMARY_TZ)
    except Exception as e:
        log.error("cannot schedule daily summary (%r): %s", DAILY_SUMMARY_TIME, e)


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("guide", guide))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CallbackQueryHandler(on_button))
    schedule_daily(app)
    schedule_silence(app)
    log.info("bot @%s started (sheet=%s, admin=%s, daily=%s)",
             BOT_USERNAME, bool(SHEET_WEBHOOK_URL), bool(ADMIN_CHAT_ID), DAILY_SUMMARY_TIME or "off")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
