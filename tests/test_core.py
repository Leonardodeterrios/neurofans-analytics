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

# 9) сканеры: «компьютер» и IP дата-центров — редирект тот же, но не удар; пересчёт истории
def scored_of(r):
    cid=int(r.headers["Location"].rsplit("-",1)[1],36)
    return sqlite3.connect(DB).execute("SELECT scored, why FROM clicks WHERE id=?",(cid,)).fetchone()
DESK="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
rd=click(app.go_threads,"leo30","31.1.1.1",ua=DESK)
assert rd.headers["Location"].startswith("https://t.me/AccessGrantedGuide_bot?start=leo30-")
assert scored_of(rd)==(0,"desktop"), scored_of(rd)
class FakeASN:
    def get(self, ip): return {"autonomous_system_organization": {"165.154.1.1":"UCloud Information Technology (HK) Limited",
        "146.190.1.1":"DigitalOcean, LLC","17.1.1.1":"Akamai Technologies, Inc."}.get(ip,"PJSC MegaFon")}
app._asn["r"]=FakeASN(); app.ASN_DB=__file__
MOB="Mozilla/5.0 (Linux; Android 16; SM-S921U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36"
assert scored_of(click(app.go_threads,"leo31","165.154.1.1",ua=MOB))==(0,"hosting")
assert scored_of(click(app.go_threads,"leo31","146.190.1.1",ua=MOB))==(0,"hosting")
assert scored_of(click(app.go_threads,"leo31","17.1.1.1",ua=MOB))==(1,None)     # iCloud Private Relay — человек
assert scored_of(click(app.go_threads,"leo32","83.1.1.1",ua=MOB))==(1,None)     # обычный мобильный оператор
print("сканеры: компьютер → desktop, дата-центр → hosting, Private Relay и оператор → удар ✅")
# пересчёт: подделаем старые «засчитанные» клики сканеров и проверим, что пересчёт их снимает, а людей оставляет
con=sqlite3.connect(DB); con.execute("UPDATE clicks SET scored=1, why=NULL WHERE source IN ('leo30','leo31')"); con.commit()
h_before=app.get_stats()["hits"]; res=app.rescore_all(); h_after=app.get_stats()["hits"]
print("пересчёт:",res,"| удары",h_before,"→",h_after)
assert h_after==h_before-4, (h_before,h_after)   # 3 сканера + старая запись с UA «Mozilla» из миграции
assert app.rescore_all()["clicks_changed"]==0          # повторный пересчёт ничего не меняет
P={p["source"]:p for p in app.get_stats()["partners"]}
assert P["leo07"]["hits"]==1 and P["leo07"]["starts"]==1, P["leo07"]   # люди и апперкоты на месте
flask.request.args={"token":"adm"}; assert json.loads(app.admin_rescore().content)["clicks_changed"]==0
flask.request.args={"token":"pub"}
try: app.admin_rescore(); assert False
except flask._Abort as a: assert a.code==401
app._asn["r"]=None; app.ASN_DB="/nonexistent"
print("пересчёт истории: сканеры сняты, люди и апперкоты на месте, /admin/rescore только админу ✅")

# 10) поддельные «айфоны» и лимит на подсеть
REAL=["Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/124.0.6367.88 Mobile/15E148 Safari/604.1"]+HUMANS
FAKE=["Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 26_3_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/144.0.7559.95 Mobile/15E148 Safari/604.1",
 "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"]
assert not [u for u in REAL if app.forged_ua(u)], [u for u in REAL if app.forged_ua(u)]
assert all(app.forged_ua(u) for u in FAKE), [u for u in FAKE if not app.forged_ua(u)]
assert scored_of(click(app.go_threads,"leo33","84.1.1.1",ua=FAKE[0]))==(0,"forged")
assert app.ip_net("172.111.15.17")==app.ip_net("172.111.15.200")!=app.ip_net("172.111.150.1")
assert app.ip_net("2a00:1:2:3::5")==app.ip_net("2a00:1:2:ffff::1")
app.MAX_HITS_PER_NET=3
got=[scored_of(click(app.go_threads,"leo34",f"172.111.15.{i}",ua=MOB))[0] for i in range(1,6)]
assert got==[1,1,1,0,0], got
assert scored_of(click(app.go_threads,"leo34","172.111.150.9",ua=MOB))[0]==1   # соседняя подсеть — отдельно
con=sqlite3.connect(DB); before=con.execute("SELECT id,scored FROM clicks WHERE source='leo34' ORDER BY id").fetchall()
app.rescore_all()                                 # пересчёт с тем же лимитом — по leo34 то же, что живой подсчёт
assert sqlite3.connect(DB).execute("SELECT id,scored FROM clicks WHERE source='leo34' ORDER BY id").fetchall()==before
app.MAX_HITS_PER_NET=10
# апперкот за клик с компьютера, если человек дошёл до бота; за хостинг/подделку — нет
rdk=click(app.go_threads,"leo35","85.1.1.1",ua=DESK); assert scored_of(rdk)==(0,"desktop")
sd=start("leo35-"+rdk.headers["Location"].rsplit("-",1)[1],9001); assert sd["scored"], sd
rfk=click(app.go_threads,"leo36","86.1.1.1",ua=FAKE[0]); sf=start("leo36-"+rfk.headers["Location"].rsplit("-",1)[1],9002); assert not sf["scored"]
P=_P=None
st=app.get_stats(); P={p["source"]:p for p in st["partners"]}
assert P["leo35"]["hits"]==0 and P["leo35"]["starts"]==1 and P["leo35"]["power"]==3, P["leo35"]
assert app.rescore_all()["starts_changed"]==0   # пересчёт сохраняет это правило
assert app._conv(5,2)==100.0
print("клик с компьютера → не удар, но апперкот, если дошёл до бота; подделка — ничего ✅")
print("поддельные айфоны → forged, настоящие (вкл. iOS 26 с OS 18_6) — люди; лимит на подсеть ✅")
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

# 11) кнопки-ссылки бота через переходник /a/<код>: подписанный initData мини-приложения или ?u=
import hmac as _h, hashlib as _hl, time as _t, urllib.parse as _up
app.BOT_TOKEN="123456:TEST-token"
def init_for(uid, token=app.BOT_TOKEN, age=0):
    d={"auth_date":str(int(_t.time())-age),"query_id":"AAE","user":json.dumps({"id":uid,"first_name":"Тест"},ensure_ascii=False,separators=(",",":"))}
    chk="\n".join(f"{k}={v}" for k,v in sorted(d.items()))
    sec=_h.new(b"WebAppData",token.encode(),_hl.sha256).digest()
    d["hash"]=_h.new(sec,chk.encode(),_hl.sha256).hexdigest()
    return _up.urlencode(d)
assert app.tg_webapp_uid(init_for(111))==111
assert app.tg_webapp_uid(init_for(111,token="999:other"))==0                    # чужой токен
assert app.tg_webapp_uid(init_for(111).replace("111","222"))==0                 # подменили id
assert app.tg_webapp_uid(init_for(111,age=3*86400))==0                         # старая подпись
assert app.tg_webapp_uid("garbage")==0 and app.tg_webapp_uid("")==0
def act(act_code, init="", u="", method="POST"):
    flask.request.method=method; flask.request.values={"init":init,"u":u}; flask.request.args={}
    try: return app.act_link(act_code)
    except flask._Abort as a: return a.code
    finally: flask.request.method="GET"
pg=act("examples",method="GET"); assert "gallery.lukaflow.shop" in pg.content and pg.headers["Cache-Control"]=="no-store"
assert act("evil",method="GET")==404                                            # неизвестная кнопка — 404, не редирект
before=app.get_stats()["action_points"]
r=json.loads(act("examples",init=init_for(888)).content); assert r["ok"], r    # uid 888 — засчитанный старт kate11
r2=json.loads(act("examples",init=init_for(888)).content); assert not r2["ok"] # второй раз то же действие — нет
r3=json.loads(act("guide",u="888").content); assert r3["ok"]                    # кнопка «в браузере» — по ?u=
r4=json.loads(act("site",u="555000").content); assert not r4["ok"]             # чужой uid без старта — тихо нет
r5=json.loads(act("site",init=init_for(888,token="999:x"),u="").content); assert not r5["ok"]  # поддельная подпись
after=app.get_stats()["action_points"]; assert after==before+8+3, (before,after)
print("переходник кнопок /a/: подпись Telegram проверяется, подделки и чужие id не проходят, редиректа на чужие адреса нет ✅")

# 12) уведомления в Telegram не задерживают редирект (раньше человек ждал ~30 с: сервер в РФ, Telegram тормозит)
import time as _tm
sent=[]
def slow_send(text, cid): _tm.sleep(1.0); sent.append(cid)
_old=(app.tg_send, app.ADMIN_IDS, app.NOTIFY_CLICKS)
app.tg_send=slow_send; app.ADMIN_IDS=[1,2,3]; app.NOTIFY_CLICKS=True
t0=_tm.time(); rr=click(app.go_threads,"leo40","87.1.1.1"); dt_=_tm.time()-t0
assert rr.headers["Location"].startswith("https://t.me/") and dt_<0.5, dt_
for _ in range(50):
    if len(sent)==3: break
    _tm.sleep(0.1)
assert sent==[1,2,3], sent                                     # уведомления всё равно ушли — в фоне
app.tg_send, app.ADMIN_IDS, app.NOTIFY_CLICKS=_old
print(f"редирект при медленном Telegram: {dt_*1000:.0f} мс, уведомления ушли в фоне ✅")

# 13) кнопки ближе к покупке: программа/хочу/оплата — свои очки и адреса
assert app.ACTIONS["program"][0]==10 and app.ACTIONS["intent"][0]==12 and app.ACTIONS["checkout"][0]==20
for k in ("program","intent","checkout"):
    pg=act(k,method="GET"); assert app.ACT_LINKS[k] in pg.content, k
b=app.get_stats()["action_points"]
assert json.loads(act("checkout",init=init_for(888)).content)["ok"]
assert json.loads(act("intent",u="888").content)["ok"]
assert not json.loads(act("checkout",u="888").content)["ok"]          # оплата — один раз на человека
assert app.get_stats()["action_points"]==b+20+12
assert app.ACTIONS["checkout_base"][0]==15 and "hLUy" in app.ACT_LINKS["checkout_base"] and "hLUB" in app.ACT_LINKS["checkout"]
assert "hLUy" in act("checkout_base",method="GET").content
print("новые кнопки: программа +10, хочу +12, оплата +20 — один раз на человека ✅")

# 14) настоящая оплата клуба: вебхук Tribute → +1000 партнёру, кнопкой не получить, возврат снимает
import base64 as _b64
app.TRIBUTE_KEY="trbkey"; app.TRIBUTE_FORWARD=""
def trib(ev, key="trbkey"):
    raw=json.dumps(ev).encode(); flask.request.data=raw
    flask.request.headers=flask._Headers({"trbt-signature":_h.new(key.encode(),raw,_hl.sha256).hexdigest()})
    try: return json.loads(app.tribute_webhook().content)
    except flask._Abort as a: return a.code
assert act("paid",method="GET")==404                                            # переходником 1000 не взять
assert trib({"name":"new_subscription","payload":{"telegram_user_id":888}},key="bad")==401   # чужая подпись
b=app.get_stats()["action_points"]
r=trib({"name":"new_subscription","payload":{"telegram_user_id":888,"subscription_id":5}}); assert r["ok"] and r["points"]==1000, r
assert not trib({"name":"new_digital_product","payload":{"telegram_user_id":888,"product_id":1}})["ok"]   # второй раз — нет
assert trib({"name":"renewed_subscription","payload":{"telegram_user_id":888}}).get("skip")               # продление — не оплата клуба
assert trib({"name":"new_subscription","payload":{"telegram_user_id":424242}})["reason"]=="uid_not_attributed"
assert app.get_stats()["action_points"]==b+1000
app.TRIBUTE_CLUB_IDS={"77"}
assert not app.tribute_event({"name":"new_digital_product","payload":{"telegram_user_id":111,"product_id":9}})   # не тот товар
assert app.tribute_event({"name":"new_digital_product","payload":{"telegram_user_id":111,"product_id":77}})=={"paid":111}
app.TRIBUTE_CLUB_IDS=set()
assert trib({"name":"refunded_payment","payload":{"telegram_user_id":888}})["refunded"]==1
assert app.get_stats()["action_points"]==b
assert app.tribute_sig_ok(b"x", _b64.b64encode(_h.new(b"trbkey",b"x",_hl.sha256).digest()).decode())  # base64-подпись тоже
flask.request.headers=flask._Headers()
print("оплата клуба: +1000 по вебхуку Tribute, один раз, возврат снимает ✅")
F=app.funnel(0,"ch"); allr=F["rows"][0]
assert allr["group"]=="Все" and allr["steps"]["start"]>0 and allr["steps"]["examples"]<=allr["steps"]["start"]
assert allr["steps"]["pay_open"]>=1 and "clicks" in allr
assert app.funnel(0,"label")["rows"][0]["group"]=="Все" and app.funnel(1,"team")["rows"]
print(f"воронка бота: стартов {allr['steps']['start']}, открыли оплату {allr['steps']['pay_open']} ✅")
import datetime as dt
# 15) автоматика бота: открыл оплату → догон через 2 ч (один раз, не ночью, не купившим); оплата → метка; вопросы → ИИ
calls=[]; app.pb_call=lambda m,**p: calls.append((m,p)) or {"code":0}
app.PB_TOKEN="tok"; app._quiet_now=lambda: False
_bgsave=app.bg; app.bg=lambda f,*a: f(*a)
with app._conn() as c: c.execute("INSERT INTO starts(ts,label,click_id,tg_uid,ch,scored) VALUES(?,?,?,?,?,?)",(app._now_iso(),"leo01",None,7001,"t",0))
assert app.note_pay_open(7001,"checkout_base") and not app.note_pay_open(424242,"checkout")   # чужой uid не пишем
assert app.run_nudges()==0                                         # рано
with app._conn() as c: c.execute("UPDATE bot_people SET pay_open_ts=? WHERE tg_uid=7001",((dt.datetime.utcnow()-dt.timedelta(hours=3)).isoformat(timespec="seconds"),))
assert app.run_nudges()==1 and calls[-1]==("sendCommand",{"command_name":app.PB_CMD_NUDGE,"platform_chat_id":7001})
assert app.run_nudges()==0                                         # второй раз не догоняем
with app._conn() as c: c.execute("INSERT INTO starts(ts,label,click_id,tg_uid,ch,scored) VALUES(?,?,?,?,?,?)",(app._now_iso(),"kate02",None,7002,"v",0))
app.note_pay_open(7002,"checkout"); app.note_paid(7002, True)
assert ("variableChange",{"variable":"nf_paid","expression":"1","user_id":7002}) in calls and calls[-1][0]=="sendCommand" and calls[-1][1]["command_name"]==app.PB_CMD_PAID
with app._conn() as c: c.execute("UPDATE bot_people SET pay_open_ts=? WHERE tg_uid=7002",((dt.datetime.utcnow()-dt.timedelta(hours=3)).isoformat(timespec="seconds"),))
assert app.run_nudges()==0                                         # купившего не догоняем
app.note_paid(7002, False); assert calls[-1]==("variableChange",{"variable":"nf_paid","expression":"0","user_id":7002})
sentb=[]; app.bot_send=lambda u,t: sentb.append((u,t)); app._ai_answer=lambda q: "Ответ — про тарифы"; notes=[]; _na=app.notify_all; app.notify_all=lambda t: notes.append(t)
app.handle_ask(7001,"сколько стоит?"); assert sentb[-1][0]==7001 and "ИИ-помощник" in sentb[-1][1] and "«Катя»" in sentb[-1][1]
app.handle_ask(7001,"а видео есть?"); assert "ИИ-помощник" not in sentb[-1][1]          # представляется один раз в сутки
app.handle_ask(7001,"Катя"); assert notes and "7001" in notes[-1] and "видео есть" in notes[-1] and "Кате" in sentb[-1][1]
app.bg=_bgsave; app.notify_all=_na; app.PB_TOKEN=""
print("автоматика бота: догон брошенной оплаты, метка «оплатил», ИИ-ассистент и «Катя» ✅")
print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✅")
