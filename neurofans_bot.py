#!/usr/bin/env python3
"""Telegram-бот с аналитикой источников. Секреты берутся из .env (в git не хранятся)."""
import os, logging, httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "NeuroFansBot")
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "")

WELCOME_TEXT = (
    "Привет! Это обучающий бот проекта по созданию и ведению AI-моделей.\n\n"
    "Здесь — бесплатный гайд и материалы: как создать свою AI-модель и монетизировать её "
    "на Fanvue (с обязательной пометкой ИИ по правилам площадки).\n\n"
    "Важно: это инструмент и обучение, а не гарантия дохода — результат зависит от тебя.\n\n"
    "Выбирай: /guide — бесплатный гайд, /about — о продукте."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neurofans")

async def log_source(source, user):
    if not SHEET_WEBHOOK_URL:
        log.warning("SHEET_WEBHOOK_URL пуст — пропускаю запись"); return
    data = {"source": source, "user_id": user.id,
            "username": user.username or "", "first_name": user.first_name or ""}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(SHEET_WEBHOOK_URL, json=data)
            log.info("sheet source=%s user=%s status=%s", source, user.id, r.status_code)
    except Exception as e:
        log.error("sheet log failed: %s", e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = context.args[0] if context.args else "direct"
    await log_source(source, update.effective_user)
    await update.message.reply_text(WELCOME_TEXT)

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Здесь ссылка/файл бесплатного гайда. (заполни)")

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Честное описание продукта без обещаний сумм. (заполни)")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("guide", guide))
    app.add_handler(CommandHandler("about", about))
    log.info("bot @%s started", BOT_USERNAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
