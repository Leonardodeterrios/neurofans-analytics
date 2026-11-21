"""NF Core: живой пульс, детектор всплесков/провалов и прогноз недели.

Всё считается формулами по кликам ученика (никаких внешних сервисов):
- пульс — люди и клики по минутам за последний час;
- всплеск/провал — сравнение последних 3 часов с «нормой» (среднее за 3 часа по прошлым 7 дням),
  сила отклонения = (факт − норма) / √(норма + 1), как для пуассоновского счёта;
- прогноз — очки недели + темп за последние 48 часов × оставшиеся часы; место прогнозируется
  тем же способом по всем моделям дивизиона;
- ранг — сколько дней до следующего ранга при темпе последних 7 дней.
"""
import datetime as dt
import math

WIN_H = 3            # окно детектора, часов
BASE_D = 7           # база «нормы», дней
RATE_H = 48          # окно темпа для прогноза, часов


def _iso(t):
    return t.isoformat(timespec="seconds")


def _dev(obs, exp):
    return (obs - exp) / math.sqrt(exp + 1)


def signals(rows_now, rows_base, base_hours):
    """rows_now/rows_base: {key: people}. Возвращает список отклонений, самые сильные сверху."""
    out = []
    for k in set(rows_now) | set(rows_base):
        obs = rows_now.get(k, 0)
        exp = rows_base.get(k, 0) * WIN_H / max(1.0, base_hours)
        z = _dev(obs, exp)
        if obs >= 4 and obs >= 2 * exp + 1 and z >= 2:
            kind = "spike"
        elif exp >= 4 and obs <= 0.3 * exp:
            kind = "drop"
        else:
            continue
        out.append({"key": k, "kind": kind, "obs": obs, "exp": round(exp, 1),
                    "x": round((obs + 1) / (exp + 1), 1), "z": round(z, 1)})
    out.sort(key=lambda s: -abs(s["z"]))
    return out


def project(pts, rate_h, hours_left):
    """Прогноз очков на конец недели и разброс (±√ ожидаемого прироста)."""
    add = max(0.0, rate_h) * max(0.0, hours_left)
    return round(pts + add), round(math.sqrt(add)) if add else 0


def core(c, where, args, now, wk, week_start, week_end, divisions, rank_of, ranks, mids):
    """Сводка для блока NF Core по области видимости ученика (where/args из _scope)."""
    q = lambda sql, a=(): c.execute(sql, args + tuple(a)).fetchall()
    t60, t3 = now - dt.timedelta(minutes=60), now - dt.timedelta(hours=WIN_H)
    tb = now - dt.timedelta(days=BASE_D)
    # пульс: 60 минут
    minutes = [[0, 0] for _ in range(60)]
    for r in q(f"SELECT ts, scored FROM clicks WHERE {where} AND ts>=?", (_iso(t60),)):
        try:
            age = (now - dt.datetime.fromisoformat(r["ts"])).total_seconds()
        except ValueError:
            continue
        i = 59 - int(age // 60)
        if 0 <= i < 60:
            minutes[i][0] += 1
            minutes[i][1] += r["scored"] or 0
    # детектор: ссылки и каналы
    base_h = BASE_D * 24 - WIN_H
    ln, lb, cn, cb = {}, {}, {}, {}
    for lo, hi, dl, dc in ((t3, now + dt.timedelta(minutes=5), ln, cn), (tb, t3, lb, cb)):
        for r in q(f"SELECT lid, ch, SUM(scored) p FROM clicks WHERE {where} AND ts>=? AND ts<? "
                   f"GROUP BY lid, ch", (_iso(lo), _iso(hi))):
            p = r["p"] or 0
            dl[r["lid"]] = dl.get(r["lid"], 0) + p
            dc[r["ch"]] = dc.get(r["ch"], 0) + p
    sig_links, sig_ch = signals(ln, lb, base_h), signals(cn, cb, base_h)
    # прогноз по моделям ученика (место — внутри своего дивизиона)
    hours_left = max(0.0, (week_end - now).total_seconds() / 3600)
    elapsed = max(0.0, (now - week_start).total_seconds() / 3600)
    t48 = now - dt.timedelta(hours=RATE_H)
    pts_all = {r["mid"]: r["p"] or 0 for r in c.execute(
        "SELECT k.mid, SUM(k.scored) p FROM clicks k JOIN models m ON m.id=k.mid JOIN users u ON u.id=m.uid "
        "WHERE k.wk=? AND m.hidden=0 AND m.archived=0 AND u.blocked=0 GROUP BY k.mid", (wk,))}
    rate_all = {r["mid"]: (r["p"] or 0) / RATE_H for r in c.execute(
        "SELECT mid, SUM(scored) p FROM clicks WHERE ts>=? AND wk=? GROUP BY mid", (_iso(t48), wk))}
    week_rate = {r["mid"]: (r["p"] or 0) / 7 for r in c.execute(
        "SELECT mid, SUM(scored) p FROM clicks WHERE ts>=? GROUP BY mid", (_iso(tb),))}
    life = {r["mid"]: r["p"] or 0 for r in c.execute("SELECT mid, SUM(scored) p FROM clicks GROUP BY mid")}
    forecast = []
    for mid, name in mids:
        div = divisions.get(mid, "start")
        rivals = [m for m in pts_all if divisions.get(m, "start") == div]
        proj = {m: project(pts_all.get(m, 0), rate_all.get(m, 0), hours_left)[0] for m in rivals}
        pts = pts_all.get(mid, 0)
        my_proj, spread = project(pts, rate_all.get(mid, 0), hours_left)
        place_now = 1 + sum(1 for m in rivals if m != mid and pts_all.get(m, 0) > pts) if pts else None
        place_proj = 1 + sum(1 for m in rivals if m != mid and proj[m] > my_proj) if my_proj else None
        above = sorted(v for m, v in proj.items() if m != mid and v > my_proj)
        target = above[0] if above else None
        rk = rank_of(life.get(mid, 0))
        eta = None
        if rk["next_at"]:
            per_day = week_rate.get(mid, 0)
            if per_day > 0:
                eta = round((rk["next_at"] - rk["people"]) / per_day, 1)
        forecast.append({"mid": mid, "name": name, "div": div, "pts": pts, "rate_h": round(rate_all.get(mid, 0), 2),
                         "proj": my_proj, "spread": spread, "place": place_now, "place_proj": place_proj,
                         "rivals": len(rivals), "target_proj": target, "rank": rk, "rank_eta_days": eta})
    geo = [{"lat": r["lat"], "lon": r["lon"], "city": r["city"], "cc": r["cc"], "ts": r["ts"],
            "scored": r["scored"], "ch": r["ch"]}
           for r in q(f"SELECT lat, lon, city, cc, ts, scored, ch FROM clicks WHERE {where} AND lat IS NOT NULL "
                      f"AND ts>=? ORDER BY ts DESC LIMIT 120", (_iso(now - dt.timedelta(hours=24)),))]
    return {"now": _iso(now), "minutes": minutes, "win_h": WIN_H, "base_d": BASE_D,
            "sig_links": sig_links[:8], "sig_ch": sig_ch[:6], "forecast": forecast,
            "confident": elapsed >= 12, "hours_left": round(hours_left, 1), "geo": geo,
            "has_data": c.execute(f"SELECT 1 FROM clicks WHERE {where} LIMIT 1", args).fetchone() is not None}
