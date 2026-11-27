"""NeuroFans Lab — служебное: резервные копии, «застрявшие» ученики, пост итогов недели,
иконки приложения (PWA). Чистые функции, без Flask."""
import datetime as dt
import glob
import gzip
import io
import os
import shutil
import sqlite3

import lab_game as game

KEEP_BACKUPS = 14


# ---------- резервные копии ----------
def make_backup(db_path, backup_dir, stamp):
    """Горячая копия SQLite (без остановки) → backups/lab-<stamp>.db.gz. Хранит KEEP_BACKUPS последних."""
    os.makedirs(backup_dir, exist_ok=True)
    tmp = os.path.join(backup_dir, f".lab-{stamp}.db")
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    out = os.path.join(backup_dir, f"lab-{stamp}.db.gz")
    with open(tmp, "rb") as f, gzip.open(out, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g)
    os.remove(tmp)
    os.chmod(out, 0o600)
    for old in sorted(glob.glob(os.path.join(backup_dir, "lab-*.db.gz")))[:-KEEP_BACKUPS]:
        os.remove(old)
    return out


def latest_backup(backup_dir):
    files = sorted(glob.glob(os.path.join(backup_dir, "lab-*.db.gz")))
    return files[-1] if files else None


# ---------- «застрявшие» ученики ----------
STUCK_TEXT = {
    "no_link": ("нет ни одной ссылки 3+ дня",
                "Привет, {nick}! Вижу, в кабинете NeuroFans Lab пока нет ни одной ссылки. Это 2 минуты: "
                "добавь модель, нажми «Проверить» и поставь ссылку в био. Если что-то непонятно, пиши, "
                "разберём вместе."),
    "no_people": ("ссылки есть, людей нет 7+ дней",
                  "Привет, {nick}! Ссылки готовы, но людей по ним пока нет. Давай посмотрим вместе: где "
                  "стоит ссылка и как выглядит био? Часто помогает ссылка в закрепе и в первом комментарии."),
    "silent": ("была активность, но 7 дней тишина",
               "{nick}, по твоим ссылкам неделю тишина, а до этого было {people} человек. Всё в порядке? "
               "Новая неделя лиги уже идёт, самое время зайти в «Рывок недели»."),
    "away": ("не открывал кабинет 10+ дней",
             "{nick}, давно не виделись в кабинете! Загляни: там твоё место в лиге и новые ачивки."),
}


def stuck_students(c, now):
    iso = lambda t: t.isoformat(timespec="seconds")
    d3, d7, d10 = (iso(now - dt.timedelta(days=n)) for n in (3, 7, 10))
    out = []
    for u in c.execute(
            "SELECT u.id, u.nick, u.created, "
            "(SELECT COUNT(*) FROM links l WHERE l.uid=u.id) links, "
            "(SELECT COALESCE(SUM(scored),0) FROM clicks k WHERE k.uid=u.id) people, "
            "(SELECT COALESCE(SUM(scored),0) FROM clicks k WHERE k.uid=u.id AND k.ts>=?) recent, "
            "(SELECT MAX(seen) FROM sessions s WHERE s.uid=u.id) seen "
            "FROM users u WHERE u.blocked=0", (d7,)).fetchall():
        reason = None
        if u["links"] == 0 and u["created"] < d3:
            reason = "no_link"
        elif u["links"] and u["people"] == 0 and u["created"] < d7:
            reason = "no_people"
        elif u["people"] and u["recent"] == 0 and u["created"] < d7:
            reason = "silent"
        elif (u["seen"] or u["created"]) < d10:
            reason = "away"
        if reason:
            label, text = STUCK_TEXT[reason]
            out.append({"id": u["id"], "nick": u["nick"], "reason": reason, "label": label,
                        "message": text.format(nick=u["nick"], people=u["people"]), "seen": u["seen"]})
    order = list(STUCK_TEXT)
    out.sort(key=lambda x: order.index(x["reason"]))
    return out


# ---------- пост итогов недели для Telegram-канала ----------
def weekly_post(c, wk, prev_wk, lab_host):
    """Текст для канала: топ-3 (ник — только с согласия ученика), «Рывок», общие цифры. Без ссылок учеников."""
    cur = game.board(c, wk)
    prev = game.board(c, prev_wk)
    consent = {r["id"]: r["public_ok"] for r in c.execute("SELECT id, public_ok FROM users")}
    who = lambda r: f"{r['name']} (@{r['nick']})" if consent.get(r["uid"]) else r["name"]
    medals = ["🥇", "🥈", "🥉"]
    n = int(wk.split("-W")[1])
    lines = [f"🏁 Итоги недели {n} в лиге NeuroFans Lab", ""]
    if not cur:
        return "\n".join(lines + ["На этой неделе в лиге пока нет очков."])
    for i, r in enumerate(cur[:3]):
        lines.append(f"{medals[i]} {who(r)}: {r['pts']} живых людей")
    rush = game.rush_rows(cur, prev)
    if rush:
        r = rush[0]
        lines += ["", f"🚀 Рывок недели: {who(r)}: ×{str(r['x']).replace('.', ',')} к прошлой неделе"]
    total = sum(r["pts"] for r in cur)
    lines += ["", f"Всего за неделю модели учеников собрали {total} живых переходов. "
                  f"В лиге {len(cur)} моделей. Боты и превью соцсетей не считаются.",
              "", "Новая неделя уже идёт 💪"]
    return "\n".join(lines)


# ---------- иконки приложения ----------
def app_icon(size, fonts):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    s = size
    img = Image.new("RGB", (s, s), (6, 5, 11))
    glow = Image.new("RGB", (s, s), (0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-s * .3, -s * .3, s * .7, s * .7), fill=(16, 120, 112))
    g.ellipse((s * .35, s * .35, s * 1.3, s * 1.3), fill=(150, 30, 80))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(s * .18)), .9)
    d = ImageDraw.Draw(img)
    # «сигнал»: три концентрические дуги + точка
    cx, cy = s * .5, s * .56
    for i, (r, col) in enumerate(((s * .34, (255, 79, 139)), (s * .24, (139, 108, 255)), (s * .14, (25, 211, 197)))):
        d.arc((cx - r, cy - r, cx + r, cy + r), start=205, end=335, fill=col, width=max(2, int(s * .045)))
    d.ellipse((cx - s * .05, cy - s * .05, cx + s * .05, cy + s * .05), fill=(237, 234, 255))
    try:
        f = ImageFont.truetype(os.path.join(fonts, "Unbounded-ExtraBold.ttf"), int(s * .13))
        d.text((cx, s * .82), "LAB", font=f, fill=(237, 234, 255), anchor="mm")
    except OSError:
        pass
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
