"""NeuroFans Lab — игровой слой: таблица лиги, «Рывок», ранги и ачивки моделей.

Чистые функции поверх SQLite (принимают соединение `c` и ключи недель), без Flask —
их зовут и эндпоинты (lab.py), и фоновый уведомитель (lab_notify.py).
Правила подсчёта очков здесь НЕ меняются: очко = clicks.scored=1, как в record_click().
"""
import datetime as dt

# Ранг модели — по уникальным людям за всё время
RANKS = [
    (0,    "Новичок",     "🌱"),
    (10,   "Первые фаны", "✨"),
    (50,   "На взлёте",   "🚀"),
    (250,  "Звезда",      "⭐"),
    (1000, "Икона",       "💎"),
    (5000, "Легенда",     "👑"),
]
MILESTONES = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

# id → (иконка, название, условие для подсказки)
BADGES = {
    "first_fan":    ("🎯", "Первый фан",       "первый живой человек по ссылкам модели"),
    "multichannel": ("🔀", "Мультиканал",      "люди из 3+ каналов за одну неделю"),
    "geo":          ("🌍", "География",        "люди из 5+ стран за всё время"),
    "streak3":      ("📅", "Стабильность",     "очки 3 недели подряд"),
    "night":        ("🌙", "Ночная смена",     "человек с 00:00 до 05:00"),
    "hundred_week": ("💯", "Сотня за неделю",  "100+ людей за одну неделю"),
    "rush":         ("🚀", "Рывок",            "топ-3 «Рывка» по итогам недели"),
    "top10":        ("🏅", "Топ-10",           "финиш недели в топ-10 лиги"),
    "podium":       ("🏆", "Пьедестал",        "финиш недели в топ-3 лиги"),
}
BADGE_ORDER = ["podium", "top10", "rush", "hundred_week", "streak3", "geo", "multichannel", "night", "first_fan"]


# Дивизионы лиги — по людям ДО текущей недели, чтобы модель не прыгала между лигами посреди недели
DIVS = [("start", "Старт", "до 50 людей"), ("pro", "Про", "50–999"), ("elite", "Элита", "1000+")]
DIV_IDS = {d[0] for d in DIVS}


def div_of(people_before):
    return "elite" if people_before >= 1000 else "pro" if people_before >= 50 else "start"


def divisions(c, wk):
    return {r[0]: div_of(r[1] or 0) for r in c.execute(
        "SELECT mid, SUM(scored) FROM clicks WHERE wk<? GROUP BY mid", (wk,))}


def rank_of(people):
    people = int(people or 0)
    lvl = 0
    for i, (need, _, _) in enumerate(RANKS):
        if people >= need:
            lvl = i
    need, name, icon = RANKS[lvl]
    nxt = RANKS[lvl + 1] if lvl + 1 < len(RANKS) else None
    prog = 1.0 if not nxt else (people - need) / max(1, nxt[0] - need)
    return {"lvl": lvl, "name": name, "icon": icon, "people": people,
            "next_at": nxt[0] if nxt else None, "next_name": nxt[1] if nxt else None,
            "progress": round(max(0.0, min(1.0, prog)), 3)}


def week_monday(wk):
    y, w = wk.split("-W")
    return dt.date.fromisocalendar(int(y), int(w), 1)


def board(c, wk, ch=None):
    """Таблица лиги недели: модели по очкам (видимые, не в архиве, ученик не отключён)."""
    sql = ("SELECT k.mid, m.name, m.uid, u.nick, SUM(k.scored) pts, COUNT(DISTINCT k.ch) chs "
           "FROM clicks k JOIN models m ON m.id=k.mid JOIN users u ON u.id=m.uid "
           "WHERE k.wk=? AND k.scored=1 AND m.hidden=0 AND m.archived=0 AND u.blocked=0")
    a = [wk]
    if ch:
        sql += " AND k.ch=?"
        a.append(ch)
    sql += " GROUP BY k.mid ORDER BY pts DESC, MIN(k.id) ASC"
    return [dict(r) for r in c.execute(sql, a)]


def rush_rows(cur, prev):
    """«Рывок недели»: (очки+5)/(прошлые+5), от 10 очков и только рост."""
    prevmap = {r["mid"]: r["pts"] for r in prev}
    out = []
    for r in cur:
        p0 = prevmap.get(r["mid"], 0)
        if r["pts"] >= 10 and r["pts"] > p0:
            out.append({**r, "prev": p0, "x": round((r["pts"] + 5) / (p0 + 5), 2)})
    out.sort(key=lambda x: (-x["x"], -x["pts"]))
    return out


def people_all(c, mids=None):
    """Уникальные люди (очки) за всё время по моделям."""
    rows = c.execute("SELECT mid, SUM(scored) p FROM clicks GROUP BY mid").fetchall()
    d = {r["mid"]: r["p"] or 0 for r in rows}
    return d if mids is None else {m: d.get(m, 0) for m in mids}


def _live_badges(c, mid):
    """Ачивки, которые видны прямо по кликам модели (без итогов недель)."""
    got = set()
    q1 = lambda sql: c.execute(sql, (mid,)).fetchone()
    if (q1("SELECT SUM(scored) FROM clicks WHERE mid=?")[0] or 0) >= 1:
        got.add("first_fan")
    if q1("SELECT 1 FROM clicks WHERE mid=? AND scored=1 GROUP BY wk HAVING COUNT(DISTINCT ch)>=3 LIMIT 1"):
        got.add("multichannel")
    if (q1("SELECT COUNT(DISTINCT cc) FROM clicks WHERE mid=? AND scored=1 AND cc<>''")[0] or 0) >= 5:
        got.add("geo")
    if q1("SELECT 1 FROM clicks WHERE mid=? AND scored=1 AND hr<5 LIMIT 1"):
        got.add("night")
    if q1("SELECT 1 FROM clicks WHERE mid=? AND scored=1 GROUP BY wk HAVING SUM(scored)>=100 LIMIT 1"):
        got.add("hundred_week")
    weeks = sorted({week_monday(r[0]) for r in c.execute(
        "SELECT DISTINCT wk FROM clicks WHERE mid=? AND scored=1", (mid,))})
    run = 1
    for a, b in zip(weeks, weeks[1:]):
        run = run + 1 if (b - a).days == 7 else 1
        if run >= 3:
            got.add("streak3")
            break
    return got


def week_awards(c, wk, prev_wk):
    """Итоги ЗАВЕРШЁННОЙ недели: топ-10, пьедестал, топ-3 «Рывка». {mid: {badge,…}}"""
    cur, prev = board(c, wk), board(c, prev_wk)
    aw = {}
    for i, r in enumerate(cur):
        if i < 10:
            aw.setdefault(r["mid"], set()).add("top10")
        if i < 3:
            aw[r["mid"]].add("podium")
    for r in rush_rows(cur, prev)[:3]:
        aw.setdefault(r["mid"], set()).add("rush")
    return aw


def sync_badges(c, finished_wk, finished_prev_wk, now_iso):
    """Записывает заработанные ачивки в badge_log (раз и навсегда). Возвращает новые [(mid, badge)]."""
    new = []
    mids = [r[0] for r in c.execute("SELECT DISTINCT mid FROM clicks WHERE scored=1")]
    awards = week_awards(c, finished_wk, finished_prev_wk) if finished_wk else {}
    for mid in mids:
        for b in _live_badges(c, mid) | awards.get(mid, set()):
            cur = c.execute("INSERT OR IGNORE INTO badge_log(mid, badge, earned) VALUES(?,?,?)", (mid, b, now_iso))
            if cur.rowcount:
                new.append((mid, b))
    c.commit()
    return new


def badges_of(c, mid):
    rows = {r["badge"]: r["earned"] for r in c.execute("SELECT badge, earned FROM badge_log WHERE mid=?", (mid,))}
    return [{"id": b, "icon": BADGES[b][0], "name": BADGES[b][1], "desc": BADGES[b][2], "earned": rows[b]}
            for b in BADGE_ORDER if b in rows]


def badge_defs():
    return [{"id": b, "icon": BADGES[b][0], "name": BADGES[b][1], "desc": BADGES[b][2]} for b in BADGE_ORDER[::-1]]


def model_week(c, mid, wk, prev_wk):
    """Сводка модели за неделю для карточки и уведомлений: очки, место, всего в лиге, рывок, топ-канал."""
    cur = board(c, wk)
    prev = board(c, prev_wk)
    place = next((i + 1 for i, r in enumerate(cur) if r["mid"] == mid), None)
    pts = next((r["pts"] for r in cur if r["mid"] == mid), None)
    if pts is None:
        pts = c.execute("SELECT COALESCE(SUM(scored),0) FROM clicks WHERE mid=? AND wk=?", (mid, wk)).fetchone()[0]
    rush = next((r for r in rush_rows(cur, prev) if r["mid"] == mid), None)
    rush_place = next((i + 1 for i, r in enumerate(rush_rows(cur, prev)) if r["mid"] == mid), None)
    top = c.execute("SELECT ch, SUM(scored) p FROM clicks WHERE mid=? AND wk=? AND scored=1 "
                    "GROUP BY ch ORDER BY p DESC LIMIT 1", (mid, wk)).fetchone()
    ahead = cur[place - 2]["pts"] - pts + 1 if place and place > 1 else 0
    return {"pts": pts or 0, "place": place, "total": len(cur), "ahead": ahead,
            "rush_x": rush["x"] if rush else None, "rush_place": rush_place,
            "top_ch": top["ch"] if top else None, "prev_pts": next((r["pts"] for r in prev if r["mid"] == mid), 0)}
