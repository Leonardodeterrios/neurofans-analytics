#!/usr/bin/env bash
# Разворачивает NeuroFans (бот + дашборд) на свежем Ubuntu-сервере.
# Запуск на сервере (скачиваем, затем запускаем — чтобы работал ввод токена):
#   curl -fsSL https://raw.githubusercontent.com/Leonardodeterrios/neurofans-analytics/main/deploy.sh -o deploy.sh && bash deploy.sh
set -e

APP_DIR=/opt/neurofans-analytics
REPO=https://github.com/Leonardodeterrios/neurofans-analytics

echo "==> 0/4 Добавляю swap (чтобы сборка не упала по памяти на 1 ГБ RAM)…"
if [ ! -f /swapfile ] && ! swapon --show | grep -q swap; then
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q "/swapfile" /etc/fstab 2>/dev/null || echo "/swapfile none swap sw 0 0" >> /etc/fstab
  echo "swap 2G включён"
else
  echo "swap уже есть, пропускаю"
fi

echo "==> 1/4 Устанавливаю Docker (если нужно)…"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

echo "==> 2/4 Забираю код…"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> 3/4 Настройка .env…"
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  read -rp "Вставь BOT_TOKEN (от @BotFather): " BT
  read -rp "Вставь BOT_USERNAME (без @): " BU
  # аккуратно прописываем значения в .env
  sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${BT}|"       .env
  sed -i "s|^BOT_USERNAME=.*|BOT_USERNAME=${BU}|"  .env
  echo "Записал BOT_TOKEN и BOT_USERNAME в .env"
else
  echo ".env уже есть — оставляю как есть"
fi

echo "==> 4/4 Запускаю бот + дашборд…"
docker compose up -d --build

IP=$(curl -fsS ifconfig.me 2>/dev/null || echo "<IP-сервера>")
echo
echo "================ ГОТОВО ================"
echo "Бот запущен. Дашборд: http://$IP:8080"
echo
echo "Дальше, чтобы включить уведомления и /stats:"
echo "  1) Напиши своему боту в Telegram команду /myid — он вернёт твой chat_id."
echo "  2) Впиши его:  nano $APP_DIR/.env   (строка ADMIN_CHAT_ID=...), сохрани (Ctrl+O, Enter, Ctrl+X)."
echo "  3) Перезапусти: cd $APP_DIR && docker compose up -d"
echo "========================================"
