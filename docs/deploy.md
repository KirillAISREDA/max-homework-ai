# Деплой бота (VPS, Docker)

Бот работает в одном контейнере на VPS `193.247.73.243` (Ubuntu 22.04, Docker 29, Compose v5),
каталог `/opt/max-homework-ai`. Режим — long polling: бот сам ходит к MAX и GigaChat,
входящих портов нет, Caddy/webhook не нужны. VPS общий с другими проектами (~25 контейнеров).

**Один поллер на токен.** Пока контейнер на VPS жив, не запускать `hwcheck bot` локально:
два поллера с одним `MAX_TOKEN` делят апдейты между собой.

## Что где

| Что | Где |
|---|---|
| Код | `/opt/max-homework-ai` — git-клон `main` + `.env` (не в git) |
| Контейнер | `homework-bot`, образ `max-homework-ai-bot`, `restart: unless-stopped`, лимит 1 ГБ |
| Журнал событий / marker | `/opt/max-homework-ai/var/` (том, переживает пересборку) |
| Кэш солвера | `/opt/max-homework-ai/.cache/solver/` (том) |
| Логи | `docker compose logs -f` (json-file, ротация 30 МБ × 3 — настройка демона) |
| Health | marker обновляется после каждого GET /updates; «unhealthy» = нет записи 5 минут |
| Остановка | `docker compose stop`: SIGTERM → бот дообрабатывает полученный батч и выходит (grace 150 с); простаивающий long poll отменяется сразу |

Файлы деплоя в репозитории: `Dockerfile`, `.dockerignore`, `docker-compose.yml`.

## Первый запуск на чистом хосте

```bash
git clone https://github.com/KirillAISREDA/max-homework-ai.git /opt/max-homework-ai
cd /opt/max-homework-ai
cp .env.example .env && nano .env            # GIGACHAT_CREDENTIALS, GIGACHAT_SCOPE, MAX_TOKEN, ENVIRONMENT
mkdir -p var .cache && chown -R 1000:1000 var .cache   # контейнер работает от uid 1000
docker compose up -d --build
```

Если каталоги `var/` и `.cache/` создаст сам Docker, они будут от root, и бот не сможет писать
marker/журнал/кэш (в логах — PermissionError, health — unhealthy). Лечится тем же `chown`.

## Обновить бота

```bash
ssh root@193.247.73.243
cd /opt/max-homework-ai
git pull
docker compose up -d --build     # пересборка ~1 мин, зависимости кэшируются по uv.lock
docker compose logs --tail 50
```

## Диагностика

```bash
docker ps --filter name=homework-bot            # статус и health
docker stats --no-stream homework-bot           # CPU/память (в норме ~130–300 МБ)
docker compose logs --no-log-prefix | grep -v INFO:httpx | tail -50
tail -5 var/events.jsonl                        # последние события пайплайна
docker compose restart
```

## Переключение dev → prod

В `.env` на VPS: `ENVIRONMENT=prod`, затем `docker compose up -d`. С этого момента события
идут в конкурсный зачёт (антифрод, Положение п. 2.2) — переключать только на реальный трафик.

## Ограничения текущей схемы

- Состояние диалога (FSM) — в памяти процесса: рестарт контейнера сбрасывает открытые диалоги
  тьютора. Redis `StateStore` — в TODO.
- Апдейты обрабатываются последовательно (PERS-тариф GigaChat = 1 поток). При росте трафика —
  webhook + очередь (арх. §8).
