#!/usr/bin/env bash
# Ускорение галереи: лёгкие копии файлов. Запускать на сервере в Амстердаме:
#   cd /opt/nf && git pull && bash gallery/optimize.sh
# Для каждого файла из /opt/nf-gallery/media.json делает:
#   media/t/<имя>.webp — превью для сетки (ширина 640, ~40–80 КБ; у видео — кадр с 1-й секунды);
#   media/w/<имя>.webp — фото для полного просмотра (до 1600 px);
#   media/w/<имя>.mp4  — видео 720p, H.264, быстрый старт (moov в начале файла).
# Оригиналы не трогает. Уже готовые копии пропускает — можно запускать после каждого добавления файлов.
# В media.json дописывает поля thumb / web / w / h, страница сама начинает брать лёгкие версии.
set -euo pipefail
DATA="${GALLERY_DATA:-/opt/nf-gallery}"; export GALLERY_DATA="$DATA"
cd "$DATA"
command -v ffmpeg >/dev/null || { echo "== ставлю ffmpeg (1–2 минуты)"; apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg >/dev/null; }
mkdir -p media/t media/w
python3 - <<'P'
import json, os, subprocess
D = os.environ.get("GALLERY_DATA", "/opt/nf-gallery")
j = json.load(open(f"{D}/media.json"))
def run(*a):
    return subprocess.run(a, capture_output=True, text=True)
def size(p):
    r = run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", p)
    try:
        w, h = r.stdout.strip().split("\n")[0].split(",")[:2]; return int(w), int(h)
    except Exception:
        return None
before = after = 0
for x in j.get("items", []):
    src = os.path.join(D, x["src"])
    if not os.path.exists(src):
        print("  нет файла:", x["src"]); continue
    base = os.path.splitext(os.path.basename(x["src"]))[0]
    t, wv = f"media/t/{base}.webp", f"media/w/{base}." + ("mp4" if x.get("type") == "video" else "webp")
    if not os.path.exists(os.path.join(D, t)):
        args = ["ffmpeg", "-y", "-v", "error"] + (["-ss", "1"] if x.get("type") == "video" else []) + \
               ["-i", src, "-frames:v", "1", "-vf", "scale='min(640,iw)':-2", "-c:v", "libwebp", "-quality", "72", os.path.join(D, t)]
        if run(*args).returncode: print("  превью не вышло:", x["src"])
    if not os.path.exists(os.path.join(D, wv)):
        if x.get("type") == "video":
            args = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", "scale=-2:'min(1280,ih)'", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", "-b:a", "96k", os.path.join(D, wv)]
        else:
            args = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", "scale='min(1600,iw)':-2", "-c:v", "libwebp", "-quality", "82", os.path.join(D, wv)]
        if run(*args).returncode: print("  копия не вышла:", x["src"])
    if os.path.exists(os.path.join(D, t)):
        x["thumb"] = t
        s = size(os.path.join(D, t))
        if s: x["w"], x["h"] = s
    if os.path.exists(os.path.join(D, wv)) and os.path.getsize(os.path.join(D, wv)) < os.path.getsize(src):
        x["web"] = wv                      # берём лёгкую копию, только если она реально легче оригинала
    before += os.path.getsize(src)
    after += os.path.getsize(os.path.join(D, x.get("web", x["src"])))
    print("  ок", x["src"])
tmp = f"{D}/media.json.tmp"
json.dump(j, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, f"{D}/media.json")
thumbs = sum(os.path.getsize(os.path.join(D, x["thumb"])) for x in j["items"] if x.get("thumb"))
print(f"\nГотово. Полные файлы: {before/1e6:.0f} МБ → {after/1e6:.0f} МБ. Вся сетка превью: {thumbs/1e6:.1f} МБ.")
P
