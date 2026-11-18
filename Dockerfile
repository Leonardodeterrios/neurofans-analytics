FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY neurofans_bot.py dashboard.py run_all.py ./
ENV DB_PATH=/app/data/starts.db DASHBOARD_PORT=8080
# Для хостинга «одна служба» (бот + дашборд вместе):
CMD ["python", "run_all.py"]
