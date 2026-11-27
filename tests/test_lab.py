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


# ---------- админка: пароль и сессия вместо токена в адресе ----------
assert c.get("/api/admin/overview?token=" + lab.ADMIN_TOKEN, headers=LAB).status_code == 401   # токен в URL больше не пускает
ad = lab.app.test_client()
assert J(ad.get("/api/admin/state", headers=LAB))["has_password"] is False
assert ad.post("/api/admin/setup", json={"token": "wrong", "password": "secretpass1"}, headers=LAB).status_code == 403
assert ad.post("/api/admin/setup", json={"token": lab.ADMIN_TOKEN, "password": "short"}, headers=LAB).status_code == 400
assert ad.post("/api/admin/setup", json={"token": lab.ADMIN_TOKEN, "password": "secretpass1"}, headers=LAB).status_code == 200
assert ad.get("/api/admin/overview", headers=LAB).status_code == 200                    # сессия после настройки
ad2 = lab.app.test_client()
assert ad2.post("/api/admin/login", json={"password": "nope-nope-1"}, headers=LAB).status_code == 401
assert ad2.post("/api/admin/login", json={"password": "secretpass1"}, headers=LAB).status_code == 200
O = J(ad2.get("/api/admin/overview", headers=LAB))
assert "stuck" in O and "backups" in O and "consent" in O
ad2.post("/api/admin/logout", json={}, headers=LAB)
assert ad2.get("/api/admin/overview", headers=LAB).status_code == 401
assert c.get("/api/admin/overview", headers=LAB).status_code == 401                     # ученик — не админ
print("админка: пароль, сессия, выход: OK")

# ---------- резервные копии ----------
import gzip as _gz
r = ad.get("/api/admin/backup", headers=LAB)
assert r.status_code == 200 and r.headers["Content-Type"] == "application/gzip"
raw = _gz.decompress(r.data); assert raw[:15] == b"SQLite format 3"
bk = sqlite3.connect(":memory:")
tmpf = os.path.join(D, "restore.db"); open(tmpf, "wb").write(raw)
assert sqlite3.connect(tmpf).execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 2
NB = lab_notify.Notifier(lab.db, lab.TZ, lab.week_key, "", "lab.neurofans.ru", db_path=lab.DB_PATH, backup_dir=lab.BACKUP_DIR)
morning = lab.now_utc().astimezone(lab.TZ).replace(hour=5).astimezone(dt.timezone.utc)
cb = lab.db(); NB.maintenance(cb, morning); NB.maintenance(cb, morning)
assert len([f for f in os.listdir(lab.BACKUP_DIR) if f.endswith(".db.gz")]) >= 1
for i in range(20):
    lab.ops.make_backup(lab.DB_PATH, lab.BACKUP_DIR, f"2030-01-{i+1:02d}")
assert len([f for f in os.listdir(lab.BACKUP_DIR) if f.endswith(".db.gz")]) == lab.ops.KEEP_BACKUPS    # храним 14
# админский чат для еженедельной копии
cb.execute("INSERT OR REPLACE INTO kv VALUES('admin_tg_code','abc123')"); cb.execute("INSERT OR REPLACE INTO kv VALUES('admin_tg_exp','2999-01-01')"); cb.commit()
N.handle_update(cb, {"update_id": 9, "message": {"text": "/start admabc123", "chat": {"id": 999, "type": "private"}}}, lab.now_utc())
assert cb.execute("SELECT v FROM kv WHERE k='admin_chat'").fetchone()[0] == "999"
print("резервные копии: OK")

# ---------- чек-лист первого дня, согласие, сравнение, конверсия, дивизионы, PWA ----------
me = J(c.get("/api/me", headers=LAB))
st = {x["id"]: x["done"] for x in me["onboarding"]["steps"]}
assert st["model"] and st["link"] and st["check"] and st["person"], st
c3 = lab.app.test_client()
c3.post("/api/register", json={"invite": codes[2], "nick": "newbie", "password": "abcdefgh"}, headers=LAB)
o3 = J(c3.get("/api/me", headers=LAB))["onboarding"]
assert o3["done"] == 0 and not o3["hidden"]
c3.post("/api/onboarding/hide", json={}, headers=LAB)
assert J(c3.get("/api/me", headers=LAB))["onboarding"]["hidden"]
assert J(c.post("/api/privacy", json={"public_ok": True}, headers=LAB))["public_ok"] is True
lids = [l["id"] for l in me["models"][0]["links"]]
CMP = J(c.get("/api/compare?ids=" + ",".join(map(str, lids)), headers=LAB))
assert len(CMP["links"]) == len(lids) and len(CMP["links"][0]["days"]) == 14 and sum(x["week"] for x in CMP["links"]) >= 7
assert J(c2.get("/api/compare?ids=" + ",".join(map(str, lids)), headers=LAB))["links"] == []     # чужие ссылки не видно
CV = J(c.post("/api/conv", json={"mid": m["id"], "subs": 3}, headers=LAB))
cur = [w for w in CV["weeks"] if w["wk"] == CV["week"]][0]
assert cur["subs"] == 3 and cur["rate"] and len(CV["weeks"]) == 8
assert c.post("/api/conv", json={"mid": m["id"], "subs": -1}, headers=LAB).status_code == 400
assert c2.post("/api/conv", json={"mid": m["id"], "subs": 1}, headers=LAB).status_code == 404
LG = J(c.get("/api/league?div=start", headers=LAB))
assert LG["div"] == "start" and LG["my_div"] in ("start", "pro", "elite") and len(LG["divs"]) == 3
assert all(r["div"] == "start" for r in LG["rows"])
assert J(c.get("/api/league?div=elite", headers=LAB))["rows"] == []
assert lab.game.div_of(0) == "start" and lab.game.div_of(50) == "pro" and lab.game.div_of(1000) == "elite"
assert c.get("/manifest.webmanifest", headers=LAB).status_code == 200
assert Image.open(io.BytesIO(c.get("/icon-512.png", headers=LAB).data)).size == (512, 512)
assert b"caches" in c.get("/sw.js", headers=LAB).data
WP = J(ad.get("/api/admin/weekly_post?wk=cur", headers=LAB))
assert "Итоги недели" in WP["text"] and "@anna" in WP["text"]            # anna дала согласие — ник есть
assert "fanvue" not in WP["text"].lower() and "to.neurofans" not in WP["text"]
print("чек-лист, согласие, сравнение, конверсия, дивизионы, PWA, пост итогов: OK")

# NF Core: пульс, детектор, прогноз
CO = J(c.get("/api/core", headers=LAB))
assert len(CO["minutes"]) == 60 and CO["has_data"] and CO["forecast"] and CO["forecast"][0]["proj"] >= CO["forecast"][0]["pts"]
assert sum(x[0] for x in CO["minutes"]) >= 1                                   # клики теста только что — видны в пульсе
assert c2.get("/api/core?mid=" + str(m["id"]), headers=LAB).status_code == 404
assert J(c3.get("/api/core", headers=LAB))["has_data"] is False                # у новичка — пусто, фронт покажет DEMO
import lab_core
sp = lab_core.signals({1: 14, 2: 0}, {1: 4 * 55, 2: 30 * 55}, 165)              # норма 4 → 14 = всплеск; норма 30 → 0 = провал
assert [x["kind"] for x in sp] == ["drop", "spike"] or {x["kind"] for x in sp} == {"spike", "drop"}
assert lab_core.signals({1: 5}, {1: 5 * 55}, 165) == []                         # как обычно — тишина
assert lab_core.project(100, 2, 10) == (120, 4)
assert "div_total" in LG
print("NF Core: пульс, всплески, прогноз: OK")

# ---------- Студия: паспорт, план недели, чаттер, лимиты, Tribute ----------
import time as _t, hmac as _h, hashlib as _hl, lab_ai
calls = []
def fake(url, secret, payload):
    calls.append(payload)
    name = payload["response_format"]["json_schema"]["name"]
    sch = payload["response_format"]["json_schema"]["schema"]
    if name == "week_outline":
        return json.dumps({"storyline": "неделя у моря", "days": [{"day": d, "beat": "тема " + d, "goal": "Прогрев"} for d in lab_ai.DAYS]})
    if name in ("day_posts", "one_post"):
        ch = sch["properties"]["posts"]["items"]["properties"]["ch"]["enum"][0] if name == "day_posts" else sch["properties"]["post"]["properties"]["ch"]["enum"][0]
        post = {"ch": ch, "time": "19:40", "format": "Личное", "goal": "Прогрев", "hook": "", "draft": "d", "problems": "p",
                "text": "вчера — дождь", "hashtags": "", "needsMedia": False, "imagePrompt": "", "sound": ""}
        return json.dumps({"posts": [post, dict(post, ch="tiktok")]} if name == "day_posts" else {"post": post})
    if name == "chatter_replies":
        return json.dumps({"replies": [{"text": "привет", "translation": ""}] * 3})
    raise AssertionError(name)
lab.AI.transport = fake; lab.AI.url, lab.AI.secret = "https://relay.test/api/chat", "s3cret"
lab.AI_URL, lab.AI_SECRET, lab.AI_READY = lab.AI.url, lab.AI.secret, True
mid = J(c.get("/api/me", headers=LAB))["models"][0]["id"]
ST = J(c.get(f"/api/studio?mid={mid}", headers=LAB))
assert ST["tariff"]["plan"] == "trial" and ST["persona"]["archetype"] == "siren" and ST["plan"] is None
assert c2.get(f"/api/studio?mid={mid}", headers=LAB).status_code == 404                     # чужая модель
r = J(c.post("/api/studio/persona", json={"mid": mid, "character": "дерзкая", "voice": "ну привет", "archetype": "rebel", "lang": "xx"}, headers=LAB))
assert r["persona"]["archetype"] == "rebel" and r["persona"]["lang"] == "ru"
jid = J(c.post("/api/ai/plan", json={"mid": mid, "scenario": "поездка"}, headers=LAB))["job"]
for _ in range(50):
    jb = J(c.get(f"/api/ai/job/{jid}", headers=LAB))
    if jb["status"] != "run": break
    _t.sleep(0.05)
assert jb["status"] == "done", jb
PL = jb["plan"]; assert len(PL["days"]) == 7 and PL["days"][0]["posts"][0]["text"] == "вчера - дождь"     # длинное тире убрано
assert "draft" not in PL["days"][0]["posts"][0]                                                           # черновик не храним
sys_prompt = calls[0]["messages"][0]["content"]
assert "вымышленная ИИ-модель" in sys_prompt and "дерзкая" in sys_prompt and "поездка" in sys_prompt
assert "relay" not in json.dumps(calls[0]) and calls[0]["model"] == lab.AI_MODEL
r = J(c.post("/api/ai/post", json={"mid": mid, "day": 2, "post": 0}, headers=LAB)); assert r["ok"] and r["tariff"]["used"]["post"] == 1
r = J(c.post("/api/ai/post", json={"mid": mid, "day": 2, "idea": "про кофе", "light": True}, headers=LAB)); assert len(r["plan"]["days"][2]["posts"]) == 3
assert "про кофе" in calls[-1]["messages"][1]["content"]
r = J(c.post("/api/studio/plan", json={"mid": mid, "day": 2, "post": 2, "done": True}, headers=LAB)); assert r["plan"]["days"][2]["posts"][2]["done"]
# чаттер: собеседник, сообщение, 3 варианта; промпт честный
f = J(c.post("/api/fans", json={"mid": mid, "name": "Mike", "stage": 3}, headers=LAB))["fan"]
assert c.post("/api/ai/chatter", json={"fid": f["id"]}, headers=LAB).status_code == 400                  # нет его сообщения
c.patch(f"/api/fans/{f['id']}", json={"add": {"role": "fan", "text": "ты настоящая?"}}, headers=LAB)
r = J(c.post("/api/ai/chatter", json={"fid": f["id"]}, headers=LAB)); assert len(r["replies"]) == 3
sp = calls[-1]["messages"][0]["content"]; assert "ИИ-модель" in sp and "живая девушка" not in sp and "ЭТАП 3" in sp
assert c2.post("/api/ai/chatter", json={"fid": f["id"]}, headers=LAB).status_code == 404
# лимиты: тест — 2 плана в неделю, потом 402; Pro от куратора снимает
jid = J(c.post("/api/ai/plan", json={"mid": mid}, headers=LAB))["job"]
for _ in range(50):
    if J(c.get(f"/api/ai/job/{jid}", headers=LAB))["status"] != "run": break
    _t.sleep(0.05)
r = c.post("/api/ai/plan", json={"mid": mid}, headers=LAB); assert r.status_code == 402 and J(r)["limit"] == "plan", r.data
anna = [x for x in J(c.get("/api/admin/overview", headers={**LAB, **ADM}))["users"] if x["nick"] == "anna"][0]
assert anna["plan"] == "trial" and anna["ai_week"] >= 3
c.patch(f"/api/admin/users/{anna['id']}", json={"pro_days": 30}, headers={**LAB, **ADM})
assert J(c.get("/api/tariff", headers=LAB))["plan"] == "pro"
c.patch(f"/api/admin/users/{anna['id']}", json={"pro_off": True}, headers={**LAB, **ADM})
# после 14 дней теста — Базовый
q = sqlite3.connect(os.path.join(D, "lab.db")); q.execute("UPDATE kv SET v='2020-01-01T00:00:00+00:00' WHERE k='ai_launch'")
q.execute("UPDATE users SET created='2020-01-01T00:00:00+00:00' WHERE id=?", (anna["id"],)); q.commit()
assert J(c.get("/api/tariff", headers=LAB))["plan"] == "base"
# Tribute: подпись, подписка по Telegram ID, повтор не дублирует
lab.TRIBUTE_KEY = "trbkey"
q.execute("UPDATE users SET tg_chat_id=777 WHERE id=?", (anna["id"],)); q.commit()
body = json.dumps({"name": "new_subscription", "created_at": "x", "sent_at": "x", "payload": {"subscription_id": 5, "telegram_user_id": 777,
                   "expires_at": "2099-01-01T00:00:00Z", "period": "monthly", "price": 149000, "amount": 140000, "currency": "rub"}}).encode()
sig = _h.new(b"trbkey", body, _hl.sha256).hexdigest()
assert c.post("/api/tribute/webhook", data=body, headers={**LAB, "trbt-signature": "bad", "Content-Type": "application/json"}).status_code == 401
assert c.post("/api/tribute/webhook", data=body, headers={**LAB, "trbt-signature": sig, "Content-Type": "application/json"}).status_code == 200
assert J(c.post("/api/tribute/webhook", data=body, headers={**LAB, "trbt-signature": sig, "Content-Type": "application/json"}))["dup"]
T = J(c.get("/api/tariff", headers=LAB)); assert T["plan"] == "pro" and T["pro_until"].startswith("2099"), T
assert lab_ai.parse_products("12:30, 7:90,x:1") == {12: 30, 7: 90}
print("Студия: паспорт, план недели, чаттер, лимиты, Tribute: OK")

# ---------- оплата курса в Tribute → личная ссылка в кабинет ----------
import re as _re, lab_pay
lab.TRIBUTE_COURSE = lab_pay.parse_course("111:base, 222:adv,333:up,x:adv,444:zzz")
assert lab.TRIBUTE_COURSE == {111: "base", 222: "adv", 333: "up"}
lab.NOTIFIER = lab_notify.Notifier(lab.db, lab.TZ, lab.week_key, "TOKEN", "lab.neurofans.ru", http=fake_http)
lab._bg = lambda f, *a: f(*a)                                      # в тестах фон — сразу
qq = lab.db(); lab.kv_set(qq, "admin_chat", "9000"); qq.commit()
def trb(name, payload):
    b = json.dumps({"name": name, "payload": payload}).encode()
    return J(c.post("/api/tribute/webhook", data=b, headers={**LAB, "trbt-signature": _h.new(b"trbkey", b, _hl.sha256).hexdigest(),
                                                             "Content-Type": "application/json"}))
N2, cn2, now2 = lab.NOTIFIER, lab.db(), lab.now_utc()
bot = lambda chat, text: N2.handle_update(cn2, {"update_id": 1, "message": {"text": text, "chat": {"id": chat, "type": "private"}}}, now2)
sent.clear(); bot(4242, "/start buy")
assert "не вижу" in sent[-1]["text"]                               # нажал кнопку раньше, чем дошёл вебхук
sent.clear()
assert trb("new_digital_product", {"product_id": 111, "purchase_id": 9001, "telegram_user_id": 4242, "amount": 390000})["course"] == "paid"
assert trb("new_digital_product", {"product_id": 111, "purchase_id": 9001, "telegram_user_id": 4242})["dup"]
to_user = [x for x in sent if x["chat_id"] == 4242]; to_adm = [x for x in sent if x["chat_id"] == 9000]
assert to_user and "?invite=NF-" in to_user[0]["text"] and "Базовый" in to_user[0]["text"], sent
assert to_adm and "Базовый" in to_adm[0]["text"] and "не зарегистрировался" in to_adm[0]["text"], to_adm
code_b = _re.search(r"\?invite=(NF-\w+)", to_user[0]["text"]).group(1)
sent.clear(); bot(4242, "/start buy"); assert code_b in sent[-1]["text"]          # повторная кнопка — та же ссылка
assert "course" not in trb("new_digital_product", {"product_id": 999, "purchase_id": 9002, "telegram_user_id": 5555})
S = J(c.get("/api/admin/overview", headers={**LAB, **ADM}))["sales"]
assert S["ready"] and S["base"] == 1 and S["waiting"] == 1, S
# регистрация по личной ссылке: Telegram привязан, тариф курса стоит, ссылка одноразовая
nb = lab.app.test_client()
assert nb.post("/api/register", json={"invite": code_b, "nick": "buyer1", "password": "12345678"}, headers=LAB).status_code == 200
me = lambda cl: J(cl.get("/api/me", headers=LAB))
assert me(nb)["course"] == "base" and me(nb)["tg_linked"]
assert lab.app.test_client().post("/api/register", json={"invite": code_b, "nick": "buyer2", "password": "12345678"}, headers=LAB).status_code == 400
assert J(c.get("/api/admin/overview", headers={**LAB, **ADM}))["sales"]["waiting"] == 0
# доплата → Продвинутый; кнопка в боте уже не даёт новую ссылку
trb("new_digital_product", {"product_id": 333, "purchase_id": 9003, "telegram_user_id": 4242})
assert me(nb)["course"] == "adv"
sent.clear(); bot(4242, "/start buy")
assert "доступ открыт" in sent[-1]["text"] and "Продвинутый" in sent[-1]["text"] and "invite" not in sent[-1]["text"]
# возврат доплаты → снова Базовый; возврат без purchase_id — последняя живая покупка курса
assert trb("digital_product_refund", {"product_id": 333, "purchase_id": 9003, "telegram_user_id": 4242})["course"] == "refund"
assert me(nb)["course"] == "base"
trb("digital_product_refund", {"telegram_user_id": 4242}); assert me(nb)["course"] is None
# доплата без Базового Продвинутый не даёт; куратор может выдать курс вручную
trb("new_digital_product", {"product_id": 333, "purchase_id": 9004, "telegram_user_id": 4242}); assert me(nb)["course"] is None
uid_b = cn2.execute("SELECT id FROM users WHERE nick='buyer1'").fetchone()[0]
c.patch(f"/api/admin/users/{uid_b}", json={"course": "base"}, headers={**LAB, **ADM}); assert me(nb)["course"] == "adv"   # Базовый от куратора + доплата
c.patch(f"/api/admin/users/{uid_b}", json={"course": ""}, headers={**LAB, **ADM}); assert me(nb)["course"] is None
sent.clear(); bot(4242, "/start"); assert "доступ открыт" not in sent[-1]["text"]   # без живой оплаты — обычный ответ
# ученик уже был в кабинете без Telegram, оплатил → привязал Telegram кодом → тариф подтянулся сам
trb("new_digital_product", {"product_id": 222, "purchase_id": 9005, "telegram_user_id": 31337})
uid3 = cn2.execute("SELECT id FROM users WHERE nick=?", (me(c3)["nick"],)).fetchone()[0]
sent.clear(); bot(31337, "/start " + N2.new_link_code(cn2, uid3, now2))
assert me(c3)["course"] == "adv" and "Продвинутый" in sent[-1]["text"], sent
# id товара нет в .env: тариф узнаётся по названию товара и запоминается (возврат тоже работает)
assert lab_pay.kind_by_name("NEUROFANS Апгрейд: с Базового на Продвинутый") == "up"
assert lab_pay.kind_by_name("NEUROFANS Продвинутый: полный запуск") == "adv"
assert lab_pay.kind_by_name("NEUROFANS Базовый: 18+ фото ИИ-модели") == "base"
assert lab_pay.kind_by_name("Pro подписка") is None
assert trb("new_digital_product", {"product_id": 777, "product_name": "NEUROFANS Базовый: 18+ фото ИИ-модели",
                                   "purchase_id": 9010, "telegram_user_id": 6060})["course"] == "paid"
assert lab.course_map(lab.db())[777] == "base"
assert trb("new_digital_product", {"product_id": 777, "purchase_id": 9011, "telegram_user_id": 6061})["course"] == "paid"  # уже без названия
assert "course" not in trb("new_digital_product", {"product_id": 778, "product_name": "Стикеры", "purchase_id": 9012, "telegram_user_id": 6062})
assert trb("digital_product_refund", {"product_id": 777, "purchase_id": 9010, "telegram_user_id": 6060})["course"] == "refund"
assert trb("new_digital_product", {"product_id": 779, "product_name": "NEUROFANS Базовый тест", "purchase_id": 9020,
                                   "telegram_user_id": 6070})["course"] == "paid"           # тест: доступ даёт
assert lab.db().execute("SELECT test FROM purchases WHERE ref='9020'").fetchone()[0] == 1   # но в продажи не идёт
print("оплата курса → кабинет: OK")
# забота: план первой недели, просьба об отзыве, переписка с куратором
import lab_care
cc = lab.db(); t0 = lab.now_utc() - dt.timedelta(days=1, hours=3)              # зарегистрировался вчера
cc.execute("INSERT INTO users(nick,pw,created,invite,tg_chat_id,course) VALUES('care1','x',?,'',7171,'base')", (lab.iso(t0),))
cuid = cc.execute("SELECT id FROM users WHERE nick='care1'").fetchone()[0]
cc.execute("INSERT INTO purchases(ref,tg_id,product_id,kind,created) VALUES('c1',7171,111,'base',?)", (lab.iso(lab.now_utc() - dt.timedelta(days=14, hours=2)),))
cc.commit()
assert lab_care.scan(N2, cc, lab.now_utc()) == 2                                 # день 2 + отзыв
assert lab_care.scan(N2, cc, lab.now_utc()) == 0                                 # повторно — ничего
keys = {r[0] for r in cc.execute("SELECT key FROM notify WHERE uid=?", (cuid,))}
assert keys == {"care:w2", "care:review"}, keys
old = lab.iso(lab.now_utc() - dt.timedelta(days=40))                             # старый ученик — план не шлём
cc.execute("UPDATE users SET created=? WHERE id=?", (old, cuid)); cc.execute("DELETE FROM notify WHERE uid=?", (cuid,)); cc.commit()
assert lab_care.scan(N2, cc, lab.now_utc()) == 1                                 # только отзыв (по дате покупки)
cc.execute("UPDATE notify SET status='sent', sent_at=? WHERE uid=? AND key='care:review'", (lab.iso(), cuid)); cc.commit()
calls = []
def http2(method, payload, timeout=30):
    calls.append((method, payload)); return {"ok": True, "result": {}}
N3 = lab_notify.Notifier(lab.db, lab.TZ, lab.week_key, "TOKEN", "lab.neurofans.ru", http=http2)
N3.handle_update(cc, {"update_id": 50, "message": {"text": "Сделала первую модель, спасибо!", "chat": {"id": 7171, "type": "private"}}}, lab.now_utc())
rv = cc.execute("SELECT id, text, consent FROM reviews WHERE uid=?", (cuid,)).fetchone()
assert rv and "первую модель" in rv["text"] and rv["consent"] is None
assert any(m == "sendMessage" and p.get("chat_id") == 9000 and "#u%d" % cuid in p["text"] for m, p in calls), calls   # куратору
N3.handle_update(cc, {"update_id": 51, "callback_query": {"id": "q", "data": f"rv:{rv['id']}:1", "message": {"chat": {"id": 7171}}}}, lab.now_utc())
assert cc.execute("SELECT consent FROM reviews WHERE id=?", (rv["id"],)).fetchone()[0] == 1
calls.clear()                                                                    # куратор отвечает «ответом»
N3.handle_update(cc, {"update_id": 52, "message": {"text": "Супер, так держать!", "chat": {"id": 9000, "type": "private"},
                 "reply_to_message": {"text": f"⭐ Отзыв от care1 #u{cuid}"}}}, lab.now_utc())
assert any(p.get("chat_id") == 7171 and "Куратор" in p["text"] for m, p in calls), calls
assert [r["nick"] for r in lab_care.admin_list(cc)][0] == "care1"
print("забота: план недели, отзывы с согласием, связь с куратором: OK")
# реферал: ученик care1 приглашает друга, тот покупает → +30 дней Pro
rc = lab_pay.ref_code(cc, cuid, lab.SECRET); cc.commit()
assert rc == lab_pay.ref_code(cc, cuid, lab.SECRET) and len(rc) >= 6
def trbr(payload, code, sig=None):
    b = json.dumps({"name": "new_digital_product", "payload": payload}).encode()
    h = {**LAB, "trbt-signature": _h.new(b"trbkey", b, _hl.sha256).hexdigest(), "Content-Type": "application/json",
         "x-nf-ref": code + ":" + (sig or lab_pay.ref_sig("trbkey", b, code))}
    return J(c.post("/api/tribute/webhook", data=b, headers=h))
assert trbr({"product_id": 111, "purchase_id": 9101, "telegram_user_id": 8181}, rc, sig="bad")["course"] == "paid"
assert lab.db().execute("SELECT COUNT(*) FROM referrals").fetchone()[0] == 0          # поддельная подпись — не считаем
assert trbr({"product_id": 111, "purchase_id": 9102, "telegram_user_id": 8282}, rc)["course"] == "paid"
assert trbr({"product_id": 222, "purchase_id": 9103, "telegram_user_id": 8282}, rc)["course"] == "paid"   # тот же друг ещё раз — одно начисление
assert trbr({"product_id": 111, "purchase_id": 9104, "telegram_user_id": 7171}, rc)["course"] == "paid"   # сам себя — нет
cx = lab.db(); assert lab_pay.ref_stats(cx, cuid) == {"bought": 1, "pro_days": lab.REF_PRO_DAYS}
assert cx.execute("SELECT COUNT(*) FROM pro WHERE uid=? AND source='ref'", (cuid,)).fetchone()[0] == 1
b = json.dumps({"name": "digital_product_refund", "payload": {"product_id": 111, "purchase_id": 9102, "telegram_user_id": 8282}}).encode()
c.post("/api/tribute/webhook", data=b, headers={**LAB, "trbt-signature": _h.new(b"trbkey", b, _hl.sha256).hexdigest(), "Content-Type": "application/json"})
cx = lab.db(); assert lab_pay.ref_stats(cx, cuid)["bought"] == 0 and cx.execute("SELECT COUNT(*) FROM pro WHERE source='ref'").fetchone()[0] == 0
print("рефералы: Pro за покупку друга, подпись, возврат: OK")
# прямой OpenAI-совместимый API (OpenRouter): Bearer-ключ вместо секрета ретранслятора
import threading as _th
from http.server import BaseHTTPRequestHandler as _B, HTTPServer as _S
seen = {}
class _H(_B):
    def log_message(self, *a): pass
    def do_POST(self):
        seen["auth"] = self.headers.get("Authorization"); seen["sec"] = self.headers.get("x-relay-secret")
        seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        d = json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
_srv = _S(("127.0.0.1", 0), _H); _th.Thread(target=_srv.serve_forever, daemon=True).start()
lab_ai.API_KEY = "or-key"
assert lab_ai.call_relay(f"http://127.0.0.1:{_srv.server_port}/api/v1/chat/completions", "", {"messages": [1], "max_tokens": 9}) == '{"ok": true}'
assert seen["auth"] == "Bearer or-key" and seen["sec"] is None and seen["body"]["max_completion_tokens"] == 9 and "max_tokens" not in seen["body"]
lab_ai.API_KEY = ""; _srv.shutdown()
print("OpenRouter-режим: OK")

print("ВСЕ ТЕСТЫ КАБИНЕТА ПРОШЛИ")
