FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY neurofans_bot.py dashboard.py ./
ENV DB_PATH=/app/data/starts.db
CMD ["python", "neurofans_bot.py"]
