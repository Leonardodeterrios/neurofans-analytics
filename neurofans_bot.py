#!/usr/bin/env python3
"""
NeuroFans — Telegram-бот с аналитикой источников.

Каждый заход по ссылке  https://t.me/<BOT_USERNAME>?start=<имя_профиля>:
  • пишется в локальную базу (data/starts.db),
  • отправляется в Google-таблицу (если задан SHEET_WEBHOOK_URL),
  • присылает тебе уведомление в Telegram (если задан ADMIN_CHAT_ID).

Команды:
  /start <метка>  — вход (метку подставляет ссылка из био)
  /stats          — сводка переходов по аккаунтам (только админ)
  /myid           — узнать свой chat_id (чтобы заполнить ADMIN_CHAT_ID)

Установка:  pip install -r requirements.txt
Запуск:     python neurofans_bot.py
"""
import os, logging, sqlite3, datetime
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
BOT_TOKEN         = os.environ["BOT_TOKEN"]
BOT_USERNAME      = os.getenv("BOT_USERNAME", "NeuroFansBot")
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "").strip()
ADMIN_CHAT_ID     = os.getenv("ADMIN_CHAT_ID", "").strip()
DB_PATH           = os.getenv("DB_PATH", "data/starts.db")

WELCOME_TEXT = (
    "Привет! Это обучающий бот проекта по созданию и ведению AI-моделей.\n\n"
    "Здесь — бесплатный гайд и материалы: как создать свою AI-модель и монетизировать её "
    "на Fanvue (с обязательной пометкой ИИ по правилам площадки).\n\n"
    "Важно: это инструмент и обучение, а не гарантия дохода — результат зависит от тебя.\n\n"
    "Выбирай: /guide — бесплатный гайд, /about — о продукте."
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

def record_start(source, user) -> bool:
    """Пишет заход, возвращает True если пользователь новый (первый заход)."""
    with _conn() as c:
        seen = c.execute("SELECT 1 FROM starts WHERE user_id=? LIMIT 1", (user.id,)).fetchone()
        is_new = seen is None
        c.execute("INSERT INTO starts(ts,source,user_id,username,first_name) VALUES(?,?,?,?,?)",
                  (datetime.datetime.utcnow().isoformat(timespec="seconds"),
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
    txt = (f"🔔 Переход в бота ({flag})\n"
           f"Источник: {source}\n"
           f"Кто: {who} (id {user.id})")
    try:
        await context.bot.send_message(int(ADMIN_CHAT_ID), txt)
    except Exception as e:
        log.error("admin notify failed: %s", e)


# ---------- хэндлеры ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = context.args[0] if context.args else "direct"
    user = update.effective_user
    is_new = record_start(source, user)
    await push_sheet(source, user)
    await notify_admin(context, source, user, is_new)
    await update.message.reply_text(WELCOME_TEXT)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_CHAT_ID or str(update.effective_chat.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("Команда доступна только администратору.")
        return
    await update.message.reply_text(stats_text())

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой chat_id: {update.effective_chat.id}\n"
                                    f"Впиши его в .env как ADMIN_CHAT_ID, чтобы получать уведомления.")

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Здесь ссылка/файл бесплатного гайда. (заполни)")

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Честное описание продукта без обещаний сумм. (заполни)")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("guide", guide))
    app.add_handler(CommandHandler("about", about))
    log.info("bot @%s started (sheet=%s, admin=%s)", BOT_USERNAME,
             bool(SHEET_WEBHOOK_URL), bool(ADMIN_CHAT_ID))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
