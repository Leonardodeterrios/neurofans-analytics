// Ретранслятор NeuroFans Lab → OpenAI (или OpenRouter) для своего зарубежного сервера.
// Зачем: кабинет стоит в РФ, OpenAI оттуда не отвечает, а Vercel/AWS/Hetzner из РФ режутся.
// Логика та же, что в ../api/chat.js: пускаем только с секретом кабинета (x-relay-secret),
// только разрешённые модели, с потолком длины ответа и размера запроса. Промптов и данных здесь нет,
// ничего не пишем на диск и не логируем тексты.
// Переменные: AI_KEY (ключ OpenAI sk-... или OpenRouter sk-or-...), LAB_AI_SECRET, (необяз.) ALLOWED_MODELS.
import http from 'node:http';
import crypto from 'node:crypto';

const KEY = process.env.AI_KEY || process.env.OPENAI_KEY || '';
const SECRET = process.env.LAB_AI_SECRET || '';
const OR = KEY.startsWith('sk-or-');
const UPSTREAM = process.env.UPSTREAM_URL ||
  (OR ? 'https://openrouter.ai/api/v1/chat/completions' : 'https://api.openai.com/v1/chat/completions');
const ALLOWED = (process.env.ALLOWED_MODELS || 'gpt-4o-mini,gpt-4.1-mini')
  .split(',').map((s) => s.trim()).filter(Boolean);
const PORT = Number(process.env.PORT || 8080);
const MAX_BODY = 1_000_000;

function sameSecret(got, want) {
  if (!want || typeof got !== 'string' || got.length !== want.length) return false;
  return crypto.timingSafeEqual(Buffer.from(got), Buffer.from(want));
}

function send(res, status, data) {
  const s = typeof data === 'string' ? data : JSON.stringify(data);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(s);
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET') {
    return send(res, 200, { ok: true, relay: 'neurofans-lab', upstream: OR ? 'openrouter' : 'openai', key: !!KEY });
  }
  if (req.method !== 'POST') return send(res, 405, { error: 'POST only' });

  // тело читаем целиком (с потолком), потом проверяем секрет
  const chunks = [];
  let size = 0;
  try {
    for await (const c of req) {
      size += c.length;
      if (size > MAX_BODY) return send(res, 413, { error: 'слишком длинный запрос' });
      chunks.push(c);
    }
  } catch {
    return send(res, 400, { error: 'обрыв запроса' });
  }
  if (!sameSecret(req.headers['x-relay-secret'], SECRET)) return send(res, 403, { error: 'forbidden' });
  if (!KEY) return send(res, 500, { error: 'AI_KEY не задан на сервере ретранслятора' });

  let b;
  try { b = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { return send(res, 400, { error: 'не JSON' }); }
  if (!Array.isArray(b.messages) || !b.messages.length) return send(res, 400, { error: 'нет messages' });
  if (JSON.stringify(b.messages).length > 120000) return send(res, 413, { error: 'слишком длинный запрос' });

  const model = ALLOWED.includes(b.model) ? b.model : ALLOWED[0];
  const body = {
    model: OR && !model.includes('/') ? 'openai/' + model : model,
    messages: b.messages,
    max_completion_tokens: Math.min(Number(b.max_tokens || b.max_completion_tokens) || 2000, 6000),
  };
  if (typeof b.temperature === 'number') body.temperature = Math.max(0, Math.min(1.2, b.temperature));
  if (b.response_format && typeof b.response_format === 'object') body.response_format = b.response_format;

  const headers = { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' };
  if (OR) { headers['HTTP-Referer'] = 'https://lab.neurofans.ru'; headers['X-Title'] = 'NeuroFans Lab'; }
  try {
    const r = await fetch(UPSTREAM, { method: 'POST', headers, body: JSON.stringify(body), signal: AbortSignal.timeout(55_000) });
    return send(res, r.status, await r.text());
  } catch (e) {
    return send(res, 502, { error: 'ИИ не ответил: ' + String((e && e.message) || e).slice(0, 200) });
  }
});
server.requestTimeout = 70_000;
server.listen(PORT, () => console.log(`relay on :${PORT}, upstream ${OR ? 'openrouter' : 'openai'}, key ${KEY ? 'set' : 'MISSING'}`));
