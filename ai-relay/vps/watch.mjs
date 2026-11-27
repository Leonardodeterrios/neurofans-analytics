// Сторож NEUROFANS: раз в 5 минут проверяет сайт, кабинет, трекер, ссылки партнёров, ИИ, галерею и сертификаты.
// Живёт на зарубежном сервере: отсюда Telegram доступен напрямую, и сторож заметит, даже если упал весь сервер в РФ.
// Переменные (.env этого сервера): WATCH_TG_TOKEN — токен бота, WATCH_CHATS — id чатов через запятую.
// Сообщения: «⚠️ Не работает» после 2 неудачных проверок подряд, напоминание раз в 3 часа, «✅ Снова работает»,
// и каждый день в 10:00 по Москве короткое «сторож на связи» — если его нет, значит лёг сам этот сервер.
import tls from "node:tls";
import fs from "node:fs";

const TOKEN = (process.env.WATCH_TG_TOKEN || "").trim();
const CHATS = (process.env.WATCH_CHATS || "").split(/[,;\s]+/).filter(Boolean);
const EVERY = Number(process.env.WATCH_EVERY_MIN || 5) * 60e3;
const GALLERY = (process.env.GALLERY_DOMAIN || "").trim();
const RELAY = (process.env.RELAY_DOMAIN || "ai.neurofans.ru").trim();
const TRACK = (process.env.WATCH_TRACKER || "go.ai-creators.ru").trim();
const PARTNER = (process.env.WATCH_PARTNER_URL || "https://go.lukaflow.shop/").trim();
const UA = "NeuroFansWatch/1.0 (uptime bot)";   // трекер узнаёт бота и не пишет клик
const STATE = "/state/watch.json";

async function get(url, opt = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), opt.timeout || 20e3);
  try {
    return await fetch(url, { redirect: "manual", signal: ctl.signal, ...opt, headers: { "user-agent": UA, ...(opt.headers || {}) } });
  } finally { clearTimeout(t); }
}
const why = (e) => (e?.name === "AbortError" ? "не отвечает 20 с" : String(e?.cause?.code || e?.message || e).slice(0, 80));

function certDays(host) {
  return new Promise((res) => {
    const s = tls.connect({ host, port: 443, servername: host, timeout: 15e3 }, () => {
      const c = s.getPeerCertificate(); s.end();
      res(c?.valid_to ? (new Date(c.valid_to) - Date.now()) / 864e5 : null);
    });
    s.on("error", () => res(null)); s.on("timeout", () => { s.destroy(); res(null); });
  });
}

// каждая проверка: [название, async () => "" (ок) или текст ошибки]
const CHECKS = [
  ["Сайт neurofans.ru", async () => {
    const r = await get("https://neurofans.ru/"); const t = r.status === 200 ? await r.text() : "";
    return r.status !== 200 ? `HTTP ${r.status}` : /NEUROFANS/i.test(t) ? "" : "страница пустая или чужая";
  }],
  ["Кабинет lab.neurofans.ru", async () => {
    const r = await get("https://lab.neurofans.ru/healthz"); return r.status === 200 ? "" : `HTTP ${r.status}`;
  }],
  ["Трекер и бот-воронка", async () => {
    const r = await get(`https://${TRACK}/healthz?deep=1`);
    if (r.status !== 200) return `HTTP ${r.status}`;
    const h = await r.json().catch(() => null);
    if (!h) return "ответ не JSON (старая версия? нужен git pull)";
    if (!h.db) return "не открывается база";
    if (h.sched_age == null || h.sched_age > 600) return "встал планировщик (итоги, догоны, копии)";
    if (h.bg_queue > 500) return `очередь фоновых задач ${h.bg_queue} — уведомления не уходят`;
    if (h.tg_bad) return `не уходят сообщения в Telegram (${h.tg_why}) — проверь мост ${RELAY}/tg`;
    return "";
  }],
  ["Приём оплат Tribute", async () => {   // без подписи трекер обязан ответить 401 — значит адрес жив
    const r = await get(`https://${TRACK}/tribute/webhook`, { method: "POST", body: "{}", headers: { "content-type": "application/json" } });
    return r.status === 401 ? "" : `HTTP ${r.status} вместо 401`;
  }],
  ["Ссылки партнёров (.shop)", async () => {
    const r = await get(PARTNER); const loc = r.headers.get("location") || "";
    return (r.status === 301 || r.status === 302) && loc.includes("t.me/") ? "" : `HTTP ${r.status}, ведёт не в бота`;
  }],
  ["ИИ-ретранслятор", async () => {
    const r = await get("http://relay:8080/"); if (r.status !== 200) return `HTTP ${r.status}`;
    const j = await r.json().catch(() => ({})); return j.key ? "" : "нет ключа ИИ в .env";
  }],
  ["Мост в Telegram", async () => {
    if (!TOKEN) return "";
    const r = await get(`https://${RELAY}/tg/bot${TOKEN}/getMe`);
    return r.status === 200 ? "" : `HTTP ${r.status}`;
  }],
  ...(GALLERY ? [["Галерея примеров", async () => {
    const r = await get(`https://${GALLERY}/`); return r.status === 200 ? "" : `HTTP ${r.status}`;
  }]] : []),
  ["Сертификаты HTTPS", async () => {
    const hosts = ["neurofans.ru", "lab.neurofans.ru", TRACK, RELAY, ...(GALLERY ? [GALLERY] : [])];
    const bad = [];
    for (const h of hosts) {
      const d = await certDays(h);
      if (d != null && d < 10) bad.push(`${h}: ${Math.max(0, Math.floor(d))} дн.`);
    }
    return bad.length ? "скоро истекают — " + bad.join(", ") : "";
  }],
];

let st = { down: {}, fails: {}, daily: "" };
try { st = { ...st, ...JSON.parse(fs.readFileSync(STATE, "utf8")) }; } catch {}
const save = () => { try { fs.writeFileSync(STATE, JSON.stringify(st)); } catch {} };

async function tg(text) {
  if (!TOKEN || !CHATS.length) return console.log("[watch]", text);
  for (const chat of CHATS) {
    try {
      await get(`https://api.telegram.org/bot${TOKEN}/sendMessage`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: chat, text, disable_web_page_preview: true }),
      });
    } catch (e) { console.log("[watch] tg failed", why(e)); }
  }
}
const mins = (ms) => { const m = Math.round(ms / 60e3); return m < 90 ? `${m} мин` : `${(m / 60).toFixed(1)} ч`; };

async function round() {
  const now = Date.now();
  const res = await Promise.all(CHECKS.map(async ([name, fn]) => {
    try { return [name, await fn()]; } catch (e) { return [name, why(e)]; }
  }));
  for (const [name, err] of res) {
    const d = st.down[name];
    if (err) {
      st.fails[name] = (st.fails[name] || 0) + 1;
      if (!d && st.fails[name] >= 2) {
        st.down[name] = { since: now, told: now, err };
        await tg(`⚠️ Не работает: ${name}\n${err}\n\nПроверю снова через 5 минут и напишу, когда поднимется.`);
      } else if (d && now - d.told > 3 * 3600e3) {
        d.told = now; d.err = err;
        await tg(`⚠️ Всё ещё не работает (${mins(now - d.since)}): ${name}\n${err}`);
      }
    } else {
      st.fails[name] = 0;
      if (d) { delete st.down[name]; await tg(`✅ Снова работает: ${name} (лежало ${mins(now - d.since)})`); }
    }
  }
  const msk = new Date(now + 3 * 3600e3), day = msk.toISOString().slice(0, 10);
  if (msk.getUTCHours() >= 10 && st.daily !== day) {
    st.daily = day;
    const bad = Object.keys(st.down);
    await tg(bad.length ? `🛡 Сторож на связи. Сейчас не работает: ${bad.join(", ")}.`
      : `🛡 Сторож на связи: всё работает (${CHECKS.length} проверок).`);
  }
  save();
}

console.log(`[watch] start: ${CHECKS.length} проверок, раз в ${EVERY / 60e3} мин, чатов ${CHATS.length}`);
round();
setInterval(round, EVERY);
