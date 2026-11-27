#!/usr/bin/env bash
# Галерея примеров генераций на ЗАРУБЕЖНОМ сервере (Амстердам, рядом с ретранслятором ИИ).
#   cd /opt/nf && git pull && bash gallery/setup.sh gallery.<домен>
# Файлы 18+ лежат только здесь, в /opt/nf-gallery (не в репозитории и не на сервере в РФ).
set -euo pipefail
DOMAIN="${1:-}"; [ -n "$DOMAIN" ] || { echo "Укажи домен: bash gallery/setup.sh gallery.example.shop"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA=/opt/nf-gallery; mkdir -p "$DATA/media"
SRC="${GALLERY_SRC:-https://ai-creators.ru}"
echo "== качаю файлы со старой галереи ($SRC)"
python3 - "$REPO/gallery/media.default.json" "$DATA/media.json" <<'P'
import json,sys,os
src,dst=sys.argv[1],sys.argv[2]
if not os.path.exists(dst): open(dst,"w").write(open(src).read()); print("media.json создан")
else: print("media.json уже есть, не трогаю")

P
python3 -c "import json;[print(x['src']) for x in json.load(open('$DATA/media.json'))['items']]" | while read -r p; do
  f="$DATA/$p"; [ -s "$f" ] && continue
  curl -fsSL --max-time 300 -o "$f" "$SRC/${p#media/}" && echo "  ок  ${p#media/}" || { rm -f "$f"; echo "  нет ${p#media/}"; }
done
cd "$REPO/ai-relay/vps"
touch .env
if grep -q '^GALLERY_DOMAIN=' .env; then sed -i "s|^GALLERY_DOMAIN=.*|GALLERY_DOMAIN=$DOMAIN|" .env; else echo "GALLERY_DOMAIN=$DOMAIN" >> .env; fi
docker compose up -d --force-recreate caddy
echo; echo "Готово: https://$DOMAIN  (A-запись $DOMAIN → IP этого сервера; сертификат появится за минуту)"
