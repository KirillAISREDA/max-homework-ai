# Спайк: OCR латиницы для английских тетрадей (Task 3)

Сравнение трёх путей распознавания рукописного английского текста учеников — тот же вопрос,
что решён для русского в `docs/research/2026-09-13-ru-handwriting-ocr.md`: чем читать «как
написано», не «исправляя» орфографию ученика. Полный отчёт — `docs/research/2026-09-17-en-handwriting-ocr.md`.

## Данные

`ai-forever/school_notebooks_EN` (HF, MIT, COCO-разметка слов). 20 листов = все 10 test + все
10 val (train держим в стороне — контрольная выборка). На лист — первые 50 слов категории
`pupil_text` в порядке чтения (бюджет CPU-инференса TrOCR, см. `task-3-brief.md`).

- `build_gold.py` — скачивает аннотации и нужные 20 фото через `huggingface_hub` (в кэш HF,
  не в репозиторий) и строит `gold.json`. Кандидаты в ошибки ученика — эвристика
  `pyspellchecker` (слово вне словаря); после автоматической разметки все 31 кандидат сверены
  визуально по кропам (контрольный лист `contact_sheet.png`, не в репозитории) — 14
  подтверждены как ошибки ученика, 17 оказались шумом разметки (см. отчёт, §caveats:
  зачёркивания, слитые в один полигон слова, перенос слова по строке, опечатки разметчика).
  Коммитится **уже curated** `gold.json` (поле `error`/`correct` после сверки, не сырой вывод
  spellchecker) — при повторном запуске `build_gold.py` эту сверку нужно повторить руками.
- `gold.json` — коммитится (без фото). `images/` — не коммитится (`.gitignore`), `build_gold.py`
  докачивает недостающие листы из `images.zip` датасета при каждом запуске.

```
uv run --project /d/Dev/mha-spike-ocr-en --with huggingface_hub --with pyspellchecker \
    python spikes/ocr_en/build_gold.py
```

## Путь (б): TrOCR по кропам слов

```
uv run --project /d/Dev/mha-spike-ocr-en --with transformers --with torch --with rapidfuzz \
    --with pillow --with datasets python spikes/ocr_en/compare.py --skip-gigachat
```

`microsoft/trocr-base-handwritten` (~1,3 ГБ, скачивается один раз в кэш HF), CPU. Кроп по
боксу разметки (плюс отступ 6 px) → `generate(max_new_tokens=16)`.

## Путь (в): две расшифровки GigaChat

```
uv run --project /d/Dev/mha-spike-ocr-en python spikes/ocr_en/compare.py --skip-trocr
```

`hwcheck.bench.client.BenchClient` поверх `hwcheck.llm.gigachat_client.GigaChatClient` —
кэш ответов (повторный прогон бесплатный), retry на 429 (PERS-тариф: 1 одновременный запрос,
тот же аккаунт использует прод-бот на VPS), лимит свежих вызовов (`--max-calls`, по умолчанию
45 — с запасом на 20 листов × 2 модели = 40). Модели — `GigaChat-2-Max` и `GigaChat-3-Ultra`
(`bench/configs/vision-3ultra.json`). На вход — не весь лист, а прямоугольник, накрывающий
размеченные 50 слов (тот же материал, что видит TrOCR по кропам) — так пути сравнимы, и меньше
токенов на вызов. Промпт — «перепиши дословно, ошибки не исправляй» (см. `TRANSCRIBE_PROMPT`
в `compare.py`). Ответ выравнивается со словами разметки через `difflib.SequenceMatcher`
(`align_words`) — GigaChat отвечает сплошным текстом, не по словам.

`.env` для кредов GigaChat — скопирован в корень воркутри вручную (не коммитится, см.
`.gitignore` репозитория). Кэш ответов — `spikes/ocr_en/.cache/gigachat/` (не коммитится).

## Путь (а): ReadingPipeline с русскими весами

**Не измерялся** в этом прогоне — см. отчёт, §caveats: полноценная установка на Windows
(клон `ai-forever/ReadingPipeline`, веса `ReadingPipeline-notebooks` ~165 МБ, обходы для
`openvino`/`ctcdecode`, наработанные в сессии 13.09) выходит за рамки «быстрого pip install»
из брифа, а контейнер для этого пути строит параллельный спайк (Task 1). `align_by_iou` в
`compare.py` — заготовка для сопоставления слов ReadingPipeline с разметкой по IoU, когда
контейнер станет доступен.

## Файлы

- `build_gold.py` — строит `gold.json` из аннотаций HF (см. выше).
- `gold.json` — 20 листов × до 50 слов: `{image, split, words: [{text, box, error, correct}]}`.
- `compare.py` — пути (б) и (в), метрики (точность по словам, CER, доля сохранённых/скрытых
  ошибок ученика, время), сигнал расхождения двух расшифровок GigaChat.
- `images/`, `.cache/`, `results/` — не коммитятся (см. `.gitignore`).
