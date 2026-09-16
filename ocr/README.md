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
движка и так тяжёлые (ReadingPipeline — torch, onnxruntime, opencv).

## Движок

`ocr/engine.py` определяет `Engine(Protocol)` с полем `name` и методом
`recognize(image: bytes) -> list[dict]`. Выбор движка — переменная окружения `OCR_ENGINE`:

- `fake` (по умолчанию) — `FakeEngine`, отдаёт слова из `OCR_FAKE_WORDS` (JSON-список) или пустой
  список; нужен для тестов и первого запуска контейнера без реальной модели.
- `readingpipeline` — `ReadingPipelineEngine`, пока заглушка (`NotImplementedError`); подключается в
  этапе «русский» по итогам спайка `docs/research/2026-09-17-readingpipeline-vps.md`.

## Запуск

Сервис не стартует по умолчанию — только по явному профилю compose:

```bash
docker compose --profile ocr up -d --build ocr
docker exec homework-ocr python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
```

Ограничение памяти контейнера — `3g` (спайк `docs/research/2026-09-17-readingpipeline-vps.md`: пик
ReadingPipeline на реальных фото ~3 ГБ, `1.5g` из первоначального черновика недостижим). Портов
наружу нет — бот ходит по compose-сети как к `http://ocr:8080` (`OCR_URL`).

## Локальный запуск без Docker

```bash
OCR_ENGINE=fake OCR_FAKE_WORDS='[{"text": "cat", "box": [1, 2, 3, 4], "confidence": 0.9, "line": 0}]' \
  python -m ocr.server
```
