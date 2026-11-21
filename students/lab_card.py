"""NeuroFans Lab — карточка «Моя неделя» для сторис и Threads (PNG 1080×1920, Pillow).

Приватность: на карточку попадают только имя модели, (по желанию) ник ученика, неделя,
очки, место, рывок, топ-канал, ранг и ачивки. Ссылок, адресов назначения, городов и
сырых кликов здесь нет — им просто неоткуда взяться: функция получает готовые числа.
"""
import io
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
TEAL, PINK, VIOLET, GOLD = (25, 211, 197), (255, 79, 139), (139, 108, 255), (255, 201, 77)
INK, MUTED, DIM = (236, 233, 255), (154, 148, 192), (107, 102, 144)


def _font(fonts, name, size):
    try:
        return ImageFont.truetype(os.path.join(fonts, name + ".ttf"), size)
    except OSError:
        return ImageFont.load_default()


def _bg(seed=11):
    """Тёмный фон с бирюзовым и розовым свечением, сеткой и «звёздами»."""
    img = Image.new("RGB", (W, H), (7, 6, 13))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-380, -260, 640, 760), fill=(14, 92, 88))
    g.ellipse((520, 180, 1460, 1120), fill=(110, 24, 62))
    g.ellipse((80, 1240, 1000, 2180), fill=(52, 36, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(170))
    img = Image.blend(img, glow, 0.85)
    d = ImageDraw.Draw(img, "RGBA")
    for x in range(0, W, 54):
        d.line([(x, 0), (x, H)], fill=(160, 150, 255, 10))
    for y in range(0, H, 54):
        d.line([(0, y), (W, y)], fill=(160, 150, 255, 10))
    rnd = random.Random(seed)
    for _ in range(90):
        x, y, r = rnd.randrange(W), rnd.randrange(H), rnd.choice([1, 1, 2])
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rnd.randrange(30, 110)))
    return img


def _glow_text(img, xy, text, font, color, anchor="mm", blur=16):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=color + (255,), anchor=anchor)
    halo = layer.filter(ImageFilter.GaussianBlur(blur))
    img.paste(halo, (0, 0), halo)
    img.paste(halo, (0, 0), halo)
    ImageDraw.Draw(img).text(xy, text, font=font, fill=color, anchor=anchor)


def _fit(d, text, fonts, name, size, max_w, min_size=40):
    """Подбирает размер шрифта под ширину; если не влезает и на минимуме — обрезает с «…»."""
    while size > min_size:
        f = _font(fonts, name, size)
        if d.textlength(text, font=f) <= max_w:
            return f, text
        size -= 4
    f = _font(fonts, name, min_size)
    if d.textlength(text, font=f) <= max_w:
        return f, text
    while len(text) > 1 and d.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return f, text.rstrip() + "…"


def _name_lines(d, text, fonts, max_w):
    """Имя модели: одна строка (112→72), иначе две строки, иначе обрезка."""
    for size in range(112, 70, -4):
        f = _font(fonts, "Unbounded-ExtraBold", size)
        if d.textlength(text, font=f) <= max_w:
            return f, [text]
    words = text.split()
    if len(words) > 1:
        for size in range(84, 50, -4):
            f = _font(fonts, "Unbounded-ExtraBold", size)
            for k in range(len(words) - 1, 0, -1):
                a, b = " ".join(words[:k]), " ".join(words[k:])
                if d.textlength(a, font=f) <= max_w and d.textlength(b, font=f) <= max_w:
                    return f, [a, b]
    f, t = _fit(d, text, fonts, "Unbounded-ExtraBold", 72, max_w, 56)
    return f, [t]


def _panel(d, box, outline=(160, 150, 255, 60), fill=(22, 19, 40, 190), r=36):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=2)


def card(data, fonts):
    """data: model, nick|None, week_no, final(bool), pts, place|None, total, rush_x|None,
    top_ch_name|None, rank{name,progress,next_name,next_at,people}, badges[{name}]"""
    img = _bg()
    d = ImageDraw.Draw(img, "RGBA")
    cx = W // 2

    # шапка
    f_logo = _font(fonts, "Unbounded-ExtraBold", 46)
    logo_w = d.textlength("NEUROFANS LAB", font=f_logo)
    d.rounded_rectangle((cx - logo_w / 2 - 74, 112, cx - logo_w / 2 - 18, 168), radius=16, fill=TEAL + (255,))
    d.text((cx - logo_w / 2 - 46, 140), "N", font=_font(fonts, "Unbounded-ExtraBold", 34),
           fill=(7, 6, 13), anchor="mm")
    d.text((cx - logo_w / 2 + 10, 140), "NEUROFANS LAB", font=f_logo, fill=INK, anchor="lm")
    head = ("ИТОГ НЕДЕЛИ " if data.get("final") else "НЕДЕЛЯ ") + str(data.get("week_no", ""))
    f_pill = _font(fonts, "Manrope-ExtraBold", 34)
    pw = d.textlength(head, font=f_pill) + 64
    d.rounded_rectangle((cx - pw / 2, 206, cx + pw / 2, 266), radius=30, outline=TEAL + (200,), width=3,
                        fill=(25, 211, 197, 28))
    d.text((cx, 236), head, font=f_pill, fill=TEAL, anchor="mm")

    # модель
    f_name, lines = _name_lines(d, data.get("model", ""), fonts, 940)
    if len(lines) == 1:
        _glow_text(img, (cx, 370), lines[0], f_name, INK, blur=22)
    else:
        lh = f_name.size * 1.12
        _glow_text(img, (cx, 370 - lh / 2), lines[0], f_name, INK, blur=22)
        _glow_text(img, (cx, 370 + lh / 2), lines[1], f_name, INK, blur=22)
    d = ImageDraw.Draw(img, "RGBA")
    if data.get("nick"):
        d.text((cx, 478 if len(lines) > 1 else 462), "@" + data["nick"],
               font=_font(fonts, "Manrope-Medium", 40), fill=MUTED, anchor="mm")

    # место
    ring_y = 780
    for r_, a in ((236, 40), (212, 90)):
        d.ellipse((cx - r_, ring_y - r_, cx + r_, ring_y + r_), outline=TEAL + (a,), width=4)
    d.arc((cx - 236, ring_y - 236, cx + 236, ring_y + 236), start=200, end=340, fill=PINK + (230,), width=10)
    d.arc((cx - 236, ring_y - 236, cx + 236, ring_y + 236), start=20, end=160, fill=TEAL + (230,), width=10)
    place = data.get("place")
    if place:
        col = GOLD if place == 1 else INK
        _glow_text(img, (cx, ring_y - 18), f"#{place}", _font(fonts, "Oxanium-SemiBold", 230), col, blur=26)
        d = ImageDraw.Draw(img, "RGBA")
        sub = f"из {data.get('total', 0)} в лиге"
    else:
        _glow_text(img, (cx, ring_y - 10), "В ИГРЕ", _font(fonts, "Unbounded-ExtraBold", 88), INK, blur=20)
        d = ImageDraw.Draw(img, "RGBA")
        sub = "очки — впереди"
    d.text((cx, ring_y + 112), sub, font=_font(fonts, "Manrope-ExtraBold", 36), fill=MUTED, anchor="mm")

    # три плитки
    tiles = [("ЛЮДЕЙ", str(data.get("pts", 0)), TEAL),
             ("РЫВОК", ("×" + f"{data['rush_x']:.1f}".replace(".", ",")) if data.get("rush_x") else "—", PINK),
             ("ТОП-КАНАЛ", data.get("top_ch_name") or "—", VIOLET)]
    tw, gap, ty = 300, 30, 1080
    x0 = cx - (3 * tw + 2 * gap) / 2
    for i, (lbl, val, col) in enumerate(tiles):
        bx = x0 + i * (tw + gap)
        _panel(d, (bx, ty, bx + tw, ty + 230), outline=col + (150,))
        d.text((bx + tw / 2, ty + 58), lbl, font=_font(fonts, "Manrope-ExtraBold", 30), fill=MUTED, anchor="mm")
        is_num = val.replace(",", "").replace("×", "").replace("—", "").isdigit() or val == "—"
        if is_num and val != "—":
            fv = _font(fonts, "Oxanium-SemiBold", 104 if len(val) <= 4 else 78)
            d.text((bx + tw / 2, ty + 148), val.replace("×", "x"), font=fv, fill=col, anchor="mm")
        else:
            fv, val2 = _fit(d, val, fonts, "Unbounded-ExtraBold", 54, tw - 36, 30)
            d.text((bx + tw / 2, ty + 148), val2, font=fv, fill=col, anchor="mm")

    # ранг и прогресс
    rk = data.get("rank") or {}
    _panel(d, (90, 1370, 990, 1540))
    d.text((130, 1418), "РАНГ", font=_font(fonts, "Manrope-ExtraBold", 30), fill=MUTED, anchor="lm")
    d.text((130, 1470), rk.get("name", ""), font=_font(fonts, "Unbounded-ExtraBold", 50), fill=INK, anchor="lm")
    if rk.get("next_name"):
        d.text((950, 1418), f"до «{rk['next_name']}»: {rk['next_at'] - rk.get('people', 0)}",
               font=_font(fonts, "Manrope-Medium", 30), fill=MUTED, anchor="rm")
    bar = (130, 1504, 950, 1518)
    d.rounded_rectangle(bar, radius=7, fill=(255, 255, 255, 20))
    pw_ = (bar[2] - bar[0]) * float(rk.get("progress", 1) or 0)
    if pw_ > 14:
        d.rounded_rectangle((bar[0], bar[1], bar[0] + pw_, bar[3]), radius=7, fill=TEAL + (255,))

    # ачивки (до 3, текстом — эмодзи в шрифтах Pillow нет)
    bs = [b["name"] for b in (data.get("badges") or [])][:3]
    if bs:
        f_b = _font(fonts, "Manrope-ExtraBold", 32)
        widths = [d.textlength(b, font=f_b) + 70 for b in bs]
        total_w = sum(widths) + 18 * (len(bs) - 1)
        x = cx - total_w / 2
        for b, w_ in zip(bs, widths):
            d.rounded_rectangle((x, 1588, x + w_, 1652), radius=32, fill=(255, 201, 77, 26),
                                outline=GOLD + (170,), width=2)
            d.ellipse((x + 22, 1612, x + 38, 1628), fill=GOLD)
            d.text((x + 48, 1620), b, font=f_b, fill=INK, anchor="lm")
            x += w_ + 18

    # подвал
    d.line((180, 1730, 900, 1730), fill=(160, 150, 255, 50), width=2)
    d.text((cx, 1778), "Только живые люди · боты не считаются", font=_font(fonts, "Manrope-Medium", 34),
           fill=MUTED, anchor="mm")
    d.text((cx, 1836), "neurofans.ru", font=_font(fonts, "Unbounded-Medium", 36), fill=TEAL, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
