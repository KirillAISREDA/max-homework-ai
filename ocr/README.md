# OCR-сервис «Домашки»

Отдельный контейнер (`homework-ocr`) с посимвольным распознаванием рукописи без языковой модели —
для предметов «языки» (спецификация каркаса §8). Лежит в корне репозитория, а не в `src/hwcheck`:
у движка (ReadingPipeline, этап 3) свои тяжёлые зависимости и свой образ, отдельный от бота.

## Контракт

- `GET /health` → `{"status": "ok", "engine": "<имя движка>"}`.
- `POST /recognize` (тело — байты изображения, заголовок `Content-Type: image/jpeg|png`) →
  `{"words": [{"text", "box": [x0, y0, x1, y1] | null, "confidence", "line"}], "seconds": float}`.
- Ошибка движка → HTTP 500, `{"error": "..."}`.

Реализация — стандартная библиотека Python (`http.server`): сам сервис маленький, а зависимости
движка и так тяжёлые (ReadingPipeline — onnxruntime, opencv; torch в образе нет, см. «Движок»).

## Движок

`ocr/engine.py` определяет `Engine(Protocol)` с полем `name` и методом
`recognize(image: bytes) -> list[dict]`. Выбор движка — переменная окружения `OCR_ENGINE`:

- `fake` (по умолчанию) — `FakeEngine`, отдаёт слова из `OCR_FAKE_WORDS` (JSON-список) или пустой
  список; нужен для тестов и первого запуска контейнера без реальной модели.
- `readingpipeline` — `ReadingPipelineEngine`: ReadingPipeline (ai-forever, MIT) на ONNX/CPU, без
  языковой модели и без torch (рецепт — `docs/research/2026-09-17-readingpipeline-memory.md`).
  Переменные окружения:
  - `OCR_WEIGHTS` — каталог с весами (по умолчанию `/app/weights`, наполняется в `ocr/Dockerfile`
    из `huggingface_hub`).
  - `OCR_THREADS` — число потоков onnxruntime (по умолчанию `2`; `1` не снижает память, но вдвое
    увеличивает время, см. спайк памяти).
  - `OCR_ORT_ARENA`, `OCR_ORT_MEMPATTERN` — `enable_cpu_mem_arena`/`enable_mem_pattern` у
    `onnxruntime.SessionOptions`, по умолчанию `0` (выключены): даёт пик 2,5 → 1,46 ГБ при
    побайтово том же тексте распознавания; `1` включает настройки onnxruntime по умолчанию.

## Запуск

Сервис не стартует по умолчанию — только по явному профилю compose:

```bash
docker compose --profile ocr up -d --build ocr
docker exec homework-ocr python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
```

Ограничение памяти контейнера — `2 ГБ` (спайк памяти `docs/research/2026-09-17-readingpipeline-memory.md`:
пик ReadingPipeline на реальных фото 1,46 ГБ без torch, с выключенными ареной и mem-pattern
onnxruntime; лимит — с запасом сверх измеренного пика). Портов наружу нет — бот ходит по
compose-сети как к `http://ocr:8080` (`OCR_URL`).

## Локальный запуск без Docker

```bash
OCR_ENGINE=fake OCR_FAKE_WORDS='[{"text": "cat", "box": [1, 2, 3, 4], "confidence": 0.9, "line": 0}]' \
  python -m ocr.server
```
