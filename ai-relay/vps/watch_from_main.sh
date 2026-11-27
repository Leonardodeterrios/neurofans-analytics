#!/usr/bin/env bash
# Включить сторожа одной командой. Запускать на ОСНОВНОМ сервере (Hostora), из /opt/neurofans-analytics:
#   bash ai-relay/vps/watch_from_main.sh            (IP Амстердама по умолчанию 129.101.125.126)
# Берёт токен бота трекера и ADMIN_CHAT_ID из .env этого сервера и по SSH (спросит пароль root Амстердама)
# кладёт их в .env сервера в Амстердаме, обновляет код и запускает сторожа. Секреты идут внутри SSH.
set -euo pipefail
IP="${1:-129.101.125.126}"
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "Нет .env — запускай в /opt/neurofans-analytics"; exit 1; }
val() { grep "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '\r\n"'"'"' ' || true; }
TOK=$(val BOT_TOKEN); CH=$(val ADMIN_CHAT_ID)
[ -n "$TOK" ] && [ -n "$CH" ] || { echo "В .env нет BOT_TOKEN или ADMIN_CHAT_ID"; exit 1; }
echo "== ставлю сторожа на $IP (спросит пароль root сервера в Амстердаме)"
{
  printf 'TOK=%q\nCH=%q\n' "$TOK" "$CH"
  cat <<'REMOTE'
set -e
cd /opt/nf && git pull -q
cd ai-relay/vps
umask 077
touch .env
sed -i '/^WATCH_TG_TOKEN=/d;/^WATCH_CHATS=/d' .env
printf 'WATCH_TG_TOKEN=%s\nWATCH_CHATS=%s\n' "$TOK" "$CH" >> .env
mkdir -p watch-state
docker compose up -d watch
sleep 8
docker compose logs --tail=5 watch
REMOTE
} | ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 "root@$IP" 'bash -s'
echo
echo "Готово. В течение минуты в Telegram придёт «🛡 Сторож на связи» (если уже больше 10:00 по Москве)."
