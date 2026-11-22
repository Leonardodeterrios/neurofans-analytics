#!/usr/bin/env bash
# Установка ретранслятора ИИ на чистый Ubuntu (запускать от root на ЗАРУБЕЖНОМ сервере):
#   git clone https://github.com/Leonardodeterrios/neurofans-analytics /opt/nf && bash /opt/nf/ai-relay/vps/install.sh
# Спросит ключ ИИ и секрет кабинета скрытым вводом и сохранит их только в .env на этом сервере.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "== ставлю Docker (1–2 минуты)"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi

DOMAIN="${RELAY_DOMAIN:-ai.neurofans.ru}"
if [ -f .env ] && grep -q '^AI_KEY=.' .env && [ "${1:-}" != "--reset" ]; then
  echo "== .env уже есть, ключи не трогаю (заново: bash install.sh --reset)"
else
  echo
  echo "Вставь ключ ИИ (OpenAI sk-... или OpenRouter sk-or-...) и нажми Enter. Символы не видны — так и задумано."
  read -rs -p "Ключ: " KEY < /dev/tty; echo
  echo "Вставь LAB_AI_SECRET с сервера кабинета (та же строка, что в его .env) и нажми Enter."
  read -rs -p "Секрет: " SEC < /dev/tty; echo
  [ -n "$KEY" ] && [ -n "$SEC" ] || { echo "Пусто — начни заново."; exit 1; }
  umask 077
  printf 'AI_KEY=%s\nLAB_AI_SECRET=%s\nRELAY_DOMAIN=%s\n' "$KEY" "$SEC" "$DOMAIN" > .env
  unset KEY SEC
fi

docker compose up -d --force-recreate
sleep 3
echo
echo "== проверка изнутри:"
docker compose exec -T relay wget -qO- http://127.0.0.1:8080/ || true
echo
echo "Готово. Адрес для кабинета: https://$DOMAIN/api/chat"
