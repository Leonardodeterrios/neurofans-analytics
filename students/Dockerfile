# Кабинет учеников NeuroFans Lab — отдельный сервис, не связан с трекером партнёров.
# Собирается из корня репозитория (нужны общие шрифты из static/fonts).
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "flask>=3.0" "maxminddb>=2.4" "pillow>=10" "gunicorn>=22"
COPY students/lab.py students/lab_game.py students/lab_card.py students/lab_notify.py students/lab_ops.py students/lab_core.py students/lab.html ./
COPY static/fonts ./static/fonts
ENV LAB_DATA=/app/data LAB_PORT=8090 LAB_BG=1
# gunicorn: 1 процесс (фоновые потоки и лимиты — в памяти одного процесса), 16 потоков на запросы.
# Журнал запросов выключен: в адресах не должно оседать ничего лишнего.
CMD ["gunicorn", "-w", "1", "--threads", "16", "-b", "0.0.0.0:8090", "--timeout", "60", "--graceful-timeout", "20", "lab:app"]
