// Ретранслятор NeuroFans Lab → OpenAI.
// Зачем: сервер кабинета стоит в РФ, а OpenAI оттуда не отвечает. Этот файл живёт на Vercel
// (отдельный проект, корневая папка ai-relay) и только пересылает запрос кабинета в OpenAI.
// Все промпты, лимиты и тарифы — в кабинете (students/lab_ai.py), здесь их нет.
//
// Защита: пускаем только с секретом кабинета (заголовок x-relay-secret = LAB_AI_SECRET),
// только chat/completions, только разрешённые модели, с потолком длины ответа и размера запроса.
// Переменные Vercel: OPENAI_KEY, LAB_AI_SECRET, (необязательно) ALLOWED_MODELS через запятую.
const crypto = require('crypto');

const ALLOWED = (process.env.ALLOWED_MODELS || 'gpt-4o-mini,gpt-4.1-mini')
  .split(',').map((s) => s.trim()).filter(Boolean);

function sameSecret(got, want) {
  if (!want || typeof got !== 'string' || got.length !== want.length) return false;
  return crypto.timingSafeEqual(Buffer.from(got), Buffer.from(want));
}

module.exports = async (req, res) => {
  if (req.method === 'GET') return res.status(200).json({ ok: true, relay: 'neurofans-lab' });
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });
  if (!sameSecret(req.headers['x-relay-secret'], process.env.LAB_AI_SECRET || '')) {
    return res.status(403).json({ error: 'forbidden' });
  }
  const key = process.env.OPENAI_KEY || process.env.OPENAI_API_KEY;
  if (!key) return res.status(500).json({ error: 'OPENAI_KEY не задан в Vercel' });

  const b = req.body || {};
  if (!Array.isArray(b.messages) || !b.messages.length) return res.status(400).json({ error: 'нет messages' });
  if (JSON.stringify(b.messages).length > 120000) return res.status(413).json({ error: 'слишком длинный запрос' });

  const body = {
    model: ALLOWED.includes(b.model) ? b.model : ALLOWED[0],
    messages: b.messages,
    max_completion_tokens: Math.min(Number(b.max_tokens) || 2000, 6000),
  };
  if (typeof b.temperature === 'number') body.temperature = Math.max(0, Math.min(1.2, b.temperature));
  if (b.response_format && typeof b.response_format === 'object') body.response_format = b.response_format;

  try {
    const r = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const text = await r.text();
    res.status(r.status).setHeader('Content-Type', 'application/json');
    return res.send(text);
  } catch (e) {
    return res.status(502).json({ error: 'OpenAI не ответил: ' + String(e && e.message || e).slice(0, 200) });
  }
};
