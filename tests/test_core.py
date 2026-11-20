"""Тесты ядра без установки зависимостей (flask/httpx/dotenv заменены заглушками).
Запуск: python3 tests/test_core.py"""
import sys, os, json, sqlite3, tempfile
HERE=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,os.path.join(HERE,"stubs"))
DB=os.path.join(tempfile.mkdtemp(),"test.db")
if os.path.exists(DB): os.remove(DB)
# старая схема базы (как на сервере сейчас) — проверим миграцию
c=sqlite3.connect(DB); c.execute("CREATE TABLE clicks(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, ip TEXT, ua TEXT)")
c.execute("INSERT INTO clicks(ts,source,ip,ua) VALUES('2026-11-20T05:00:00','test','9.9.9.9','Mozilla')"); c.commit(); c.close()
os.environ.update({"DB_PATH":DB,"BOT_TOKEN":"","ADMIN_CHAT_ID":"","DASHBOARD_TOKEN":"adm","PUBLIC_TOKEN":"pub",
  "INGEST_TOKEN":"k123","DAILY_SUMMARY_TIME":"21:00","SUMMARY_TZ":"Asia/Bangkok","START_POWER":"3",
  "MAX_PARTNERS_PER_VISITOR":"3","SILENCE_HOURS":"24          # комментарий в .env","TEAM_A_COLOR":"#19d3c5"})
sys.path.insert(0,os.path.dirname(HERE))
import app, flask
H=flask.__dict__['_Headers']
assert app.SILENCE_HOURS==24 and app.TEAM_A_COLOR=="#19d3c5", (app.SILENCE_HOURS, app.TEAM_A_COLOR)
app.init_db()
cols={r[1] for r in sqlite3.connect(DB).execute("PRAGMA table_info(clicks)")}
assert {"vid","scored","rnd","ch"}<=cols; print("миграция старой базы: OK, колонки:",sorted(cols))

def click(fn, label, ip, ua="Mozilla/5.0 (iPhone) Instagram 300", cookie=None):
    flask.request.args={}; flask.request.values={}
    flask.request.headers=H({"User-Agent":ua,"CF-Connecting-IP":ip,"X-Forwarded-Proto":"https"})
    flask.request.cookies={"nf_vid":cookie} if cookie else {}
    return fn(label)

# 1) один человек = один удар на партнёра за раунд
r1=click(app.go_threads,"leo07","10.0.0.1"); loc=r1.headers["Location"]; print("redirect:",loc)
assert loc.startswith("https://t.me/AccessGrantedGuide_bot?start=leo07__") and "nf_vid" in r1.cookies
vid=r1.cookies["nf_vid"][0]; print("cookie nf_vid ставится, secure =",r1.cookies["nf_vid"][1]["secure"])
click(app.go_threads,"leo07","10.0.0.1",cookie=vid)            # повтор тем же человеком
click(app.go_threads,"leo07","10.0.0.77",cookie=vid)           # тот же человек сменил IP (cookie та же)
click(app.go_video,"leo08","10.0.0.1",cookie=vid)
click(app.go_video,"leo09","10.0.0.1",cookie=vid)
click(app.go_video,"leo10","10.0.0.1",cookie=vid)              # 4-й партнёр от одного человека — уже не в силу
rows=sqlite3.connect(DB).execute("SELECT source,scored FROM clicks WHERE vid=? ORDER BY id",(vid,)).fetchall()
print("клики одного человека (метка, засчитан):",rows)
assert rows==[('leo07',1),('leo07',0),('leo07',0),('leo08',1),('leo09',1),('leo10',0)]

# 2) боты и превью не пишутся, но ведут туда же
before=sqlite3.connect(DB).execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
for ua in ["facebookexternalhit/1.1","TelegramBot (like TwitterBot)","python-requests/2.31","curl/8.0","Mozilla/5.0 HeadlessChrome/120",""]:
    r=click(app.go_threads,"kate03","5.5.5.5",ua=ua); assert r.headers["Location"]=="https://t.me/AccessGrantedGuide_bot?start=kate03",r.headers
after=sqlite3.connect(DB).execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
assert before==after; print("боты/превью/скрипты: не записаны, редирект тот же ✅")

# второй живой человек по kate03 (другой браузер)
rk=click(app.go_threads,"kate03","10.1.1.1",ua="Mozilla/5.0 (Android) Barcelona 300")
kate_cid=rk.headers["Location"].split("__")[1]

# 3) старты бота из PuzzleBot
def start(p, uid, key="k123"):
    flask.request.values={"p":p,"uid":str(uid) if uid is not None else "","key":key}; flask.request.args={}
    try: return json.loads(app.bot_start().content)
    except flask._Abort as a: return {"http":a.code}
assert start("leo07__"+loc.split("__")[1],111,key="wrong")=={"http":403}; print("старт без ключа: 403 ✅")
s1=start("leo07__"+loc.split("__")[1],111); print("старт по клику leo07:",s1); assert s1["scored"] and s1["ch"]=="threads"
s2=start("leo07__"+loc.split("__")[1],111); assert not s2["scored"]; print("тот же человек жмёт /start ещё раз: не засчитан ✅")
s3=start("kate03__"+kate_cid,222); assert s3["scored"]
s4=start("leo08",333); assert not s4["scored"]; print("«голый» старт по метке без клика: не засчитан ✅")
s5=start("",444); assert not s5["scored"]; print("старт без метки (direct): не засчитан ✅")
s6=start("kate03__zzzzzz",555); assert not s6["scored"]; print("чужой/битый id клика: не засчитан ✅")
s7=start("kate03__"+kate_cid,666); assert not s7["scored"]; print("второй аккаунт по тому же клику: не засчитан ✅")
r10=click(app.go_video,"leo10","10.0.0.1",cookie=vid); c10=r10.headers["Location"].split("__")[1]
s8=start("leo10__"+c10,777); assert not s8["scored"]; print("старт по НЕзасчитанному клику (лимит): не засчитан ✅")

# 4) статистика
st=app.get_stats(); P={p["source"]:p for p in st["partners"]}
print("leo07:",{k:P['leo07'][k] for k in ('hits','starts','power','conv','today')})
assert P["leo07"]["hits"]==1 and P["leo07"]["starts"]==1 and P["leo07"]["power"]==4 and P["leo07"]["conv"]==100.0
assert all((p["conv"] is None or p["conv"]<=100) for p in st["partners"]) and "direct" not in P
assert P["leo10"]["hits"]==0 and P["leo10"]["clicks"]==2
A,B=st["teams"]; print("команды:",A["name"],A["clicks"],"/",B["name"],B["clicks"],"| доходимость",A["conv"],B["conv"])
print("каналы:",[(c["name"],c["clicks"],c["hits"],c["starts"],c["conv"]) for c in st["channels"]])
print("итого: кликов",st["total"],"ударов",st["hits"],"в бот",st["starts"],"доходимость",st["conv"])

# 5) лента арены
flask.request.args={"token":"pub","since_c":"0","since_s":"0"}
f=json.loads(app.api_feed().content); print("лента:",[(i["kind"],i["source"],i["team"]) for i in f["items"]])
assert all(i["kind"] in ("hit","start") for i in f["items"]) and any(i["kind"]=="start" for i in f["items"])
flask.request.args={"token":"pub"}; f0=json.loads(app.api_feed().content); assert f0["items"]==[] and f0["last_s"]>0

# 6) доступы
def code(fn,args):
    flask.request.args=args
    try: fn(); return 200
    except flask._Abort as a: return a.code
assert code(app.export_starts,{"token":"pub"})==401 and code(app.export_starts,{"token":"adm"})==200
print("\n"+app.daily_results_text())
print("\n"+app.stats_text())
print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✅")
