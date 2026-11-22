// Ретранслятор NeuroFans Lab → OpenAI для Deno Deploy (то же, что api/chat.js для Vercel).
// Зачем: сервер кабинета в РФ, OpenAI оттуда не отвечает, а адреса Vercel с сервера режутся.
// Точка входа в Deno Deploy: ai-relay/deno.ts. Переменные: OPENAI_KEY, LAB_AI_SECRET, (необяз.) ALLOWED_MODELS.
// Пускаем только с секретом кабинета (заголовок x-relay-secret), только chat/completions,
// только разрешённые модели, с потолком длины ответа и размера запроса. Промптов и данных здесь нет.

const ALLOWED = (Deno.env.get("ALLOWED_MODELS") || "gpt-4o-mini,gpt-4.1-mini")
  .split(",").map((s) => s.trim()).filter(Boolean);

function sameSecret(got: string, want: string): boolean {
  if (!want || got.length !== want.length) return false;
  let r = 0;
  for (let i = 0; i < got.length; i++) r |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return r === 0;
}

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

Deno.serve(async (req: Request) => {
  if (req.method === "GET") return json({ ok: true, relay: "neurofans-lab" });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  if (!sameSecret(req.headers.get("x-relay-secret") || "", Deno.env.get("LAB_AI_SECRET") || "")) {
    return json({ error: "forbidden" }, 403);
  }
  const key = Deno.env.get("OPENAI_KEY") || Deno.env.get("OPENAI_API_KEY");
  if (!key) return json({ error: "OPENAI_KEY не задан в Deno Deploy" }, 500);

  let b: Record<string, unknown>;
  try {
    b = await req.json();
  } catch {
    return json({ error: "не JSON" }, 400);
  }
  const messages = b.messages;
  if (!Array.isArray(messages) || !messages.length) return json({ error: "нет messages" }, 400);
  if (JSON.stringify(messages).length > 120000) return json({ error: "слишком длинный запрос" }, 413);

  const body: Record<string, unknown> = {
    model: ALLOWED.includes(String(b.model)) ? b.model : ALLOWED[0],
    messages,
    max_completion_tokens: Math.min(Number(b.max_tokens) || 2000, 6000),
  };
  if (typeof b.temperature === "number") body.temperature = Math.max(0, Math.min(1.2, b.temperature));
  if (b.response_format && typeof b.response_format === "object") body.response_format = b.response_format;

  try {
    const r = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return new Response(await r.text(), { status: r.status, headers: { "Content-Type": "application/json" } });
  } catch (e) {
    return json({ error: "OpenAI не ответил: " + String((e as Error)?.message || e).slice(0, 200) }, 502);
  }
});
