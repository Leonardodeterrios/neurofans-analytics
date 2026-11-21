# Кабинет учеников NeuroFans Lab — отдельный сервис, не связан с трекером партнёров.
# Собирается из корня репозитория (нужны общие шрифты из static/fonts).
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "flask>=3.0" "maxminddb>=2.4"
COPY students/lab.py students/lab.html ./
COPY static/fonts ./static/fonts
ENV LAB_DATA=/app/data LAB_PORT=8090
CMD ["python", "lab.py"]
