"""NeuroFans Lab — событийные Telegram-уведомления ученикам (отдельный бот, без спама).

Бот кабинета — ОТДЕЛЬНЫЙ (LAB_BOT_TOKEN), не PuzzleBot и не бот трекера: мы сами читаем
его getUpdates, конфликтов нет. Без токена модуль спит, а ачивки всё равно синхронизируются.

Защита от дублей и спама:
- у каждого события уникальный ключ: UNIQUE(uid, key) в таблице notify — дубль не запишется;
- не больше DAILY_CAP сообщений в сутки на ученика (дайджест/итоги/служебные — вне лимита);
- тихие часы QUIET (23:00–09:00 по LAB_TZ): сообщения ждут утра;
- старше MAX_AGE часов — не отправляются вовсе (status=expired);
- режим ученика: all | digest (только итоги недели) | off.
"""
import datetime as dt
import json
import logging
import secrets
import time
import urllib.error
import urllib.request

from contextlib import closing

import lab_game as game

log = logging.getLogger("lab.notify")

DAILY_CAP = 4
QUIET = (23, 9)
MAX_AGE_H = 24
EXEMPT = {"digest", "results", "system"}
DIGEST_ONLY = EXEMPT
SCAN_EVERY = 120
CH_NAMES = {"threads": "Threads", "instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube",
            "telegram": "Telegram", "x": "X", "reddit": "Reddit", "other": "«Другое»"}


def plural(n, a, b, c):
    n = abs(int(n)) % 100
    m = n % 10
    if 10 < n < 20:
        return c
    return b if 1 < m < 5 else a if m == 1 else c


class Notifier:
    def __init__(self, db, tz, week_key, token="", lab_host="lab.neurofans.ru", username="", http=None):
        self.db, self.tz, self.week_key = db, tz, week_key
        self.token, self.lab_host = token or "", lab_host
        self._username = username or ""
        self.http = http or self._http
        self.last_scan = 0.0

    # ---------- Telegram API ----------
    @property
    def enabled(self):
        return bool(self.token)

    def _http(self, method, payload, timeout=30):
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error_code": e.code}

    def username(self):
        if not self._username and self.enabled:
            r = self.http("getMe", {}, 10)
            self._username = ((r or {}).get("result") or {}).get("username", "")
        return self._username

    # ---------- привязка ----------
    def new_link_code(self, c, uid, now):
        code = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
        exp = (now + dt.timedelta(minutes=30)).isoformat(timespec="seconds")
        c.execute("UPDATE users SET tg_code=?, tg_code_exp=? WHERE id=?", (code, exp, uid))
        c.commit()
        return code

    def handle_update(self, c, upd, now):
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not chat or (msg.get("chat") or {}).get("type") != "private":
            return
        if text.startswith("/start"):
            code = text[6:].strip()
            u = c.execute("SELECT id, nick, tg_code_exp FROM users WHERE tg_code=? AND tg_code IS NOT NULL",
                          (code,)).fetchone() if code else None
            if u and u["tg_code_exp"] and u["tg_code_exp"] >= now.isoformat(timespec="seconds"):
                c.execute("UPDATE users SET tg_chat_id=NULL WHERE tg_chat_id=?", (chat,))
                c.execute("UPDATE users SET tg_chat_id=?, tg_code=NULL, tg_code_exp=NULL, "
                          "tg_mode=CASE WHEN tg_mode='off' THEN 'all' ELSE tg_mode END WHERE id=?", (chat, u["id"]))
                c.commit()
                self._send(chat, f"✅ Готово! Уведомления подключены к кабинету {u['nick']}.\n\n"
                                 "Пришлю: первого человека по новой ссылке, вехи по людям, места в лиге, "
                                 "новые ачивки, воскресный дайджест и итоги недели. Не больше 4 сообщений "
                                 "в день, ночью — тишина.\n\nВыключить: /stop")
            else:
                self._send(chat, "Чтобы подключить уведомления, открой кабинет NeuroFans Lab → меню → "
                                 f"«Уведомления в Telegram» → «Подключить».\nhttps://{self.lab_host}/")
        elif text.startswith("/stop"):
            c.execute("UPDATE users SET tg_mode='off' WHERE tg_chat_id=?", (chat,))
            c.commit()
            self._send(chat, "Уведомления выключены. Включить снова — в кабинете, раздел «Уведомления в Telegram».")

    def poll(self, c, now, timeout=20):
        if not self.enabled:
            return 0
        off = c.execute("SELECT v FROM kv WHERE k='tg_offset'").fetchone()
        r = self.http("getUpdates", {"offset": int(off[0]) if off else 0, "timeout": timeout,
                                     "allowed_updates": ["message"]}, timeout + 10)
        ups = (r or {}).get("result") or []
        for u in ups:
            try:
                self.handle_update(c, u, now)
            except Exception as e:
                log.warning("update failed: %s", e)
            c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('tg_offset',?)", (str(u["update_id"] + 1),))
        c.commit()
        return len(ups)

    def _send(self, chat, text):
        r = self.http("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": True}, 15)
        return r or {}

    # ---------- очередь ----------
    def enqueue(self, c, uid, key, text, kind, now):
        u = c.execute("SELECT tg_chat_id, tg_mode FROM users WHERE id=? AND blocked=0", (uid,)).fetchone()
        if not u or not u["tg_chat_id"]:
            return False
        mode = u["tg_mode"] or "all"
        if mode == "off" or (mode == "digest" and kind not in DIGEST_ONLY):
            return False
        cur = c.execute("INSERT OR IGNORE INTO notify(uid, key, kind, text, created, status) "
                        "VALUES(?,?,?,?,?,'queued')", (uid, key, kind, text, now.isoformat(timespec="seconds")))
        return bool(cur.rowcount)

    def flush(self, c, now):
        """Отправляет очередь с учётом тихих часов, суточного лимита и срока годности."""
        loc = now.astimezone(self.tz)
        c.execute("UPDATE notify SET status='expired' WHERE status='queued' AND created<?",
                  ((now - dt.timedelta(hours=MAX_AGE_H)).isoformat(timespec="seconds"),))
        c.commit()
        if loc.hour >= QUIET[0] or loc.hour < QUIET[1]:
            return 0
        day0 = loc.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
        sent = 0
        for n in c.execute("SELECT n.*, u.tg_chat_id FROM notify n JOIN users u ON u.id=n.uid "
                           "WHERE n.status='queued' ORDER BY n.id").fetchall():
            if not n["tg_chat_id"]:
                c.execute("UPDATE notify SET status='skipped' WHERE id=?", (n["id"],))
                continue
            if n["kind"] not in EXEMPT:
                today = c.execute("SELECT COUNT(*) FROM notify WHERE uid=? AND status='sent' AND sent_at>=? "
                                  "AND kind NOT IN ('digest','results','system')",
                                  (n["uid"], day0.isoformat(timespec="seconds"))).fetchone()[0]
                if today >= DAILY_CAP:
                    continue                      # подождёт завтра (или истечёт через сутки)
            r = self._send(n["tg_chat_id"], n["text"])
            if r.get("ok"):
                c.execute("UPDATE notify SET status='sent', sent_at=? WHERE id=?",
                          (now.isoformat(timespec="seconds"), n["id"]))
                sent += 1
            elif r.get("error_code") in (400, 403):  # бот заблокирован / чат удалён → отвязываем
                c.execute("UPDATE users SET tg_chat_id=NULL WHERE tg_chat_id=?", (n["tg_chat_id"],))
                c.execute("UPDATE notify SET status='failed' WHERE id=?", (n["id"],))
            elif r.get("error_code") == 429:
                break                             # Telegram просит притормозить — продолжим в следующий проход
            c.commit()
            time.sleep(0.05)
        c.commit()
        return sent

    # ---------- события ----------
    def scan(self, c, now):
        """Находит события и кладёт их в очередь. Идемпотентно: повторный проход ничего не дублирует."""
        loc = now.astimezone(self.tz)
        wk, pwk = self.week_key(now), self.week_key(now - dt.timedelta(days=7))
        ppwk = self.week_key(now - dt.timedelta(days=14))
        cab = f"https://{self.lab_host}/"
        linked = {r["id"]: r for r in c.execute(
            "SELECT id, nick, tg_mode FROM users WHERE tg_chat_id IS NOT NULL AND blocked=0 AND tg_mode<>'off'")}
        # ачивки (в т.ч. итоги прошлой недели); первый проход — молча, чтобы не завалить старыми
        seeded = c.execute("SELECT 1 FROM kv WHERE k='badges_seeded'").fetchone()
        new_badges = game.sync_badges(c, pwk, ppwk, now.isoformat(timespec="seconds"))
        if not seeded:
            c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('badges_seeded','1')")
            new_badges = []
        if not linked:
            c.commit()
            return 0
        models = {r["id"]: r for r in c.execute(
            "SELECT id, uid, name FROM models WHERE archived=0 AND uid IN (%s)" % ",".join("?" * len(linked)),
            tuple(linked))}
        q = 0
        for mid, b in new_badges:
            m = models.get(mid)
            if m:
                ic, nm, ds = game.BADGES[b]
                q += self.enqueue(c, m["uid"], f"badge:{mid}:{b}",
                                  f"{ic} Новая ачивка у {m['name']}: «{nm}» — {ds}.\n{cab}", "badge", now)
        # 1) первый живой человек по ссылке (за последние сутки — без старого «хвоста»)
        since = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        for r in c.execute(
                "SELECT l.id lid, l.uid, l.ch, m.name, k.city FROM links l JOIN models m ON m.id=l.mid "
                "JOIN clicks k ON k.id=(SELECT MIN(id) FROM clicks WHERE lid=l.id AND scored=1) "
                "WHERE k.ts>=?", (since,)).fetchall():
            if r["uid"] in linked:
                city = f" из города {r['city']}" if r["city"] else ""
                q += self.enqueue(c, r["uid"], f"first:{r['lid']}",
                                  f"🎉 Ссылка {CH_NAMES.get(r['ch'], r['ch'])} модели {r['name']} заработала: "
                                  f"первый живой человек{city}!\n{cab}", "first", now)
        # 2) вехи по людям — только самая высокая достигнутая
        allp = game.people_all(c, list(models))
        for mid, p in allp.items():
            reached = [x for x in game.MILESTONES if p >= x]
            if reached:
                ms = reached[-1]
                rk = game.rank_of(p)
                extra = f" Новый ранг — {rk['icon']} {rk['name']}!" if ms in [x[0] for x in game.RANKS] else ""
                q += self.enqueue(c, models[mid]["uid"], f"ms:{mid}:{ms}",
                                  f"🔥 {models[mid]['name']}: {ms} {plural(ms, 'живой человек', 'живых человека', 'живых людей')} "
                                  f"за всё время.{extra}\n{cab}", "milestone", now)
        # 3) места в лиге: вход в топ-10 / топ-3, вылет из топ-3
        cur, prev = game.board(c, wk), game.board(c, pwk)
        n = len(cur)
        places = {r["mid"]: (i + 1, r["pts"]) for i, r in enumerate(cur)}
        for mid, m in models.items():
            p, pts = places.get(mid, (None, 0))
            if p and pts >= 5:
                ahead = cur[p - 2]["pts"] - pts + 1 if p > 1 else 0
                tail = f" До #{p - 1} — {ahead} {plural(ahead, 'человек', 'человека', 'человек')}." if p > 1 else " Ты лидер недели 👑"
                if p <= 3 and n >= 5:
                    q += self.enqueue(c, m["uid"], f"top3:{wk}:{mid}",
                                      f"🏆 {m['name']} — #{p} в лиге недели!{tail}\n{cab}", "top", now)
                elif p <= 10 and n >= 12:
                    q += self.enqueue(c, m["uid"], f"top10:{wk}:{mid}",
                                      f"🏅 {m['name']} — в топ-10 лиги недели: #{p}.{tail}\n{cab}", "top", now)
            kvk = f"pl:{wk}:{mid}"
            was = c.execute("SELECT v FROM kv WHERE k=?", (kvk,)).fetchone()
            if was and int(was[0]) <= 3 and (not p or p > 3) and n >= 5:
                gap = (cur[2]["pts"] - pts + 1) if n >= 3 else 1
                q += self.enqueue(c, m["uid"], f"down3:{mid}:{loc.date()}",
                                  f"⚔️ {m['name']} обогнали: теперь #{p or '—'}. До пьедестала — {gap} "
                                  f"{plural(gap, 'человек', 'человека', 'человек')}.\n{cab}", "down", now)
            if p:
                c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (kvk, str(p)))
        # 4) «Рывок недели» — со среды, когда цифры уже устоялись
        if loc.weekday() >= 2:
            for i, r in enumerate(game.rush_rows(cur, prev)[:3]):
                m = models.get(r["mid"])
                if m:
                    x = f"{r['x']:.1f}".replace(".", ",")
                    q += self.enqueue(c, m["uid"], f"rush:{wk}:{r['mid']}",
                                      f"🚀 {m['name']} — #{i + 1} в «Рывке недели»: ×{x} к прошлой неделе.\n{cab}",
                                      "rush", now)
        # 5) воскресный дайджест, 19:00
        if loc.weekday() == 6 and loc.hour >= 19:
            left = 24 - loc.hour
            for uid, u in linked.items():
                mine = [m for m in models.values() if m["uid"] == uid]
                if not mine:
                    continue
                lines = []
                for m in sorted(mine, key=lambda m: -places.get(m["id"], (0, 0))[1])[:3]:
                    s = game.model_week(c, m["id"], wk, pwk)
                    pl = f"#{s['place']} из {s['total']}" if s["place"] else "пока без очков"
                    nxt = f", до #{s['place'] - 1}: {s['ahead']}" if s["place"] and s["place"] > 1 else ""
                    lines.append(f"• {m['name']} — {pl}, {s['pts']} {plural(s['pts'], 'очко', 'очка', 'очков')}{nxt}")
                q += self.enqueue(c, uid, f"digest:{wk}",
                                  f"⏳ До конца недели ~{left} ч.\n" + "\n".join(lines) +
                                  f"\n\nПоследний рывок решает места 💪\n{cab}", "digest", now)
        # 6) итоги недели, понедельник 10:00
        if loc.weekday() == 0 and loc.hour >= 10:
            pboard = {r["mid"]: (i + 1, r["pts"]) for i, r in enumerate(prev)}
            for uid in linked:
                res = [(models[m]["name"], *pboard[m]) for m in models if models[m]["uid"] == uid and m in pboard]
                if res:
                    res.sort(key=lambda x: x[1])
                    body = "\n".join(f"• {nm} — #{p} из {len(prev)} ({pts} {plural(pts, 'очко', 'очка', 'очков')})"
                                     for nm, p, pts in res[:3])
                    q += self.enqueue(c, uid, f"results:{pwk}",
                                      f"🏁 Итоги недели {pwk.split('-W')[1]}:\n{body}\n\n"
                                      f"Карточка для сторис — в кабинете, новая неделя уже идёт.\n{cab}", "results", now)
        c.commit()
        return q

    # ---------- фоновый цикл ----------
    def tick(self, now):
        with closing(self.db()) as c:
            if self.enabled:
                self.poll(c, now, timeout=20)
            else:
                time.sleep(20)
            if time.time() - self.last_scan >= SCAN_EVERY:
                self.last_scan = time.time()
                self.scan(c, now)
                if self.enabled:
                    self.flush(c, now)

    def run(self, now_fn):
        log.info("notifier: %s", "Telegram on" if self.enabled else "без токена — только ачивки")
        while True:
            try:
                self.tick(now_fn())
            except Exception as e:
                log.warning("notifier tick failed: %s", e)
                time.sleep(10)
