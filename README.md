# NeuroFans — аналитика переходов (интеграция с PuzzleBot)

Воронка живёт на **PuzzleBot** и остаётся нетронутой. Этот сервис добавляет к ней
аналитику источников: с какого аккаунта (ссылки в био) пришёл человек, сколько
переходов, уведомления в Telegram и алерты «тишины».

## Как это работает

```
ссылка в био  ──►  t.me/<bot>?start=<метка_аккаунта>  ──►  PuzzleBot (воронка)
                                                              │
                                   на /start PuzzleBot шлёт  ▼
                              GET http://СЕРВЕР:8080/track?source=<метка>&uid=...&username=...
                                                              │
                                                              ▼
                               наш сервис: пишет в базу, шлёт уведомление в ТГ,
                               (опц.) пишет в Google-таблицу, показывает дашборд,
                               шлёт авто-сводку и алерты тишины
```

Телеграм-бот остаётся за PuzzleBot (polling мы НЕ запускаем — используем токен
только для отправки уведомлений через `sendMessage`, поэтому конфликта нет).

## Эндпоинты

- `GET /track?source=...&uid=...&username=...&name=...` — приём перехода (сюда бьёт PuzzleBot).
  Если задан `INGEST_TOKEN`, добавь `&key=<INGEST_TOKEN>`.
- `GET /` — дашборд (если задан `DASHBOARD_TOKEN` → `/?token=...`).
- `GET /api/stats` — JSON со статистикой.
- `GET /export` — выгрузка переходов в CSV.
- `GET /healthz` — проверка живости.

## Запуск на сервере

```
curl -fsSL https://raw.githubusercontent.com/Leonardodeterrios/neurofans-analytics/main/deploy.sh -o deploy.sh && bash deploy.sh
```

Скрипт поставит Docker, соберёт и поднимет сервис. Токен и username бота спросит при установке.

## Настройка в PuzzleBot

1. Конструктор → сценарий на команду `/start`.
2. Добавить действие **HTTP-запрос** (GET), URL:
   `http://СЕРВЕР:8080/track?source={{start_param}}&uid={{user_id}}&username={{username}}&name={{first_name}}`
   (имена переменных подставить из списка PuzzleBot; `source` = параметр ссылки/`start`).
3. Сохранить. Воронку не трогаем — действие работает в фоне.

## Переменные окружения

См. `.env.example`. Ключевые: `BOT_TOKEN`, `ADMIN_CHAT_ID`, `INGEST_TOKEN`,
`DASHBOARD_TOKEN`, `DAILY_SUMMARY_TIME`, `SILENCE_HOURS`.

## Ссылки для аккаунтов

`python make_links.py` — печатает ссылку с меткой для каждого профиля
(метка = имя аккаунта). Вставляй такую ссылку в био соответствующего аккаунта.
