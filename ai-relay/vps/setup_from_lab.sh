#!/usr/bin/env bash
# Всё в одну команду: запускать на сервере КАБИНЕТА (Hostora), из /opt/neurofans-analytics:
#   bash ai-relay/vps/setup_from_lab.sh <IP зарубежного сервера> [домен, по умолчанию ai.neurofans.ru]
# Что делает:
#   1) ждёт, пока DNS домена покажет на этот IP;
#   2) скрыто спрашивает ключ ИИ, берёт LAB_AI_SECRET из .env кабинета;
#   3) по SSH (спросит пароль root зарубежного сервера) ставит туда Docker и ретранслятор;
#      секреты идут внутри зашифрованного SSH-соединения и пишутся только в .env того сервера;
#   4) переключает кабинет на https://<домен>/api/chat и пересобирает его;
#   5) проверяет большим запросом, что ИИ отвечает.
set -euo pipefail
IP="${1:-}"; DOM="${2:-ai.neurofans.ru}"
[ -n "$IP" ] || { echo "Укажи IP: bash ai-relay/vps/setup_from_lab.sh 1.2.3.4"; exit 1; }
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "Нет .env — запускай в /opt/neurofans-analytics"; exit 1; }
SEC=$(grep '^LAB_AI_SECRET=' .env | cut -d= -f2- | tr -d '\r\n"'"'"' ' || true)
[ -n "$SEC" ] || { echo "В .env нет LAB_AI_SECRET"; exit 1; }

echo "== 1/5 жду DNS: $DOM → $IP"
for i in $(seq 1 60); do
  got=$(getent ahostsv4 "$DOM" | awk 'NR==1{print $1}' || true)
  [ "$got" = "$IP" ] && break
  [ "$i" = 1 ] && echo "   сейчас: ${got:-нет записи}. Жду до 10 минут…"
  sleep 10
done
[ "$got" = "$IP" ] || { echo "DNS всё ещё не показывает на $IP — проверь A-запись и запусти снова."; exit 1; }
echo "   ок"

echo "== 2/5 ключ ИИ"
echo "Вставь ключ (OpenAI sk-… или OpenRouter sk-or-…) и нажми Enter. Символы не видны — так и задумано."
read -rs -p "Ключ: " KEY < /dev/tty; echo
[ -n "$KEY" ] || { echo "Пусто — запусти снова."; exit 1; }

echo "== 3/5 ставлю ретранслятор на $IP (спросит пароль root этого сервера, 2–3 минуты)"
{
  printf 'AI_KEY=%q\nSEC=%q\nDOM=%q\n' "$KEY" "$SEC" "$DOM"
  cat <<'REMOTE'
set -e
export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git >/dev/null; }
if [ -d /opt/nf/.git ]; then git -C /opt/nf pull -q; else git clone -q https://github.com/Leonardodeterrios/neurofans-analytics /opt/nf; fi
command -v docker >/dev/null || { echo "   ставлю Docker…"; curl -fsSL https://get.docker.com | sh >/dev/null 2>&1; }
cd /opt/nf/ai-relay/vps
umask 077
printf 'AI_KEY=%s\nLAB_AI_SECRET=%s\nRELAY_DOMAIN=%s\n' "$AI_KEY" "$SEC" "$DOM" > .env
docker compose up -d --force-recreate
echo "   ретранслятор запущен"
REMOTE
} | ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 "root@$IP" 'bash -s'
unset KEY

echo "== 4/5 переключаю кабинет на https://$DOM/api/chat"
if grep -q '^LAB_AI_URL=' .env; then sed -i "s|^LAB_AI_URL=.*|LAB_AI_URL=https://$DOM/api/chat|" .env
else echo "LAB_AI_URL=https://$DOM/api/chat" >> .env; fi
sed -i 's|^LAB_AI_RESOLVE=.*|LAB_AI_RESOLVE=|; s|^LAB_AI_KEY=.*|LAB_AI_KEY=|' .env
docker compose up -d --build lab
echo "   ок"

echo "== 5/5 проверка (сертификат HTTPS выпускается до минуты)"
for i in $(seq 1 18); do
  curl -sf -m 10 "https://$DOM/" | grep -q neurofans-lab && break
  sleep 5
done
python3 -c "import json;print(json.dumps({'model':'gpt-4o-mini','messages':[{'role':'user','content':'Ответь одним словом по-русски: связь есть? '+'проверка '*4000}],'max_tokens':20},ensure_ascii=False))" > /tmp/big.json
OUT=$(curl -s -m 70 "https://$DOM/api/chat" -H "x-relay-secret: $SEC" -H 'Content-Type: application/json' --data-binary @/tmp/big.json || true)
rm -f /tmp/big.json
ANS=$(printf '%s' "$OUT" | python3 -c "import sys,json
try:
  d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])
except Exception: print('')" 2>/dev/null || true)
if [ -n "$ANS" ]; then
  echo; echo "ГОТОВО ✅  ИИ ответил: $ANS"; echo "Студия в кабинете работает."
else
  echo; echo "Что-то не так ❌. Ответ сервера (без секретов):"; printf '%s\n' "$OUT" | head -c 400; echo
fi
