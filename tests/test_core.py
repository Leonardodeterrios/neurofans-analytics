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
  "MAX_PARTNERS_PER_VISITOR":"3","TRUST_CF":"1","SILENCE_HOURS":"24          # комментарий в .env","TEAM_A_COLOR":"#19d3c5"})
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
assert loc.startswith("https://t.me/AccessGrantedGuide_bot?start=leo07-") and "nf_vid" in r1.cookies
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
kate_cid=rk.headers["Location"].rsplit("-",1)[1]

# 3) старты бота из PuzzleBot
def start(p, uid, key="k123"):
    flask.request.values={"p":p,"uid":str(uid) if uid is not None else "","key":key}; flask.request.args={}
    try: return json.loads(app.bot_start().content)
    except flask._Abort as a: return {"http":a.code}
assert start("leo07-"+loc.rsplit("-",1)[1],111,key="wrong")=={"http":403}; print("старт без ключа: 403 ✅")
s1=start("leo07-"+loc.rsplit("-",1)[1],111); print("старт по клику leo07:",s1); assert s1["scored"] and s1["ch"]=="threads"
s2=start("leo07-"+loc.rsplit("-",1)[1],111); assert not s2["scored"]; print("тот же человек жмёт /start ещё раз: не засчитан ✅")
s3=start("kate03-"+kate_cid,222); assert s3["scored"]
s4=start("leo08",333); assert not s4["scored"]; print("«голый» старт по метке без клика: не засчитан ✅")
s5=start("",444); assert not s5["scored"]; print("старт без метки (direct): не засчитан ✅")
s6=start("kate03__zzzzzz",555); assert not s6["scored"]; print("чужой/битый id клика: не засчитан ✅")
s7=start("kate03-"+kate_cid,666); assert not s7["scored"]; print("второй аккаунт по тому же клику: не засчитан ✅")
r10=click(app.go_video,"leo10","10.0.0.1",cookie=vid); c10=r10.headers["Location"].rsplit("-",1)[1]
s8=start("leo10-"+c10,777); assert not s8["scored"]; print("старт по НЕзасчитанному клику (лимит): не засчитан ✅")

# старый формат «__» тоже понимаем
rl=click(app.go_threads,"kate11","10.2.2.2",ua="Mozilla/5.0 (iPhone) Instagram 301")
sl=start("kate11__"+rl.headers["Location"].rsplit("-",1)[1],888); assert sl["scored"] and sl["label"]=="kate11"
print("старый формат ссылок leo07__id: понимается ✅")
# метка с дефисом без id клика не ломается
lab,cid,whole=app.parse_payload("team-a"); assert whole=="team-a"
# подделка IP заголовками: без Cloudflare заголовки игнорируются
app.TRUST_CF=False
flask.request.headers=H({"CF-Connecting-IP":"6.6.6.6","X-Forwarded-For":"7.7.7.7"}); flask.request.remote_addr="1.2.3.4"
assert app._client_ip()=="1.2.3.4"; app.TRUST_CF=True
assert app._client_ip()=="6.6.6.6"; print("подделка IP через заголовки: не проходит без Cloudflare ✅")
app.TRUST_CF=False
flask.request.headers=H({"X-Real-IP":"8.8.8.8"}); flask.request.remote_addr="1.2.3.4"
assert app._client_ip()=="1.2.3.4"            # из интернета X-Real-IP игнорируется
flask.request.remote_addr="172.18.0.3"
assert app._client_ip()=="8.8.8.8"            # от нашего caddy — берём
flask.request.remote_addr="1.2.3.4"; app.TRUST_CF=True
print("X-Real-IP: верим только нашему caddy ✅")
# встроенные браузеры приложений — люди, превью и скрипты — нет
HUMANS=["Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 309.0.0.40.113 (iPhone14,5; iOS 17_0; ru_RU)",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Barcelona 307.0.0.30.109 (iPhone15,2; iOS 17_1)",
 "Mozilla/5.0 (Linux; Android 13; SM-A536B; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/119.0 Mobile Safari/537.36 Instagram 307.0.0.34.111 Android",
 "Mozilla/5.0 (Linux; Android 12; 2201117TY; wv) AppleWebKit/537.36 Chrome/112.0 Mobile Safari/537.36 trill_2023109030 AppName/trill BytedanceWebview/d8a21c6",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 [FBAN/FBIOS;FBDV/iPhone13,2;FBSV/16.6]",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 YouTube/19.20.2",
 "Mozilla/5.0 (Linux; Android 10; CUBOT X30) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"]
BOTS=["facebookexternalhit/1.1","meta-externalagent/1.1","Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
 "TelegramBot (like TwitterBot)","Twitterbot/1.0","Slackbot-LinkExpanding 1.0","WhatsApp/2.23.20","Mozilla/5.0 (compatible; YandexBot/3.0)",
 "Discordbot/2.0","python-requests/2.31","curl/8.4.0","Mozilla/5.0 HeadlessChrome/120.0"]
assert not [u for u in HUMANS if app.BOT_UA.search(u)], [u for u in HUMANS if app.BOT_UA.search(u)]
assert all(app.BOT_UA.search(u) for u in BOTS), [u for u in BOTS if not app.BOT_UA.search(u)]
print("фильтр ботов: 7 встроенных браузеров = люди, 12 ботов/превью = отсечены ✅")

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
# 7) раунды, ранги, ачивки
st=app.get_stats(); P={p["source"]:p for p in st["partners"]}
assert st["round_no"]>=1 and "prev_round" in st and st["badge_defs"]["first_blood"]["name"]
assert P["leo07"]["rank_name"]=="Новичок" and "first_blood" in P["leo07"]["badges"], P["leo07"]
print("раунд",st["round_no"],"| ачивки leo07:",P["leo07"]["badges"],"| ранг:",P["leo07"]["rank_name"])
# кэш статистики сбрасывается кликом
n0=app.get_stats()["hits"]; click(app.go_threads,"kate20","44.4.4.4"); assert app.get_stats()["hits"]==n0+1
# 8) гео: город пишется в клик и уходит в ленту
class FakeGeo:
    def get(self, ip): return {"city":{"names":{"ru":"Москва"}},"country":{"iso_code":"RU"},"location":{"latitude":55.7558,"longitude":37.6173}}
app._geo["r"]=FakeGeo(); app.GEO_DB=__file__
g=app.geo_lookup("5.5.5.5"); assert g=={"city":"Москва","cc":"RU","lat":55.8,"lon":37.6},g
flask.request.args={"token":"pub"}; base=json.loads(app.api_feed().content)
click(app.go_video,"leo21","77.7.7.7")
flask.request.args={"token":"pub","since_c":str(base["last_c"]),"since_s":str(base["last_s"])}
it=json.loads(app.api_feed().content)["items"]; assert it and it[-1]["city"]=="Москва" and it[-1]["lat"]==55.8, it
app._geo["r"]=None; app.GEO_DB="/nonexistent"
print("гео: город в ленте ✅", it[-1]["city"])
# 9) живая лента SSE
flask.request.args={"token":"pub","since_c":str(base["last_c"]),"since_s":str(base["last_s"])}
gen=app.api_stream().content; first=next(gen); assert "hello" in first and "id: " in first
ev=next(gen); assert '"items"' in ev and "leo21" in ev, ev
print("SSE: событие пришло ✅")
# 10) личная страница партнёра
k=app.partner_key("leo07")
flask.request.args={"k":k}; pp=json.loads(app.api_partner("leo07").content)
assert pp["me"]["source"]=="leo07" and pp["place"]>=1 and len(pp["days"])==14 and pp["role"]=="partner"
flask.request.args={"k":"bad"}
try: app.api_partner("leo07"); raise SystemExit("должно быть 401")
except flask._Abort as a: assert a.code==401
flask.request.args={"k":app.partner_key("leo40")}; p40=json.loads(app.api_partner("leo40").content)
assert p40["me"]["power"]==0 and p40["place"]==p40["of"]
print("личная страница: место",pp["place"],"из",pp["of"],"| чужой ключ = 401 ✅")
# 11) карточки
try:
    import PIL
    png=app.round_card_png("current","leo07"); assert png[:4]==b"\x89PNG"; open("/tmp/story_test.png","wb").write(png)
    import cards; ogp=cards.og(app.get_stats(), os.path.join(os.path.dirname(HERE),"static","fonts"), "leo07"); open("/tmp/og_test.png","wb").write(ogp)
    print("карточки: story", len(png)//1024, "КБ, og", len(ogp)//1024, "КБ ✅")
except ImportError:
    print("Pillow нет — карточки пропущены")
# 12) действия в боте
def act(uid,a,key="k123"):
    flask.request.values={"uid":str(uid),"act":a,"key":key}
    return json.loads(app.bot_action().content)
flask.request.values={"uid":"1","act":"site","key":"bad"}
try: app.bot_action(); raise SystemExit("без ключа должно быть 403")
except flask._Abort as e: assert e.code==403
assert act(999999,"site")["reason"]=="uid_not_attributed"          # человек без засчитанного /start — тихо игнорируем
assert act(555,"nonsense")["reason"]=="unknown_act"
u7=[r for r in sqlite3.connect(DB).execute("SELECT tg_uid,label FROM starts WHERE scored=1 AND label='leo07'")][0][0]
p0=next(p for p in app.get_stats()["partners"] if p["source"]=="leo07")["power"]
flask.request.args={"token":"pub"}; base=json.loads(app.api_feed().content)
r1=act(u7,"site"); r2=act(u7,"site"); r3=act(u7,"examples")
assert r1["ok"] and r1["points"]==8 and r1["label"]=="leo07" and r2["reason"]=="duplicate" and r3["points"]==8, (r1,r2,r3)
st=app.get_stats(); P7=next(p for p in st["partners"] if p["source"]=="leo07")
assert P7["power"]==p0+16 and P7["act_points"]==16 and P7["act_points_today"]==16, P7
assert "heavy" in P7["badges"] and P7["heavy"]==2 and P7["heavy_today"]==2, P7   # два действия по +8 → «Тяжёлая артиллерия»
assert all(k in st["badge_defs"] for k in ("combo","heavy","globe","funnel","rush","marathon","night")) and "hot" in P7 and "cities_today" in P7
flask.request.args={"token":"pub","since_c":str(base["last_c"]),"since_s":str(base["last_s"]),"since_a":str(base["last_a"])}
fa=json.loads(app.api_feed().content); kinds=[(i["kind"],i.get("points")) for i in fa["items"]]
assert ("site",8) in kinds and ("examples",8) in kinds, kinds
flask.request.args={"token":"pub","since_c":str(base["last_c"]),"since_s":str(base["last_s"]),"since_a":str(base["last_a"])}
gen=app.api_stream().content; next(gen); ev=next(gen); assert '"site"' in ev and '"points": 8' in ev, ev
print("действия в боте: +8 сайт, +8 примеры, повтор игнор, чужой uid игнор, в ленте и SSE ✅")
print("\n"+app.daily_results_text())
print("\n"+app.stats_text())
print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✅")
