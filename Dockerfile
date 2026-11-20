FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py cards.py make_links.py arena.html partner.html ./
COPY static ./static
ENV DB_PATH=/app/data/starts.db DASHBOARD_PORT=8080
# Единый сервис: приём вебхуков от PuzzleBot + дашборд + уведомления/алерты.
CMD ["python", "app.py"]
