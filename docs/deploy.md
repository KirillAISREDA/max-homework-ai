# Деплой бота (VPS, Docker)

Бот работает в контейнере на VPS `193.247.73.243` (рядом — Redis для состояния диалогов) (Ubuntu 22.04, Docker 29, Compose v5),
каталог `/opt/max-homework-ai`. Режим — long polling: бот сам ходит к MAX и GigaChat,
входящих портов нет, Caddy/webhook не нужны. VPS общий с другими проектами (~25 контейнеров).

**Один поллер на токен.** Пока контейнер на VPS жив, не запускать `hwcheck bot` локально:
два поллера с одним `MAX_TOKEN` делят апдейты между собой.

## Что где

| Что | Где |
|---|---|
| Код | `/opt/max-homework-ai` — git-клон `main` + `.env` (не в git) |
| Контейнер | `homework-bot`, образ `max-homework-ai-bot`, `restart: unless-stopped`, лимит 1 ГБ |
| Состояние диалогов | контейнер `homework-redis` (`redis:8-alpine`, AOF, том `redis-data`, без портов), ключи `fsm:<хэш chat_id>` с TTL 24 ч |
| Журнал событий / marker | `/opt/max-homework-ai/var/` (том, переживает пересборку) |
| Фото домашек | `/opt/max-homework-ai/var/photos/<дата UTC>/<хэш user>-<id>.jpg`, удаляются через 30 дней (`PHOTOS_TTL_DAYS`); ключ фото — поле `photo` в `vision_recognized`/`photo_failed` |
| Кэш солвера | `/opt/max-homework-ai/.cache/solver/` (том) |
| Логи | `var/bot.log` (ротация 5 МБ × 3, переживает пересборку) и `docker compose logs -f` (только текущий контейнер) |
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

## PostgreSQL и ключи id (онбординг, этап 1)

С этапа 1 онбординга compose требует `POSTGRES_PASSWORD` в `.env`, а бот хэширует id через `ID_HASH_KEY`
(HMAC) и проверяет `USER_ID_KEY` при старте. Первая выкатка — один раз:

```bash
# на VPS, в /opt/max-homework-ai; перед этим — нет событий за 10 минут
git pull
cp .env .env.bak-$(date +%F) && chmod 600 .env.bak-*
docker build -t max-homework-ai-bot .
docker run --rm max-homework-ai-bot python -m hwcheck keys >> .env   # секреты сразу в .env, не на экран
grep -o '^[A-Z_]*=' .env                                               # проверить только имена
mkdir -p var/backups && chmod 700 var/backups   # дампы с данными детей — только root
docker compose up -d postgres pgbackup bot                             # Redis не трогаем
docker compose logs --tail 20 bot                                      # «migrations applied: 001_onboarding.sql»
```

Копию `ID_HASH_KEY`, `USER_ID_KEY`, `POSTGRES_PASSWORD` Кирилл хранит вне VPS: без `USER_ID_KEY` бот не
сможет писать пользователям, без `ID_HASH_KEY` — найти их. Переход на HMAC один раз сбрасывает открытые
разборы (ключи Redis `fsm:<хэш>` меняются); тестеры из старого `TEST_USERS` узнаются и по прежнему хэшу.

```bash
docker exec homework-postgres psql -U homework -c '\dt'                   # таблицы онбординга
docker exec homework-postgres psql -U homework -c 'SELECT * FROM schema_migrations'
ls -la var/backups                                                       # ежедневные дампы, хранение 7 дней
```

## Онбординг: включение и аварийный выключатель (этап 2)

Миграция `002_children.sql` применяется при старте бота и пересоздаёт пустые таблицы профилей и согласий;
если в них уже есть данные — бот не стартует (в логе `002_children: в student_profiles…`). Перед выкаткой
миграции — проверка, что таблицы пусты:

```bash
docker exec homework-postgres psql -U homework -c 'SELECT (SELECT count(*) FROM student_profiles) sp, (SELECT count(*) FROM consents) c, (SELECT count(*) FROM homeworks) h'
```

Все три числа — нули, иначе миграция 002 не применится и бот уйдёт в перезапуски.

Включить:

```bash
# на VPS, в /opt/max-homework-ai; перед этим — нет событий за 10 минут
# printf, а не echo: если .env не кончается переводом строки, echo склеил бы строки
grep -q '^ONBOARDING_REQUIRED=' .env \
  && sed -i 's/^ONBOARDING_REQUIRED=.*/ONBOARDING_REQUIRED=true/' .env \
  || printf '\nONBOARDING_REQUIRED=true\n' >> .env
docker compose up -d --force-recreate bot        # env_file перечитывается только при пересоздании
docker compose logs --tail 20 bot                # «onboarding: required»
```

Флаг `ONBOARDING_REQUIRED=true` в prod держится только на время живого теста, пока нет меню отзыва согласия
(этап 3), вычитки политики юристом и реквизитов оператора в политике (§14 спецификации); после теста —
`false`, если Кирилл не решил иначе.

Выключить (поломка онбординга мешает проверке): то же с `ONBOARDING_REQUIRED=false`, в логе
`onboarding: off` (в prod ещё предупреждение «фото проверяются без согласия родителя»). Профили и согласия
в базе остаются.

Сбросить тестовый аккаунт для повторного прохода (хэш — поле `user` в `var/events.jsonl`). Команды одного
`psql -c` выполняются одной транзакцией; порядок важен — дети 1–4 удаляются раньше родителя:

```bash
docker exec homework-postgres psql -U homework -c "
  UPDATE consents SET revoked_at = now() WHERE revoked_at IS NULL
    AND (parent_hash = '<хэш>' OR student_hash = '<хэш>');
  DELETE FROM student_profiles WHERE user_id IS NULL
    AND parent_user_id = (SELECT id FROM users WHERE max_user_hash = '<хэш>');
  DELETE FROM users WHERE max_user_hash = '<хэш>';
  DELETE FROM login_attempts WHERE user_hash = '<хэш>';
  DELETE FROM subject_waitlist WHERE user_hash = '<хэш>';"
docker exec homework-redis redis-cli DEL 'onb:<хэш>'
```

- Сброс родителя: профили его детей 1–4 удаляются, а у ребёнка 5–9 класса со своим MAX связь с родителем
  обнуляется (профиль остаётся, согласие отозвано) — для чистого повтора сбрасывать оба хэша, родителя и ребёнка.
- Записи согласий не удаляются, а остаются с `revoked_at` — это юридическая запись. Без отзыва ребёнок остался
  бы «с согласием», а связка с другим родителем упала бы на `consents_one_active`.
- Это сброс тестового аккаунта, а не процедура удаления данных пользователя (этап 3).

## OCR-сервис

Контейнер `homework-ocr` (посимвольное распознавание рукописи для языков, спецификация каркаса §8) —
не стартует вместе с остальными по умолчанию, только явно, через профиль compose `ocr`. **На этой
стадии контейнер на VPS только собирается, не запускается:**

```bash
docker compose --profile ocr build ocr
```

Так образ готов к моменту подключения реального движка (`ReadingPipelineEngine`), но не занимает
память на общем VPS раньше времени. Лимит памяти сервиса — `3 ГБ` (не `1,5 ГБ`, как в исходных
ограничениях): по спайку `docs/research/2026-09-17-readingpipeline-vps.md` пайплайн ReadingPipeline
на реальных фото стабильно занимает ~2,3–2,5 ГБ и пиково до ~3,06 ГБ, `1,5 ГБ` недостижим.

Запуск (`docker compose --profile ocr up -d ocr`) и health-проверка —

```bash
free -m                                    # свободная память до запуска — лимит контейнера 3 ГБ
docker compose --profile ocr up -d ocr
docker exec homework-ocr python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
```

— переносятся на этап «русский», после решения по размещению (этот VPS с апгрейдом до 16 ГБ или
Cloud.ru с отдельным резервированием памяти, см. «Решения» в спайке): на общем VPS свободно сейчас
~3,7–3,8 ГБ, а бот+Redis+PostgreSQL уже используют остальное — без этого решения запуск рискует
уронить либо OCR, либо прод при первом же плотном фото. Движок по умолчанию — `OCR_ENGINE=fake`
(переменная в `.env`), без портов наружу: бот ходит по compose-сети как к `http://ocr:8080`
(`OCR_URL` в `environment` бота).

## Диагностика

```bash
docker ps --filter name=homework                # статус и health бота и Redis
docker stats --no-stream homework-bot           # CPU/память (в норме ~130–300 МБ)
tail -50 var/bot.log                            # роли страниц, ошибки (переживает пересборку)
tail -5 var/events.jsonl                        # последние события пайплайна (trace_id связывает события апдейта)
docker exec homework-redis redis-cli --scan --pattern 'fsm:*' | wc -l   # открытые диалоги
du -sh var/photos                               # объём сохранённых фото
docker compose restart
```

Сводка вердиктов и причин «не уверен» (prod / test отдельно; причины пишутся с 14.09):

```bash
docker exec homework-bot python -m hwcheck report var/events.jsonl
```

Причины: `unreadable` — на странице «неразборчиво»; `ambiguous_equation` — корень сменился без явной связи;
`answer_unparseable` — ответ записан, но не разобран; `no_answer` — ответа нет и последняя строка не эталон;
`steps_unparseable` — не разобрана ни одна строка. Рядом `ref_status`: `no_condition`, `solver_failed`,
`ref_not_verified`, `ok`.

Разобрать спорную проверку: найти в `var/events.jsonl` событие `task_checked` с нужным вердиктом,
по его `trace_id` — `vision_recognized` с полем `photo`, открыть `var/photos/<photo>`.

## Переключение dev → prod

В `.env` на VPS: `ENVIRONMENT=prod` и `TEST_USERS=<хэш>,<хэш>` — обезличенные id команды и
тестеров (поле `user` в `var/events.jsonl`), затем `docker compose up -d`. С этого момента события
идут в конкурсный зачёт (антифрод, Положение п. 2.2), а события тестеров пишутся с `env=test` и
в зачёт не попадают. Новый тестер — добавить его хэш в `TEST_USERS` и `docker compose up -d`.

## Ограничения текущей схемы

- Фото хранятся на диске VPS, а не в Object Storage; удаление по запросу пользователя — вручную
  по хэшу в имени файла (`rm var/photos/*/<хэш>-*`).
- Апдейты обрабатываются последовательно (PERS-тариф GigaChat = 1 поток). При росте трафика —
  webhook + очередь (арх. §8).
