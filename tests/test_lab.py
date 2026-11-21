"""Тесты кабинета учеников (students/lab.py) на настоящем Flask.
Запуск: python3 tests/test_lab.py   (нужен только flask — он есть в образе и в песочнице)"""
import os, sys, tempfile, sqlite3, json
HERE = os.path.dirname(os.path.abspath(__file__))
D = tempfile.mkdtemp()
os.environ.update({"LAB_DATA": D, "ROOT_HOST": "neurofans.ru", "GEO_ENABLE": "0", "MAX_HITS_PER_IP": "5"})
sys.path.insert(0, os.path.join(HERE, "..", "students"))
import lab

c = lab.app.test_client()
ADM = {"X-Admin-Token": lab.ADMIN_TOKEN}
J = lambda r: json.loads(r.data)
LAB = {"Host": "lab.neurofans.ru"}
IPH = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit Instagram 300.0"

# админка закрыта без токена, инвайты создаются с токеном
assert c.get("/api/admin/overview", headers=LAB).status_code == 401
r = c.post("/api/admin/invites", json={"count": 3, "note": "поток 1"}, headers={**LAB, **ADM})
codes = [x["code"] for x in J(r)["codes"]]; assert len(codes) == 3 and codes[0].startswith("NF-")
assert J(r)["codes"][0]["url"].startswith("https://lab.neurofans.ru/?invite=NF-")
print("инвайты: OK", codes[0])

# регистрация: без инвайта нельзя, по инвайту — можно, повторно тот же код — нельзя
assert c.post("/api/register", json={"invite": "NF-XXXXXX", "nick": "anna", "password": "12345678"}, headers=LAB).status_code == 400
assert c.post("/api/register", data="nick=x", headers=LAB).status_code == 415          # только JSON (защита от чужих форм)
assert c.post("/api/register", json={"invite": codes[0], "nick": "anna", "password": "x"}, headers=LAB).status_code == 400
r = c.post("/api/register", json={"invite": codes[0].lower(), "nick": "anna", "password": "12345678"}, headers=LAB)
assert r.status_code == 200, r.data
assert c.post("/api/register", json={"invite": codes[0], "nick": "bob", "password": "12345678"}, headers=LAB).status_code == 400
assert c.post("/api/register", json={"invite": codes[1], "nick": "ANNA", "password": "12345678"}, headers=LAB).status_code == 400  # ник занят
assert c.post("/api/register", json={"invite": codes[1], "nick": "x", "password": "12345678"}, headers={**LAB, "Origin": "https://evil.com"}).status_code == 403
print("регистрация по инвайту: OK")

# модель + ссылки на каналы
r = c.post("/api/models", json={"name": "Анна Мур", "target": "fanvue.com/annamoore", "channels": ["threads", "tiktok"]}, headers=LAB)
m = J(r)["models"][0]; assert m["slug"] == "anna-mur" and m["target"] == "https://fanvue.com/annamoore", m
slugs = sorted(l["slug"] for l in m["links"]); assert slugs == ["anna-mur-t", "anna-mur-tt"], slugs
assert m["links"][0]["url"].startswith("https://to.neurofans.ru/anna-mur-")
assert c.post("/api/models", json={"name": "X", "target": "javascript:alert(1)"}, headers=LAB).status_code == 400
assert c.post("/api/models", json={"name": "X", "target": "https://to.neurofans.ru/loop"}, headers=LAB).status_code == 400
r = c.post("/api/links", json={"mid": m["id"], "ch": "instagram", "slug": "anna-reels", "target": "https://t.me/annamoore"}, headers=LAB)
m = J(r)["models"][0]; assert any(l["slug"] == "anna-reels" for l in m["links"])
assert c.post("/api/links", json={"mid": m["id"], "ch": "instagram", "slug": "anna-reels"}, headers=LAB).status_code == 400
print("модели и ссылки: OK")

# клики по to.: человек → 302 и +1; повтор → не очко; бот → тот же редирект, без записи
TO = lambda ip: {"Host": "to.neurofans.ru", "User-Agent": IPH, "X-Real-IP": ip}
o = lab.app.test_client()                                  # «посетитель» — другой браузер
r = o.get("/anna-mur-t", headers=TO("5.5.5.1"), environ_base={"REMOTE_ADDR": "172.18.0.3"})
assert r.status_code == 302 and r.headers["Location"] == "https://fanvue.com/annamoore", r.headers
r = o.get("/anna-mur-tt", headers=TO("5.5.5.1"), environ_base={"REMOTE_ADDR": "172.18.0.3"})
r = o.get("/anna-reels", headers=TO("5.5.5.1"), environ_base={"REMOTE_ADDR": "172.18.0.3"})
assert r.headers["Location"] == "https://t.me/annamoore"
b = lab.app.test_client()
r = b.get("/anna-mur-t", headers={"Host": "to.neurofans.ru", "User-Agent": "facebookexternalhit/1.1"})
assert r.status_code == 302 and r.headers["Location"] == "https://fanvue.com/annamoore"
assert b.get("/nope-404", headers={"Host": "to.neurofans.ru", "User-Agent": IPH}).status_code == 404
q = sqlite3.connect(os.path.join(D, "lab.db"))
rows = q.execute("SELECT scored, why, iph FROM clicks ORDER BY id").fetchall()
assert [x[0] for x in rows] == [1, 0, 0] and rows[1][1] == "repeat", rows
assert "5.5.5.1" not in json.dumps(rows)                    # IP не храним — только хэш
print("редирект, повторы, боты: OK")

# подделка X-Real-IP снаружи не работает (доверяем только внутреннему адресу caddy)
e = lab.app.test_client()
e.get("/anna-mur-t", headers=TO("9.9.9.9"), environ_base={"REMOTE_ADDR": "8.8.8.8"})
assert q.execute("SELECT iph FROM clicks ORDER BY id DESC").fetchone()[0] == lab.ip_hash("8.8.8.8")

# свой клик ученика не идёт в очки (кука nfo ставится на весь домен при входе)
r = c.get("/anna-mur-tt", headers={"Host": "to.neurofans.ru", "User-Agent": IPH, "X-Real-IP": "7.7.7.7"},
          environ_base={"REMOTE_ADDR": "172.18.0.3"})
assert q.execute("SELECT why FROM clicks ORDER BY id DESC").fetchone()[0] == "own"
print("свои клики не считаются: OK")

# лимит с одного IP за неделю (в тесте 5)
for i in range(8):
    lab.app.test_client().get("/anna-mur-t", headers={**TO("6.6.6.6"), "User-Agent": IPH + f" v{i}"},
                              environ_base={"REMOTE_ADDR": "172.18.0.3"})
assert q.execute("SELECT SUM(scored) FROM clicks WHERE iph=?", (lab.ip_hash("6.6.6.6"),)).fetchone()[0] == 5
assert q.execute("SELECT COUNT(*) FROM clicks WHERE why='cap_ip'").fetchone()[0] == 3
print("лимит IP: OK")

# второй ученик и лига
c2 = lab.app.test_client()
assert c2.post("/api/register", json={"invite": codes[1], "nick": "kira", "password": "abcdefgh"}, headers=LAB).status_code == 200
m2 = J(c2.post("/api/models", json={"name": "Kira", "target": "https://onlyfans.com/kira", "channels": ["reddit"]}, headers=LAB))["models"][0]
assert c2.patch(f"/api/models/{m['id']}", json={"name": "hack"}, headers=LAB).status_code == 404   # чужая модель
lab.app.test_client().get("/kira-r", headers=TO("4.4.4.4"), environ_base={"REMOTE_ADDR": "172.18.0.3"})
L = J(c.get("/api/league", headers=LAB))
assert [x["model"] for x in L["rows"]] == ["Анна Мур", "Kira"] and L["rows"][0]["mine"] and not L["rows"][1]["mine"], L["rows"]
assert L["rows"][0]["pts"] == 7, L["rows"][0]      # 5.5.5.1 + 8.8.8.8 + 5 с 6.6.6.6
assert J(c.get("/api/league?ch=reddit", headers=LAB))["rows"][0]["model"] == "Kira"
c2.patch(f"/api/models/{m2['id']}", json={"hidden": True}, headers=LAB)
assert [x["model"] for x in J(c.get("/api/league", headers=LAB))["rows"]] == ["Анна Мур"]
print("лига недели и скрытие из лиги: OK")

# статистика
S = J(c.get(f"/api/stats?mid={m['id']}", headers=LAB))
t = S["totals"]; assert t["week_people"] == 7 and t["own_week"] == 1 and t["all_clicks"] == 13, t
assert sum(x["clicks"] for x in S["days"]) == 13 and len(S["days"]) == 14
assert {x["ch"] for x in S["channels"]} == {"threads", "tiktok", "instagram"}
assert S["apps"][0]["k"] == "Instagram" and S["devices"][0]["k"] == "iOS"
assert J(c2.get(f"/api/stats?mid={m['id']}", headers=LAB))["ok"] is False      # чужую статистику не видно
print("статистика: OK")

# хосты: корень → кабинет, to./ → корень; блокировка отключает ссылки
assert c.get("/", headers={"Host": "neurofans.ru"}).headers["Location"] == "https://lab.neurofans.ru/"
assert c.get("/", headers={"Host": "to.neurofans.ru"}).status_code == 302
uid2 = J(c.get("/api/admin/overview", headers={**LAB, **ADM}))["users"]
kira = [u for u in uid2 if u["nick"] == "kira"][0]
c.patch(f"/api/admin/users/{kira['id']}", json={"blocked": True}, headers={**LAB, **ADM})
assert lab.app.test_client().get("/kira-r", headers=TO("4.4.4.5")).status_code == 410
assert c2.get("/api/me", headers=LAB).status_code == 401
r = J(c.patch(f"/api/admin/users/{kira['id']}", json={"blocked": False, "reset_password": True}, headers={**LAB, **ADM}))
assert c2.post("/api/login", json={"nick": "kira", "password": r["password"]}, headers=LAB).status_code == 200
assert c2.post("/api/login", json={"nick": "kira", "password": "wrongpass"}, headers=LAB).status_code == 401
print("хосты, блокировка, сброс пароля: OK")

# ---------- тест-клик владельца: в клики не попадает ----------
n0 = q.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
r = c.get("/anna-mur-t?check=1", headers={"Host": "to.neurofans.ru", "User-Agent": IPH, "X-Real-IP": "7.7.7.8"},
          environ_base={"REMOTE_ADDR": "172.18.0.3"})
assert r.status_code == 302 and r.headers["Location"] == "https://fanvue.com/annamoore"
assert q.execute("SELECT COUNT(*) FROM clicks").fetchone()[0] == n0
assert q.execute("SELECT COUNT(*) FROM link_checks").fetchone()[0] == 1
lk = [l for l in J(c.get("/api/me", headers=LAB))["models"][0]["links"] if l["slug"] == "anna-mur-t"][0]
assert lk["checked_at"] and lk["first_at"], lk
lab.app.test_client().get("/anna-mur-t?check=1", headers=TO("3.3.3.3"), environ_base={"REMOTE_ADDR": "172.18.0.3"})
assert q.execute("SELECT COUNT(*) FROM clicks").fetchone()[0] == n0 + 1      # чужой ?check=1 — обычный клик
assert q.execute("SELECT COUNT(*) FROM link_checks").fetchone()[0] == 1
print("тест-клик: OK")

# ---------- ранги и ачивки ----------
A = J(c.get("/api/achievements", headers=LAB))
am = A["models"][0]
assert am["rank"]["name"] == "Новичок" and am["rank"]["next_at"] == 10, am["rank"]
assert "first_fan" in [b["id"] for b in am["badges"]] and am["week"]["place"] == 1, am
assert len(A["defs"]) == len(lab.game.BADGES)
L = J(c.get("/api/league", headers=LAB)); assert L["rows"][0]["rank"] == "🌱"
assert lab.game.rank_of(260)["name"] == "Звезда" and lab.game.rank_of(5000)["next_at"] is None
print("ранги и ачивки: OK")

# ---------- карточка недели ----------
import io
from PIL import Image
r = c.get(f"/api/card.png?mid={m['id']}&nick=1", headers=LAB)
assert r.status_code == 200 and r.headers["Content-Type"] == "image/png"
assert Image.open(io.BytesIO(r.data)).size == (1080, 1920)
open("/tmp/lab_card_test.png", "wb").write(r.data)
assert c2.get(f"/api/card.png?mid={m['id']}", headers=LAB).status_code == 404      # чужая модель
assert c.get(f"/api/card.png?mid={m['id']}&kind=last", headers=LAB).status_code == 200
print("карточка недели: OK (/tmp/lab_card_test.png)")

# ---------- «Пульс лиги»: только агрегаты и только когда моделей ≥ 3 ----------
P = J(c.get("/api/public/pulse", headers=LAB))
assert P["ok"] and P["ready"] is False and "people_week" not in P
print("пульс лиги: OK")

# ---------- уведомления ----------
import datetime as dt
import lab_notify
sent = []
def fake_http(method, payload, timeout=30):
    if method == "sendMessage":
        sent.append(payload); return {"ok": True, "result": {}}
    if method == "getMe":
        return {"ok": True, "result": {"username": "nf_lab_bot"}}
    return {"ok": True, "result": []}
N = lab_notify.Notifier(lab.db, lab.TZ, lab.week_key, "TOKEN", "lab.neurofans.ru", http=fake_http)
cn = lab.db()
uid = cn.execute("SELECT id FROM users WHERE nick='anna'").fetchone()[0]
now = lab.now_utc()
code = N.new_link_code(cn, uid, now)
N.handle_update(cn, {"update_id": 1, "message": {"text": "/start WRONG", "chat": {"id": 555, "type": "private"}}}, now)
assert cn.execute("SELECT tg_chat_id FROM users WHERE id=?", (uid,)).fetchone()[0] is None
N.handle_update(cn, {"update_id": 2, "message": {"text": "/start " + code, "chat": {"id": 555, "type": "private"}}}, now)
assert cn.execute("SELECT tg_chat_id FROM users WHERE id=?", (uid,)).fetchone()[0] == 555
assert "Готово" in sent[-1]["text"]
N.handle_update(cn, {"update_id": 3, "message": {"text": "/start " + code, "chat": {"id": 777, "type": "private"}}}, now)
assert cn.execute("SELECT tg_chat_id FROM users WHERE id=?", (uid,)).fetchone()[0] == 555   # код одноразовый
# добиваем модель до 12 людей, чтобы сработала веха 10
for i in range(6):
    lab.app.test_client().get("/anna-mur-tt", headers={**TO(f"9.1.1.{i}"), "User-Agent": IPH + f" w{i}"},
                              environ_base={"REMOTE_ADDR": "172.18.0.3"})
k1 = N.scan(cn, now)
keys = [r[0] for r in cn.execute("SELECT key FROM notify WHERE uid=?", (uid,))]
assert any(k.startswith("first:") for k in keys) and f"ms:{m['id']}:10" in keys, keys
assert not any(k.startswith("badge:") for k in keys)          # первый проход — ачивки молча (без «хвоста»)
assert N.scan(cn, now) == 0                                    # повторный проход ничего не дублирует
loc = now.astimezone(lab.TZ)
night = loc.replace(hour=2, minute=0).astimezone(dt.timezone.utc)
assert N.flush(cn, night) == 0                                 # тихие часы
day = loc.replace(hour=12, minute=0).astimezone(dt.timezone.utc)
queued = cn.execute("SELECT COUNT(*) FROM notify WHERE status='queued'").fetchone()[0]
n_sent = N.flush(cn, day)
assert n_sent == min(queued, lab_notify.DAILY_CAP), (n_sent, queued)
assert N.flush(cn, day) == 0                                   # лимит в сутки
# воскресный дайджест (вне лимита) и режим «выкл»
sunday = (loc + dt.timedelta(days=6 - loc.weekday())).replace(hour=19, minute=30).astimezone(dt.timezone.utc)
if lab.week_key(sunday) == lab.week_key(now):
    N.scan(cn, sunday)
    assert cn.execute("SELECT COUNT(*) FROM notify WHERE kind='digest'").fetchone()[0] == 1
    assert N.flush(cn, sunday) >= 1 and "До конца недели" in sent[-1]["text"]
cn.execute("UPDATE users SET tg_mode='off' WHERE id=?", (uid,)); cn.commit()
assert N.enqueue(cn, uid, "x:test", "t", "first", now) is False
cn.execute("UPDATE users SET tg_mode='digest' WHERE id=?", (uid,)); cn.commit()
assert N.enqueue(cn, uid, "x:test2", "t", "first", now) is False and N.enqueue(cn, uid, "x:test3", "t", "digest", now)
N.handle_update(cn, {"update_id": 4, "message": {"text": "/stop", "chat": {"id": 555, "type": "private"}}}, now)
assert cn.execute("SELECT tg_mode FROM users WHERE id=?", (uid,)).fetchone()[0] == "off"
print("уведомления: OK (привязка, дубли, тихие часы, лимит, дайджест, режимы)")

print("ВСЕ ТЕСТЫ КАБИНЕТА ПРОШЛИ")
