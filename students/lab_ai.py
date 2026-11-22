"""Студия NeuroFans Lab: ИИ-инструменты для продюсера ИИ-модели.

- паспорт модели (характер, голос, внешность) — один на модель, его используют все инструменты;
- план недели: сюжет + посты на каждый день под каналы, где у модели есть ссылки кабинета;
- чаттер: 3 варианта ответа фанату голосом модели по этапам воронки;
- тарифы и лимиты: Тест (14 дней) → Базовый (бесплатно) / Pro (оплата через Tribute).

ИИ вызывается через ретранслятор на Vercel (ai-relay/api/chat.js): сервер кабинета в РФ,
а OpenAI оттуда не отвечает. Промпты перенесены из ai-tma и поправлены под честность:
модель — вымышленный ИИ-персонаж, и на прямой вопрос «ты ИИ?» в чаттере она не врёт.
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse

# ---------- тарифы ----------
TRIAL_DAYS = 14
PLANS = {
    #          план недели (раз в нед.)   переписать/добавить пост (в день)   ответы чаттера (в день)
    "trial": {"name": "Тест", "plan": 2, "post": 15, "chat": 30},
    "base": {"name": "Базовый", "plan": 1, "post": 5, "chat": 10},
    "pro": {"name": "Pro", "plan": 7, "post": 60, "chat": 200},
}
KIND_WINDOW = {"plan": "week", "post": "day", "chat": "day"}
KIND_LABEL = {"plan": "планы недели", "post": "посты", "chat": "ответы чаттера"}


class AIError(Exception):
    def __init__(self, msg, code=502):
        super().__init__(msg)
        self.code = code


# ---------- вызов ретранслятора ----------
RESOLVE = []          # LAB_AI_RESOLVE: IP-адреса ретранслятора через запятую (обход блокировки DNS-адресов Vercel)
CAFILE = None        # только для тестов


def _post_via_ip(url, ip, body, headers, timeout):
    """HTTPS-запрос на конкретный IP, но с настоящим именем хоста (SNI, сертификат, Host) —
    как curl --resolve. Нужен, когда основной адрес Vercel недоступен из сети сервера."""
    import socket, ssl, http.client
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or 443
    ctx = ssl.create_default_context(cafile=CAFILE)
    sock = socket.create_connection((ip, port), timeout=min(8, timeout))
    sock.settimeout(timeout)
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    conn.sock = ctx.wrap_socket(sock, server_hostname=host)
    conn.request("POST", (u.path or "/") + (("?" + u.query) if u.query else ""), body=body, headers=headers)
    r = conn.getresponse()
    return r.status, r.read().decode() or "{}"


API_KEY = ""         # LAB_AI_KEY: если задан — LAB_AI_URL это OpenAI-совместимый API (напр. OpenRouter), без ретранслятора


def call_relay(url, secret, payload, timeout=58):
    """POST в ретранслятор (или прямо в OpenAI-совместимый API при LAB_AI_KEY). Возвращает текст ответа модели."""
    if not url or not (secret or API_KEY):
        raise AIError("ИИ ещё не подключён: куратор не задал LAB_AI_URL и ключ.", 503)
    if API_KEY:
        payload = dict(payload)
        payload["max_completion_tokens"] = payload.pop("max_tokens", 2000)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}",
                   "HTTP-Referer": "https://lab.neurofans.ru", "X-Title": "NeuroFans Lab"}
    else:
        headers = {"Content-Type": "application/json", "x-relay-secret": secret}
    body = json.dumps(payload, ensure_ascii=False).encode()
    status, text = None, ""
    for ip in RESOLVE:
        try:
            status, text = _post_via_ip(url, ip, body, headers, timeout)
            break
        except Exception:
            continue
    if status is None:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST", headers=headers),
                                        timeout=timeout) as r:
                status, text = r.status, r.read().decode() or "{}"
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode() or "{}"
        except Exception:
            raise AIError("Нет связи с ИИ. Попробуй ещё раз через минуту.", 502)
    try:
        data = json.loads(text)
    except ValueError:
        raise AIError(f"ИИ ответил не по формату ({status}).", 502)
    if status != 200:
        msg = data.get("error")
        msg = msg.get("message") if isinstance(msg, dict) else msg
        if status == 429:
            raise AIError("ИИ сейчас перегружен. Попробуй через минуту.", 429)
        raise AIError(f"ИИ не ответил ({status}). {msg or ''}".strip(), 502)
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    if msg.get("refusal"):
        raise AIError("ИИ отказался писать это. Попробуй смягчить формулировки.", 422)
    return msg.get("content") or ""


class Client:
    def __init__(self, url, secret, model="gpt-4o-mini", transport=None):
        self.url, self.secret, self.model = url, secret, model
        self.transport = transport or call_relay

    def ask_json(self, name, schema, messages, temperature=0.9, max_tokens=2500):
        payload = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                   "response_format": {"type": "json_schema",
                                       "json_schema": {"name": name, "strict": True, "schema": schema}}}
        content = self.transport(self.url, self.secret, payload)
        try:
            return _dash(json.loads(content))
        except Exception:
            raise AIError("ИИ вернул неполный ответ. Попробуй ещё раз.", 502)


def _dash(o):
    """Длинные тире выдают текст от ИИ — меняем на дефис во всех строках ответа."""
    if isinstance(o, str):
        return o.replace("\u2014", "-")
    if isinstance(o, list):
        return [_dash(x) for x in o]
    if isinstance(o, dict):
        return {k: _dash(v) for k, v in o.items()}
    return o


# ---------- паспорт модели ----------
ARCHETYPES = {
    "siren": ("Сирена", "Соблазн и власть взгляда", "соблазн и власть взгляда. Говорит намёками, дразнит, оставляет недосказанность. Короткие фразы, много подтекста."),
    "mystery": ("Загадка", "Её не прочитать, и это цепляет", "холодная недосказанность. Не отвечает прямо, задаёт вопросы, исчезает. Минимум эмодзи, максимум пауз."),
    "girl": ("Своя девчонка", "Тепло и легко, как со старым другом", "тёплая, простая, как подруга. Пишет как в мессенджере: сбивчиво, искренне, с «блин» и «ой»."),
    "queen": ("Королева", "Холод, статус, высокая цена", "статус и дистанция. Говорит коротко, спокойно, без нужды нравиться. Дорогие детали, высокие требования."),
    "rebel": ("Бунтарка", "Не по правилам, непредсказуемо", "дерзко и наперекор. Не извиняется, делает что хочет, провоцирует. Живой сленг."),
    "muse": ("Муза", "Эстетика, свет, атмосфера", "эстетика и атмосфера. Сенсорные детали: свет, запах, музыка. Немного мечтательности."),
    "huntress": ("Охотница", "Спорт, драйв, она выбирает сама", "энергия и соревнование. Спорт, движение, азарт. Она выбирает сама."),
    "joker": ("Комедиантка", "Юмор и самоирония", "юмор и самоирония. Не боится быть смешной, шутит над собой, подкалывает его."),
}
LANGS = {"ru": "Русский", "en": "Английский", "en_ru": "Английский + перевод для меня"}
PERSONA_FIELDS = {"age": 10, "archetype": 20, "character": 600, "life_facts": 800, "story": 1500, "voice": 2000,
                  "appearance": 800, "lang": 10, "offer": 300, "bans": 600}
EMPTY_PERSONA = {k: "" for k in PERSONA_FIELDS} | {"archetype": "siren", "lang": "ru"}


def clean_persona(d):
    p = dict(EMPTY_PERSONA)
    for k, n in PERSONA_FIELDS.items():
        v = d.get(k)
        if isinstance(v, (str, int)):
            p[k] = str(v).strip()[:n]
    if p["archetype"] not in ARCHETYPES:
        p["archetype"] = "siren"
    if p["lang"] not in LANGS:
        p["lang"] = "ru"
    return p


def persona_filled(p):
    """Сколько важного заполнено (для подсказки «заполни паспорт»)."""
    keys = ("character", "life_facts", "voice", "appearance")
    return sum(1 for k in keys if (p.get(k) or "").strip()), len(keys)


def persona_block(name, p):
    a = ARCHETYPES[p["archetype"]]
    lines = [
        f"ПЕРСОНАЖ: {name}{', ' + p['age'] + ' лет' if p.get('age') else ''} (вымышленная ИИ-модель; это раскрыто в её профилях)",
        f"Архетип: {a[0]} — {a[2]}",
        p.get("character") and f"Характер: {p['character']}",
        p.get("life_facts") and f"Факты из жизни персонажа: {p['life_facts']}",
        f"История: {p.get('story') or 'не расписана — придумай логичную под архетип и держись её'}",
        p.get("offer") and f"Что продаёт (закрытая часть): {p['offer']}",
        p.get("bans") and f"ЗАПРЕТЫ (этого не должно быть нигде): {p['bans']}",
        ("Как она пишет (копируй длину фраз, регистр, эмодзи, сленг, пунктуацию):\n" + p["voice"]) if p.get("voice")
        else "Манера речи: как в живом мессенджере, строчными буквами, без официальщины.",
    ]
    return "\n".join(x for x in lines if x)


LIVE_TEXT_RULES = """ПРАВИЛА ЖИВОГО ТЕКСТА:
- Пиши как живой человек в мессенджере, а не как реклама. Коротко. Одна мысль на пост.
- Первая строка — крючок: неожиданная деталь, вопрос, лёгкая провокация.
- Конкретика вместо общих слов: не «хороший день», а «третий кофе и дождь по стеклу».
- Восклицательный знак — максимум один на пост. Эмодзи — максимум 1–2. Хэштеги только там, где их требует площадка.
- СТОП-ФРАЗЫ (выдают ИИ — нельзя ни в каком виде): «не просто …, а …», «только представьте», «изменить свою жизнь», «давайте поговорим», «хочу поделиться», «в современном мире», «секреты и фишки», длинные тире.
- Это публичные площадки: никакой наготы и откровенных описаний — только намёк и интрига (иначе бан аккаунта)."""

FUNNEL = """ВОРОНКА НЕДЕЛИ (неделя — цепочка к подписке, а не набор случайных постов):
- Пн–Вт: Доверие и Вовлечение — кто она, что у неё происходит, вопросы подписчикам.
- Ср–Чт: Прогрев — интрига, намёки на закрытое, «вчера сняла кое-что».
- Пт: мягкая Продажа — тизер закрытого контента и где он.
- Сб: Вовлечение — лёгкое, смешное, близкое.
- Вс: Продажа или Доверие — итог недели, тёплый финал с намёком."""

PLATFORM_RULES = {
    "threads": "Threads — охват и комментарии. Короткий цепляющий тезис или вопрос, 1–2 части (вторую отдели строкой ---). Можно со строчной буквы. Без хэштегов и ссылок (максимум «ссылка в профиле»). В конце повод написать комментарий.",
    "instagram": "Instagram — Reels на охват. hook: текст на экране в первые 1–2 секунды. sound: трендовый звук и его настроение. imagePrompt: раскадровка 3–5 коротких кадров. text: подпись до 40 слов с мягким призывом («ссылка в профиле»). 3–5 хэштегов.",
    "tiktok": "TikTok — короткое видео. hook: первые 1–2 секунды. sound: трендовый звук. imagePrompt: раскадровка. text: подпись до 25 слов, 2–4 хэштега.",
    "youtube": "YouTube Shorts — hook: первая фраза. text: название + 1–2 предложения. imagePrompt: план ролика.",
    "telegram": "Telegram-канал модели — сюда приходят уже тёплые. Личнее и ближе, 40–100 слов, прогрев к закрытому. Без хэштегов.",
    "x": "X (Twitter) — один короткий твит до 200 символов, дерзко и лично. Без хэштегов.",
    "reddit": "Reddit — title: в поле hook, цепляющий заголовок для сабреддита; text: 1–3 коротких предложения от первого лица. Без ссылок в тексте и без хэштегов, в духе сабреддита.",
    "other": "Нейтральный короткий пост 20–60 слов.",
}
GOALS = ["Прогрев", "Вовлечение", "Доверие", "Продажа"]
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def post_schema(chs):
    return {"type": "object", "additionalProperties": False,
            "required": ["ch", "time", "format", "goal", "hook", "draft", "problems", "text", "hashtags",
                         "needsMedia", "imagePrompt", "sound"],
            "properties": {
                "ch": {"type": "string", "enum": list(chs) or ["other"]},
                "time": {"type": "string", "description": "время публикации ЧЧ:ММ"},
                "format": {"type": "string", "description": "формат одним-двумя словами"},
                "goal": {"type": "string", "enum": GOALS},
                "hook": {"type": "string", "description": "хук/заголовок или пусто"},
                "draft": {"type": "string", "description": "быстрый черновик"},
                "problems": {"type": "string", "description": "что в черновике плохо: стоп-фразы, вялый крючок, нет конкретики, откровенность, не по правилам площадки"},
                "text": {"type": "string", "description": "чистовик с исправлениями из problems"},
                "hashtags": {"type": "string"},
                "needsMedia": {"type": "boolean"},
                "imagePrompt": {"type": "string", "description": "что снять / раскадровка / промпт фото на английском, или пусто"},
                "sound": {"type": "string"},
            }}


OUTLINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["storyline", "days"],
                  "properties": {
                      "storyline": {"type": "string", "description": "сюжет недели, до 8 слов, строчными"},
                      "days": {"type": "array", "items": {
                          "type": "object", "additionalProperties": False, "required": ["day", "beat", "goal"],
                          "properties": {"day": {"type": "string", "enum": DAYS},
                                         "beat": {"type": "string", "description": "задача дня, 1 предложение"},
                                         "goal": {"type": "string", "enum": GOALS}}}}}}


def _base(name, p, scenario=""):
    return "\n\n".join(x for x in [
        "Ты — сильный SMM-редактор, ведёшь соцсети вымышленного ИИ-персонажа. Пишешь посты от её лица.",
        persona_block(name, p),
        p.get("appearance") and f"Внешность (для промптов фото): {p['appearance']}",
        scenario and f"СОБЫТИЯ НЕДЕЛИ ОТ ПРОДЮСЕРА (обязательно вплети): {scenario}",
        LIVE_TEXT_RULES,
    ] if x)


def _post_rules(chs):
    return ("ПРАВИЛА ПОСТА:\n- От первого лица, голосом персонажа. Время ЧЧ:ММ, реалистичное и не круглое (09:10, 13:20, 22:10).\n"
            "- needsMedia = true, если нужно фото или видео; тогда imagePrompt на английском: сцена, свет, одежда, поза, объектив, "
            "внешность персонажа, в конце \"photorealistic, natural skin texture, candid\". Иначе imagePrompt пустой.\n"
            "- hook и sound — только для видео и Reddit, иначе пустые.\n"
            "ПРАВИЛА ПЛОЩАДОК (поле ch — одна из них):\n" + "\n".join(f"- {c}: {PLATFORM_RULES.get(c, PLATFORM_RULES['other'])}" for c in chs))


def _day_task(chs):
    if len(chs) > 1:
        return (f"Сделай ровно по одному посту для каждой площадки: {', '.join(chs)}. Одна тема дня, но каждый пост "
                f"написан под правила своей площадки, а не один текст на всех.")
    return "Сделай 2 поста в разное время, с разными форматами."


def make_outline(cl, name, p, chs, scenario="", storyline=""):
    out = cl.ask_json("week_outline", OUTLINE_SCHEMA, [
        {"role": "system", "content": _base(name, p, scenario)},
        {"role": "user", "content": f"Придумай сюжет этой недели и задачу на каждый день (Пн–Вс, ровно 7).\n{FUNNEL}\n"
                                    f"Площадки: {', '.join(chs)}."
                                    + (f"\nСюжет уже задан продюсером: «{storyline}». Оставь его." if storyline else "")}],
        max_tokens=1200)
    days = out.get("days") or []
    if len(days) < 7:
        raise AIError("ИИ не расписал все 7 дней. Попробуй ещё раз.")
    return {"storyline": out.get("storyline", ""), "days": [{"day": DAYS[i], "beat": d.get("beat", ""),
                                                             "goal": d.get("goal", "")} for i, d in enumerate(days[:7])]}


def make_day(cl, name, p, chs, outline, i, scenario=""):
    week = [f"{d['day']}: {d['beat']} ({d['goal']})" for d in outline["days"]]
    d = outline["days"][i]
    out = cl.ask_json("day_posts", {"type": "object", "additionalProperties": False, "required": ["posts"],
                                    "properties": {"posts": {"type": "array", "items": post_schema(chs)}}}, [
        {"role": "system", "content": _base(name, p, scenario) + "\n\n" + _post_rules(chs)},
        {"role": "user", "content": f"Сюжет недели: {outline['storyline']}\nПлан недели по дням:\n" + "\n".join(week)
                                    + f"\n\nНапиши посты на {d['day']}. Задача дня: {d['beat']}. Главная цель дня: {d['goal']}.\n{_day_task(chs)}"}],
        max_tokens=3500)
    return [_slim(x) for x in (out.get("posts") or [])][:6]


def make_post(cl, name, p, chs, outline, i, current="", idea="", light=False, ch=""):
    d = outline["days"][i]
    if idea:
        task = (f"Продюсер описал своими словами, о чём пост{(' для ' + ch) if ch else ''}:\n«{idea}»\n\n"
                + ("Только причеши: сохрани слова и интонацию, убери воду, добавь цепляющую первую строку."
                   if light else "Сделай из этого сильный пост под правила площадки: крючок, мысль, призыв."))
    elif current:
        task = f"Перепиши этот пост совсем по-другому (другой крючок, другая деталь), сохранив задачу дня{(' и площадку ' + ch) if ch else ''}:\n«{current}»"
    else:
        task = f"Добавь ещё один пост в этот день, не похожий на остальные{(' для ' + ch) if ch else ''}."
    out = cl.ask_json("one_post", {"type": "object", "additionalProperties": False, "required": ["post"],
                                   "properties": {"post": post_schema(chs)}}, [
        {"role": "system", "content": _base(name, p) + "\n\n" + _post_rules(chs)},
        {"role": "user", "content": f"Сюжет недели: {outline.get('storyline', '')}\nДень: {d['day']}. Задача дня: {d['beat']}.\n\n{task}"}],
        max_tokens=1500)
    return _slim(out.get("post") or {})


def _slim(x):
    """Черновик и самопроверку пользователю не показываем — храним только чистовик."""
    return {k: x.get(k, "" if k != "needsMedia" else False)
            for k in ("ch", "time", "format", "goal", "hook", "text", "hashtags", "needsMedia", "imagePrompt", "sound")}


# ---------- чаттер ----------
STAGES = {
    1: ("Знакомство", "ЭТАП 1 — ЗНАКОМСТВО. Никаких продаж, намёков на платное и прайсов. Просто общаешься: встречные вопросы, шутки."),
    2: ("Флирт", "ЭТАП 2 — ФЛИРТ. Лёгкая интрига, двусмысленные шутки. Продажи запрещены."),
    3: ("Прогрев", "ЭТАП 3 — ПРОГРЕВ. Создавай интерес к закрытой части, намекай, но прямо купить не предлагай."),
    4: ("Предложение", "ЭТАП 4 — ПЕРВОЕ ПРЕДЛОЖЕНИЕ. Мягко предложи платный контент: «если хочешь, могу показать…»."),
    5: ("Продажа", "ЭТАП 5 — ПРОДАЖА. Можно предлагать прямо, но вперемешку с обычным общением, без давления."),
}
SLANG = ("Английский — супер-разговорный, как в iMessage: u, ur, wanna, gonna, rn, tbh, idk, lol. Всё строчными, "
         "без книжных слов. Пример: \"hey bb just chilling in bed rn. u miss me? 🥺\"")
HONESTY = ("ЧЕСТНОСТЬ (обязательно): ты — вымышленный ИИ-персонаж, и это указано в профиле. Говоришь в её характере, "
           "но если он прямо спрашивает, настоящая ли ты, живой ли ты человек или ИИ — отвечаешь честно, что ты ИИ-модель, "
           "в своей манере и без потери тона. Не обещай встреч, звонков вживую и ничего, что возможно только с живым человеком.")


def lang_rule(mode):
    if mode == "en":
        return f"Поле text — СТРОГО на английском. {SLANG}\nПоле translation — перевод text на русский (для продюсера)."
    if mode == "en_ru":
        return f"Поле text — СТРОГО на английском, это уйдёт фанату. {SLANG}\nПоле translation — тот же смысл по-русски."
    return "Поле text — на русском. Поле translation — пустая строка."


CHAT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["replies"],
               "properties": {"replies": {"type": "array", "items": {
                   "type": "object", "additionalProperties": False, "required": ["text", "translation"],
                   "properties": {"text": {"type": "string"}, "translation": {"type": "string"}}}}}}


def make_replies(cl, name, p, fan, recent_posts=()):
    stage = min(5, max(1, int(fan.get("stage") or 1)))
    hist = [f"{'ОН' if m.get('role') == 'fan' else 'ТЫ'}: {str(m.get('text', ''))[:600]}" for m in (fan.get("history") or [])[-20:]]
    if not hist or not any(m.get("role") == "fan" for m in (fan.get("history") or [])[-20:]):
        raise AIError("Сначала добавь его сообщение.", 400)
    sys = "\n".join(x for x in [
        f"Ты — {name}. Ведёшь переписку с фанатом в личке, в её характере.",
        persona_block(name, p),
        HONESTY,
        STAGES[stage][1],
        f"Что ты о нём знаешь: {str(fan.get('notes') or '')[:1000] or 'пока ничего'}",
        recent_posts and "Твои последние посты (можно сослаться, если к месту):\n" + "\n".join(f"• {t[:300]}" for t in recent_posts[:6]),
        "ПРАВИЛА:\n- Ответь на суть его последнего сообщения.\n- Коротко, как в мессенджере: 1–2 предложения. Без официальщины и длинных тире.\n"
        "- Ровно 3 варианта: №1 тёплый, №2 игривый и дерзкий, №3 с шагом вперёд по этапу.\n- " + lang_rule(p.get("lang", "ru")),
    ] if x)
    out = cl.ask_json("chatter_replies", CHAT_SCHEMA, [
        {"role": "system", "content": sys},
        {"role": "user", "content": "Переписка:\n" + "\n".join(hist) + "\n\nНапиши 3 варианта твоего следующего сообщения."}],
        temperature=0.95, max_tokens=700)
    rep = [r for r in (out.get("replies") or []) if (r.get("text") or "").strip()][:3]
    if not rep:
        raise AIError("ИИ не вернул вариантов. Попробуй ещё раз.")
    return rep


# ---------- оплата Tribute ----------
def tribute_check(raw, sig, key):
    """Подпись trbt-signature: HMAC-SHA256 тела на API-ключе (принимаем hex и base64)."""
    import hmac, hashlib, base64
    if not key or not sig:
        return False
    mac = hmac.new(key.encode(), raw, hashlib.sha256)
    return hmac.compare_digest(sig.strip(), mac.hexdigest()) or hmac.compare_digest(sig.strip(), base64.b64encode(mac.digest()).decode())


def parse_products(s):
    """TRIBUTE_PRODUCTS="123:30,456:90" → {123: 30, 456: 90} (цифровой товар → дней Pro)."""
    out = {}
    for part in (s or "").split(","):
        if ":" in part:
            a, b = part.split(":", 1)
            if a.strip().isdigit() and b.strip().isdigit():
                out[int(a)] = int(b)
    return out
