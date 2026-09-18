# Реестр сторонних компонентов

Раскрытие сторонних библиотек, моделей и API проекта — п. 5.3 Положения о конкурсе Sber500xDisrupt
(см. `docs/contest/contest.md`). Актуально на: **2026-09-17** (OCR-сервис — `ocrsvc/` на
ReadingPipeline, см. §5, §5.1, §6).

## 1. Python-зависимости runtime

Прямые зависимости `[project.dependencies]` в `pyproject.toml`, версии зафиксированы в `uv.lock`.
Ставятся в образ (`uv sync --locked --no-dev`, `Dockerfile`).

| Компонент | Версия | Лицензия | Назначение |
|---|---|---|---|
| asyncpg | 0.31.0 | Apache-2.0 | Асинхронный драйвер PostgreSQL: пул и миграции при старте бота (`db/pool.py`, `db/migrate.py`) — профили, согласия, приглашения онбординга |
| certifi | 2026.7.22 | MPL-2.0 | Корневые сертификаты для TLS; база, к которой `bot/max_api.py` добавляет корень НУЦ Минцифры при обращении к MAX API |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | Fernet: id MAX в базе хранится только шифротекстом (`crypto.py`, спецификация онбординга §10.1) |
| gigachat | 0.2.3 | MIT | Официальный SDK GigaChat API — единственный клиент LLM/vision (`llm/gigachat_client.py`), OAuth и его обновление берёт на себя |
| httpx | 0.28.1 | BSD-3-Clause | Асинхронный HTTP-клиент MAX Bot API (`bot/max_api.py`): long polling `/updates`, отправка сообщений |
| pillow | 12.3.0 | MIT-CMU | Обработка фотографий тетради перед vision-запросом (`pipeline/normalize.py`: `Image`, `ImageOps`) |
| pydantic | 2.13.5 | MIT | Модели данных и валидация: JSON Schema LLM-контрактов (`pipeline/schemas.py`), состояние FSM (`bot/fsm.py`, `bot/models.py`) |
| pydantic-settings | 2.15.0 | MIT | Загрузка конфигурации из `.env` в `Settings` (`config.py`) |
| redis | 8.1.0 | MIT | Асинхронный клиент Redis: состояние диалога `RedisStateStore` (`bot/fsm.py`, `bot/runner.py`) |
| spylls | 0.1.7 | MIT | Hunspell на Python: словарные кандидаты для пропусков (`subjects/russian/gaps.py`) |
| sympy | 1.14.0 | BSD | Детерминированная проверка арифметики/алгебры (`pipeline/mathparse.py`, `pipeline/validator.py`) — источник истины по математике, не LLM |

Транзитивных зависимостей, импортируемых напрямую из `src/` в обход прямых зависимостей выше, не
обнаружено (проверено по `import`/`from` в `src/hwcheck/`).

## 2. Dev-зависимости

`[dependency-groups].dev` в `pyproject.toml`. Только разработка/CI — `Dockerfile` ставит зависимости
командой `uv sync --locked --no-dev`, в образ не попадают.

| Компонент | Версия | Лицензия | Назначение |
|---|---|---|---|
| pytest | 9.1.1 | MIT | Тестовый раннер |
| pytest-asyncio | 1.4.0 | Apache-2.0 | Поддержка `async def` тестов (`asyncio_mode = "auto"`) |
| ruff | 0.16.5 | MIT | Линтер и форматтер (CI: `ruff check`, `ruff format --check`) |
| mypy | 2.3.1 | MIT | Статическая типизация, `strict = true` (CI: `mypy`) |
| asyncpg-stubs | 0.31.3 | BSD-3-Clause | Типы asyncpg для mypy strict |
| fakeredis | 2.38.0 | BSD-3-Clause | In-memory замена Redis в тестах `RedisStateStore` без поднятия сервера |

## 3. Модели (LLM/vision)

Провайдер — **GigaChat API** (ПАО Сбербанк). Доступ по API (HTTP), веса моделей не распространяются
и не хранятся в проекте. Идентификаторы — из `src/hwcheck/config.py` (`Settings`), роутинг — арх. §4.

| Настройка | Идентификатор модели | Используется для |
|---|---|---|
| `vision_model` | GigaChat-2-Max | Vision-транскрипция фото тетради/учебника свободным текстом — `pipeline/vision.py` через `bot/handlers.py`, `cli.py` |
| `solver_model` | GigaChat-2-Max | Эталонное решение задачи (Solver), эталон перепроверяется SymPy — `bot/handlers.py`, `cli.py` |
| `tutor_model` | GigaChat-2-Pro | Структурирование транскрипции в задания (второй этап vision), классификатор ошибок, диалог тьютора, генератор похожих задач — `bot/handlers.py`, `cli.py` |
| `lite_model` | GigaChat-2 | Проверка доступа к API (`hwcheck ping`); в боте пока не используется |

Примечание: идентификаторы моделей заданы дефолтами в коде и подлежат сверке с актуальной линейкой
GigaChat при обновлении (комментарий в `config.py`).

## 4. Внешние API

| API | Хост(ы) | Назначение | Аутентификация |
|---|---|---|---|
| GigaChat API | OAuth: `ngw.devices.sberbank.ru`; данные: `gigachat.devices.sberbank.ru` | LLM- и vision-инференс (см. раздел 3) | OAuth 2.0 по `gigachat_credentials`/`gigachat_scope` (`.env`, не в git); токен на 30 минут, обновление — внутри SDK `gigachat` |
| MAX Bot API | `platform-api2.max.ru` (настраивается через `max_base_url`) | Приём апдейтов (long polling `GET /updates`), скачивание фото из сообщений, отправка сообщений и ответов на кнопки | Токен бота в заголовке `Authorization` (`max_token`, `.env`, не в git) |

TLS к `platform-api2.max.ru` требует корня НУЦ Минцифры сверх `certifi` (см. раздел 5, сертификат).

## 5. Инфраструктура и образы

| Компонент | Версия/образ | Лицензия | Назначение |
|---|---|---|---|
| python | 3.12-slim (образ `python:3.12-slim`, Docker Official Image) | составной образ (Debian + CPython); лицензия CPython — PSF License, лицензия образа отдельно не проверялась | Базовый образ контейнера бота (`Dockerfile`) |
| uv | 0.12.7 (`pip install uv==0.12.7` в `Dockerfile`) | Apache-2.0 / MIT (по данным репозитория astral-sh/uv, отдельно не проверялось через metadata) | Установка зависимостей по `uv.lock`, версия синхронизирована с локальной |
| Redis | `redis:8-alpine` (unmodified, отдельный сервис в `docker-compose.yml`) | Redis Ltd. — тройная лицензия на выбор: AGPLv3 / RSALv2 / SSPLv1 | Хранилище состояния диалога (FSM) с TTL 24 ч; контейнер без портов наружу. Образ используется немодифицированным |
| `ocrsvc/` (контейнер `homework-ocr`, профиль compose `ocr`) | `python:3.10-slim` (образ `python:3.10-slim`, Docker Official Image) + пакеты и апстрим-репозитории из таблиц ниже | составной образ (Debian + CPython); лицензия CPython — PSF License, лицензия образа отдельно не проверялась | OCR-сервис для языков (посимвольное распознавание рукописи, спецификация каркаса §8): движок `ReadingPipelineEngine` на ONNX/CPU, свой образ (`ocrsvc/Dockerfile`), лимит памяти 2 ГБ. Python 3.10, а не 3.12 — требование апстрима ai-forever |
| Docker / Docker Compose | Docker 29, Compose v5 (на VPS, см. `docs/deploy.md`) | Apache-2.0 | Сборка и запуск контейнера бота |
| GitHub Actions: `actions/checkout` | v4 | MIT (по репозиторию действия, отдельно не проверялось) | Чекаут репозитория в CI (`.github/workflows/ci.yml`) |
| GitHub Actions: `astral-sh/setup-uv` | v5 | Apache-2.0 / MIT (по репозиторию действия, отдельно не проверялось) | Установка uv и Python 3.13 в CI |
| Сертификат НУЦ Минцифры | `certs/russian_trusted_root_ca.cer` | — (государственный корневой сертификат, не программный компонент) | Доверенный корень для TLS к `platform-api2.max.ru` (подписан НУЦ Минцифры) поверх `certifi` |

### 5.1. Python-зависимости образа OCR-сервиса

Прямые зависимости `ocrsvc/requirements.txt`, версии закреплены (рецепт — спайк
`docs/research/2026-09-17-readingpipeline-memory.md`). В образ бота не попадают: это отдельный
контейнер. `torch` в образе нет — модели исполняются через ONNX Runtime/OpenVINO.

| Компонент | Версия | Лицензия | Назначение |
|---|---|---|---|
| numpy | 1.23.1 | BSD-3-Clause | Массивы изображений и тензоров во всём пайплайне |
| opencv-python-headless | 4.6.0.66 | Apache-2.0 (сборка `opencv-python` — MIT) | Чтение и геометрия изображений, перспективное выравнивание строк |
| tqdm | 4.62.3 | MPL-2.0 AND MIT | Прогресс-бары апстримного кода (в сервисе не отображаются) |
| matplotlib | 3.5.0 | PSF-подобная (Matplotlib License) | Импортируется апстримным кодом визуализации; в сервисе не рисует |
| Pillow | 8.4.0 | MIT-CMU (HPND) | Декодирование JPEG/PNG и поворот по EXIF перед распознаванием |
| pyclipper | 1.3.0 | MIT | Сжатие/расширение полигонов сегментации строк и слов |
| shapely | 1.8.0 | BSD-3-Clause | Геометрия полигонов слов (пересечения, площади) |
| onnxruntime | 1.17.3 | MIT | Исполнение ONNX-моделей сегментации и OCR на CPU |
| openvino | 2024.6.0 | Apache-2.0 | Альтернативный CPU-бэкенд инференса (используется в рецепте по памяти) |
| pandas | 1.3.4 | BSD-3-Clause | Импортируется апстримным кодом (таблицы разметки) |
| scikit-learn | 1.3.2 | BSD-3-Clause | Импортируется апстримным кодом (кластеризация строк) |
| scipy | 1.10.1 | BSD-3-Clause | Импортируется апстримным кодом (обработка изображений) |
| albumentations | 1.1.0 | MIT | Преобразования изображения на входе модели (нормализация, ресайз) |
| huggingface_hub | не закреплена (`requirements.txt` без версии) | Apache-2.0 | Скачивание весов на сборке образа (`snapshot_download`) |

## 6. Данные/датасеты

| Компонент | Лицензия | Статус |
|---|---|---|
| `ai-forever/school_notebooks_RU` (датасет, Hugging Face) | MIT | **Рассматривается, не используется.** Упомянут в `HISTORY.md` как кандидат для оценки self-hosted OCR рукописного текста; в коде проекта не подключён |
| `ai-forever/ReadingPipeline` (код пайплайна, GitHub) | MIT | **Используется.** Клонируется в образ OCR-сервиса (`ocrsvc/Dockerfile`), путь в `PYTHONPATH`; языковая модель (`ctcdecode`) отключена. Клон — `--depth 1` без пина по хэшу, целостность структуры проверяет `ocrsvc/patch_no_torch.py` (assert на сборке) |
| `ai-forever/OCR-model` (код модели распознавания, GitHub) | MIT | **Используется.** Клонируется и ставится в образ OCR-сервиса (`pip install --no-deps ./OCR-model`) |
| `ai-forever/SEGM-model` (код модели сегментации, GitHub) | MIT | **Используется.** Клонируется и ставится в образ OCR-сервиса (`pip install --no-deps ./SEGM-model`) |
| `ai-forever/ReadingPipeline-notebooks` (веса моделей, Hugging Face) | MIT | **Используется.** Веса сегментации и OCR скачиваются на сборке образа в `/app/weights` (`OCR_WEIGHTS`); `.ckpt` и `.arpa` (языковая модель) не скачиваются |

## 7. Словари и данные

Данные предметных модулей (не Python-пакеты из §1 — файлы фиксированного содержимого, читаются с
диска, `COPY assets ./assets` в `Dockerfile`).

| Компонент | Лицензия | Источник | Назначение |
|---|---|---|---|
| `ru_RU` (словарь Hunspell) | BSD-подобная (Copyright Alexander I. Lebedev; текст — `assets/hunspell/README_ru_RU.txt`) | github.com/LibreOffice/dictionaries, ветка `master`, каталог `ru_RU` | Словарные кандидаты для эталона «вставь буквы / раскрой скобки» — `subjects/russian/gaps.py` (`assets/hunspell/ru_RU.dic`, `ru_RU.aff`) |

## Как обновлять реестр

При изменении `pyproject.toml`/`uv.lock`, `ocrsvc/requirements.txt`, `ocrsvc/Dockerfile`, набора
моделей в `src/hwcheck/config.py` или сервисов в `docker-compose.yml` — обновить соответствующую
таблицу в этом файле в том же PR.
