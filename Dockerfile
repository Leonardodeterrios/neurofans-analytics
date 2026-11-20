FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py make_links.py arena.html ./
ENV DB_PATH=/app/data/starts.db DASHBOARD_PORT=8080
# Единый сервис: приём вебхуков от PuzzleBot + дашборд + уведомления/алерты.
CMD ["python", "app.py"]
