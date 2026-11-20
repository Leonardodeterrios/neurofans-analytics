"""Картинки для соцсетей (Pillow): карточка итогов раунда 9:16 и превью ссылки 1200×630.
Стиль арены: ночной неоновый мегаполис под дождём, цвета команд."""
import io, os, random
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

BG_TOP, BG_BOT = (5, 6, 15), (24, 10, 40)
AMBER, WHITE, MUTED = (255, 181, 71), (240, 244, 255), (150, 160, 196)


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _font(fonts, name, size):
    try:
        return ImageFont.truetype(os.path.join(fonts, name + ".ttf"), size)
    except OSError:
        return ImageFont.load_default()


def _bg(w, h, ca, cb, seed=7):
    """Градиентное небо, силуэты небоскрёбов с окнами, неоновые ореолы и косой дождь."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse([-w * .35, h * .25, w * .55, h * .95], fill=tuple(int(v * .55) for v in ca))
    g.ellipse([w * .45, h * .2, w * 1.35, h * .9], fill=tuple(int(v * .55) for v in cb))
    glow = glow.filter(ImageFilter.GaussianBlur(w // 5))
    img = ImageChops.screen(img, glow)
    d = ImageDraw.Draw(img)
    for layer, (shade, top, wmin, wmax) in enumerate([((14, 14, 34), .42, 40, 110), ((9, 9, 22), .55, 60, 150)]):
        x = -20
        while x < w:
            bw = rnd.randint(int(wmin * w / 1080), int(wmax * w / 1080))
            bh = int(h * rnd.uniform(top, top + .25))
            y0 = h - bh
            d.rectangle([x, y0, x + bw, h], fill=shade)
            for wy in range(y0 + 12, h - 6, 16):
                for wx in range(x + 6, x + bw - 6, 12):
                    r = rnd.random()
                    if r < .16:
                        col = [ca, cb, AMBER, (120, 140, 255)][rnd.randint(0, 3)]
                        k = .35 if layer == 0 else .6
                        d.rectangle([wx, wy, wx + 4, wy + 6], fill=tuple(int(v * k) for v in col))
            x += bw + rnd.randint(2, 18)
    rain = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    r = ImageDraw.Draw(rain)
    for _ in range(int(w * h / 2600)):
        x, y = rnd.randint(-100, w), rnd.randint(-100, h)
        ln = rnd.randint(18, 46)
        r.line([(x, y), (x - ln * .28, y + ln)], fill=(170, 200, 255, rnd.randint(25, 70)), width=1)
    img = img.convert("RGBA")
    img.alpha_composite(rain)
    return img


def _glow_text(img, xy, text, font, color, anchor="la", blur=14, strength=2):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=color + (255,), anchor=anchor)
    halo = layer.filter(ImageFilter.GaussianBlur(blur))
    for _ in range(strength):
        img.alpha_composite(halo)
    ImageDraw.Draw(img).text(xy, text, font=font, fill=WHITE + (255,) if color != WHITE else color + (255,),
                             anchor=anchor)


def _bar(d, x0, y0, x1, y1, col):
    d.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) // 2, fill=col)


def _png(img):
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def _plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def story(summ, round_no, live, me, start_power, fonts):
    """Карточка 1080×1920 для сторис: счёт раунда, победитель, топ-3, личный результат."""
    W, H = 1080, 1920
    a, b = summ["teams"]
    ca, cb = _hex(a["color"]), _hex(b["color"])
    img = _bg(W, H, ca, cb, seed=summ["idx"])
    d = ImageDraw.Draw(img)
    F = lambda n, s: _font(fonts, n, s)
    d.text((W // 2, 120), "NEUROFANS  ·  CYBER ARENA", font=F("Unbounded-Medium", 30), fill=MUTED, anchor="mm")
    _glow_text(img, (W // 2, 240), f"РАУНД {round_no}", F("Unbounded-ExtraBold", 96), AMBER, anchor="mm")
    sub = "идёт бой" if live else f"итоги · {summ['date']}"
    d.text((W // 2, 330), sub.upper(), font=F("Unbounded-Medium", 34), fill=WHITE, anchor="mm")

    y = 470
    for t, col, x in ((a, ca, W // 4), (b, cb, 3 * W // 4)):
        d.text((x, y), t["name"].upper(), font=F("Unbounded-Medium", 32), fill=col, anchor="mm")
        _glow_text(img, (x, y + 140), str(t["power"]), F("Unbounded-ExtraBold", 170), col, anchor="mm", blur=22)
        d = ImageDraw.Draw(img)
        line = (f"{t['hits']} {_plural(t['hits'], 'удар', 'удара', 'ударов')} · "
                f"{t['starts']} {_plural(t['starts'], 'апперкот', 'апперкота', 'апперкотов')}")
        d.text((x, y + 280), line, font=F("Manrope-Medium", 32), fill=MUTED, anchor="mm")
    d.text((W // 2, y + 140), "VS", font=F("Unbounded-ExtraBold", 54), fill=(90, 96, 130), anchor="mm")
    tot = max(1, a["power"] + b["power"])
    bx0, bx1, by = 90, W - 90, y + 360
    _bar(d, bx0, by, bx1, by + 22, (30, 34, 64))
    split = bx0 + int((bx1 - bx0) * a["power"] / tot) if (a["power"] + b["power"]) else (bx0 + bx1) // 2
    _bar(d, bx0, by, max(bx0 + 22, split), by + 22, ca)
    _bar(d, min(bx1 - 22, split), by, bx1, by + 22, cb)

    y = by + 130
    if summ["winner"]:
        wt = a if summ["winner"] == "A" else b
        _glow_text(img, (W // 2, y), ("ЛИДИРУЕТ" if live else "ПОБЕДА") + f": {wt['name'].upper()}",
                   F("Unbounded-ExtraBold", 50), ca if summ["winner"] == "A" else cb, anchor="mm")
    else:
        _glow_text(img, (W // 2, y), "НИЧЬЯ" if (a["power"] or live) else "БЕЗ УДАРОВ",
                   F("Unbounded-ExtraBold", 50), AMBER, anchor="mm")
    d = ImageDraw.Draw(img)

    y += 120
    d.text((W // 2, y), "ЛУЧШИЕ БОЙЦЫ РАУНДА", font=F("Unbounded-Medium", 30), fill=MUTED, anchor="mm")
    y += 60
    for i, r in enumerate(summ["top"][:3]):
        col = ca if r["team"] == "A" else cb
        d.rounded_rectangle([110, y, W - 110, y + 110], radius=22, fill=(14, 16, 36), outline=col, width=3)
        d.text((170, y + 55), str(i + 1), font=F("Unbounded-ExtraBold", 52), fill=AMBER if i == 0 else WHITE, anchor="mm")
        d.text((240, y + 55), r["source"], font=F("Unbounded-ExtraBold", 46), fill=WHITE, anchor="lm")
        d.text((W - 150, y + 55), f"{r['power']}", font=F("Unbounded-ExtraBold", 50), fill=col, anchor="rm")
        y += 135
    if not summ["top"]:
        d.text((W // 2, y + 40), "Ударов пока не было", font=F("Manrope-Medium", 36), fill=MUTED, anchor="mm")
        y += 120

    if me:
        col = ca if me["team"] == "A" else cb
        y = max(y + 30, 1560)
        d.rounded_rectangle([90, y, W - 90, y + 170], radius=28, fill=tuple(int(v * .18) for v in col), outline=col, width=4)
        d.text((W // 2, y + 50), f"МОЙ РЕЗУЛЬТАТ · {me['source']}", font=F("Unbounded-Medium", 30), fill=col, anchor="mm")
        place = f"{me['place']} место из {me['of']}" if me["place"] else "в этом раунде без ударов"
        d.text((W // 2, y + 115), f"{place} · сила {me['power']}", font=F("Unbounded-ExtraBold", 44), fill=WHITE, anchor="mm")
    d.text((W // 2, H - 90), f"удар +1 · апперкот +{start_power}", font=F("Manrope-Medium", 30), fill=MUTED, anchor="mm")
    return _png(img)


def og(stats, fonts, label=None):
    """Превью ссылки 1200×630: текущий счёт раунда (+ метка партнёра для личной страницы)."""
    W, H = 1200, 630
    a, b = stats["teams"]
    ca, cb = _hex(a["color"]), _hex(b["color"])
    img = _bg(W, H, ca, cb, seed=stats.get("round_no", 1))
    d = ImageDraw.Draw(img)
    F = lambda n, s: _font(fonts, n, s)
    d.text((W // 2, 60), "NEUROFANS  ·  CYBER ARENA", font=F("Unbounded-Medium", 26), fill=MUTED, anchor="mm")
    _glow_text(img, (W // 2, 130), f"РАУНД {stats.get('round_no', 1)} · LIVE", F("Unbounded-ExtraBold", 50), AMBER, anchor="mm")
    for t, col, x in ((a, ca, W // 4 + 20), (b, cb, 3 * W // 4 - 20)):
        d = ImageDraw.Draw(img)
        d.text((x, 245), t["name"].upper(), font=F("Unbounded-Medium", 30), fill=col, anchor="mm")
        _glow_text(img, (x, 365), str(t["today"]), F("Unbounded-ExtraBold", 150), col, anchor="mm", blur=20)
    d = ImageDraw.Draw(img)
    d.text((W // 2, 365), "VS", font=F("Unbounded-ExtraBold", 50), fill=(90, 96, 130), anchor="mm")
    tot = a["today"] + b["today"]
    split = 100 + int(1000 * a["today"] / tot) if tot else W // 2
    _bar(d, 100, 480, 1100, 498, (30, 34, 64))
    _bar(d, 100, 480, max(118, split), 498, ca)
    _bar(d, min(1082, split), 480, 1100, 498, cb)
    if label:
        p = next((x for x in stats["partners"] if x["source"] == label), None)
        txt = f"{label} · сила {p['power'] if p else 0} · {p['rank_name'] if p else 'Новичок'}"
    else:
        lt = stats.get("leader_today")
        txt = f"лучший боец раунда: {lt['source']} ({lt['clicks']})" if lt else "бой начинается — заходи на арену"
    d.text((W // 2, 560), txt, font=F("Manrope-ExtraBold", 34), fill=WHITE, anchor="mm")
    return _png(img)
