#!/usr/bin/env python3
"""
Единый запуск для сервера: бот (polling) + веб-дашборд в одном процессе.
Удобно для хостингов «одна служба» (Railway/Render/Fly): одна база, один контейнер.

Бот крутится в главном потоке, дашборд — в фоновом.
Запуск:  python run_all.py   (слушает дашборд на $DASHBOARD_PORT, по умолчанию 8080)
"""
import threading, logging
import neurofans_bot as bot
import dashboard

log = logging.getLogger("run_all")

def _run_dashboard():
    # Flask dev-сервер; для низкой нагрузки дашборда достаточно. use_reloader off (мы в потоке).
    dashboard.app.run(host="0.0.0.0", port=dashboard.PORT, use_reloader=False)

def main():
    bot.init_db()
    t = threading.Thread(target=_run_dashboard, daemon=True)
    t.start()
    log.info("dashboard thread started on port %s", dashboard.PORT)
    bot.main()  # блокирует (polling)

if __name__ == "__main__":
    main()
