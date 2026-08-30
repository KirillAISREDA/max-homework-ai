# TODO

Приоритеты сверху вниз внутри каждого блока. План по неделям — из `docs/architecture.md`, раздел 10.

## Блокеры (нужно от Кирилла)

- [ ] Доступ к GigaChat API: client_id / client_secret (developers.sber.ru)
- [ ] Аккаунт Cloud.ru Evolution
- [ ] Регистрация бота в MAX, получение токена Bot API
- [ ] Датасет: 30–50 фото реальных домашних работ (математика) для офлайн-оценки

## Неделя 1 — фундамент и офлайн-оценка

- [x] Репозиторий, стартовые файлы (HISTORY, TODO, CLAUDE.md)
- [ ] Скелет проекта: pyproject.toml, структура пакетов, линтеры, pytest, CI (GitHub Actions)
- [ ] Клиент GigaChat: OAuth (токен на 30 мин, кэш + lock), сертификат Минцифры, retry/backoff
- [ ] Абстракции `LLMClient` / `VisionClient` (GigaChat — реализация №1)
- [ ] Скрипт офлайн-оценки: прогон датасета через Vision, подсчёт accuracy распознавания
- [ ] Замер латентности vision-этапа на реальных фото (валидация цели 20–60 с на проверку)

## Неделя 2 — ядро пайплайна (CLI, без инфраструктуры)

- [ ] Vision/Parser: сегментация фото на задания, JSON с confidence
- [ ] Solver: независимое решение задания, structured output (pydantic)
- [ ] Validator: SymPy-пересчёт шагов, проверка единиц и знаков
- [ ] Кэш решений: hash(изображение + условие) → результат, Redis, TTL 30 дней
- [ ] Промпты v1 в `prompts/` с версионированием
- [ ] Пайплайн Vision → Solver → Validator как CLI-команда

## Неделя 3 — тьютор и генератор

- [ ] Comparator + Error Classifier (таксономия ошибок, JSON-режим)
- [ ] Tutor: FSM подсказок 0→3, ответ в промпт только на уровне 3
- [ ] Exercise Generator + валидация эталона Validator'ом
- [ ] Всё в одном контейнере воркера (Dockerfile)

## Неделя 4 — интеграция с MAX

- [ ] MAX Gateway: webhook, подпись, дедуп по update_id, мгновенный 200 OK
- [ ] Core API: users, homework, sessions, лимиты (token bucket), FSM диалога в Redis
- [ ] PostgreSQL-схема (миграции), Object Storage для фото
- [ ] Деплой на Cloud.ru (Container Apps), end-to-end сценарий в MAX

## Неделя 5 — student model, отчёты, admin

- [ ] Student Model Service (rule-based scoring, контракт под будущий BKT)
- [ ] Report Scheduler (CronJob, воскресенье 18:00) + Parent Report
- [ ] Admin/Review UI (Streamlit за basic-auth): очередь спорных проверок
- [ ] Мониторинг: метрики пайплайна, токены/стоимость по шагам, hit rate кэша

## Неделя 6 — пилот

- [ ] Пилот 10–20 семей
- [ ] Нагрузочный тест k6 на 1000 DAU (синтетика с кэшированными ответами GigaChat)
- [ ] Проверка блокирующих показателей: accuracy ≥ 90 %, ложные «ошибки» ≤ 5 %
