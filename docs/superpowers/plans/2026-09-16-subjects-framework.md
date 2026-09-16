# Каркас предметов, база знаний, русский и английский — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** проверка домашки по любому предмету через единый контракт модуля с силой вердикта по способу проверки; база знаний, накапливаемая от работы; к 15.10 в проде русский («вставь буквы») и английский (упражнения с однозначным ответом).

**Architecture:** предмет — пакет `src/hwcheck/subjects/<code>/`, реализующий `SubjectModule` (распознавание → эталон → проверка → тьютор); бот работает с общими типами `SubjectPage`, `Reference`, `Finding`. Математика заворачивается в контракт без изменения поведения. База знаний — таблицы в том же PostgreSQL, репозиторий с протоколом и фейком для тестов (как `ProfileRepository`). OCR — отдельный контейнер с HTTP-интерфейсом. Работа этапами: спайки → каркас → русский → английский; план следующего этапа пишется по коду предыдущего.

**Tech Stack:** Python 3.12, pydantic, asyncpg/PostgreSQL 17, Redis, GigaChat, ReadingPipeline (ai-forever, ONNX/CPU), Hunspell-словари (`spylls`), pytest + pytest-asyncio, ruff, mypy strict, Docker Compose на VPS.

**Spec:** `docs/superpowers/specs/2026-09-16-subjects-framework-design.md`

## Global Constraints

- Сила вердикта: `verified` — только детерминированное сравнение с эталоном `trust=verified`; `candidate` — расхождение OCR с эталоном, непроверенный эталон или суждение LLM; `feedback` — без вердикта. Непроверенный эталон даёт не выше `candidate`. При сомнении вердикт понижается.
- Порог открытия формы задания (флаг `available`): ложных `verified` ошибок ≤ 5 % на ≥ 10 реальных фото стенда.
- Математика переезжает в контракт **без изменения поведения**: существующие тесты (530 на 16.09) проходят без правок логики; тексты сводки ученику не меняются.
- База знаний: страницы учебников из фото учеников, наши ответы со статусом `unverified | verified | rejected`, правила, словари. Учебники целиком не парсим, ГДЗ не парсим, фото тетрадей в базе не храним.
- «Как написано» для языков — посимвольный OCR без языковой модели; LLM-vision для текста ученика не используется (исследование 13.09).
- Уточняющих вопросов ученику — не больше `MAX_QUESTIONS` (2) на домашку, как в `bot/clarify.py`.
- OCR — отдельный контейнер `homework-ocr` в compose-сети, без портов наружу, лимит памяти 1,5 ГБ; при недоступности — `candidate`/«не уверен» и событие `ocr_failed`, не падение проверки.
- VPS общий: перед рестартом — нет событий за 10 минут; Redis не перезапускать; проверять свободную память до запуска нового контейнера.
- Персональные данные: в базе знаний — только печатный текст учебника; фото учебников в `var/kb_photos` (срок дольше тетрадей, указать в политике); в событиях — хэши.
- Код: TDD, `ruff check`, `ruff format` (форматирует и python-блоки в markdown), `mypy` strict, conventional commits на русском, тексты и комментарии по-русски.

**Отступление от спецификации (сознательное):** шаг `hint(finding, level, kb) -> Hint` реализуется как `start_tutoring(finding, task, reference, kb) -> TutorSession` + существующий `tutor_reply(session, message)`: уровень подсказки живёт в `TutorSession.hint_level`, его поднимает код, ответ раскрывается на уровне 3 — семантика та же, а математический тьютор остаётся разговорным без переписывания.

## Этапы

| Этап | Что даёт | Разделы спецификации | План |
|---|---|---|---|
| 1. Спайки | решения по OCR на VPS, доле однозначных заполнений Hunspell, OCR латиницы и силе вердикта английского | §9 | ниже, подробно |
| 2. Каркас | типы контракта, математика в контракте, `Finding` в сводке, база знаний с миграцией и фейком, таблица находок и события, контейнер OCR с клиентом, кропы слов в MAX, `hwcheck kb review` | §4, §5, §8 | ниже, подробно |
| 3. Русский | модуль `russian`, Hunspell, выравнивание, правила орфограмм 2–4 класса, стенд, флаг | §6, §8 (тесты) | по коду этапа 2 |
| 4. Английский | модуль `english`, таблица глаголов, правила времён 3–6 класса, стенд, флаг | §7 | по коду этапа 3 |

Ветки: `spike/…` для этапа 1 (только `docs/research` и `spikes/`), `feat/subjects-framework` для этапа 2, по ветке на предмет.

---

# Этап 1. Спайки

Результат этапа: три отчёта в `docs/research/`, решения записаны в `docs/PROJECT_MEMORY.md` §4. Код спайков — в `spikes/` (не пакет, не в образе, не под mypy: добавить `spikes` в `exclude` ruff/mypy не нужно — mypy проверяет только `packages = ["hwcheck"]`, ruff проверяет; писать чисто).

Спайки независимы — можно вести параллельно (три субагента), результаты не зависят друг от друга.

### Task 1: Спайк — ReadingPipeline в контейнере на CPU VPS

**Files:**
- Create: `spikes/ocr_ru/Dockerfile`, `spikes/ocr_ru/run.py`, `spikes/ocr_ru/README.md`, `docs/research/2026-09-17-readingpipeline-vps.md`

**Interfaces:**
- Consumes: исследование `docs/research/2026-09-13-ru-handwriting-ocr.md` (§ReadingPipeline: репозиторий, веса `ai-forever/ReadingPipeline-notebooks`, ONNX, без LM), фото в `data/live0913/`, `data/live0914/` (реальные из MAX).
- Produces: отчёт с числами: время на страницу (p50/p95), пиковая память контейнера, размер образа; на 5–10 фото — доля слов, прочитанных дословно (ручная сверка), доля явно искажённых верных слов; вывод: лимит контейнера, таймаут клиента, «на VPS или в Cloud.ru». Формат ответа сервиса для Task 9: список слов `{text, box: [x0, y0, x1, y1], confidence, line}`.

- [ ] **Step 1: Образ**

`spikes/ocr_ru/Dockerfile`:

```dockerfile
# Спайк: ReadingPipeline (ai-forever) без языковой модели на CPU. Не для прода — только измерения.
FROM python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN git clone --depth 1 https://github.com/ai-forever/ReadingPipeline.git \
    && pip install --no-cache-dir torch==2.2.2 torchvision==0.17.2 \
       --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r ReadingPipeline/requirements.txt onnxruntime huggingface_hub
# веса ~165 МБ: сегментация + OCR (ONNX), без KenLM
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('ai-forever/ReadingPipeline-notebooks', local_dir='/app/weights')"
COPY run.py /app/run.py
ENTRYPOINT ["python", "/app/run.py"]
```

Версии torch и путь к весам сверить с `ReadingPipeline/requirements.txt` и карточкой модели на HF при сборке; расхождения записать в README спайка.

- [ ] **Step 2: Скрипт прогона**

`spikes/ocr_ru/run.py` — читает все изображения из `/data`, для каждого пишет JSON со словами и временем:

```python
"""Прогон ReadingPipeline без LM по каталогу фото: слова с координатами, время, память."""

import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/ReadingPipeline")
from ocrpipeline.predictor import PipelinePredictor  # noqa: E402

CONFIG = "/app/weights/pipeline_config.json"  # уточнить имя по содержимому local_dir


def main() -> None:
    predictor = PipelinePredictor(pipeline_config_path=CONFIG)
    out_dir = Path("/out")
    out_dir.mkdir(exist_ok=True)
    timings: list[float] = []
    for path in sorted(Path("/data").glob("*.jp*g")):
        import cv2

        image = cv2.imread(str(path))
        started = time.perf_counter()
        _rotated, pred = predictor(image)
        elapsed = time.perf_counter() - started
        timings.append(elapsed)
        words = [
            {
                "text": p["text"],
                "box": [int(v) for v in p["bbox"]] if "bbox" in p else None,
                "confidence": p.get("confidence"),
                "line": p.get("line_idx"),
            }
            for p in pred["predictions"]
            if p.get("class_name") in (None, "handwritten_text_shrinked_mask1", "text")
        ]
        (out_dir / f"{path.stem}.json").write_text(
            json.dumps({"seconds": elapsed, "words": words}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"{path.name}: {elapsed:.1f} s, {len(words)} words")
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    timings.sort()
    print(f"p50={timings[len(timings) // 2]:.1f}s p95={timings[int(len(timings) * 0.95)]:.1f}s")
    print(f"peak RSS={peak_mb:.0f} MB")


if __name__ == "__main__":
    main()
```

Ключи `pred["predictions"]` (`text`, `bbox`/`polygon`, `class_name`) сверить с `ocrpipeline/predictor.py` в клоне; поправить фильтр классов, чтобы оставались только слова.

- [ ] **Step 3: Сборка и прогон на VPS** (не на рабочем хосте Кирилла: локальный Docker не тянет образы)

```bash
# проверить память перед запуском: свободно должно быть ≥ 2.5 ГБ
ssh root@193.247.73.243 "free -m"
scp -r spikes/ocr_ru root@193.247.73.243:/opt/spike-ocr-ru
scp data/live0913/*.jpg data/live0914/*.jpg root@193.247.73.243:/opt/spike-ocr-ru/data/
ssh root@193.247.73.243 "cd /opt/spike-ocr-ru && docker build -t spike-ocr-ru . \
  && docker run --rm --memory=2g --cpus=2 -v \$PWD/data:/data -v \$PWD/out:/out spike-ocr-ru"
scp -r root@193.247.73.243:/opt/spike-ocr-ru/out spikes/ocr_ru/out
ssh root@193.247.73.243 "docker rmi spike-ocr-ru; rm -rf /opt/spike-ocr-ru"   # фото детей с VPS убрать
```

Expected: JSON на каждое фото; p50/p95 и peak RSS в выводе. `--cpus=2` — столько отдаём проду на общем хосте.

- [ ] **Step 4: Ручная сверка** — для 5 фото сравнить слова из JSON с фото: сколько слов прочитано дословно, сколько верных слов искажено, сколько ошибок ребёнка сохранено (если есть). Таблица в отчёте.

- [ ] **Step 5: Отчёт** `docs/research/2026-09-17-readingpipeline-vps.md`: выводы (первым абзацем), числа, формат слов, решения: лимит памяти контейнера, таймаут клиента (p95 × 2), «VPS или Cloud.ru», грабли сборки. Обновить `docs/PROJECT_MEMORY.md` §4 (решение) и §9 (грабли).

- [ ] **Step 6: Commit**

```bash
git add spikes/ocr_ru docs/research/2026-09-17-readingpipeline-vps.md docs/PROJECT_MEMORY.md
git commit -m "docs: спайк ReadingPipeline на CPU VPS — время, память, качество на фото MAX"
```

---

### Task 2: Спайк — заполнение пропусков по Hunspell

**Files:**
- Create: `spikes/hunspell_gaps/gaps.py`, `spikes/hunspell_gaps/exercises.json`, `docs/research/2026-09-17-hunspell-gaps.md`

**Interfaces:**
- Consumes: словарь `ru_RU` Hunspell (пакет `spylls` — чистый Python, MIT; словарь LibreOffice ru_RU, лицензия BSD/LGPL — записать в `docs/components.md` при переносе в прод).
- Produces: доля пропусков с ровно одним словарным кандидатом (по 30 упражнениям 2–4 класса), список типичных неоднозначностей (`м_шина`: машина / мишина?), решение по правилу «один кандидат → `verified`»; алгоритм `fill_gap(pattern) -> list[str]` для этапа 3.

- [ ] **Step 1: Набор упражнений** — `spikes/hunspell_gaps/exercises.json`: 30 упражнений вида «вставь буквы» из учебников 2–4 класса (Канакина, Рамзаева), набрать вручную с фото учебников из `data/` и с открытых страниц издательств; формат:

```json
[
  {"grade": 2, "source": "Канакина 2 кл. ч.1 упр. 34", "text": "На п_ляне р_сли б_рёзы.", "answer": "На поляне росли берёзы."}
]
```

- [ ] **Step 2: Скрипт**

```python
"""Спайк: сколько пропусков «вставь букву» закрывает словарь Hunspell без LLM."""

import itertools
import json
import re
from pathlib import Path

from spylls.hunspell import Dictionary

LETTERS = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
GAP = re.compile(r"[а-яё]*_+[а-яё_]*", re.IGNORECASE)


def fill_gap(word: str, dictionary: Dictionary) -> list[str]:
    """Кандидаты: словарные слова, совпадающие с шаблоном; «_» — одна буква."""
    slots = word.count("_")
    found: list[str] = []
    for letters in itertools.product(LETTERS, repeat=slots):
        candidate = word
        for letter in letters:
            candidate = candidate.replace("_", letter, 1)
        if dictionary.lookup(candidate.lower()):
            found.append(candidate)
    return found


def main() -> None:
    dictionary = Dictionary.from_files("spikes/hunspell_gaps/ru_RU")
    rows = json.loads(Path("spikes/hunspell_gaps/exercises.json").read_text(encoding="utf-8"))
    single = ambiguous = none = wrong = 0
    for row in rows:
        answers = row["answer"].split()
        for token, expected in zip(row["text"].split(), answers, strict=True):
            if "_" not in token:
                continue
            candidates = fill_gap(token.strip(".,!?"), dictionary)
            expected = expected.strip(".,!?")
            if len(candidates) == 1:
                single += 1
                if candidates[0].lower() != expected.lower():
                    wrong += 1
                    print("НЕВЕРНО:", token, candidates, expected)
            elif candidates:
                ambiguous += 1
                print("НЕОДНОЗНАЧНО:", token, candidates)
            else:
                none += 1
                print("НЕТ:", token, expected)
    total = single + ambiguous + none
    print(
        f"один кандидат: {single}/{total} (неверных {wrong}); несколько: {ambiguous}; ноль: {none}"
    )


if __name__ == "__main__":
    main()
```

Run: `uv run --with spylls python spikes/hunspell_gaps/gaps.py` (словарь `ru_RU.dic/.aff` скачать из репозитория LibreOffice dictionaries в `spikes/hunspell_gaps/`, в git не класть — `.gitignore`).

- [ ] **Step 3: Отчёт** `docs/research/2026-09-17-hunspell-gaps.md`: доли, примеры неоднозначностей (омофоны, имена, формы слова), где словаря мало (безударные окончания — кандидатов несколько, нужен контекст → LLM), решение: правило `verified` при одном кандидате и совпадении длины; иначе LLM + очередь проверки. Обновить `docs/PROJECT_MEMORY.md` §4.

- [ ] **Step 4: Commit**

```bash
git add spikes/hunspell_gaps/gaps.py spikes/hunspell_gaps/exercises.json spikes/hunspell_gaps/.gitignore docs/research/2026-09-17-hunspell-gaps.md docs/PROJECT_MEMORY.md
git commit -m "docs: спайк Hunspell — доля однозначных заполнений пропусков"
```

---

### Task 3: Спайк — OCR латиницы для английских тетрадей

**Files:**
- Create: `spikes/ocr_en/compare.py`, `spikes/ocr_en/README.md`, `docs/research/2026-09-17-en-handwriting-ocr.md`

**Interfaces:**
- Consumes: `ai-forever/school_notebooks_EN` (HF, MIT, COCO-разметка со словами), методика исследования 13.09 (доля сохранённых ошибок ученика / скрытых / CER), `hwcheck.bench.client` для двух расшифровок GigaChat (`bench/configs/`), спайк Task 1 (контейнер).
- Produces: таблица по трём путям — (а) ReadingPipeline с русскими весами, (б) TrOCR `microsoft/trocr-base-handwritten` по кропам слов из разметки, (в) GigaChat 2-Max + 3-Ultra, расхождение как сигнал; решение: путь для этапа 4 и стартовая сила вердикта английского (`verified` возможен или только `candidate`).

- [ ] **Step 1: Данные** — 20 листов из `school_notebooks_EN` (сбалансировать почерки), разметка слов из COCO → `spikes/ocr_en/gold.json`: `[{image, words: [{text, box}]}]`. Ошибки ученика в разметке уже сохранены (`colomns`).

- [ ] **Step 2: Пути (а) и (б)**

```python
"""Спайк: три пути OCR латиницы на школьных тетрадях; метрики как в исследовании 13.09."""

import json
from pathlib import Path

from PIL import Image
from rapidfuzz.distance import Levenshtein

GOLD = json.loads(Path("spikes/ocr_en/gold.json").read_text(encoding="utf-8"))


def cer(predicted: str, expected: str) -> float:
    return Levenshtein.distance(predicted, expected) / max(1, len(expected))


def trocr_words(image: Image.Image, boxes: list[list[int]]) -> list[str]:
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel

    processor = TrOCRProcessor.from_pretrained("microsoft/trocr-base-handwritten")
    model = VisionEncoderDecoderModel.from_pretrained("microsoft/trocr-base-handwritten")
    texts: list[str] = []
    for x0, y0, x1, y1 in boxes:
        pixels = processor(images=image.crop((x0, y0, x1, y1)), return_tensors="pt").pixel_values
        ids = model.generate(pixels, max_new_tokens=16)
        texts.append(processor.batch_decode(ids, skip_special_tokens=True)[0].strip())
    return texts


def score(name: str, predicted: dict[str, list[str]]) -> None:
    """predicted: image -> слова в порядке разметки. Считаем CER по словам и точность."""
    total = exact = 0
    cers: list[float] = []
    for sheet in GOLD:
        for word, guess in zip(sheet["words"], predicted[sheet["image"]], strict=True):
            total += 1
            exact += guess.lower() == word["text"].lower()
            cers.append(cer(guess.lower(), word["text"].lower()))
    print(f"{name}: точных {exact}/{total} ({exact / total:.0%}), CER {sum(cers) / len(cers):.2f}")


def main() -> None:
    trocr = {
        sheet["image"]: trocr_words(
            Image.open(f"spikes/ocr_en/images/{sheet['image']}").convert("RGB"),
            [w["box"] for w in sheet["words"]],
        )
        for sheet in GOLD
    }
    score("TrOCR-base-handwritten (кропы по разметке)", trocr)
    # (а) ReadingPipeline: прогнать контейнер из spikes/ocr_ru на images/, выровнять слова
    # по пересечению боксов с разметкой (IoU > 0.5) и передать в score() тем же форматом


if __name__ == "__main__":
    main()
```

Run: `uv run --with transformers --with torch --with rapidfuzz --with pillow python spikes/ocr_en/compare.py` (torch CPU; TrOCR-base ~1,3 ГБ, скачивается один раз). Путь (а) — контейнер из Task 1 на `spikes/ocr_en/images/`, выравнивание по IoU боксов (дописать в `compare.py`, функция `align_by_iou(pred_words, gold_words) -> list[str]`).

- [ ] **Step 3: Путь (в)** — две расшифровки GigaChat через `hwcheck.bench.client` (промпт «переписать дословно, ошибки сохранять»): для 20 листов посчитать по словам разметки долю сохранённых ошибок ученика (слова с ошибкой в разметке — есть ли они в расшифровках дословно), долю «исправленных», и точность сигнала «расхождение двух расшифровок ⇒ слово с ошибкой».

- [ ] **Step 4: Отчёт** `docs/research/2026-09-17-en-handwriting-ocr.md`: таблица трёх путей (точность по словам, CER, сохранённые/скрытые ошибки, время, память, лицензии), оговорка о датасете (IELTS-уровень, не младшая школа), решение: путь этапа 4 и стартовая сила вердикта. Если (б) TrOCR по кропам хорош, а кропы требуют сегментации — вывод «сегментация ReadingPipeline + распознавание TrOCR». Обновить `docs/PROJECT_MEMORY.md` §4, §6.

- [ ] **Step 5: Commit**

```bash
git add spikes/ocr_en/compare.py spikes/ocr_en/README.md spikes/ocr_en/.gitignore docs/research/2026-09-17-en-handwriting-ocr.md docs/PROJECT_MEMORY.md
git commit -m "docs: спайк OCR латиницы — ReadingPipeline, TrOCR, две расшифровки GigaChat"
```

---

# Этап 2. Каркас

Результат этапа: в проде — та же математика, но через `SubjectModule`; сводка ученику собирается из `Finding`; в PostgreSQL — таблицы базы знаний и находок, события `finding_created`/`reference_resolved`; контейнер `homework-ocr` с фейковым движком проходит health и отвечает клиенту; бот умеет присылать кроп слова; `hwcheck kb review` показывает очередь. Пользователь изменений не видит.

Ветка: `feat/subjects-framework`, один PR.

## Файлы этапа

| Файл | Ответственность |
|---|---|
| `src/hwcheck/subjects/__init__.py` (нов.) | пакет |
| `src/hwcheck/subjects/base.py` (нов.) | типы контракта: `Strength`, `Trust`, `Origin`, `Box`, `Word`, `SubjectTask`, `SubjectPage`, `Reference`, `Finding`, `TaskResult`, `Usage`; протоколы `SubjectModule`, `KnowledgeBase` |
| `src/hwcheck/subjects/registry.py` (нов.) | `module_for(code, deps) -> SubjectModule`, `SubjectDeps` |
| `src/hwcheck/subjects/math/__init__.py`, `module.py` (нов.) | `MathModule` над `bot/check.py`, `pipeline/*`; `findings_from_grade` |
| `src/hwcheck/bot/summary.py` (нов.) | строка сводки и кнопка «Разобрать» из `Finding` |
| `src/hwcheck/bot/fsm.py` (изм.) | `CheckedTask.findings` |
| `src/hwcheck/bot/handlers.py` (изм.) | сводка через `summary.py`; события `finding_created`, `reference_resolved`; запись находок в базу |
| `src/hwcheck/db/migrations/003_knowledge_base.sql` (нов.) | `kb_pages`, `kb_tasks`, `kb_answers`, `kb_rules`, `kb_words`, `findings` |
| `src/hwcheck/db/kb.py` (нов.) | `PgKnowledgeBase`, `fingerprint`, модели |
| `src/hwcheck/db/kb_memory.py` (нов.) | `InMemoryKnowledgeBase` |
| `src/hwcheck/db/findings.py` (нов.) | `FindingsRepository` (Pg + InMemory) |
| `src/hwcheck/ocr_client.py` (нов.) | `OcrClient`, `OcrWord`, `OcrError` |
| `ocr/` (нов.: `server.py`, `engine.py`, `Dockerfile`, `requirements.txt`) | сервис OCR: HTTP `POST /recognize`, `GET /health`; движки `FakeEngine`, заглушка `ReadingPipelineEngine` (наполняется в этапе 3) |
| `docker-compose.yml`, `.env.example`, `docs/deploy.md` (изм.) | сервис `ocr`, `OCR_URL`, runbook |
| `src/hwcheck/bot/max_api.py` (изм.) | `upload_image(image) -> str`, `send_message(..., image_token=...)` |
| `src/hwcheck/bot/clarify.py` (изм.) | вид вопроса `word` с кропом |
| `src/hwcheck/cli.py`, `src/hwcheck/kb_cli.py` (нов.) | `hwcheck kb review`, `hwcheck kb load-words` |
| `docs/components.md`, `docs/architecture.md` (изм.) | новые компоненты |
| `tests/test_subjects_base.py`, `tests/test_math_module.py`, `tests/test_summary.py`, `tests/test_kb.py`, `tests/test_findings_repo.py`, `tests/test_ocr_client.py`, `tests/test_ocr_server.py`, `tests/test_max_upload.py`, `tests/test_clarify_word.py`, `tests/test_kb_cli.py` (нов.), `tests/test_db_migrations.py` (изм.) | тесты |

Локальная база для тестов: `docker start hwcheck-test-pg`, `export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/hwcheck_test`.

---

### Task 4: Типы контракта и протоколы

**Files:**
- Create: `src/hwcheck/subjects/__init__.py`, `src/hwcheck/subjects/base.py`, `tests/test_subjects_base.py`

**Interfaces:**
- Consumes: pydantic, `hwcheck.pipeline.tutor.TutorSession` (для протокола тьютора).
- Produces (все в `hwcheck.subjects.base`):
  - `Strength = Literal["verified", "candidate", "feedback"]`, `Trust = Literal["verified", "unverified"]`, `Origin = Literal["photo", "kb", "derived"]`, `PageRole = Literal["textbook", "notebook", "unknown"]`
  - `Box(x0, y0, x1, y1: int)`, `Word(text: str, box: Box | None = None, confidence: float | None = None, line: int | None = None)`
  - `SubjectTask(number: str, number_on_page: bool = True, condition: str = "", lines: list[str] = [], answer: str | None = None, words: list[Word] = [], confidence: float = 1.0)`
  - `Usage(calls: int = 0, tokens: int = 0)`, `SubjectPage(subject: str, role: PageRole, tasks: list[SubjectTask], comment: str | None = None, transcript: str | None = None, usage: Usage = Usage())`
  - `Reference(task_number: str, origin: Origin, trust: Trust, payload: dict[str, Any] = {})`
  - `Finding(task_index: int, kind: str, strength: Strength, expected: str | None = None, actual: str | None = None, line: int | None = None, word: Word | None = None, detail: str | None = None, rule_code: str | None = None, confirmed: bool | None = None, resolved: bool = False)` + свойство `is_error: bool` (`verified` или подтверждённый `candidate`)
  - `TaskResult(task_index: int, findings: list[Finding], reference: Reference | None = None, payload: dict[str, Any] = {})` — `payload` хранит предметные данные для тьютора (у математики — `GradeResult`, эталон)
  - `KnowledgeBase(Protocol)` — методы Task 7 (`find_page`, `save_page`, `answers_for`, `save_answer`, `rule`, `words`)
  - `SubjectModule(Protocol)`: `code: str`; `async recognize(image: bytes) -> SubjectPage`; `async resolve_reference(tasks: list[SubjectTask], kb: KnowledgeBase | None) -> list[Reference]`; `async check(tasks, references) -> list[TaskResult]`; `async start_tutoring(result: TaskResult, task: SubjectTask, kb: KnowledgeBase | None) -> TutorSession`
  - `strength_of_task(findings) -> Literal["ok", "verified", "candidate", "feedback"]`

- [ ] **Step 1: Тест**

`tests/test_subjects_base.py`:

```python
"""Типы контракта предметного модуля (спецификация каркаса §4): сила вердикта по находкам."""

from hwcheck.subjects.base import Finding, SubjectTask, Word, strength_of_task


def finding(strength: str, confirmed: bool | None = None) -> Finding:
    return Finding(
        task_index=0,
        kind="spelling",
        strength=strength,
        confirmed=confirmed,  # type: ignore[arg-type]
    )


def test_task_strength_is_worst_finding() -> None:
    assert strength_of_task([]) == "ok"
    assert strength_of_task([finding("feedback")]) == "feedback"
    assert strength_of_task([finding("candidate")]) == "candidate"
    assert strength_of_task([finding("candidate"), finding("verified")]) == "verified"
    # подтверждённый учеником кандидат — ошибка; отклонённый — не считается
    assert strength_of_task([finding("candidate", confirmed=True)]) == "verified"
    assert strength_of_task([finding("candidate", confirmed=False)]) == "ok"


def test_finding_is_error() -> None:
    assert finding("verified").is_error
    assert finding("candidate", confirmed=True).is_error
    assert not finding("candidate").is_error
    assert not finding("feedback").is_error


def test_subject_task_defaults() -> None:
    task = SubjectTask(number="17", condition="803 + 169", lines=["803 + 169 = 972"])
    assert task.number_on_page and task.answer is None and task.words == []
    word = Word(text="машына", confidence=0.4)
    assert word.box is None and word.line is None
```

- [ ] **Step 2: Тест падает**

Run: `uv run pytest tests/test_subjects_base.py -v`
Expected: FAIL — `ModuleNotFoundError: hwcheck.subjects`.

- [ ] **Step 3: Реализация**

`src/hwcheck/subjects/__init__.py`:

```python
"""Предметные модули (спецификация каркаса 2026-09-16): один контракт для математики, русского,
английского. Точка входа — `registry.module_for`; здесь ничего не импортируется."""
```

`src/hwcheck/subjects/base.py`:

```python
"""Контракт предметного модуля (спецификация каркаса §3–4).

Четыре шага: распознавание → эталон → проверка → тьютор. Общие типы для бота: страница с заданиями,
эталон с происхождением и доверием, находка с силой вердикта. Сила вердикта — по способу проверки:
`verified` только при детерминированном сравнении с проверенным эталоном.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from hwcheck.pipeline.tutor import TutorSession

Strength = Literal["verified", "candidate", "feedback"]
Trust = Literal["verified", "unverified"]
Origin = Literal["photo", "kb", "derived"]
PageRole = Literal["textbook", "notebook", "unknown"]
TaskStrength = Literal["ok", "verified", "candidate", "feedback"]


class Box(BaseModel):
    x0: int
    y0: int
    x1: int
    y1: int


class Word(BaseModel):
    """Слово «как написано» с координатами на фото — для кропа в уточняющем вопросе."""

    text: str
    box: Box | None = None
    confidence: float | None = None
    line: int | None = None


class SubjectTask(BaseModel):
    number: str
    number_on_page: bool = True  # номер написан на странице, а не присвоен распознаванием
    condition: str = ""  # условие задания (с учебника или переписанное учеником)
    lines: list[str] = Field(default_factory=list)  # решение/текст ученика по строкам
    answer: str | None = None
    words: list[Word] = Field(default_factory=list)  # для языков: слова с координатами
    confidence: float = 1.0


class Usage(BaseModel):
    calls: int = 0
    tokens: int = 0


class SubjectPage(BaseModel):
    subject: str
    role: PageRole
    tasks: list[SubjectTask]
    comment: str | None = None  # почему страница непригодна
    transcript: str | None = None  # сырая транскрипция — только для dev-логов и стенда
    usage: Usage = Field(default_factory=Usage)


class Reference(BaseModel):
    task_number: str
    origin: Origin
    trust: Trust
    payload: dict[str, Any] = Field(default_factory=dict)  # предметное содержимое эталона


class Finding(BaseModel):
    task_index: int
    kind: str  # arithmetic, spelling, verb_form, missing_word, …
    strength: Strength
    expected: str | None = None
    actual: str | None = None
    line: int | None = None  # 1-based строка решения
    word: Word | None = None
    detail: str | None = None  # текст для сводки: причина «не уверен», описание
    rule_code: str | None = None  # карточка правила из базы знаний
    confirmed: bool | None = None  # ответ ученика на «здесь написано …?»
    resolved: bool = False  # разобрано с тьютором

    @property
    def is_error(self) -> bool:
        return self.strength == "verified" or (
            self.strength == "candidate" and self.confirmed is True
        )


class TaskResult(BaseModel):
    task_index: int
    findings: list[Finding]
    reference: Reference | None = None
    payload: dict[str, Any] = Field(default_factory=dict)  # для тьютора: эталон, разбор строк


def strength_of_task(findings: list[Finding]) -> TaskStrength:
    """Худшая находка задания; отклонённые учеником кандидаты не считаются."""
    live = [f for f in findings if not (f.strength == "candidate" and f.confirmed is False)]
    if any(f.is_error for f in live):
        return "verified"
    if any(f.strength == "candidate" for f in live):
        return "candidate"
    if any(f.strength == "feedback" for f in live):
        return "feedback"
    return "ok"


class KnowledgeBase(Protocol):
    """База знаний (спецификация §5); реализации — db/kb.py и db/kb_memory.py (Task 7)."""

    async def find_page(self, subject: str, text: str) -> KbPage | None: ...

    async def save_page(self, page: KbPage, tasks: list[KbTask]) -> KbPage: ...

    async def answers_for(self, task_id: int) -> list[KbAnswer]: ...

    async def save_answer(self, answer: KbAnswer) -> KbAnswer: ...

    async def rule(self, code: str) -> KbRule | None: ...

    async def words(self, subject: str, source: str) -> set[str]: ...


class SubjectModule(Protocol):
    code: str

    async def recognize(self, image: bytes) -> SubjectPage: ...

    async def resolve_reference(
        self, tasks: list[SubjectTask], kb: KnowledgeBase | None
    ) -> list[Reference]: ...

    async def check(
        self, tasks: list[SubjectTask], references: list[Reference]
    ) -> list[TaskResult]: ...

    async def start_tutoring(
        self, result: TaskResult, task: SubjectTask, kb: KnowledgeBase | None
    ) -> TutorSession: ...


from hwcheck.subjects.kb_models import KbAnswer, KbPage, KbRule, KbTask  # noqa: E402
```

Модели базы знаний живут в отдельном модуле `src/hwcheck/subjects/kb_models.py` (создать в этой задаче — протокол ссылается на них):

```python
"""Записи базы знаний (спецификация §5). Хранилище — db/kb.py; здесь только формы данных."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AnswerStatus = Literal["unverified", "verified", "rejected"]


class KbTask(BaseModel):
    id: int | None = None
    number: str | None
    condition: str
    task_kind: str  # fill_letters, expand_brackets, verb_form, choose, math


class KbPage(BaseModel):
    id: int | None = None
    subject: str
    grade: int | None = None
    fingerprint: str
    text: str
    photo_path: str | None = None
    tasks: list[KbTask] = Field(default_factory=list)


class KbAnswer(BaseModel):
    id: int | None = None
    task_id: int
    answer: dict[str, Any]
    derived_by: str  # dictionary | rule | llm:<модель>@<версия промпта>
    checked_by: str | None = None  # dictionary | rule | manual
    status: AnswerStatus = "unverified"
    reviewed_at: datetime | None = None

    @property
    def trust(self) -> Literal["verified", "unverified"]:
        deterministic = self.checked_by in ("dictionary", "rule")
        return "verified" if self.status == "verified" or deterministic else "unverified"


class KbRule(BaseModel):
    code: str
    subject: str
    grade_from: int
    title: str
    statement: str
    example: str
    finding_kinds: list[str]
```

Импорт в конце `base.py` с `noqa: E402` — чтобы `kb_models` не зависел от `base`, а протокол видел модели; при `from __future__ import annotations` аннотации протокола не вычисляются, поэтому цикла нет. Если ruff/mypy на это ругаются — перенести протокол `KnowledgeBase` в `kb_models.py` и реэкспортировать из `base.py`.

- [ ] **Step 4: Тест проходит**

Run: `uv run pytest tests/test_subjects_base.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS (3 теста).

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/subjects tests/test_subjects_base.py
git commit -m "feat: типы контракта предметного модуля и базы знаний"
```

---

### Task 5: Математика в контракте — `MathModule`

**Files:**
- Create: `src/hwcheck/subjects/math/__init__.py`, `src/hwcheck/subjects/math/module.py`, `src/hwcheck/subjects/registry.py`, `tests/test_math_module.py`

**Interfaces:**
- Consumes: `bot/check.py` (`CheckModels`, `recognize_photo`, `check_task`, `validator_only_grade`, `TaskCheck`), `pipeline/schemas.py` (`VisionTask`, `VisionPage`), `pipeline/grade.py` (`GradeResult`), `pipeline/classifier.classify_error`, `pipeline/tutor.TutorSession`, `handlers.py` (`UNCERTAIN_TEXT`, `_pseudo_ref`, `_error_line_value` — переносятся сюда), типы Task 4.
- Produces:
  - `hwcheck.subjects.math.module`: `MathModule(llm, models: CheckModels, cache: FileCache | None)`, `to_subject_task(VisionTask) -> SubjectTask`, `to_vision_task(SubjectTask) -> VisionTask`, `findings_from_grade(task_index, grade: GradeResult) -> list[Finding]`, `UNCERTAIN_TEXT`
  - `hwcheck.subjects.registry`: `SubjectDeps(llm, models, cache)`, `module_for(code: str, deps: SubjectDeps) -> SubjectModule` (`math` → `MathModule`; неизвестный код → `KeyError`)

- [ ] **Step 1: Тест**

`tests/test_math_module.py`:

```python
"""Математика через контракт предметного модуля: те же вердикты, что у bot/check.py."""

from pathlib import Path

import pytest

from hwcheck.bot.check import CheckModels, validator_only_grade
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import SubjectTask
from hwcheck.subjects.math.module import (
    MathModule,
    findings_from_grade,
    to_subject_task,
    to_vision_task,
)
from hwcheck.subjects.registry import SubjectDeps, module_for

MODELS = CheckModels(vision="v", structure="s", solver="m")


def test_vision_task_round_trip() -> None:
    task = VisionTask(
        number=17,
        task_text="803 + 169",
        student_solution_steps=["803 + 169 = 972"],
        student_answer="972",
        confidence=0.9,
        number_on_page=False,
    )
    converted = to_subject_task(task)
    assert (converted.number, converted.number_on_page) == ("17", False)
    assert converted.lines == ["803 + 169 = 972"] and converted.answer == "972"
    assert to_vision_task(converted) == task
    assert to_vision_task(SubjectTask(number="задание 3")).number == 3  # цифры из номера


def test_findings_from_grade_keep_summary_texts() -> None:
    wrong = findings_from_grade(0, validator_only_grade(["2 + 2 = 5"]))
    assert [(f.kind, f.strength, f.line) for f in wrong] == [("arithmetic", "verified", 1)]
    assert findings_from_grade(0, validator_only_grade(["2 + 2 = 4"])) == []
    [unsure] = findings_from_grade(2, validator_only_grade(["<неразборчиво>"]))
    assert (unsure.strength, unsure.task_index) == ("candidate", 2)
    assert unsure.detail == "часть записи неразборчива"  # текст сводки как в handlers.py


async def test_check_without_condition_uses_validator_only(tmp_path: Path) -> None:
    module = MathModule(llm=None, models=MODELS, cache=None)  # type: ignore[arg-type]
    task = SubjectTask(number="1", lines=["999 + 1 = 1000", "950 + 50 - 660 = 320"])
    references = await module.resolve_reference([task], kb=None)
    assert references == []  # условия нет — эталона нет, LLM не вызывается
    [result] = await module.check([task], references)
    assert [(f.kind, f.line) for f in result.findings] == [("arithmetic", 2)]
    assert result.payload["grade"]["first_error_line"] == 2
    session = await module.start_tutoring(result, task, kb=None)
    assert session.ref.answer == "340" and session.first_error_line == 2


def test_registry() -> None:
    deps = SubjectDeps(llm=None, models=MODELS, cache=None)  # type: ignore[arg-type]
    assert isinstance(module_for("math", deps), MathModule)
    with pytest.raises(KeyError):
        module_for("history", deps)
```

- [ ] **Step 2: Тест падает**

Run: `uv run pytest tests/test_math_module.py -v`
Expected: FAIL — нет `hwcheck.subjects.math`.

- [ ] **Step 3: Реализация**

`src/hwcheck/subjects/math/__init__.py` — пустой с docstring `"""Математика в контракте предметного модуля."""`.

`src/hwcheck/subjects/math/module.py`:

```python
"""Математика через контракт `SubjectModule` — обёртка над bot/check.py и pipeline/* без изменения
поведения: распознавание двухэтапным vision, эталон солвера, пересчёт SymPy, тьютор.

`GradeResult` остаётся источником вердикта; в `Finding` он переводится так, чтобы строки сводки
ученику не изменились (тесты test_bot.py).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hwcheck.bot.check import CheckModels, check_task, recognize_photo
from hwcheck.pipeline.classifier import classify_error
from hwcheck.pipeline.grade import GradeResult
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.pipeline.solver import FileCache, RefSolution, StructuredOutputError
from hwcheck.pipeline.tutor import TutorSession
from hwcheck.pipeline.vision import VisionAndChatClient
from hwcheck.subjects.base import (
    Finding,
    KnowledgeBase,
    Reference,
    SubjectPage,
    SubjectTask,
    TaskResult,
    Usage,
)

logger = logging.getLogger(__name__)

# «не уверен» без вопроса — с причиной, а не безличное «покажи взрослому» (из handlers.py)
UNCERTAIN_TEXT = {
    "unreadable": "часть записи неразборчива",
    "ambiguous_equation": "не уверен в ходе решения уравнений",
    "answer_unparseable": "не разобрал ответ",
    "no_answer": "не нашёл итоговый ответ",
    "steps_unparseable": "не смог разобрать решение",
    "column_unreadable": "не смог прочитать деление уголком",
}
_DIGITS = re.compile(r"\d+")


def to_subject_task(task: VisionTask) -> SubjectTask:
    return SubjectTask(
        number=str(task.number),
        number_on_page=task.number_on_page,
        condition=task.task_text,
        lines=list(task.student_solution_steps),
        answer=task.student_answer,
        confidence=task.confidence,
    )


def to_vision_task(task: SubjectTask) -> VisionTask:
    match = _DIGITS.search(task.number)
    return VisionTask(
        number=int(match.group()) if match else 0,
        task_text=task.condition,
        student_solution_steps=list(task.lines),
        student_answer=task.answer,
        confidence=task.confidence,
        number_on_page=task.number_on_page,
    )


def findings_from_grade(task_index: int, grade: GradeResult) -> list[Finding]:
    """`wrong` → verified ошибка в первой расходящейся строке; `uncertain` → candidate с причиной."""
    if grade.verdict == "wrong":
        line = grade.first_error_line
        expected = None
        if line is not None:
            check = grade.line_checks[line - 1]
            expected = check.values[0] if check.status == "mismatch" and check.values else None
        return [
            Finding(
                task_index=task_index,
                kind="arithmetic",
                strength="verified",
                line=line,
                expected=expected,
            )  # fmt: skip
        ]
    if grade.verdict == "uncertain":
        detail = UNCERTAIN_TEXT.get(grade.uncertain_reason or "", "не уверен в проверке")
        return [
            Finding(
                task_index=task_index,
                kind="uncertain",
                strength="candidate",
                detail=detail,
                rule_code=None,
            )  # fmt: skip
        ]
    return []


class MathModule:
    code = "math"

    def __init__(
        self, llm: VisionAndChatClient, models: CheckModels, cache: FileCache | None
    ) -> None:
        self._llm = llm
        self._models = models
        self._cache = cache

    async def recognize(self, image: bytes) -> SubjectPage:
        recognized = await recognize_photo(self._llm, image, self._models)
        page, rec = recognized.page, recognized.rec
        role = recognized.role if recognized.role in ("textbook", "notebook") else "unknown"
        return SubjectPage(
            subject=self.code,
            role=role,
            tasks=[to_subject_task(t) for t in (page.tasks if page else [])],
            comment=page.page_comment if page else None,
            transcript=rec.raw,
            usage=Usage(calls=rec.attempts + 1, tokens=rec.tokens_in + rec.tokens_out),
        )

    async def resolve_reference(
        self, tasks: list[SubjectTask], kb: KnowledgeBase | None
    ) -> list[Reference]:
        """Эталон солвера считается внутри `check_task` (кэш, самопроверка) — здесь только
        отмечаем, у каких заданий есть условие; сам эталон появится в `TaskResult.reference`."""
        return [
            Reference(task_number=t.number, origin="derived", trust="unverified", payload={})
            for t in tasks
            if t.condition.strip()
        ]

    async def check(
        self, tasks: list[SubjectTask], references: list[Reference]
    ) -> list[TaskResult]:
        results: list[TaskResult] = []
        for index, task in enumerate(tasks):
            checked = await check_task(self._llm, to_vision_task(task), self._models, self._cache)
            reference = None
            if checked.ref is not None:
                trust = "verified" if checked.ref_status == "ok" else "unverified"
                reference = Reference(
                    task_number=task.number,
                    origin="derived",
                    trust=trust,
                    payload={"ref": checked.ref.model_dump()},
                )
            payload: dict[str, Any] = {
                "grade": checked.grade.model_dump(),
                "ref_status": checked.ref_status,
                "solver_from_cache": checked.solved.from_cache if checked.solved else None,
                "solver_tokens": (
                    checked.solver_result.tokens_in + checked.solver_result.tokens_out
                    if checked.solver_result
                    else 0
                ),
            }
            results.append(
                TaskResult(
                    task_index=index,
                    findings=findings_from_grade(index, checked.grade),
                    reference=reference,
                    payload=payload,
                )
            )
        return results

    async def start_tutoring(
        self, result: TaskResult, task: SubjectTask, kb: KnowledgeBase | None
    ) -> TutorSession:
        grade = GradeResult.model_validate(result.payload["grade"])
        ref = (
            RefSolution.model_validate(result.reference.payload["ref"])
            if result.reference is not None and "ref" in result.reference.payload
            else _pseudo_ref(grade)
        )
        error = None
        if result.reference is not None and "ref" in result.reference.payload:
            try:
                error = await classify_error(
                    self._llm, task.condition, task.lines, task.answer, ref, grade,
                    model=self._models.structure,
                )  # fmt: skip
            except StructuredOutputError:
                logger.warning("classifier failed")
        return TutorSession(
            task_text=task.condition or "\n".join(task.lines),
            student_steps=task.lines,
            student_answer=task.answer,
            ref=ref,
            error=error,
            first_error_line=grade.first_error_line,
            expected=_error_line_value(grade),
        )


def _error_line_value(result: GradeResult) -> str | None:
    if result.first_error_line is None:
        return None
    check = result.line_checks[result.first_error_line - 1]
    return check.values[0] if check.status == "mismatch" and check.values else None


def _pseudo_ref(result: GradeResult) -> RefSolution:
    """Для задания без условия: «эталон» — верное значение первой ошибочной строки."""
    for check in result.line_checks:
        if check.status == "mismatch" and check.values:
            return RefSolution(steps=[], answer=check.values[0], units=None)
    return RefSolution(steps=[], answer="", units=None)
```

`classify_error` в `handlers.py` вызывается с `model=self._settings.tutor_model`; `CheckModels.structure` в раннере равен `tutor_model` (см. `Bot._models`), поэтому поведение то же. `_pseudo_ref` и `_error_line_value` в `handlers.py` остаются (их импортирует `tests/test_bot.py`) — в Task 6 `handlers.py` начнёт импортировать их отсюда.

`src/hwcheck/subjects/registry.py`:

```python
"""Реестр предметных модулей: код предмета из профиля → модуль. Новый предмет — строка здесь."""

from __future__ import annotations

from dataclasses import dataclass

from hwcheck.bot.check import CheckModels
from hwcheck.pipeline.solver import FileCache
from hwcheck.pipeline.vision import VisionAndChatClient
from hwcheck.subjects.base import SubjectModule
from hwcheck.subjects.math.module import MathModule


@dataclass(frozen=True)
class SubjectDeps:
    llm: VisionAndChatClient
    models: CheckModels
    cache: FileCache | None


def module_for(code: str, deps: SubjectDeps) -> SubjectModule:
    if code == "math":
        return MathModule(deps.llm, deps.models, deps.cache)
    raise KeyError(f"предметный модуль не реализован: {code}")
```

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest tests/test_math_module.py -q && uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS; вся прежняя выборка без изменений.

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/subjects tests/test_math_module.py
git commit -m "feat: математика через контракт предметного модуля"
```


---

### Task 6: Сводка ученику из `Finding`

**Files:**
- Create: `src/hwcheck/bot/summary.py`, `tests/test_summary.py`
- Modify: `src/hwcheck/bot/fsm.py` (`CheckedTask`), `src/hwcheck/bot/handlers.py` (`_task_line`, `_clarified_line`, `_remaining_buttons`, `_send_review`, импорты `_pseudo_ref`/`_error_line_value`/`UNCERTAIN_TEXT`)

**Interfaces:**
- Consumes: `Finding`, `strength_of_task` (Task 4); `findings_from_grade`, `UNCERTAIN_TEXT`, `_pseudo_ref`, `_error_line_value` (Task 5); `CheckedTask`, `task_label`, `callback_button`.
- Produces (`hwcheck.bot.summary`): `task_findings(index: int, item: CheckedTask) -> list[Finding]`, `task_line(index, item) -> tuple[str, list[dict[str, str]] | None]`, `clarified_line(index, item)`, `remaining_buttons(state: ChatState) -> Buttons`, `review_header(state) -> str`; `CheckedTask.findings: list[Finding]` (пусто — считать из `grade`).

- [ ] **Step 1: Тест**

`tests/test_summary.py`:

```python
"""Сводка ученику из находок: тексты те же, что были у математики (test_bot.py), плюс новые силы."""

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.fsm import ChatState, CheckedTask
from hwcheck.bot.summary import remaining_buttons, review_header, task_findings, task_line
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import Finding


def checked(number: int, steps: list[str], findings: list[Finding] | None = None) -> CheckedTask:
    task = VisionTask(number=number, task_text="", student_solution_steps=steps, confidence=1)
    return CheckedTask(
        task=task, ref=None, grade=validator_only_grade(steps), findings=findings or []
    )


def test_math_lines_unchanged() -> None:
    assert task_line(0, checked(4, ["2 + 2 = 4"])) == ("№4 — верно ✅", None)
    text, button = task_line(1, checked(7, ["2 + 2 = 5"]))
    assert text == "№7 — есть ошибка (строка 1) ❌"
    assert button == [{"type": "callback", "text": "Разобрать №7", "payload": "tutor:1"}]
    assert task_line(2, checked(9, ["<неразборчиво>"])) == (
        "№9 — часть записи неразборчива 🤔",
        None,
    )


def test_explicit_findings_override_grade() -> None:
    word = Finding(task_index=0, kind="spelling", strength="candidate", actual="машына")
    item = checked(3, ["2 + 2 = 5"], findings=[word])
    assert task_findings(0, item) == [word]
    assert task_line(0, item) == ("№3 — стоит перепроверить 🤔 (1 место)", None)
    essay = Finding(task_index=0, kind="essay", strength="feedback")
    assert task_line(0, checked(3, [], findings=[essay])) == ("№3 — разобрал, оценки нет 📝", None)
    confirmed = word.model_copy(update={"confirmed": True})
    text, button = task_line(0, checked(3, [], findings=[confirmed]))
    assert text == "№3 — есть ошибка (слово «машына») ❌" and button is not None


def test_header_and_remaining_buttons() -> None:
    state = ChatState(
        phase="review",
        tasks=[checked(4, ["2 + 2 = 5"]), checked(5, ["1 + 1 = 2"]), checked(6, ["3 + 3 = 7"])],
        resolved_indices=[0],
    )
    assert review_header(state) == "Проверил! 1 из 3 верно.\n"
    assert [b[0]["payload"] for b in remaining_buttons(state)] == ["tutor:2"]
```

- [ ] **Step 2: Тест падает**

Run: `uv run pytest tests/test_summary.py -v`
Expected: FAIL — нет `hwcheck.bot.summary`; `CheckedTask` не принимает `findings`.

- [ ] **Step 3: Реализация**

В `src/hwcheck/bot/fsm.py` — `CheckedTask`:

```python
class CheckedTask(BaseModel):
    task: VisionTask
    ref: RefSolution | None  # None — условия нет, проверка только пересчётом
    grade: GradeResult
    # находки предметного модуля (спецификация каркаса §4); пусто — вывести из grade (математика)
    findings: list[Finding] = Field(default_factory=list)
```

с импортом `from hwcheck.subjects.base import Finding` (цикла нет: `subjects.base` импортирует только `pipeline.tutor`).

`src/hwcheck/bot/summary.py`:

```python
"""Сводка ученику из находок (спецификация каркаса §8): одна форма для всех предметов.

Строка задания — по худшей находке: верно / есть ошибка / стоит перепроверить / разобрал без
оценки. Кнопка «Разобрать» — только для ошибок (verified или подтверждённый candidate).
"""

from __future__ import annotations

from hwcheck.bot.fsm import ChatState, CheckedTask
from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.pages import task_label
from hwcheck.subjects.base import Finding, strength_of_task
from hwcheck.subjects.math.module import findings_from_grade


def task_findings(index: int, item: CheckedTask) -> list[Finding]:
    return item.findings or findings_from_grade(index, item.grade)


def task_line(index: int, item: CheckedTask) -> tuple[str, list[dict[str, str]] | None]:
    label = task_label(item.task)
    findings = task_findings(index, item)
    strength = strength_of_task(findings)
    if strength == "ok":
        return f"{label} — верно ✅", None
    if strength == "verified":
        error = next(f for f in findings if f.is_error)
        button = [callback_button(f"Разобрать {lower(label)}", f"tutor:{index}")]
        return f"{label} — есть ошибка{_where(error)} ❌", button
    if strength == "candidate":
        candidates = [f for f in findings if f.strength == "candidate" and f.confirmed is None]
        if len(candidates) == 1 and candidates[0].detail:
            return f"{label} — {candidates[0].detail} 🤔", None
        return f"{label} — стоит перепроверить 🤔 ({_places(len(candidates))})", None
    return f"{label} — разобрал, оценки нет 📝", None


def clarified_line(index: int, item: CheckedTask) -> tuple[str, list[dict[str, str]] | None]:
    """Итог после ответа ученика: «не уверен» здесь — ответ понят, но проверка не сошлась."""
    if strength_of_task(task_findings(index, item)) == "candidate":
        return f"{task_label(item.task)} — спасибо, но и так не получилось проверить 🤔", None
    return task_line(index, item)


def review_header(state: ChatState) -> str:
    correct = sum(
        1 for i, t in enumerate(state.tasks) if strength_of_task(task_findings(i, t)) == "ok"
    )
    return f"Проверил! {correct} из {len(state.tasks)} верно.\n"


def remaining_buttons(state: ChatState) -> Buttons:
    """Кнопки для ещё не разобранных ошибок."""
    return [
        [callback_button(f"Разобрать {lower(task_label(t.task))}", f"tutor:{i}")]
        for i, t in enumerate(state.tasks)
        if strength_of_task(task_findings(i, t)) == "verified" and i not in state.resolved_indices
    ]


def lower(label: str) -> str:
    """«Задание 1» посреди фразы: «Разобрать задание 1»; «№19» не меняется."""
    return label[:1].lower() + label[1:]


def _where(finding: Finding) -> str:
    if finding.line is not None and finding.kind == "arithmetic":
        return f" (строка {finding.line})"
    if finding.actual:
        return f" (слово «{finding.actual}»)"
    return ""


def _places(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} место"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} места"
    return f"{n} мест"
```

В `src/hwcheck/bot/handlers.py`:
- удалить `UNCERTAIN_TEXT`, `_task_line`, `_clarified_line`, `_remaining_buttons`, `_error_line_value`, `_pseudo_ref`, `_lower`; добавить импорты `from hwcheck.bot.summary import clarified_line, lower as _lower, remaining_buttons as _remaining_buttons, review_header, task_line` и `from hwcheck.subjects.math.module import _error_line_value, _pseudo_ref  # noqa: F401 — совместимость тестов` (тесты `test_bot.py` импортируют `_pseudo_ref`, `_remaining_buttons` из `handlers`);
- `_send_review`: `header = review_header(state)`, строки через `task_line(i, item)`;
- `_answer_clarification`: `message, button = clarified_line(clarification.task_index, updated)`;
- `_start_tutoring` остаётся (математика здесь пока вызывается напрямую; переход на `MathModule.start_tutoring` — Task 8), но использует импортированные `_pseudo_ref`, `_error_line_value`.

- [ ] **Step 4: Тесты проходят, старые не изменились**

Run: `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS, включая `tests/test_bot.py::test_remaining_buttons_exclude_resolved` и `test_text_after_review_points_to_buttons_not_welcome` без правок.

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/bot/summary.py src/hwcheck/bot/fsm.py src/hwcheck/bot/handlers.py tests/test_summary.py
git commit -m "feat: сводка ученику из находок предметного модуля"
```

---

### Task 7: База знаний — миграция и репозиторий

**Files:**
- Create: `src/hwcheck/db/migrations/003_knowledge_base.sql`, `src/hwcheck/db/kb.py`, `src/hwcheck/db/kb_memory.py`, `tests/test_kb.py`
- Modify: `tests/test_db_migrations.py` (список миграций и таблиц)

**Interfaces:**
- Consumes: `KbPage`, `KbTask`, `KbAnswer`, `KbRule` (Task 4), `create_pool`, паттерн `PgProfileRepository` (`db/repo.py`).
- Produces: `hwcheck.db.kb`: `fingerprint(text: str) -> str` (sha256 нормализованного текста: нижний регистр, `ё→е`, только буквы/цифры и пробелы, схлопнутые пробелы), `PgKnowledgeBase(pool)`; `hwcheck.db.kb_memory.InMemoryKnowledgeBase()`; оба реализуют `KnowledgeBase`: `find_page(subject, text)`, `save_page(page, tasks) -> KbPage` (с id; повторный `fingerprint` — вернуть существующую, задания не дублировать), `answers_for(task_id)`, `save_answer(answer) -> KbAnswer`, `rule(code)`, `words(subject, source) -> set[str]`; плюс `add_rule(rule)`, `add_words(subject, source, words: dict[str, dict | None])`, `unverified_answers(subject, limit) -> list[tuple[KbTask, KbAnswer]]`, `set_answer_status(answer_id, status, checked_by)` для `hwcheck kb review`.

- [ ] **Step 1: Миграция**

`src/hwcheck/db/migrations/003_knowledge_base.sql`:

```sql
-- База знаний по предметам и находки проверки (спецификация каркаса 2026-09-16, §5, §8).
-- В базе — только печатный текст учебника и наши ответы; фото тетрадей и персональных данных нет.

CREATE TABLE kb_pages (
  id            bigserial PRIMARY KEY,
  subject       text NOT NULL,
  grade         smallint CHECK (grade BETWEEN 1 AND 9),
  fingerprint   text NOT NULL,
  text          text NOT NULL,
  photo_path    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (subject, fingerprint)
);

CREATE TABLE kb_tasks (
  id            bigserial PRIMARY KEY,
  page_id       bigint NOT NULL REFERENCES kb_pages ON DELETE CASCADE,
  number        text,
  condition     text NOT NULL,
  task_kind     text NOT NULL
);
CREATE INDEX kb_tasks_page ON kb_tasks (page_id);

CREATE TABLE kb_answers (
  id            bigserial PRIMARY KEY,
  task_id       bigint NOT NULL REFERENCES kb_tasks ON DELETE CASCADE,
  answer        jsonb NOT NULL,
  derived_by    text NOT NULL,
  checked_by    text,
  status        text NOT NULL DEFAULT 'unverified'
                CHECK (status IN ('unverified', 'verified', 'rejected')),
  reviewed_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX kb_answers_task ON kb_answers (task_id);
CREATE INDEX kb_answers_review ON kb_answers (status) WHERE status = 'unverified';

CREATE TABLE kb_rules (
  code          text PRIMARY KEY,
  subject       text NOT NULL,
  grade_from    smallint NOT NULL,
  title         text NOT NULL,
  statement     text NOT NULL,
  example       text NOT NULL,
  finding_kinds text[] NOT NULL
);

CREATE TABLE kb_words (
  subject       text NOT NULL,
  word          text NOT NULL,
  source        text NOT NULL,
  attrs         jsonb,
  PRIMARY KEY (subject, word, source)
);
CREATE INDEX kb_words_source ON kb_words (subject, source);

-- находки проверки: основа аналитики качества и модели ученика; homework_id — этап 4 онбординга
CREATE TABLE findings (
  id            bigserial PRIMARY KEY,
  user_hash     text NOT NULL,
  subject       text NOT NULL,
  trace_id      text,
  task_number   text,
  kind          text NOT NULL,
  strength      text NOT NULL CHECK (strength IN ('verified', 'candidate', 'feedback')),
  rule_code     text,
  confirmed     boolean,
  resolved      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX findings_user_time ON findings (user_hash, created_at);
```

В `tests/test_db_migrations.py`: список миграций `["001_onboarding.sql", "002_children.sql", "003_knowledge_base.sql"]`, `TABLES` + `kb_pages, kb_tasks, kb_answers, kb_rules, kb_words, findings`, `test_create_pool_applies_migrations` ожидает 3.

- [ ] **Step 2: Контрактные тесты**

`tests/test_kb.py`:

```python
"""Контракт базы знаний (спецификация каркаса §5) — одни тесты для памяти и PostgreSQL."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from hwcheck.db.kb import PgKnowledgeBase, fingerprint
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.db.pool import create_pool
from hwcheck.subjects.kb_models import KbAnswer, KbPage, KbRule, KbTask

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
KnowledgeBaseImpl = PgKnowledgeBase | InMemoryKnowledgeBase


@pytest.fixture(params=["memory", "postgres"])
async def kb(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeBaseImpl]:
    if request.param == "memory":
        yield InMemoryKnowledgeBase()
        return
    if TEST_DATABASE_URL is None:
        if "CI" in os.environ:
            pytest.fail("в CI нужен TEST_DATABASE_URL")
        pytest.skip("нужен TEST_DATABASE_URL (PostgreSQL)")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await create_pool(TEST_DATABASE_URL, server_settings={"search_path": schema})
        try:
            yield PgKnowledgeBase(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await admin.close()


def page(text: str = "Упр. 34. На п_ляне р_сли б_рёзы.") -> KbPage:
    return KbPage(subject="russian", grade=2, fingerprint=fingerprint(text), text=text)


def test_fingerprint_ignores_layout_and_yo() -> None:
    assert fingerprint("На  поляне\nрОсли берёзы.") == fingerprint("на поляне росли березы")
    assert fingerprint("803 + 169") != fingerprint("803 + 196")


async def test_page_saved_once_and_found_by_text(kb: KnowledgeBaseImpl) -> None:
    tasks = [KbTask(number="34", condition="На п_ляне р_сли б_рёзы.", task_kind="fill_letters")]
    saved = await kb.save_page(page(), tasks)
    assert saved.id is not None and [t.id for t in saved.tasks] != [None]
    again = await kb.save_page(page(), tasks)  # то же фото от другого ученика
    assert again.id == saved.id and len(again.tasks) == 1
    found = await kb.find_page("russian", "  на поляне  росли берёзы. упр. 34")
    assert found is None  # другой порядок слов — другая страница
    found = await kb.find_page("russian", "Упр. 34. На п_ляне р_сли б_рёзы.")
    assert found is not None and found.tasks[0].number == "34"
    assert await kb.find_page("english", "Упр. 34. На п_ляне р_сли б_рёзы.") is None


async def test_answers_trust_and_review_queue(kb: KnowledgeBaseImpl) -> None:
    saved = await kb.save_page(
        page(), [KbTask(number="34", condition="x", task_kind="fill_letters")]
    )
    task_id = saved.tasks[0].id
    assert task_id is not None
    by_dict = await kb.save_answer(
        KbAnswer(
            task_id=task_id,
            answer={"text": "поляне"},
            derived_by="dictionary",
            checked_by="dictionary",
        )  # fmt: skip
    )
    by_llm = await kb.save_answer(
        KbAnswer(task_id=task_id, answer={"text": "росли"}, derived_by="llm:GigaChat-2-Pro@v1")
    )
    assert by_dict.trust == "verified" and by_llm.trust == "unverified"
    assert [a.id for a in await kb.answers_for(task_id)] == [by_dict.id, by_llm.id]
    queue = await kb.unverified_answers("russian", limit=10)
    assert [(t.number, a.id) for t, a in queue] == [("34", by_llm.id)]
    assert by_llm.id is not None
    await kb.set_answer_status(by_llm.id, "verified", checked_by="manual")
    [_, reviewed] = await kb.answers_for(task_id)
    assert reviewed.status == "verified" and reviewed.reviewed_at is not None
    assert await kb.unverified_answers("russian", limit=10) == []


async def test_rules_and_words(kb: KnowledgeBaseImpl) -> None:
    rule = KbRule(
        code="ru.orth.unstressed_vowel", subject="russian", grade_from=2,
        title="Безударная гласная в корне", statement="Подбери проверочное слово.",
        example="п_ляне — по́ле → поляне", finding_kinds=["spelling"],
    )  # fmt: skip
    await kb.add_rule(rule)
    await kb.add_rule(rule)  # повтор не падает и не дублирует
    assert await kb.rule("ru.orth.unstressed_vowel") == rule
    assert await kb.rule("nope") is None
    await kb.add_words(
        "english", "irregular_verbs", {"go": {"past": "went"}, "see": {"past": "saw"}}
    )
    await kb.add_words("english", "irregular_verbs", {"go": {"past": "went"}})
    assert await kb.words("english", "irregular_verbs") == {"go", "see"}
    assert await kb.words("english", "grade_list:3") == set()
```

- [ ] **Step 3: Тесты падают**

Run: `uv run pytest tests/test_kb.py tests/test_db_migrations.py -v`
Expected: FAIL — нет `hwcheck.db.kb`; список миграций не совпадает.

- [ ] **Step 4: Реализация в памяти**

`src/hwcheck/db/kb_memory.py`:

```python
"""База знаний в памяти — для тестов модулей и бота; контракт тот же, что у PgKnowledgeBase."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hwcheck.db.kb import fingerprint
from hwcheck.subjects.kb_models import AnswerStatus, KbAnswer, KbPage, KbRule, KbTask


class InMemoryKnowledgeBase:
    def __init__(self) -> None:
        self._pages: dict[int, KbPage] = {}
        self._answers: dict[int, KbAnswer] = {}
        self._rules: dict[str, KbRule] = {}
        self._words: dict[tuple[str, str], dict[str, dict[str, Any] | None]] = {}
        self._last_id = 0

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    async def find_page(self, subject: str, text: str) -> KbPage | None:
        key = fingerprint(text)
        return next(
            (p for p in self._pages.values() if p.subject == subject and p.fingerprint == key),
            None,
        )

    async def save_page(self, page: KbPage, tasks: list[KbTask]) -> KbPage:
        existing = await self.find_page(page.subject, page.text)
        if existing is not None:
            return existing
        saved_tasks = [t.model_copy(update={"id": self._next_id()}) for t in tasks]
        saved = page.model_copy(update={"id": self._next_id(), "tasks": saved_tasks})
        assert saved.id is not None
        self._pages[saved.id] = saved
        return saved

    async def answers_for(self, task_id: int) -> list[KbAnswer]:
        return sorted(
            (a for a in self._answers.values() if a.task_id == task_id), key=lambda a: a.id or 0
        )

    async def save_answer(self, answer: KbAnswer) -> KbAnswer:
        saved = answer.model_copy(update={"id": self._next_id()})
        assert saved.id is not None
        self._answers[saved.id] = saved
        return saved

    async def unverified_answers(self, subject: str, limit: int) -> list[tuple[KbTask, KbAnswer]]:
        tasks = {t.id: t for p in self._pages.values() if p.subject == subject for t in p.tasks}
        queue = [
            (tasks[a.task_id], a)
            for a in sorted(self._answers.values(), key=lambda a: a.id or 0)
            if a.status == "unverified" and a.task_id in tasks
        ]
        return queue[:limit]

    async def set_answer_status(
        self, answer_id: int, status: AnswerStatus, checked_by: str | None
    ) -> None:
        answer = self._answers[answer_id]
        self._answers[answer_id] = answer.model_copy(
            update={"status": status, "checked_by": checked_by, "reviewed_at": datetime.now(UTC)}
        )

    async def rule(self, code: str) -> KbRule | None:
        return self._rules.get(code)

    async def add_rule(self, rule: KbRule) -> None:
        self._rules[rule.code] = rule

    async def words(self, subject: str, source: str) -> set[str]:
        return set(self._words.get((subject, source), {}))

    async def add_words(
        self, subject: str, source: str, words: dict[str, dict[str, Any] | None]
    ) -> None:
        self._words.setdefault((subject, source), {}).update(words)
```

- [ ] **Step 5: PostgreSQL**

`src/hwcheck/db/kb.py`:

```python
"""База знаний в PostgreSQL (спецификация каркаса §5): страницы учебников, задания, наши ответы,
правила, словари. Поиск страницы — по отпечатку нормализованного текста."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import asyncpg

from hwcheck.subjects.kb_models import AnswerStatus, KbAnswer, KbPage, KbRule, KbTask

_KEEP = re.compile(r"[^0-9a-zа-я ]+")


def fingerprint(text: str) -> str:
    """Один и тот же учебный текст с разных фото даёт один отпечаток: регистр, ё, пунктуация и
    переносы не важны; порядок слов — важен."""
    normalized = " ".join(_KEEP.sub(" ", text.lower().replace("ё", "е")).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _task(row: asyncpg.Record) -> KbTask:
    return KbTask(
        id=row["id"], number=row["number"], condition=row["condition"], task_kind=row["task_kind"]
    )


def _answer(row: asyncpg.Record) -> KbAnswer:
    return KbAnswer(
        id=row["id"],
        task_id=row["task_id"],
        answer=json.loads(row["answer"]),
        derived_by=row["derived_by"],
        checked_by=row["checked_by"],
        status=row["status"],
        reviewed_at=row["reviewed_at"],
    )


class PgKnowledgeBase:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def find_page(self, subject: str, text: str) -> KbPage | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM kb_pages WHERE subject = $1 AND fingerprint = $2",
            subject,
            fingerprint(text),
        )
        if row is None:
            return None
        tasks = await self._pool.fetch(
            "SELECT * FROM kb_tasks WHERE page_id = $1 ORDER BY id", row["id"]
        )
        return KbPage(
            id=row["id"],
            subject=row["subject"],
            grade=row["grade"],
            fingerprint=row["fingerprint"],
            text=row["text"],
            photo_path=row["photo_path"],
            tasks=[_task(t) for t in tasks],
        )

    async def save_page(self, page: KbPage, tasks: list[KbTask]) -> KbPage:
        async with self._pool.acquire() as conn, conn.transaction():
            page_id = await conn.fetchval(
                "INSERT INTO kb_pages (subject, grade, fingerprint, text, photo_path) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (subject, fingerprint) DO NOTHING "
                "RETURNING id",
                page.subject,
                page.grade,
                page.fingerprint,
                page.text,
                page.photo_path,
            )
            if page_id is None:
                existing = await self.find_page(page.subject, page.text)
                assert existing is not None
                return existing
            saved: list[KbTask] = []
            for task in tasks:
                task_id = await conn.fetchval(
                    "INSERT INTO kb_tasks (page_id, number, condition, task_kind) "
                    "VALUES ($1, $2, $3, $4) RETURNING id",
                    page_id,
                    task.number,
                    task.condition,
                    task.task_kind,
                )
                saved.append(task.model_copy(update={"id": task_id}))
            return page.model_copy(update={"id": page_id, "tasks": saved})

    async def answers_for(self, task_id: int) -> list[KbAnswer]:
        rows = await self._pool.fetch(
            "SELECT * FROM kb_answers WHERE task_id = $1 ORDER BY id", task_id
        )
        return [_answer(r) for r in rows]

    async def save_answer(self, answer: KbAnswer) -> KbAnswer:
        answer_id = await self._pool.fetchval(
            "INSERT INTO kb_answers (task_id, answer, derived_by, checked_by, status) "
            "VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING id",
            answer.task_id,
            json.dumps(answer.answer, ensure_ascii=False),
            answer.derived_by,
            answer.checked_by,
            answer.status,
        )
        return answer.model_copy(update={"id": answer_id})

    async def unverified_answers(self, subject: str, limit: int) -> list[tuple[KbTask, KbAnswer]]:
        rows = await self._pool.fetch(
            "SELECT a.*, t.id AS t_id, t.number AS t_number, t.condition AS t_condition, "
            "t.task_kind AS t_task_kind FROM kb_answers a "
            "JOIN kb_tasks t ON t.id = a.task_id JOIN kb_pages p ON p.id = t.page_id "
            "WHERE a.status = 'unverified' AND p.subject = $1 ORDER BY a.id LIMIT $2",
            subject,
            limit,
        )
        return [
            (
                KbTask(
                    id=r["t_id"],
                    number=r["t_number"],
                    condition=r["t_condition"],
                    task_kind=r["t_task_kind"],
                ),  # fmt: skip
                _answer(r),
            )
            for r in rows
        ]

    async def set_answer_status(
        self, answer_id: int, status: AnswerStatus, checked_by: str | None
    ) -> None:
        await self._pool.execute(
            "UPDATE kb_answers SET status = $2, checked_by = $3, reviewed_at = $4 WHERE id = $1",
            answer_id,
            status,
            checked_by,
            datetime.now(UTC),
        )

    async def rule(self, code: str) -> KbRule | None:
        row = await self._pool.fetchrow("SELECT * FROM kb_rules WHERE code = $1", code)
        if row is None:
            return None
        return KbRule(
            code=row["code"],
            subject=row["subject"],
            grade_from=row["grade_from"],
            title=row["title"],
            statement=row["statement"],
            example=row["example"],
            finding_kinds=list(row["finding_kinds"]),
        )

    async def add_rule(self, rule: KbRule) -> None:
        await self._pool.execute(
            "INSERT INTO kb_rules (code, subject, grade_from, title, statement, example, "
            "finding_kinds) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (code) DO UPDATE SET "
            "subject = EXCLUDED.subject, grade_from = EXCLUDED.grade_from, title = EXCLUDED.title, "
            "statement = EXCLUDED.statement, example = EXCLUDED.example, "
            "finding_kinds = EXCLUDED.finding_kinds",
            rule.code,
            rule.subject,
            rule.grade_from,
            rule.title,
            rule.statement,
            rule.example,
            rule.finding_kinds,
        )

    async def words(self, subject: str, source: str) -> set[str]:
        rows = await self._pool.fetch(
            "SELECT word FROM kb_words WHERE subject = $1 AND source = $2", subject, source
        )
        return {r["word"] for r in rows}

    async def add_words(
        self, subject: str, source: str, words: dict[str, dict[str, Any] | None]
    ) -> None:
        await self._pool.executemany(
            "INSERT INTO kb_words (subject, word, source, attrs) VALUES ($1, $2, $3, $4::jsonb) "
            "ON CONFLICT (subject, word, source) DO UPDATE SET attrs = EXCLUDED.attrs",
            [
                (subject, word, source, json.dumps(attrs) if attrs is not None else None)
                for word, attrs in words.items()
            ],
        )
```

- [ ] **Step 6: Тесты проходят**

Run: `uv run pytest tests/test_kb.py tests/test_db_migrations.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS (4 × memory + 4 × postgres + миграции).

- [ ] **Step 7: Commit**

```bash
git add src/hwcheck/db/migrations/003_knowledge_base.sql src/hwcheck/db/kb.py src/hwcheck/db/kb_memory.py tests/test_kb.py tests/test_db_migrations.py
git commit -m "feat: база знаний — миграция, репозиторий PostgreSQL и в памяти"
```

---

### Task 8: Находки в базе и событиях; бот проверяет через модуль

**Files:**
- Create: `src/hwcheck/db/findings.py`, `tests/test_findings_repo.py`
- Modify: `src/hwcheck/bot/handlers.py` (`_check_task`, `_start_tutoring`, конструктор), `src/hwcheck/bot/runner.py`, `tests/test_bot.py` (только фабрика `make_bot`, если нужен новый аргумент — нет: аргументы необязательные)

**Interfaces:**
- Consumes: `Finding`, `TaskResult`, `SubjectModule`, `module_for`, `SubjectDeps` (Task 4–5); `to_subject_task`; таблица `findings` (Task 7); `EventLog`; `trace()` (`hwcheck.events._trace_id` — добавить публичный `current_trace_id() -> str | None`).
- Produces: `hwcheck.db.findings`: `FindingRecord(user_hash, subject, trace_id, task_number, kind, strength, rule_code, confirmed, resolved)`, `FindingsRepository(Protocol)` с `save(records) -> None`, `count_by_strength(user_hash, since) -> dict[str, int]`; `PgFindingsRepository(pool)`, `InMemoryFindingsRepository()` (поле `saved: list[FindingRecord]`); `Bot(..., subjects: SubjectDeps | None = None, findings: FindingsRepository | None = None)`; события `finding_created{subject, kind, strength, rule_code}`, `reference_resolved{subject, origin, trust}`; `_check_task` идёт через `MathModule.check`, `_start_tutoring` — через `MathModule.start_tutoring`.

- [ ] **Step 1: Тест репозитория находок**

`tests/test_findings_repo.py` — та же фикстура `params=["memory", "postgres"]`, что в `tests/test_kb.py` (скопировать целиком, репозиторий `PgFindingsRepository(pool)` / `InMemoryFindingsRepository()`):

```python
async def test_save_and_count(repo: FindingsImpl) -> None:
    now = datetime.now(UTC)
    await repo.save(
        [
            FindingRecord(
                user_hash="u1",
                subject="math",
                trace_id="t1",
                task_number="17",
                kind="arithmetic",
                strength="verified",
            ),
            FindingRecord(
                user_hash="u1",
                subject="math",
                trace_id="t1",
                task_number="18",
                kind="uncertain",
                strength="candidate",
            ),
            FindingRecord(
                user_hash="u2",
                subject="math",
                trace_id="t2",
                task_number="1",
                kind="arithmetic",
                strength="verified",
            ),
        ]  # fmt: skip
    )
    assert await repo.count_by_strength("u1", since=now - timedelta(days=1)) == {
        "verified": 1,
        "candidate": 1,
    }
    assert await repo.count_by_strength("u1", since=now + timedelta(days=1)) == {}
    await repo.save([])  # пустой список — без обращения к базе и без ошибки
```

- [ ] **Step 2: Тест бота** — добавить в `tests/test_bot.py`:

```python
async def test_findings_logged_and_saved(tmp_path: Path) -> None:
    """Каждая находка — событие finding_created и запись в репозитории; всё в одном trace_id."""
    from hwcheck.bot.fsm import ChatState
    from hwcheck.db.findings import InMemoryFindingsRepository
    from hwcheck.pipeline.schemas import VisionTask

    bot, fake_max, events_path = make_bot(tmp_path)
    findings = InMemoryFindingsRepository()
    bot._findings = findings
    task = VisionTask(number=7, task_text="", student_solution_steps=["2 + 2 = 5"], confidence=1)
    checked = await bot._check_task(42, task)
    assert checked.grade.verdict == "wrong" and checked.findings[0].strength == "verified"
    [record] = findings.saved
    assert (record.user_hash, record.task_number, record.kind) == (anonymize(42), "7", "arithmetic")
    created = [e for e in read_events(events_path) if e["type"] == "finding_created"]
    assert [(e["subject"], e["strength"]) for e in created] == [("math", "verified")]
    assert record.trace_id is None  # trace_id есть только внутри handle_update
    await bot._store.set(7, ChatState(phase="review", tasks=[checked]))
```

- [ ] **Step 3: Тесты падают**

Run: `uv run pytest tests/test_findings_repo.py tests/test_bot.py::test_findings_logged_and_saved -v`
Expected: FAIL — нет `hwcheck.db.findings`.

- [ ] **Step 4: Репозиторий находок**

`src/hwcheck/db/findings.py`:

```python
"""Находки проверки в PostgreSQL (спецификация каркаса §8): аналитика качества по предметам и
основа модели ученика. Персональных данных нет — только хэш пользователя и коды."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import asyncpg
from pydantic import BaseModel

from hwcheck.subjects.base import Strength


class FindingRecord(BaseModel):
    user_hash: str
    subject: str
    trace_id: str | None
    task_number: str | None
    kind: str
    strength: Strength
    rule_code: str | None = None
    confirmed: bool | None = None
    resolved: bool = False
    created_at: datetime | None = None


class FindingsRepository(Protocol):
    async def save(self, records: list[FindingRecord]) -> None: ...

    async def count_by_strength(self, user_hash: str, since: datetime) -> dict[str, int]: ...


class InMemoryFindingsRepository:
    def __init__(self) -> None:
        self.saved: list[FindingRecord] = []

    async def save(self, records: list[FindingRecord]) -> None:
        from datetime import UTC

        now = datetime.now(UTC)
        self.saved.extend(r.model_copy(update={"created_at": now}) for r in records)

    async def count_by_strength(self, user_hash: str, since: datetime) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.saved:
            if r.user_hash == user_hash and r.created_at is not None and r.created_at >= since:
                counts[r.strength] = counts.get(r.strength, 0) + 1
        return counts


class PgFindingsRepository:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def save(self, records: list[FindingRecord]) -> None:
        if not records:
            return
        await self._pool.executemany(
            "INSERT INTO findings (user_hash, subject, trace_id, task_number, kind, strength, "
            "rule_code, confirmed, resolved) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            [
                (
                    r.user_hash,
                    r.subject,
                    r.trace_id,
                    r.task_number,
                    r.kind,
                    r.strength,
                    r.rule_code,
                    r.confirmed,
                    r.resolved,
                )  # fmt: skip
                for r in records
            ],
        )

    async def count_by_strength(self, user_hash: str, since: datetime) -> dict[str, int]:
        rows = await self._pool.fetch(
            "SELECT strength, count(*) AS n FROM findings WHERE user_hash = $1 "
            "AND created_at >= $2 GROUP BY strength",
            user_hash,
            since,
        )
        return {r["strength"]: r["n"] for r in rows}
```

- [ ] **Step 5: Бот через модуль**

В `src/hwcheck/events.py` добавить:

```python
def current_trace_id() -> str | None:
    return _trace_id.get()
```

В `src/hwcheck/bot/handlers.py`:
- конструктор: `subjects: SubjectDeps | None = None, findings: FindingsRepository | None = None`; поле `self._findings = findings`; `self._module = MathModule(llm, self._models, self._cache)` (через `module_for("math", subjects or SubjectDeps(llm, self._models, self._cache))`);
- `_check_task(user_id, task)`:

```python
async def _check_task(self, user_id: int | None, task: VisionTask) -> CheckedTask:
    subject_task = to_subject_task(task)
    [result] = await self._module.check([subject_task], [])
    payload = result.payload
    if payload.get("solver_from_cache") is not None:
        self._events.log(
            "solver_call",
            user_id=user_id,
            component="solver",
            from_cache=payload["solver_from_cache"],
            tokens=payload["solver_tokens"],
        )
    grade = GradeResult.model_validate(payload["grade"])
    ref = (
        RefSolution.model_validate(result.reference.payload["ref"])
        if result.reference is not None and "ref" in result.reference.payload
        else None
    )
    if result.reference is not None:
        self._events.log(
            "reference_resolved",
            user_id=user_id,
            subject=self._module.code,
            origin=result.reference.origin,
            trust=result.reference.trust,
        )
    self._events.log(
        "task_checked",
        user_id=user_id,
        component="validator",
        verdict=grade.verdict,
        reason=grade.uncertain_reason,
        ref_status=payload["ref_status"],
        n_steps=len(task.student_solution_steps),
        n_parsed=sum(1 for c in grade.line_checks if c.status in ("ok", "mismatch")),
        has_answer=bool((task.student_answer or "").strip()),
    )
    await self._record_findings(user_id, subject_task, result.findings)
    return CheckedTask(task=task, ref=ref, grade=grade, findings=result.findings)


async def _record_findings(
    self, user_id: int | None, task: SubjectTask, findings: list[Finding]
) -> None:
    for finding in findings:
        self._events.log(
            "finding_created",
            user_id=user_id,
            subject=self._module.code,
            kind=finding.kind,
            strength=finding.strength,
            rule_code=finding.rule_code,
        )
    user_hash = anonymize(user_id)
    if self._findings is None or user_hash is None or not findings:
        return
    records = [
        FindingRecord(
            user_hash=user_hash,
            subject=self._module.code,
            trace_id=current_trace_id(),
            task_number=task.number,
            kind=f.kind,
            strength=f.strength,
            rule_code=f.rule_code,
            confirmed=f.confirmed,
        )
        for f in findings
    ]
    try:
        await self._findings.save(records)
    except Exception:
        # аналитика не должна ломать проверку: ребёнок ждёт сводку
        logger.exception("findings save failed")
```

  Событие `task_checked` остаётся прежним (его читает `hwcheck report`); `solver_call` — прежним по полям.
- `_start_tutoring(user_id, item)` → строит `TaskResult` из `item` (`findings=item.findings or findings_from_grade(...)`, `reference` из `item.ref`, `payload={"grade": item.grade.model_dump(), "ref_status": ...}`) и вызывает `self._module.start_tutoring(result, to_subject_task(item.task), kb=None)`; событие `error_classified` логируется, если `session.error is not None`, как сейчас.
- `regrade` в `bot/clarify.py`: после пересчёта `CheckedTask(task=task, ref=item.ref, grade=result, findings=findings_from_grade(0, result))` — индекс 0 заменяется в `handlers._answer_clarification` через `model_copy(update={"findings": [f.model_copy(update={"task_index": clarification.task_index}) ...]})`; проще: `regrade` принимает `task_index: int` (добавить параметр, обновить вызовы в `apply_text`/`apply_sign`/`_replace_line`, которые уже знают `clarification.task_index`).

В `src/hwcheck/bot/runner.py`: если `pool` есть — `findings=PgFindingsRepository(pool)` в `Bot(...)`.

- [ ] **Step 6: Тесты проходят**

Run: `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS; `tests/test_bot.py`, `tests/test_clarify.py`, `tests/test_bot_pages.py` без правок ожиданий.

- [ ] **Step 7: Commit**

```bash
git add src/hwcheck/db/findings.py src/hwcheck/bot/handlers.py src/hwcheck/bot/clarify.py src/hwcheck/bot/runner.py src/hwcheck/events.py tests/test_findings_repo.py tests/test_bot.py
git commit -m "feat: бот проверяет через предметный модуль, находки в базе и событиях"
```


---

### Task 9: Сервис OCR и клиент

**Files:**
- Create: `ocr/server.py`, `ocr/engine.py`, `ocr/Dockerfile`, `ocr/requirements.txt`, `ocr/README.md`, `src/hwcheck/ocr_client.py`, `tests/test_ocr_client.py`, `tests/test_ocr_server.py`
- Modify: `docker-compose.yml`, `.env.example`, `src/hwcheck/config.py`, `docs/deploy.md`, `docs/components.md`

**Interfaces:**
- Consumes: формат слова из спайка Task 1 (`{text, box, confidence, line}`); `Word`, `Box` (Task 4); `httpx`.
- Produces:
  - сервис `ocr/`: `GET /health` → `{"status": "ok", "engine": "<имя>"}`; `POST /recognize` (тело — байты изображения, `Content-Type: image/jpeg|png`) → `{"words": [{"text", "box": [x0, y0, x1, y1] | null, "confidence", "line"}], "seconds": float}`; ошибка движка → 500 с `{"error": "..."}`. Движок — `Engine(Protocol)` с `name` и `recognize(image: bytes) -> list[dict]`; `FakeEngine` (читает слова из переменной `OCR_FAKE_WORDS`, JSON) и заглушка `ReadingPipelineEngine` (`NotImplementedError` до этапа 3).
  - `hwcheck.ocr_client`: `OcrWord = Word`, `OcrError(Exception)`, `OcrClient(base_url, *, timeout_s: float)` с `async recognize(image: bytes) -> list[Word]` и `async health() -> bool`; таймаут и сетевые ошибки → `OcrError`.
  - `Settings.ocr_url: str | None = None`, `Settings.ocr_timeout_s: float = 60.0`; compose-сервис `ocr` под профилем `ocr` (не стартует по умолчанию), контейнер `homework-ocr`, лимит памяти 1.5g, без портов, healthcheck.

- [ ] **Step 1: Тест клиента**

`tests/test_ocr_client.py`:

```python
"""Клиент OCR-сервиса: слова с координатами, таймаут и сбой сети → OcrError, не падение проверки."""

import httpx
import pytest

from hwcheck.ocr_client import OcrClient, OcrError
from hwcheck.subjects.base import Box


def client(handler: httpx.MockTransport) -> OcrClient:
    ocr = OcrClient("http://ocr", timeout_s=1)
    ocr._http = httpx.AsyncClient(base_url="http://ocr", transport=handler)
    return ocr


async def test_recognize_parses_words() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/recognize" and request.content == b"img"
        assert request.headers["content-type"] == "image/jpeg"
        return httpx.Response(
            200,
            json={
                "words": [
                    {"text": "машына", "box": [10, 20, 90, 40], "confidence": 0.4, "line": 0},
                    {"text": "и", "box": None, "confidence": None, "line": 0},
                ],
                "seconds": 5.1,
            },
        )

    words = await client(httpx.MockTransport(handler)).recognize(b"img")
    assert [w.text for w in words] == ["машына", "и"]
    assert words[0].box == Box(x0=10, y0=20, x1=90, y1=40) and words[1].box is None


@pytest.mark.parametrize(
    "outcome", [httpx.Response(500, json={"error": "engine"}), httpx.ReadTimeout("slow")]
)
async def test_failures_become_ocr_error(outcome: httpx.Response | Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(OcrError):
        await client(httpx.MockTransport(handler)).recognize(b"img")


async def test_health() -> None:
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok", "engine": "fake"}))
    assert await client(ok).health()
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    assert not await client(down).health()
```

- [ ] **Step 2: Тест сервера**

`tests/test_ocr_server.py` — сервер на стандартной библиотеке, тестируется в потоке:

```python
"""OCR-сервис: HTTP-обёртка над движком; движок подменяется фейком."""

import json
import threading
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from ocr.engine import FakeEngine
from ocr.server import make_handler


@pytest.fixture
def server_url() -> Iterator[str]:
    engine = FakeEngine(words=[{"text": "cat", "box": [1, 2, 3, 4], "confidence": 0.9, "line": 0}])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()


def test_health_and_recognize(server_url: str) -> None:
    with urllib.request.urlopen(f"{server_url}/health") as response:
        assert json.loads(response.read()) == {"status": "ok", "engine": "fake"}
    request = urllib.request.Request(
        f"{server_url}/recognize", data=b"img", headers={"Content-Type": "image/jpeg"}
    )
    with urllib.request.urlopen(request) as response:
        body = json.loads(response.read())
    assert body["words"][0]["text"] == "cat" and body["seconds"] >= 0


def test_engine_failure_is_500(server_url: str) -> None:
    class Broken:
        name = "broken"

        def recognize(self, image: bytes) -> list[dict[str, object]]:
            raise RuntimeError("no weights")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Broken()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/recognize", data=b"x",
            headers={"Content-Type": "image/png"},
        )  # fmt: skip
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code == 500 and json.loads(exc.value.read())["error"] == "no weights"
    finally:
        httpd.shutdown()
```

Пакет `ocr/` лежит в корне репозитория (не в `src/hwcheck`: у него свои тяжёлые зависимости и свой образ); для тестов он импортируется как пакет с корня — добавить `ocr/__init__.py` и в `pyproject.toml` `[tool.pytest.ini_options] pythonpath = ["."]`. В `[tool.ruff] src` добавить `"ocr"`; mypy пакет `ocr` не проверяет (только `hwcheck`) — писать с аннотациями всё равно.

- [ ] **Step 3: Тесты падают**

Run: `uv run pytest tests/test_ocr_client.py tests/test_ocr_server.py -v`
Expected: FAIL — нет модулей.

- [ ] **Step 4: Сервис**

`ocr/engine.py`:

```python
"""Движки распознавания рукописи: контракт и фейк. ReadingPipeline — в этапе «русский»."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol


class Engine(Protocol):
    name: str

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        """Слова «как написано»: {text, box: [x0, y0, x1, y1] | None, confidence, line}."""
        ...


class FakeEngine:
    """Для тестов и первого запуска контейнера: слова из аргумента или OCR_FAKE_WORDS (JSON)."""

    name = "fake"

    def __init__(self, words: list[dict[str, Any]] | None = None) -> None:
        raw = os.environ.get("OCR_FAKE_WORDS")
        self._words = words if words is not None else (json.loads(raw) if raw else [])

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        return list(self._words)


class ReadingPipelineEngine:
    name = "readingpipeline"

    def __init__(self, weights_dir: str) -> None:
        self._weights_dir = weights_dir

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        raise NotImplementedError("подключается в этапе 3 по итогам спайка ReadingPipeline")


def engine_from_env() -> Engine:
    if os.environ.get("OCR_ENGINE", "fake") == "readingpipeline":
        return ReadingPipelineEngine(os.environ.get("OCR_WEIGHTS", "/app/weights"))
    return FakeEngine()
```

`ocr/server.py`:

```python
"""HTTP-обёртка OCR: GET /health, POST /recognize (байты изображения → слова). Стандартная
библиотека: сервис маленький, а зависимости движка и так тяжёлые."""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ocr.engine import Engine, engine_from_env

logger = logging.getLogger("ocr")
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def make_handler(engine: Engine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
            if self.path != "/health":
                self._json(404, {"error": "not found"})
                return
            self._json(200, {"status": "ok", "engine": engine.name})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/recognize":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_IMAGE_BYTES:
                self._json(413, {"error": "image size"})
                return
            image = self.rfile.read(length)
            started = time.perf_counter()
            try:
                words = engine.recognize(image)
            except Exception as exc:
                logger.exception("recognize failed")
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"words": words, "seconds": time.perf_counter() - started})

        def _json(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.info("%s " + format, self.address_string(), *args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    engine = engine_from_env()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), make_handler(engine))
    logger.info("ocr started: engine=%s", engine.name)
    server.serve_forever()


if __name__ == "__main__":
    main()
```

`ocr/Dockerfile` (движок — фейк; веса и зависимости ReadingPipeline добавляются в этапе 3):

```dockerfile
# OCR-сервис «Домашки»: посимвольное распознавание рукописи без языковой модели.
FROM python:3.12-slim
WORKDIR /app
COPY ocr/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY ocr ./ocr
RUN useradd --uid 1000 --create-home app && chown -R app:app /app
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "ocr.server"]
```

`ocr/requirements.txt` — пусто (комментарий «зависимости ReadingPipeline — этап 3»).

`docker-compose.yml` — сервис:

```yaml
  ocr:
    build:
      context: .
      dockerfile: ocr/Dockerfile
    image: max-homework-ai-ocr
    container_name: homework-ocr
    restart: unless-stopped
    profiles: ["ocr"]            # стартует только явно: docker compose --profile ocr up -d ocr
    environment:
      OCR_ENGINE: ${OCR_ENGINE:-fake}
    deploy:
      resources:
        limits:
          memory: 1.5g
    # без портов наружу: бот ходит по compose-сети как к http://ocr:8080
```

У сервиса `bot` — `OCR_URL: http://ocr:8080` в `environment` (клиент создаётся только при заданном `OCR_URL`; без запущенного контейнера обращений нет, потому что модуль `math` OCR не использует).

- [ ] **Step 5: Клиент и настройки**

`src/hwcheck/ocr_client.py`:

```python
"""Клиент OCR-сервиса (спецификация каркаса §8): слова «как написано» с координатами.

Сервис недоступен или медленен — OcrError; предметный модуль переводит это в «не уверен» и
событие ocr_failed, проверка не падает.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx

from hwcheck.subjects.base import Box, Word

OcrWord = Word


class OcrError(Exception):
    pass


class OcrClient:
    def __init__(self, base_url: str, *, timeout_s: float) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout_s))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def recognize(self, image: bytes) -> list[Word]:
        try:
            response = await self._http.post(
                "/recognize", content=image, headers={"Content-Type": "image/jpeg"}
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OcrError(f"ocr: {type(exc).__name__}") from exc
        return [_word(w) for w in data.get("words", [])]

    async def health(self) -> bool:
        try:
            response = await self._http.get("/health")
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _word(raw: dict[str, Any]) -> Word:
    box = raw.get("box")
    return Word(
        text=str(raw.get("text", "")),
        box=Box(x0=box[0], y0=box[1], x1=box[2], y1=box[3]) if box else None,
        confidence=raw.get("confidence"),
        line=raw.get("line"),
    )
```

В `config.py` после `database_url`:

```python
# OCR-сервис для языков (контейнер homework-ocr); пусто — предметы с OCR недоступны
ocr_url: str | None = None
ocr_timeout_s: float = (
    60.0  # p95 спайка × 2; уточнить по docs/research/2026-09-17-readingpipeline-vps.md
)
```

`.env.example`: `# OCR_URL=http://localhost:8080`, `# OCR_ENGINE=fake  # readingpipeline — этап «русский»`. `docs/deploy.md`: раздел «OCR-сервис»: `free -m` перед запуском, `docker compose --profile ocr up -d ocr`, `docker exec homework-ocr python -c "..."` health, лимит 1.5 ГБ. `docs/components.md`: строка про `ocr/` (stdlib), ReadingPipeline — при подключении.

- [ ] **Step 6: Тесты проходят**

Run: `uv run pytest tests/test_ocr_client.py tests/test_ocr_server.py -v && uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ocr src/hwcheck/ocr_client.py src/hwcheck/config.py docker-compose.yml .env.example docs/deploy.md docs/components.md pyproject.toml tests/test_ocr_client.py tests/test_ocr_server.py
git commit -m "feat: OCR-сервис с фейковым движком и клиент бота"
```

---

### Task 10: Кроп слова в уточняющем вопросе — загрузка изображений в MAX

**Files:**
- Create: `src/hwcheck/bot/crops.py`, `tests/test_max_upload.py`, `tests/test_clarify_word.py`
- Modify: `src/hwcheck/bot/max_api.py`, `src/hwcheck/bot/fsm.py` (`Clarification.kind`, `finding_index`, `ChatState.photo_paths`), `src/hwcheck/bot/clarify.py`, `src/hwcheck/bot/handlers.py` (`_process_photos`, `_ask_clarification`, `_on_callback`), `tests/test_bot.py` (`FakeMax.upload_image`)

**Interfaces:**
- Consumes: MAX Bot API `POST /uploads?type=image` → `{"url", "token"}`; загрузка файла `POST <url>` multipart, поле `data` → `{"token"}`; вложение сообщения `{"type": "image", "payload": {"token": ...}}` (dev.max.ru/docs-api/methods/POST/uploads, проверено 16.09); `Word`, `Box`, `Finding` (Task 4); `PhotoStore.save` возвращает относительный путь — уже есть.
- Produces:
  - `MaxClient.upload_image(image: bytes) -> str` (токен), `send_message(chat_id, text, *, buttons=None, image_token: str | None = None)`;
  - `hwcheck.bot.crops.crop_word(image: bytes, box: Box, *, margin: int = 12) -> bytes` (JPEG, Pillow);
  - `Clarification.kind` расширяется `"word"`, поле `finding_index: int | None = None`; `ChatState.photo_paths: list[str]`;
  - `clarify.plan_clarifications` добавляет вопросы `word` по находкам `candidate` с `word.box` (после математических, в общий лимит `MAX_QUESTIONS`); `clarify.question` для `word` → `("<label>: здесь написано «машына»?", кнопки Да/Нет `clarify:<token>:yes|no`)`; `clarify.apply_word(item, clarification, key) -> CheckedTask | None` — `yes` → `confirmed=True`, `no` → `confirmed=False` у находки;
  - `handlers._ask_clarification` для `word`: читает фото из `PhotoStore` по `state.photo_paths`, кропает, `upload_image`, шлёт с `image_token`; без фото/при ошибке загрузки — текстом.

- [ ] **Step 1: Тест загрузки в MAX**

`tests/test_max_upload.py`:

```python
"""Загрузка изображения в MAX (POST /uploads → файл → токен) и сообщение с картинкой."""

import json

import httpx

from hwcheck.bot.max_api import MaxClient


async def test_upload_image_and_send_with_attachment() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/uploads":
            assert request.url.params["type"] == "image"
            return httpx.Response(200, json={"url": "https://iu.test/upload/abc", "token": "t0"})
        if request.url.host == "iu.test":
            assert b'name="data"' in request.content and b"jpegbytes" in request.content
            return httpx.Response(200, json={"token": "tok123"})
        return httpx.Response(200, json={})

    async with MaxClient("token") as client:
        await client._http.aclose()
        await client._files.aclose()
        transport = httpx.MockTransport(handler)
        client._http = httpx.AsyncClient(base_url="https://max.test", transport=transport)
        client._files = httpx.AsyncClient(transport=transport)
        token = await client.upload_image(b"jpegbytes")
        await client.send_message(7, "здесь написано «машына»?", image_token=token)

    assert token == "tok123"
    body = json.loads(requests[-1].content)
    assert body["attachments"] == [{"type": "image", "payload": {"token": "tok123"}}]
    assert "Authorization" not in requests[1].headers  # файл — на сторонний хост без токена бота
```

- [ ] **Step 2: Тест кропа и вопроса**

`tests/test_clarify_word.py`:

```python
"""Уточняющий вопрос «здесь написано …?» с кропом слова (спецификация каркаса §8)."""

import io
from pathlib import Path

from PIL import Image

from hwcheck.bot.check import validator_only_grade
from hwcheck.bot.crops import crop_word
from hwcheck.bot.fsm import CheckedTask, Clarification
from hwcheck.bot.clarify import apply_word, plan_clarifications, question
from hwcheck.pipeline.schemas import VisionTask
from hwcheck.subjects.base import Box, Finding, Word


def test_crop_word_adds_margin_and_clamps() -> None:
    image = Image.new("RGB", (100, 50), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    crop = Image.open(io.BytesIO(crop_word(buffer.getvalue(), Box(x0=5, y0=5, x1=40, y1=20))))
    assert crop.size == (52, 32)  # поля 12 px, обрезано по краю изображения слева/сверху


def item_with_word() -> CheckedTask:
    task = VisionTask(number=3, task_text="", student_solution_steps=[], confidence=1)
    word = Word(text="машына", box=Box(x0=1, y0=1, x1=9, y1=9), confidence=0.4)
    finding = Finding(
        task_index=0, kind="spelling", strength="candidate", actual="машына", word=word
    )
    return CheckedTask(task=task, ref=None, grade=validator_only_grade([]), findings=[finding])


def test_word_question_and_answers() -> None:
    item = item_with_word()
    [clarification] = plan_clarifications([item])
    assert (clarification.kind, clarification.finding_index) == ("word", 0)
    text, buttons = question(item, clarification)
    assert text == "№3: здесь написано «машына»?"
    assert buttons is not None and [b["payload"] for b in buttons[0]] == [
        f"clarify:{clarification.token}:yes",
        f"clarify:{clarification.token}:no",
    ]
    confirmed = apply_word(item, clarification, "yes")
    assert confirmed is not None and confirmed.findings[0].confirmed is True
    denied = apply_word(item, clarification, "no")
    assert denied is not None and denied.findings[0].confirmed is False
    assert apply_word(item, clarification, "maybe") is None


def test_word_questions_share_limit_with_math(tmp_path: Path) -> None:
    unsure = VisionTask(
        number=1, task_text="", student_solution_steps=["<неразборчиво>"], confidence=1
    )
    math_item = CheckedTask(task=unsure, ref=None, grade=validator_only_grade(["<неразборчиво>"]))
    plan = plan_clarifications([math_item, item_with_word(), item_with_word()])
    assert [c.kind for c in plan] == ["line", "word"]  # MAX_QUESTIONS = 2
```

- [ ] **Step 3: Тесты падают**

Run: `uv run pytest tests/test_max_upload.py tests/test_clarify_word.py -v`
Expected: FAIL — нет `upload_image`, `crops`, `apply_word`.

- [ ] **Step 4: Клиент MAX**

В `src/hwcheck/bot/max_api.py`:

```python
async def upload_image(self, image: bytes) -> str:
    """Токен вложения: POST /uploads?type=image даёт адрес загрузки, файл уходит туда
    multipart-полем data (без токена бота — хост сторонний), в ответ — token (dev.max.ru)."""
    response = await self._http.post("/uploads", params={"type": "image"})
    response.raise_for_status()
    upload_url = response.json()["url"]
    uploaded = await self._files.post(upload_url, files={"data": ("word.jpg", image, "image/jpeg")})
    uploaded.raise_for_status()
    token: str = uploaded.json()["token"]
    return token


async def send_message(
    self,
    chat_id: int,
    text: str,
    *,
    buttons: Buttons | None = None,
    image_token: str | None = None,
) -> None:
    await self._post_message({"chat_id": chat_id}, text, buttons, image_token)
```

и `_post_message(params, text, buttons, image_token=None)`: при `image_token` добавляет `{"type": "image", "payload": {"token": image_token}}` в `attachments` первым (клавиатура — после).

- [ ] **Step 5: Кроп, состояние, вопрос**

`src/hwcheck/bot/crops.py`:

```python
"""Кроп слова с фото тетради для уточняющего вопроса: ребёнок видит, о чём спрашивают."""

from __future__ import annotations

import io

from PIL import Image

from hwcheck.subjects.base import Box


def crop_word(image: bytes, box: Box, *, margin: int = 12) -> bytes:
    with Image.open(io.BytesIO(image)) as source:
        width, height = source.size
        area = (
            max(0, box.x0 - margin),
            max(0, box.y0 - margin),
            min(width, box.x1 + margin),
            min(height, box.y1 + margin),
        )
        crop = source.convert("RGB").crop(area)
        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=90)
        return out.getvalue()
```

`fsm.py`: `Clarification.kind: Literal["answer", "sign", "line", "word"]`, `finding_index: int | None = None`; `ChatState.photo_paths: list[str] = Field(default_factory=list)` (относительные пути `PhotoStore`; заполняются в `_process_photos` из `_save_photo`, в `_recognize_all` вернуть пути вместе с результатами).

`clarify.py`:
- `plan_clarifications`: после математических — по каждому `item` с находками `candidate`, у которых `word is not None and word.box is not None and confirmed is None`, добавить `Clarification(task_index=i, kind="word", finding_index=j)`, пока `len(plan) < MAX_QUESTIONS`;
- `question`: для `word` — `f"{label}: здесь написано «{finding.actual or finding.word.text}»?"`, кнопки `word_buttons(clarification)` = `[[Да → clarify:<token>:yes, Нет → clarify:<token>:no]]`;
- `retry_prompt` для `word`: `"Нажми «Да» или «Нет» 👇"`, те же кнопки;
- `apply_word(item, clarification, key)`: `key in ("yes", "no")` → `item.model_copy` с находкой `finding_index` и `confirmed = key == "yes"`; иначе `None`; `apply_text` для `word`: «да/нет» текстом → `apply_word`.

`handlers.py`:
- `_on_callback` для `clarify:`: если `current.kind == "word"` → `apply_word`, иначе `apply_sign` (как сейчас);
- `_ask_clarification`: для `word` — путь фото `state.photo_paths[finding.word.photo_index]` (в `Word` из Task 4 добавить поле `photo_index: int = 0` — номер фото в альбоме, координаты относятся к нему; модуль проставляет его в `recognize`), `image = self._photos.load(path)`, `crop_word`, `upload_image`, сообщение с `image_token`. Ошибка чтения/кропа/загрузки → `logger.warning`, вопрос текстом без картинки; событие `clarification_asked{kind="word", with_image: bool}`.
- `PhotoStore.load(rel_path) -> bytes` — добавить в `photos.py` (чтение файла; `FileNotFoundError` наверх).

- [ ] **Step 6: Тесты проходят**

Run: `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS; `tests/test_bot.py::FakeMax` дополнен методом `upload_image` (возвращает `"tok"`), прежние тесты не меняются.

- [ ] **Step 7: Живая проверка загрузки** — с локальным `hwcheck bot` нельзя (один поллер на токен): на VPS `docker compose exec -T bot python -c "..."` → `MaxClient.upload_image` на маленький JPEG и `send_message(<chat Кирилла>, 'проверка кропа', image_token=...)`. Результат (пришла ли картинка, формат ответа `/uploads`) — в `docs/deploy.md` и `HISTORY.md`; если формат отличается от документации — поправить `upload_image` и тест.

- [ ] **Step 8: Commit**

```bash
git add src/hwcheck/bot/max_api.py src/hwcheck/bot/crops.py src/hwcheck/bot/fsm.py src/hwcheck/bot/clarify.py src/hwcheck/bot/handlers.py src/hwcheck/photos.py src/hwcheck/subjects/base.py tests/test_max_upload.py tests/test_clarify_word.py tests/test_bot.py
git commit -m "feat: уточняющий вопрос по слову с кропом — загрузка изображений в MAX"
```

---

### Task 11: Консоль базы знаний — `hwcheck kb review`, `hwcheck kb load-words`

**Files:**
- Create: `src/hwcheck/kb_cli.py`, `tests/test_kb_cli.py`
- Modify: `src/hwcheck/cli.py`, `docs/deploy.md`

**Interfaces:**
- Consumes: `KnowledgeBase` реализации (Task 7), `create_pool`, `Settings.database_url`.
- Produces: `hwcheck.kb_cli`: `review(kb, subject, *, limit, read=input, write=print) -> int` (число обработанных), `load_words(kb, subject, source, path: Path) -> int` (файл — по слову в строке или JSON `{слово: attrs}`); подкоманды `hwcheck kb review --subject russian [--limit 20]`, `hwcheck kb load-words --subject english --source irregular_verbs data/kb/irregular_verbs.json`.

- [ ] **Step 1: Тест**

`tests/test_kb_cli.py`:

```python
"""Консольная проверка базы знаний: очередь непроверенных ответов, загрузка словарей."""

from pathlib import Path

from hwcheck.db.kb import fingerprint
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.kb_cli import load_words, review
from hwcheck.subjects.kb_models import KbAnswer, KbPage, KbTask


async def seed(kb: InMemoryKnowledgeBase) -> list[int]:
    page = KbPage(subject="russian", grade=2, fingerprint=fingerprint("x"), text="x")
    saved = await kb.save_page(
        page, [KbTask(number="34", condition="п_ляне", task_kind="fill_letters")]
    )
    task_id = saved.tasks[0].id
    assert task_id is not None
    ids = []
    for text in ("поляне", "пыляне"):
        answer = await kb.save_answer(
            KbAnswer(task_id=task_id, answer={"text": text}, derived_by="llm:GigaChat-2-Pro@v1")
        )
        assert answer.id is not None
        ids.append(answer.id)
    return ids


async def test_review_applies_answers_and_stops_on_quit() -> None:
    kb = InMemoryKnowledgeBase()
    ids = await seed(kb)
    answers = iter(["y", "n"])
    shown: list[str] = []
    done = await review(kb, "russian", limit=10, read=lambda _: next(answers), write=shown.append)
    assert done == 2
    assert "п_ляне" in shown[0] and "поляне" in shown[0]
    statuses = {a.id: (a.status, a.checked_by) for a in await kb.answers_for(1)}
    assert statuses[ids[0]] == ("verified", "manual") and statuses[ids[1]] == ("rejected", "manual")
    assert await review(kb, "russian", limit=10, read=lambda _: "q", write=shown.append) == 0


async def test_review_edit_creates_verified_answer() -> None:
    kb = InMemoryKnowledgeBase()
    [first, _] = await seed(kb)
    answers = iter(["e", "поляне", "q"])
    await review(kb, "russian", limit=10, read=lambda _: next(answers), write=lambda _: None)
    all_answers = await kb.answers_for(1)
    assert [a.status for a in all_answers if a.id == first] == ["rejected"]
    edited = all_answers[-1]
    assert (edited.answer, edited.status, edited.derived_by) == (
        {"text": "поляне"},
        "verified",
        "manual",
    )


async def test_load_words(tmp_path: Path) -> None:
    kb = InMemoryKnowledgeBase()
    plain = tmp_path / "words.txt"
    plain.write_text("собака\nкорова\n\nсобака\n", encoding="utf-8")
    assert await load_words(kb, "russian", "grade_list:2", plain) == 2
    assert await kb.words("russian", "grade_list:2") == {"собака", "корова"}
    verbs = tmp_path / "verbs.json"
    verbs.write_text('{"go": {"past": "went"}}', encoding="utf-8")
    assert await load_words(kb, "english", "irregular_verbs", verbs) == 1
```

- [ ] **Step 2: Тест падает**

Run: `uv run pytest tests/test_kb_cli.py -v`
Expected: FAIL — нет `hwcheck.kb_cli`.

- [ ] **Step 3: Реализация**

`src/hwcheck/kb_cli.py`:

```python
"""Консоль базы знаний (спецификация каркаса §5): очередь непроверенных ответов и словари.

Ответы `y` (подтвердить), `n` (отклонить), `e` (ввести верный ответ: старый отклоняется, новый —
verified от manual), `q` (выйти). Веб-интерфейс — вне объёма.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hwcheck.db.kb import PgKnowledgeBase
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.subjects.kb_models import KbAnswer

KnowledgeBaseImpl = PgKnowledgeBase | InMemoryKnowledgeBase


async def review(
    kb: KnowledgeBaseImpl,
    subject: str,
    *,
    limit: int,
    read: Callable[[str], str] = input,
    write: Callable[[str], object] = print,
) -> int:
    done = 0
    for task, answer in await kb.unverified_answers(subject, limit):
        assert answer.id is not None
        write(f"[{task.number or '—'}] {task.condition}\n  наш ответ: {answer.answer}")
        while True:
            choice = read("y — верно, n — неверно, e — исправить, q — выйти: ").strip().lower()
            if choice in ("y", "n", "e", "q"):
                break
        if choice == "q":
            break
        if choice == "y":
            await kb.set_answer_status(answer.id, "verified", checked_by="manual")
        else:
            await kb.set_answer_status(answer.id, "rejected", checked_by="manual")
            if choice == "e":
                text = read("верный ответ: ").strip()
                await kb.save_answer(
                    KbAnswer(
                        task_id=answer.task_id,
                        answer={"text": text},
                        derived_by="manual",
                        checked_by="manual",
                        status="verified",
                    )  # fmt: skip
                )
        done += 1
    return done


async def load_words(kb: KnowledgeBaseImpl, subject: str, source: str, path: Path) -> int:
    raw = path.read_text(encoding="utf-8")
    words: dict[str, dict[str, Any] | None]
    if path.suffix == ".json":
        words = json.loads(raw)
    else:
        words = {line.strip(): None for line in raw.splitlines() if line.strip()}
    await kb.add_words(subject, source, words)
    return len(words)
```

В `cli.py`: подпарсер `kb` с командами `review` (`--subject`, `--limit` по умолчанию 20) и `load-words` (`--subject`, `--source`, `path`); обе требуют `DATABASE_URL` (иначе `SystemExit`), создают пул через `create_pool`, `PgKnowledgeBase(pool)`, закрывают пул. `docs/deploy.md`: как запускать на VPS — `docker compose exec bot python -m hwcheck kb review --subject russian`.

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest tests/test_kb_cli.py -v && uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/kb_cli.py src/hwcheck/cli.py docs/deploy.md tests/test_kb_cli.py
git commit -m "feat: консоль базы знаний — проверка ответов и загрузка словарей"
```

---

### Task 12: Ревью, слияние и выкатка каркаса

**Files:**
- Modify: `docs/architecture.md` (§3 компоненты: предметные модули, база знаний, OCR), `docs/components.md`, `HISTORY.md`, `TODO.md`, `docs/PROJECT_MEMORY.md`

**Interfaces:**
- Consumes: всё из Task 4–11.
- Produces: `main` с каркасом; prod с миграцией 003 и тем же поведением математики; контейнер OCR собран, но не запущен (профиль).

- [ ] **Step 1: Ревью** — параллельно `ecc:python-reviewer` и `ecc:code-reviewer` на `git diff main...HEAD`; фокус: поведение математики не изменилось (сводка, уточнения, тьютор), контракт не течёт в `handlers.py`, SQL параметризован, OCR-сервер без утечек памяти на больших телах. Исправить CRITICAL/HIGH; полный прогон проверок.

- [ ] **Step 2: PR** — `git push -u origin feat/subjects-framework`, `gh pr create` (что сделано, отступление про `start_tutoring`, план проверки: prod-миграция 003, сводки по живым фото без изменений), зелёный CI.

- [ ] **Step 3: Выкатка** — тишина 10 минут → слияние → на VPS `git pull`, `docker compose up -d --build bot` (Redis и PostgreSQL не трогать) → лог `migrations applied: 003_knowledge_base.sql` → Кирилл присылает фото математики: сводка как раньше → `docker compose --profile ocr build ocr` (только сборка, без запуска — память проверим в этапе 3).

- [ ] **Step 4: Документы сессии** — `HISTORY.md`, `TODO.md` (каркас закрыт; следующий шаг — план этапа 3 по итогам спайков), `docs/PROJECT_MEMORY.md` (§2 состояние, §4 решения 16.09 по предметам, §5 архитектура: модули/база знаний/OCR, §8 артефакты).

---

## Самопроверка плана этапов 1–2 по спецификации

- §3 п.1 (научить, не решить) — тьютор без изменений (Task 5, 8); п.2 (два источника) — OCR-сервис и клиент (Task 9), эталон с `origin`/`trust` (Task 4–5); п.3 (сила вердикта) — `Strength`, `strength_of_task`, сводка (Task 4, 6); п.4 (модуль) — Task 4–5, 8; п.5 (база накапливается) — Task 7, 11; п.6 (стенд) — этапы 3–4.
- §4 контракт — Task 4; математика без изменения поведения — Task 5–6, 8 (прежние тесты без правок); `hint` → `start_tutoring` — отступление зафиксировано в шапке.
- §5 схема (`kb_pages`, `kb_tasks`, `kb_answers`, `kb_rules`, `kb_words`), отпечаток, сила эталона по `status`/`checked_by`, `hwcheck kb review`, «не делаем» — Task 7, 11.
- §6–7 — этапы 3–4 (ниже, по коду каркаса).
- §8 сводка — Task 6; уточнение `word` с кропом и `POST /uploads` — Task 10; `findings` и события `finding_created`, `reference_resolved` — Task 8; `finding_confirmed` — Task 10 (`clarification_answered` уже пишется; добавить `finding_confirmed{answer}` в `_answer_clarification` для `word`); `kb_review{action}` — Task 11 (событие при каждом ответе, если задан `EventLog`; в CLI — `EventLog(settings.events_path, settings.environment)`); контейнер `homework-ocr`, лимит, `ocr_failed` — Task 9 (событие пишет предметный модуль в этапе 3); фото учебников `var/kb_photos` — этап 3 (первый предмет, который сохраняет страницы); тесты контракта — Task 5 (`test_math_module.py` — образец для языков).
- §9 спайки — Task 1–3. §10 порядок — этапы. §11 риски: память VPS — шаг `free -m` в Task 1 и 9; кропы — Task 10 Step 7 живая проверка.

---

# Этап 3. Русский язык — «вставь буквы / раскрой скобки»

План пишется по коду этапа 2 и отчётам спайков (skill writing-plans). Состав задач (спецификация §6):

| Задача | Файлы | Что даёт |
|---|---|---|
| R1. Движок ReadingPipeline в `ocr/` | `ocr/engine.py`, `ocr/Dockerfile`, `ocr/requirements.txt` | контейнер с реальными весами по параметрам спайка 1; health; лимит памяти по замеру |
| R2. Словарь Hunspell и заполнение пропусков | `src/hwcheck/subjects/russian/gaps.py`, `data/kb/ru_RU.{dic,aff}` (лицензия в `docs/components.md`) | `fill_gap(pattern) -> list[str]` из спайка 2; `derive_reference(condition) -> (text, trust)` — один кандидат → `verified`, иначе LLM + `unverified` |
| R3. Распознавание страниц | `subjects/russian/recognize.py` | учебник — GigaChat-vision (промпт `prompts/ru_textbook/v1.md`: текст упражнения с пропусками как напечатано, номер, задание), тетрадь — `OcrClient` → `SubjectTask.words`; страница учебника → `kb.save_page` (+ фото в `var/kb_photos`); `ocr_failed` |
| R4. Выравнивание и находки | `subjects/russian/align.py`, `check.py` | Левенштейн по словам, затем по буквам; находки `spelling` (в пропуске / вне), `missing_word`, `extra_word` с `Word` для кропа; приоритеты и `candidate` |
| R5. Модуль и тьютор | `subjects/russian/module.py`, `prompts/ru_tutor/v1.md`, `prompts/ru_orthogram/v1.md` | `RussianModule` в реестре; классификатор орфограммы → `rule_code`; `start_tutoring` с правилом из `kb_rules` в контексте; уровни 0–3 |
| R6. Правила орфограмм 2–4 класса | `data/kb/rules_russian.json`, `hwcheck kb load-rules` | ~20 карточек: LLM-черновик → вычитка; загрузка в `kb_rules` |
| R7. Бот: маршрутизация по предмету | `bot/handlers.py`, `bot/runner.py`, `bot/pages.py` | модуль по `student_profiles.subject` (через `ProfileRepository`); альбом (учебник/тетрадь) и «условия из памяти» — на `SubjectTask`; `grade: GradeResult | None` в `CheckedTask` (для языков `None`; `clarify.py` — только по находкам) |
| R8. Стенд | `bench/golden/ru-*.json`, `bench/runner.py` | golden-кейсы с ожидаемыми находками (≥ 10 реальных фото), метрики ложных `verified`/пропусков по силе; порог ≤ 5 % |
| R9. Открытие | `bot/subjects.py`, `docs/`, выкатка | флаг `russian.available = True`, память VPS, рассылка листу ожидания (по спецификации онбординга — отдельная задача), живой тест |

# Этап 4. Английский язык — упражнения с однозначным ответом

План пишется по коду этапа 3 и отчёту спайка 3 (§7). Состав:

| Задача | Что даёт |
|---|---|
| E1. OCR латиницы | по спайку 3: движок в `ocr/` (TrOCR по кропам сегментации / ReadingPipeline) или запасной путь «две расшифровки GigaChat» в `subjects/english/recognize.py`; стартовая сила вердикта |
| E2. Эталон | `subjects/english/reference.py`: формы глаголов по правилу + `kb_words(irregular_verbs)`; выбор из рамки — LLM с проверкой «ответ ∈ рамка»; `trust` по способу |
| E3. Проверка | `subjects/english/check.py`: ответ по позиции пропуска, выравнивание по номерам предложений; `verb_form`, `spelling` («форма верна, проверь написание»), `choice` |
| E4. Модуль и тьютор | `EnglishModule`, промпты `en_textbook`, `en_tutor`, `en_error`; правила времён 3–6 класса (`data/kb/rules_english.json`) |
| E5. Стенд и открытие | golden-кейсы ≥ 10 фото, порог, флаг `foreign_language.available`, живой тест |
