# Русский язык, этап 3 — «спиши, вставь пропущенные буквы» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** в проде работает проверка домашки по русскому языку в форме «спиши текст, вставь пропущенные буквы / раскрой скобки» (1–6 класс): фото учебника даёт текст с пропусками, фото тетради читается посимвольным OCR без языковой модели, расхождения с эталоном уходят ребёнку как «здесь написано *машына*?» с кропом слова, подтверждённая ошибка разбирается тьютором по карточке орфограммы.

**Architecture:** предметный модуль `subjects/russian/` реализует контракт `SubjectModule` каркаса (этап 2): `recognize` — GigaChat-vision решает роль страницы и читает **печатный** текст учебника, тетрадь читает контейнер `homework-ocr` (ReadingPipeline, ONNX, без torch); `resolve_reference` — пропуски заполняются словарём Hunspell (ровно один кандидат → `verified`), иначе GigaChat выбирает из кандидатов → `unverified` и очередь `hwcheck kb review`; эталоны копятся в базе знаний по отпечатку текста; `check` — выравнивание слов тетради с эталоном (Левенштейн по словам, затем по буквам) → находки `spelling | missing_word | extra_word` силой `candidate`; `start_tutoring` — классификатор орфограммы → карточка правила из `kb_rules` → `TutorSession` вида `word`. Бот получает маршрутизацию по предмету профиля: математика идёт прежним путём без изменения поведения, языки — новым путём через `SubjectPage`.

**Tech Stack:** Python 3.12, pydantic, asyncpg/PostgreSQL 17, Redis, GigaChat (vision + chat), ReadingPipeline (ai-forever, MIT; onnxruntime CPU, образ python:3.10-slim без torch), `spylls` (Hunspell на Python, MIT) + словарь `ru_RU` LibreOffice (BSD-подобная лицензия), pytest + pytest-asyncio, ruff, mypy strict, Docker Compose на VPS.

**Spec:** `docs/superpowers/specs/2026-09-16-subjects-framework-design.md` (§6 русский, §5 база знаний, §8 общие части бота). Контур задач R1–R9 — `docs/superpowers/plans/2026-09-16-subjects-framework.md`, раздел «Этап 3». Отчёты спайков: `docs/research/2026-09-17-readingpipeline-memory.md` (рецепт образа OCR), `docs/research/2026-09-17-hunspell-gaps.md` (алгоритм `fill_gap`, 72 % однозначных), `docs/research/2026-09-13-ru-handwriting-ocr.md` (почему не LLM-vision).

## Global Constraints

- Сила вердикта: `verified` — только детерминированное сравнение с эталоном `trust=verified`; `candidate` — расхождение OCR с эталоном, непроверенный эталон или суждение LLM; при сомнении вердикт понижается. **Для русского на старте все находки — `candidate`**; ошибкой (`Finding.is_error`) находка становится только после «да» ребёнка на «здесь написано …?». Ложных `verified` ошибок ≤ 5 % на ≥ 10 реальных фото — порог флага `available`.
- «Как написано» — посимвольный OCR без языковой модели; GigaChat-vision **не транскрибирует текст ученика** (исследование 13.09: скрывает 78–80 % ошибок). Vision читает только печатный учебник и решает роль страницы.
- Математика: поведение и тексты сводки **не меняются**, существующие тесты (580 на 17.09) проходят без правок логики. Маршрут `subject == "math"` в боте остаётся прежним кодом.
- Уточняющих вопросов — не больше `MAX_QUESTIONS` (2) на домашку (`bot/clarify.py`); остальное — в сводке «стоит перепроверить 🤔 (N мест)».
- OCR — контейнер `homework-ocr` в compose-сети без портов наружу, лимит памяти **2 ГБ** (спайк памяти: пик 1,46 ГБ без torch и без арены onnxruntime; запас на плотный почерк), один прогон за раз (семафор), таймаут клиента 60 с; недоступен → страница тетради «не смог прочитать», событие `ocr_failed`, проверка не падает. Печатные страницы в OCR не отправляются (пик до 3 ГБ).
- База знаний: в `kb_pages`/`kb_tasks` — только печатный текст упражнения; ответы `unverified | verified | rejected`; фото учебников — `var/kb_photos` (TTL 365 дней; не персональные данные ребёнка; отметить в политике); фото тетрадей в базе не хранятся.
- Персональные данные: в событиях и находках — хэши; кропы слов уходят в MAX и не сохраняются.
- VPS общий (8 ГБ, доступно ~3,8 ГБ): перед рестартом — нет событий за 10 минут; Redis не перезапускать; `free -m` до запуска `ocr`.
- Код: TDD, `ruff check`, `ruff format` (форматирует и python-блоки в markdown — этот файл тоже), `mypy` strict (пакеты `hwcheck` и `ocr`), conventional commits на русском, тексты и комментарии по-русски. Ветка `feat/russian` от `main`; PR на этап целиком, ревью после каждой задачи.
- `data/` в `.gitignore` — словарь и карточки правил лежат в `assets/` (копируются в образ бота).

**Отступления от спецификации (сознательные):**
- §6 «расхождение в позиции пропуска, эталон verified → candidate → уточнение», «вне пропуска — candidate ниже приоритетом» — так и делаем; **`verified` без подтверждения ребёнка не ставится вовсе**, даже при `verified` эталоне: OCR искажает 17–24 % верных слов (исследование 13.09). Порог 5 % ложных `verified` при этом выполняется по построению, а мера качества стенда — точность кандидатов (доля «нет»).
- §4 `recognize(photos)` → в каркасе `recognize(image)` (одно фото); роль страницы и текст учебника — один vision-вызов на фото, тетрадь — плюс вызов OCR.
- §6 «страница учебника → `kb.save_page`» происходит в `resolve_reference` (у `recognize` нет базы знаний по контракту); путь фото учебника передаётся через новое поле `SubjectTask.photo_path`.

---

## Файлы этапа

| Файл | Ответственность |
|---|---|
| `ocrsvc/engine.py` (изменить) | `ReadingPipelineEngine`: загрузка ONNX-пайплайна без torch, конвертация предсказаний в слова; EXIF-поворот и лимит пикселей входа |
| `ocrsvc/patch_no_torch.py` (создать, перенос из `spikes/ocr_memory/`) | патч апстрима: убрать безусловные `import torch` на ONNX-пути |
| `ocrsvc/Dockerfile`, `ocrsvc/requirements.txt`, `ocrsvc/README.md` (изменить) | образ по рецепту `Dockerfile.notorch`: python:3.10-slim, клоны ai-forever, веса при сборке, ENV арены onnxruntime |
| `docker-compose.yml` (изменить) | `ocr`: лимит 2g, `restart: unless-stopped`, `OCR_ENGINE`; бот: `OCR_URL`, том `var/kb_photos` |
| `assets/hunspell/ru_RU.dic`, `ru_RU.aff`, `README_ru_RU.txt` (создать) | словарь LibreOffice ru_RU с лицензией |
| `src/hwcheck/subjects/russian/__init__.py` (создать) | пакет предмета |
| `src/hwcheck/subjects/russian/gaps.py` (создать) | токенизация, поиск пропусков и скобок, `fill_gap`, `derive_text` (словарь → LLM) |
| `prompts/ru_gaps/v1.md` (создать) | выбор кандидата LLM по контексту предложения |
| `prompts/ru_page/v1.md` (создать) | vision: роль страницы + печатный текст упражнений с пропусками |
| `src/hwcheck/subjects/russian/recognize.py` (создать) | `recognize_page` (vision), `notebook_task` (слова OCR → задание), номер упражнения |
| `src/hwcheck/subjects/russian/align.py` (создать) | нормализация слов, выравнивание по словам с буквенным сходством |
| `src/hwcheck/subjects/russian/check.py` (создать) | находки из выравнивания, приоритеты, защита от «не то упражнение» |
| `src/hwcheck/subjects/russian/module.py` (создать) | `RussianModule`: четыре шага контракта, база знаний, тьютор |
| `src/hwcheck/subjects/russian/rules.py` (создать) | коды орфограмм, классификатор (`prompts/ru_orthogram/v1.md`), загрузка карточек |
| `prompts/ru_orthogram/v1.md`, `prompts/ru_tutor/v1.md` (создать) | классификатор орфограммы; тьютор по слову |
| `assets/kb/rules_russian.json` (создать) | ~20 карточек правил 2–4 класса |
| `src/hwcheck/pipeline/tutor.py` (изменить) | `TutorSession.word: WordTutoring | None`; ветка `word` в `tutor_reply` (решённость по слову, защита от утечки слова) |
| `src/hwcheck/subjects/base.py` (изменить) | `SubjectTask.photo_path`, `SubjectPage.failure` |
| `src/hwcheck/subjects/registry.py` (изменить) | `SubjectDeps.ocr / kb / dictionary`; `russian` в реестре |
| `src/hwcheck/kb_cli.py`, `src/hwcheck/cli.py` (изменить) | `hwcheck kb load-rules`, `hwcheck bench ru` |
| `src/hwcheck/bot/fsm.py` (изменить) | `CheckedTask.grade: GradeResult | None`, `subject_task`, `payload`, `reference`; `ChatState.subject`, `conditions` |
| `src/hwcheck/bot/summary.py`, `bot/clarify.py` (изменить) | работа без `grade` |
| `src/hwcheck/bot/onboarding/router.py`, `parent.py` (изменить) | `CheckPhotos.subject` |
| `src/hwcheck/bot/handlers.py` (изменить) | маршрут по предмету, путь языков через `SubjectPage`, `photo_index`, `ocr_failed`, тьютор по модулю предмета |
| `src/hwcheck/bot/runner.py`, `config.py` (изменить) | `OcrClient`, `PgKnowledgeBase`, словарь в `SubjectDeps`; `kb_photos_dir`, `kb_photos_ttl_days` |
| `src/hwcheck/bench/russian.py`, `bench/golden_ru/*.json` (создать) | стенд русского: golden-кейсы с ожидаемыми находками, метрики |
| `src/hwcheck/bot/subjects.py`, `docs/deploy.md`, `docs/components.md`, `docs/PROJECT_MEMORY.md` (изменить) | флаг `russian.available`, runbook OCR, реестр компонентов |

Тесты — `tests/test_ru_*.py` по задачам (`test_ru_gaps.py`, `test_ru_recognize.py`, `test_ru_align.py`, `test_ru_module.py`, `test_ru_rules.py`, `test_ru_tutor.py`, `test_bot_russian.py`, `test_bench_ru.py`) и `tests/test_ocr_engine.py`.

Порядок: R1 и R2 независимы (можно параллельно), R3 → R4 → R5 (R5 нужны R2–R4), R6 параллельно с R3–R5, R7 после R5, R8 после R7, R9 после R8.

---

### Task 1 (R1): Движок ReadingPipeline в контейнере OCR

**Files:**
- Modify: `ocrsvc/engine.py` (заменить `ReadingPipelineEngine`)
- Create: `ocrsvc/patch_no_torch.py` (перенос `spikes/ocr_memory/patch_no_torch.py` без изменений кода, докстринг «не для прода» убрать)
- Modify: `ocrsvc/Dockerfile`, `ocrsvc/requirements.txt`, `ocrsvc/README.md`, `ocrsvc/server.py` (комментарий о лимите), `docker-compose.yml` (сервис `ocr`), `pyproject.toml` (mypy overrides)
- Test: `tests/test_ocr_engine.py`

**Interfaces:**
- Consumes: контракт `Engine.recognize(image: bytes) -> list[dict]` из `ocrsvc/engine.py`; HTTP-сервер `ocrsvc/server.py` без изменений.
- Produces: `ReadingPipelineEngine(weights_dir, threads=2)`; функции `decode_image(image: bytes) -> "np.ndarray"` и `words_from_predictions(pred: dict) -> list[dict]` (чистые, тестируются без весов); переменные окружения `OCR_ENGINE=readingpipeline`, `OCR_WEIGHTS`, `OCR_THREADS`, `OCR_ORT_ARENA` (по умолчанию `0`), `OCR_ORT_MEMPATTERN` (`0`).

- [ ] **Step 1: Тест конвертации предсказаний и декодирования (RED)**

`tests/test_ocr_engine.py`:

```python
"""Движок ReadingPipeline: чистые части (конвертация предсказаний, декодирование входа)
проверяются без весов и без onnxruntime."""

import io

import pytest
from PIL import Image

from ocrsvc.engine import MAX_IMAGE_PIXELS, decode_image, words_from_predictions


def test_words_from_predictions_keeps_text_class_only() -> None:
    pred = {
        "predictions": [
            {"class_name": "shrinked_text", "text": "машына", "bbox": [1, 2, 30, 20],
             "confidence": 0.71, "line_idx": 0},
            {"class_name": "shrinked_text", "text": "едет", "bbox": [35, 2, 60, 20],
             "confidence": 0.9, "line_idx": 0},
            {"class_name": "pupil_comment", "text": "зачёркнуто", "bbox": [0, 0, 1, 1]},
        ]
    }  # fmt: skip
    assert words_from_predictions(pred) == [
        {"text": "машына", "box": [1, 2, 30, 20], "confidence": 0.71, "line": 0},
        {"text": "едет", "box": [35, 2, 60, 20], "confidence": 0.9, "line": 0},
    ]


def test_words_from_predictions_tolerates_missing_fields() -> None:
    pred = {"predictions": [{"class_name": "shrinked_text", "text": None}]}
    assert words_from_predictions(pred) == [
        {"text": "", "box": None, "confidence": None, "line": None}
    ]


def _jpeg(width: int, height: int, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    out = io.BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    image.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def test_decode_image_applies_exif_rotation() -> None:
    # ориентация 6 = поворот на 90°: телефон снял «лёжа», пиксели хранятся повернутыми
    array = decode_image(_jpeg(40, 20, orientation=6))
    assert array.shape[:2] == (40, 20)  # (высота, ширина): после поворота вертикальное
    assert array.shape[2] == 3  # BGR для cv2-пайплайна


def test_decode_image_rejects_huge_input() -> None:
    huge = Image.new("L", (1, 1))
    huge = huge.resize((MAX_IMAGE_PIXELS // 1000 + 1, 1000))  # чуть больше лимита
    out = io.BytesIO()
    huge.save(out, format="PNG")
    with pytest.raises(ValueError, match="слишком большое"):
        decode_image(out.getvalue())
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_ocr_engine.py -v`
Expected: FAIL, `ImportError: cannot import name 'decode_image'`.

- [ ] **Step 3: Реализация движка**

`ocrsvc/engine.py` — заменить класс `ReadingPipelineEngine` и добавить функции (остальное без изменений):

```python
import numpy as np  # numpy есть у движка; в тестах бота ставится через pillow? нет — добавить в dev

MAX_IMAGE_PIXELS = 30_000_000  # ~ 6000×5000: больше — не фото тетради с телефона
TEXT_CLASS = "shrinked_text"


def decode_image(image: bytes) -> "np.ndarray[Any, Any]":
    """Байты → BGR-массив для ReadingPipeline (cv2-соглашение).

    EXIF-поворот применяется здесь: телефон хранит пиксели «лёжа» и пишет ориентацию в EXIF,
    cv2.imdecode её игнорирует и сегментация получила бы повёрнутую страницу (бэклог OCR из
    ревью каркаса). Лимит пикселей — до раскодирования: decompression bomb в общем контейнере
    с лимитом 2 ГБ убил бы и текущую проверку.
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image)) as source:
        width, height = source.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"изображение слишком большое: {width}×{height}")
        rotated = ImageOps.exif_transpose(source) or source
        rgb = np.asarray(rotated.convert("RGB"))
    return rgb[:, :, ::-1].copy()  # RGB → BGR


def words_from_predictions(pred: dict[str, Any]) -> list[dict[str, Any]]:
    """Формат ответа сервиса (ocrsvc/README.md) из предсказаний PipelinePredictor."""
    return [
        {
            "text": p.get("text") or "",
            "box": p.get("bbox"),
            "confidence": p.get("confidence"),
            "line": p.get("line_idx"),
        }
        for p in pred.get("predictions", [])
        if p.get("class_name") == TEXT_CLASS
    ]


class ReadingPipelineEngine:
    """ReadingPipeline (ai-forever) на ONNX/CPU без языковой модели и без torch.

    Настройки onnxruntime по спайку памяти 17.09: арена и mem-pattern выключены (пик 2,5 → 1,46 ГБ
    при побайтово том же тексте); сегментация и OCR — родное разрешение весов (896×896 / 64×512),
    понижать нельзя (теряется 10–31 % слов).
    """

    name = "readingpipeline"

    def __init__(self, weights_dir: str, *, threads: int = 2) -> None:
        import onnxruntime as ort  # type: ignore[import-not-found]

        arena = os.environ.get("OCR_ORT_ARENA", "0") == "1"
        mem_pattern = os.environ.get("OCR_ORT_MEMPATTERN", "0") == "1"
        original = ort.SessionOptions

        def session_options(*args: Any, **kwargs: Any) -> Any:
            options = original(*args, **kwargs)
            options.enable_cpu_mem_arena = arena
            options.enable_mem_pattern = mem_pattern
            return options

        # апстримный OCR-model/SEGM-model создаёт ort.SessionOptions() сам; подменяем конструктор
        # в общем модуле, не правя клоны (как в спайке)
        ort.SessionOptions = session_options
        os.chdir(weights_dir)  # пути в pipeline_config.json относительные
        config_path = _runtime_config(weights_dir, threads)
        from ocrpipeline.predictor import PipelinePredictor  # type: ignore[import-not-found]

        self._predictor = PipelinePredictor(pipeline_config_path=config_path)

    def recognize(self, image: bytes) -> list[dict[str, Any]]:
        _rotated, pred = self._predictor(decode_image(image))
        return words_from_predictions(pred)


def _runtime_config(weights_dir: str, threads: int) -> str:
    """pipeline_config.json весов настроен на GPU/PyTorch/KenLM; для CPU-ONNX без LM пишем копию."""
    original = os.path.join(weights_dir, "pipeline_config.json")
    runtime = os.path.join(weights_dir, "pipeline_config.runtime.json")
    with open(original, encoding="utf-8") as source:
        config = json.load(source)
    main_process = config["main_process"]
    for step, onnx_path in (
        ("SegmPrediction", "segm/segm_model.onnx"),
        ("OCRPrediction", "ocr/ocr_model.onnx"),
    ):
        main_process[step]["device"] = "cpu"
        main_process[step]["runtime"] = "ONNX"
        main_process[step]["model_path"] = onnx_path
        main_process[step]["num_threads"] = threads
    main_process["OCRPrediction"]["lm_path"] = ""  # LM выключен (исследование 13.09)
    with open(runtime, "w", encoding="utf-8") as out:
        json.dump(config, out, ensure_ascii=False, indent=1)
    return runtime


def engine_from_env() -> Engine:
    if os.environ.get("OCR_ENGINE", "fake") == "readingpipeline":
        return ReadingPipelineEngine(
            os.environ.get("OCR_WEIGHTS", "/app/weights"),
            threads=int(os.environ.get("OCR_THREADS", "2")),
        )
    return FakeEngine()
```

Добавить в начало файла `import io`, `import json`, `from typing import Any` (уже есть). `numpy` и `pillow` нужны движку в образе (в `requirements.txt`) и тестам бота — в `pyproject.toml` группа `dev` получает `"numpy>=1.26"` (pillow уже есть в runtime). В `[tool.mypy.overrides]` добавить `module = ["onnxruntime.*", "ocrpipeline.*", "cv2.*"]` с `ignore_missing_imports = true`.

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_ocr_engine.py tests/test_ocr_server.py -v && uv run mypy`
Expected: PASS; mypy без ошибок (импорты внутри `__init__` с `type: ignore[import-not-found]`).

- [ ] **Step 5: Образ по рецепту спайка**

`ocrsvc/requirements.txt`:

```
# ReadingPipeline (ai-forever) на ONNX/CPU без torch — версии из spikes/ocr_memory/Dockerfile.notorch
numpy==1.23.1
opencv-python-headless==4.6.0.66
tqdm==4.62.3
matplotlib==3.5.0
Pillow==8.4.0
pyclipper==1.3.0
shapely==1.8.0
onnxruntime==1.17.3
openvino==2024.6.0
pandas==1.3.4
scikit-learn==1.3.2
scipy==1.10.1
albumentations==1.1.0
huggingface_hub
```

`ocrsvc/Dockerfile`:

```dockerfile
# OCR-сервис «Домашки»: ReadingPipeline (ai-forever, MIT) на ONNX/CPU без языковой модели и без
# torch. Рецепт — docs/research/2026-09-17-readingpipeline-memory.md (пик 1,46 ГБ на страницу).
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends git g++ libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ocrsvc/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# апстрим не запинен по хэшу: patch_no_torch.py падает assert'ом на сборке, если структура
# файлов изменилась (явная ошибка вместо тихо сломанного пайплайна)
RUN git clone --depth 1 https://github.com/ai-forever/ReadingPipeline.git \
    && git clone --depth 1 https://github.com/ai-forever/OCR-model.git \
    && git clone --depth 1 https://github.com/ai-forever/SEGM-model.git
RUN printf 'class CTCBeamDecoder:\n    def __init__(self, *a, **kw):\n        raise NotImplementedError("LM disabled")\n' \
        > /usr/local/lib/python3.10/site-packages/ctcdecode.py
COPY ocrsvc/patch_no_torch.py ./patch_no_torch.py
RUN python patch_no_torch.py && pip install --no-cache-dir --no-deps ./OCR-model ./SEGM-model
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('ai-forever/ReadingPipeline-notebooks', local_dir='/app/weights', \
    ignore_patterns=['*.arpa', '*.ipynb', '*.jpg', 'ocr/*.ckpt', 'segm/*.ckpt'])"

ENV PYTHONPATH=/app:/app/ReadingPipeline \
    MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=2 OCR_THREADS=2 \
    OCR_ORT_ARENA=0 OCR_ORT_MEMPATTERN=0 \
    OCR_ENGINE=readingpipeline OCR_WEIGHTS=/app/weights
COPY ocrsvc ./ocrsvc
RUN useradd --uid 1000 --create-home app && chown -R app:app /app
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "ocrsvc.server"]
```

`docker-compose.yml`, сервис `ocr`: `memory: 2g` (комментарий: спайк памяти 17.09, пик 1,46 ГБ + запас), `restart: unless-stopped`, `OCR_ENGINE: ${OCR_ENGINE:-readingpipeline}`; профиль `ocr` оставить (запуск явный). В `ocrsvc/server.py` комментарий у семафора: «пик 1,46 ГБ при лимите 2 ГБ — два параллельных прогона не помещаются».

`ocrsvc/README.md`: обновить раздел «Движок» (readingpipeline — реальный), «Ограничение памяти — 2 ГБ», переменные `OCR_ORT_ARENA/OCR_ORT_MEMPATTERN/OCR_THREADS`.

- [ ] **Step 6: Локальная сборка и дымовой тест (Docker недоступен локально — на VPS, см. R9); здесь — только линт**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: чисто.

- [ ] **Step 7: Commit**

```bash
git add ocr tests/test_ocr_engine.py docker-compose.yml pyproject.toml uv.lock
git commit -m "feat(ocr): движок ReadingPipeline без torch — EXIF-поворот, лимит пикселей, арена onnxruntime выключена"
```

---

### Task 2 (R2): Словарь Hunspell и заполнение пропусков

**Files:**
- Create: `assets/hunspell/ru_RU.dic`, `assets/hunspell/ru_RU.aff`, `assets/hunspell/README_ru_RU.txt` (из `github.com/LibreOffice/dictionaries/tree/master/ru_RU`, как в спайке)
- Create: `src/hwcheck/subjects/russian/__init__.py` (пустой), `src/hwcheck/subjects/russian/gaps.py`, `prompts/ru_gaps/v1.md`
- Modify: `pyproject.toml` (`spylls>=0.1.7` в dependencies), `Dockerfile` (`COPY assets ./assets`), `docs/components.md` (spylls MIT; словарь ru_RU BSD-подобная, автор Alexander I. Lebedev)
- Test: `tests/test_ru_gaps.py`

**Interfaces:**
- Consumes: `hwcheck.llm.base.LLMClient`, `chat_structured`, `load_prompt`; `Trust` из `subjects/base.py`.
- Produces:
  - `class Dictionary(Protocol): def lookup(self, word: str) -> bool`
  - `class HunspellDictionary(Dictionary)`, `HunspellDictionary.load(path: Path = ASSETS / "ru_RU") -> HunspellDictionary`
  - `tokenize(text: str) -> list[str]` — слова (кириллица, дефис внутри, `_` и скобки сохраняются), пунктуация отброшена
  - `class Gap(BaseModel): index: int; pattern: str; candidates: list[str]`
  - `find_gaps(words: list[str], dictionary) -> list[Gap]`
  - `fill_gap(pattern: str, dictionary: Dictionary) -> list[str]` (кэш по паттерну, `MAX_SLOTS = 3`)
  - `class DerivedText(BaseModel): words: list[str]; gap_indices: list[int]; trust: Trust; derived_by: str; unresolved: list[int]`
  - `async derive_text(text: str, dictionary, llm: LLMClient | None, *, model: str, prompt_version="v1") -> DerivedText`

- [ ] **Step 1: Тесты (RED)**

`tests/test_ru_gaps.py`:

```python
"""Заполнение пропусков «вставь буквы» по словарю: один кандидат → verified, иначе LLM."""

import json

import pytest

from hwcheck.subjects.russian.gaps import (
    MAX_SLOTS,
    Dictionary,
    HunspellDictionary,
    derive_text,
    fill_gap,
    find_gaps,
    tokenize,
)
from tests.conftest import FakeLLMClient


class SetDictionary:
    def __init__(self, *words: str) -> None:
        self._words = set(words)

    def lookup(self, word: str) -> bool:
        return word in self._words


WORDS: Dictionary = SetDictionary(
    "машина", "щука", "щека", "лиса", "леса", "сделать", "с", "делать", "погода", "поздняя"
)


def test_tokenize_keeps_gaps_and_brackets_drops_punctuation() -> None:
    assert tokenize("Наступила п_здняя осень. (С)делать — быстро, м_шина!") == [
        "Наступила", "п_здняя", "осень", "(С)делать", "быстро", "м_шина"
    ]  # fmt: skip


def test_fill_gap_single_and_ambiguous() -> None:
    assert fill_gap("м_шина", WORDS) == ["машина"]
    assert sorted(fill_gap("щ_ка", WORDS)) == ["щека", "щука"]
    assert fill_gap("х_х", WORDS) == []
    assert fill_gap("_" * (MAX_SLOTS + 1) + "а", WORDS) == []  # перебор 33^4 не делаем


def test_fill_gap_keeps_case_of_pattern() -> None:
    assert fill_gap("М_шина", WORDS) == ["Машина"]


def test_brackets_joined_or_separate() -> None:
    [gap] = find_gaps(["(с)делать"], WORDS)
    assert sorted(gap.candidates) == ["с делать", "сделать"]  # предлог + слово или приставка


async def test_derive_text_verified_when_all_gaps_single() -> None:
    derived = await derive_text("Наступила п_здняя осень.", WORDS, None, model="m")
    assert derived.words == ["Наступила", "поздняя", "осень"]
    assert (derived.gap_indices, derived.trust, derived.derived_by) == (
        [1],
        "verified",
        "dictionary",
    )


async def test_derive_text_asks_llm_for_ambiguous_and_is_unverified() -> None:
    llm = FakeLLMClient([json.dumps({"choices": [{"index": 3, "word": "леса"}]})])
    derived = await derive_text("За дальние л_са несёт м_шина.", WORDS, llm, model="m")
    assert derived.words == ["За", "дальние", "леса", "несёт", "машина"]
    assert derived.trust == "unverified" and derived.derived_by == "llm:m@v1"
    prompt = llm.calls[0][1].content
    assert "л_са" in prompt and "леса" in prompt and "лиса" in prompt  # кандидаты — из словаря


async def test_derive_text_llm_must_pick_from_candidates() -> None:
    llm = FakeLLMClient([json.dumps({"choices": [{"index": 0, "word": "щёки"}]})])
    derived = await derive_text("щ_ка", WORDS, llm, model="m")
    assert derived.words == ["щ_ка"] and derived.unresolved == [0]  # чужое слово не берём


async def test_derive_text_without_llm_leaves_pattern() -> None:
    derived = await derive_text("щ_ка плывёт", WORDS, None, model="m")
    assert derived.words == ["щ_ка", "плывёт"] and derived.trust == "unverified"
    assert derived.unresolved == [0]


@pytest.mark.slow
def test_real_hunspell_dictionary_loads() -> None:
    dictionary = HunspellDictionary.load()
    assert dictionary.lookup("машина") and not dictionary.lookup("машына")
```

В `pyproject.toml` `[tool.pytest.ini_options]` добавить `markers = ["slow: реальный словарь, секунды на загрузку"]`.

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_ru_gaps.py -v`
Expected: FAIL, `ModuleNotFoundError: hwcheck.subjects.russian`.

- [ ] **Step 3: Реализация**

`uv add spylls` и скачать словарь:

```bash
mkdir -p assets/hunspell
for f in ru_RU.dic ru_RU.aff README_ru_RU.txt; do
  curl -sSL "https://raw.githubusercontent.com/LibreOffice/dictionaries/master/ru_RU/$f" -o "assets/hunspell/$f"
done
```

`src/hwcheck/subjects/russian/gaps.py`:

```python
"""Эталон для «вставь буквы / раскрой скобки» (спецификация §6, спайк Hunspell 17.09).

Пропуск заполняется словарём: ровно одно словарное слово под шаблон → эталон `verified`
(`derived_by=dictionary`); несколько или ноль → GigaChat выбирает из словарных кандидатов по
контексту предложения → `unverified`, очередь `hwcheck kb review`. LLM не придумывает слово —
только выбирает из списка (иначе кандидат отбрасывается).
"""

from __future__ import annotations

import itertools
import re
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, StructuredOutputError, chat_structured
from hwcheck.prompts import load_prompt
from hwcheck.subjects.base import Trust

ASSETS = Path(__file__).resolve().parents[4] / "assets" / "hunspell"
LETTERS = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
MAX_SLOTS = 3  # 33³ = 35 937 обращений к словарю; 4 слота — уже миллион, не перебираем
# предлоги, которые встречаются в «раскрой скобки»: (с)делать, (в)лесу, (по)дороге
PREPOSITIONS = frozenset(
    "с со в во на по за из от до под над о об у к ко при без про через для между".split()
)
_TOKEN = re.compile(r"[А-Яа-яЁё_()]+(?:-[А-Яа-яЁё_()]+)*")
_BRACKET = re.compile(r"^\(([А-Яа-яЁё]+)\)([А-Яа-яЁё]+)$")


class Dictionary(Protocol):
    def lookup(self, word: str) -> bool: ...


class HunspellDictionary:
    def __init__(self, inner: object) -> None:
        self._inner = inner

    @classmethod
    def load(cls, path: Path = ASSETS / "ru_RU") -> HunspellDictionary:
        from spylls.hunspell import Dictionary as Spylls  # секунды на загрузку — один раз

        return cls(Spylls.from_files(str(path)))

    def lookup(self, word: str) -> bool:
        return bool(self._inner.lookup(word))  # type: ignore[attr-defined]


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


class Gap(BaseModel):
    index: int  # позиция слова в tokenize(text)
    pattern: str
    candidates: list[str] = Field(default_factory=list)


def fill_gap(pattern: str, dictionary: Dictionary) -> list[str]:
    """Кандидаты под шаблон `м_шина`: по одной букве на каждый `_`; регистр шаблона сохраняется."""
    return list(_fill(pattern, dictionary))


@lru_cache(maxsize=4096)
def _fill(pattern: str, dictionary: Dictionary) -> tuple[str, ...]:
    slots = pattern.count("_")
    if slots == 0 or slots > MAX_SLOTS:
        return ()
    found: list[str] = []
    for letters in itertools.product(LETTERS, repeat=slots):
        candidate = pattern
        for letter in letters:
            candidate = candidate.replace("_", letter, 1)
        if dictionary.lookup(candidate.lower()):
            found.append(candidate)
    return tuple(found)


def _bracket_candidates(token: str, dictionary: Dictionary) -> list[str]:
    match = _BRACKET.match(token)
    if match is None:
        return []
    prefix, rest = match.group(1), match.group(2)
    candidates = []
    if dictionary.lookup((prefix + rest).lower()):
        candidates.append(prefix + rest)
    if prefix.lower() in PREPOSITIONS and dictionary.lookup(rest.lower()):
        candidates.append(f"{prefix} {rest}")
    return candidates


def find_gaps(words: list[str], dictionary: Dictionary) -> list[Gap]:
    gaps = []
    for index, word in enumerate(words):
        if "_" in word:
            gaps.append(Gap(index=index, pattern=word, candidates=fill_gap(word, dictionary)))
        elif "(" in word:
            gaps.append(
                Gap(index=index, pattern=word, candidates=_bracket_candidates(word, dictionary))
            )
    return gaps


class DerivedText(BaseModel):
    words: list[str]  # слова эталона; неразрешённый пропуск остаётся шаблоном
    gap_indices: list[int]  # позиции в `words`, где был пропуск (для приоритета находок)
    trust: Trust
    derived_by: str  # dictionary | llm:<модель>@<версия промпта>
    unresolved: list[int] = Field(default_factory=list)  # позиции, оставшиеся шаблоном


class _Choice(BaseModel):
    index: int
    word: str


class _Choices(BaseModel):
    choices: list[_Choice]


async def derive_text(
    text: str,
    dictionary: Dictionary,
    llm: LLMClient | None,
    *,
    model: str,
    prompt_version: str = "v1",
) -> DerivedText:
    words = tokenize(text)
    gaps = find_gaps(words, dictionary)
    chosen: dict[int, str] = {g.index: g.candidates[0] for g in gaps if len(g.candidates) == 1}
    ambiguous = [g for g in gaps if len(g.candidates) != 1]
    derived_by = "dictionary"
    if ambiguous and llm is not None:
        derived_by = f"llm:{model}@{prompt_version}"
        chosen |= await _ask_llm(llm, words, ambiguous, model=model, version=prompt_version)
    filled = [chosen.get(i, w) for i, w in enumerate(words)]
    unresolved = [g.index for g in gaps if g.index not in chosen]
    trust: Trust = "verified" if not ambiguous else "unverified"
    return DerivedText(
        words=filled,
        gap_indices=[g.index for g in gaps],
        trust=trust,
        derived_by=derived_by if ambiguous else "dictionary",
        unresolved=unresolved,
    )


async def _ask_llm(
    llm: LLMClient, words: list[str], gaps: list[Gap], *, model: str, version: str
) -> dict[int, str]:
    listing = "\n".join(
        f"{g.index}: {g.pattern} — варианты: {', '.join(g.candidates) or 'словарь не нашёл'}"
        for g in gaps
    )
    messages = [
        ChatMessage(role="system", content=load_prompt("ru_gaps", version)),
        ChatMessage(role="user", content=f"Текст: {' '.join(words)}\n\nПропуски:\n{listing}"),
    ]
    try:
        answer, _ = await chat_structured(llm, messages, _Choices, model=model)
    except StructuredOutputError:
        return {}
    allowed = {g.index: set(g.candidates) for g in gaps}
    # без словарных кандидатов LLM восстанавливает слово свободно — но только словарное
    return {
        c.index: c.word
        for c in answer.choices
        if c.index in allowed and (c.word in allowed[c.index] or not allowed[c.index])
    }
```

`_fill` кэшируется по `(pattern, dictionary)` — `HunspellDictionary` хэшируется по идентичности объекта (один на процесс), `SetDictionary` в тестах — тоже. Для случая «0 кандидатов, LLM восстанавливает свободно» ответ LLM принимается только если `dictionary.lookup(word)` — добавить проверку в генератор (`or (not allowed[c.index] and dictionary.lookup(c.word.lower()))`; для этого передать `dictionary` в `_ask_llm`).

`prompts/ru_gaps/v1.md`:

```
Ты — учитель русского языка начальной школы. Тебе дан текст упражнения «вставь пропущенные буквы / раскрой скобки» и список пропусков с номером слова и словарными вариантами.

Для каждого пропуска выбери ОДИН вариант из предложенных, который подходит по смыслу предложения. Если вариантов нет («словарь не нашёл») — напиши слово сам, в начальной форме той же грамматической формы, что в тексте.

Правила:
- Не меняй другие слова и не исправляй ничего, кроме указанных пропусков.
- Выбирай только из предложенных вариантов, когда они есть.
- Сохраняй регистр первой буквы шаблона.

Верни ТОЛЬКО валидный JSON без markdown:

{"choices": [{"index": 3, "word": "леса"}, {"index": 7, "word": "машина"}]}
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_ru_gaps.py -v && uv run mypy && uv run ruff check src tests`
Expected: PASS (медленный тест словаря — тоже; отметить время загрузки и RSS: `python -c "import resource; from hwcheck.subjects.russian.gaps import HunspellDictionary; HunspellDictionary.load(); print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024, 'MB')"` на Linux/VPS — если > 300 МБ, лимит бота в compose поднять до 1.5g в R9).

- [ ] **Step 5: Реестр компонентов и Dockerfile**

`docs/components.md` §1: строка `spylls | 0.1.x | MIT | Hunspell на Python: словарные кандидаты для пропусков (subjects/russian/gaps.py)`; новый §5 «Словари и данные»: `ru_RU (LibreOffice dictionaries) | BSD-подобная (Alexander I. Lebedev) | assets/hunspell — эталон «вставь буквы»`. В `Dockerfile` бота после `COPY prompts ./prompts` — `COPY assets ./assets`.

- [ ] **Step 6: Commit**

```bash
git add assets/hunspell src/hwcheck/subjects/russian prompts/ru_gaps tests/test_ru_gaps.py pyproject.toml uv.lock Dockerfile docs/components.md
git commit -m "feat(russian): словарь Hunspell и эталон пропусков — один кандидат verified, иначе выбор LLM unverified"
```

---

### Task 3 (R3): Распознавание страниц — роль и текст учебника через vision, тетрадь через OCR

**Files:**
- Create: `src/hwcheck/subjects/russian/recognize.py`, `prompts/ru_page/v1.md`
- Modify: `src/hwcheck/subjects/base.py` (`SubjectTask.photo_path: str | None = None`; `SubjectPage.failure: Literal["ocr_failed"] | None = None`)
- Test: `tests/test_ru_recognize.py`

**Interfaces:**
- Consumes: `VisionClient.analyze_image`, `normalize_image`, `rotate_image` (`pipeline/normalize.py`), `extract_json`; `OcrClient.recognize(image) -> list[Word]`, `OcrError`.
- Produces:
  - `class RuExercise(BaseModel): number: str | None; instruction: str = ""; text: str`
  - `class RuPage(BaseModel): role: Literal["textbook", "notebook", "unknown"]; exercises: list[RuExercise] = []; comment: str | None = None`
  - `async recognize_page(client: VisionClient, image: bytes, *, model: str, prompt_version="v1") -> tuple[RuPage, Usage]` — пробует ориентации `(0, 270, 90)`, пока роль `unknown` и нет упражнений
  - `textbook_tasks(page: RuPage, photo_path: str | None) -> list[SubjectTask]`
  - `notebook_task(words: list[Word]) -> SubjectTask` — одно задание на страницу тетради; номер из первых двух строк (`Упр. 245`, `№ 245`, `245.`)
  - `task_kind(condition: str) -> str` — `fill_letters | expand_brackets | copy`

- [ ] **Step 1: Тесты (RED)**

`tests/test_ru_recognize.py`:

```python
"""Русский: роль страницы и печатный текст — vision; тетрадь — слова OCR → задание."""

import json
from collections.abc import Sequence

from hwcheck.llm.base import ChatMessage, LLMResult
from hwcheck.subjects.base import Box, Word
from hwcheck.subjects.russian.recognize import (
    RuExercise,
    RuPage,
    notebook_task,
    recognize_page,
    task_kind,
    textbook_tasks,
)


class FakeVision:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def analyze_image(
        self, image: bytes, *, prompt: str, model: str, filename: str = "image.jpg"
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(content=self._responses.pop(0), model=model, tokens_in=7, tokens_out=3)

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str, temperature: float = 0.1
    ) -> LLMResult:
        raise AssertionError("chat не нужен")


TEXTBOOK = json.dumps(
    {
        "role": "textbook",
        "exercises": [
            {"number": "245", "instruction": "Спиши, вставляя буквы.", "text": "Наступила п_здняя ос_нь."}
        ],
    },
    ensure_ascii=False,
)  # fmt: skip
NOTEBOOK = json.dumps({"role": "notebook", "exercises": []})


async def test_recognize_textbook_page() -> None:
    vision = FakeVision([TEXTBOOK])
    page, usage = await recognize_page(vision, b"img", model="v")
    assert page.role == "textbook" and page.exercises[0].number == "245"
    assert (usage.calls, usage.tokens) == (1, 10)


async def test_notebook_page_has_no_text_from_vision() -> None:
    vision = FakeVision([NOTEBOOK])
    page, _ = await recognize_page(vision, b"img", model="v")
    assert page.role == "notebook" and page.exercises == []


async def test_unknown_role_tries_next_orientation_then_gives_up() -> None:
    unknown = json.dumps({"role": "unknown", "exercises": [], "comment": "пусто"})
    vision = FakeVision([unknown, unknown, unknown])
    page, usage = await recognize_page(vision, b"img", model="v")
    assert page.role == "unknown" and usage.calls == 3


async def test_invalid_json_counts_as_unknown() -> None:
    vision = FakeVision(["не json", NOTEBOOK])
    page, _ = await recognize_page(vision, b"img", model="v")
    assert page.role == "notebook"


def test_textbook_tasks_number_and_kind() -> None:
    page = RuPage(
        role="textbook",
        exercises=[
            RuExercise(number="245", text="Наступила п_здняя ос_нь."),
            RuExercise(number=None, text="(С)делать уроки."),
            RuExercise(number=None, text="Спиши текст."),
        ],
    )
    tasks = textbook_tasks(page, photo_path="2026-09-17/abc.jpg")
    assert [(t.number, t.number_on_page) for t in tasks] == [
        ("245", True),
        ("2", False),
        ("3", False),
    ]
    assert (
        tasks[0].condition == "Наступила п_здняя ос_нь."
        and tasks[0].photo_path == "2026-09-17/abc.jpg"
    )
    assert [task_kind(t.condition) for t in tasks] == ["fill_letters", "expand_brackets", "copy"]


def _word(text: str, line: int, x: int = 0) -> Word:
    return Word(text=text, box=Box(x0=x, y0=line * 20, x1=x + 30, y1=line * 20 + 15), line=line)


def test_notebook_task_number_from_header_and_lines() -> None:
    words = [
        _word("Упражнение", 0), _word("245.", 0, 40),
        _word("Наступила", 1), _word("позняя", 1, 40), _word("осень.", 1, 80),
    ]  # fmt: skip
    task = notebook_task(words)
    assert (task.number, task.number_on_page) == ("245", True)
    assert task.lines == ["Упражнение 245.", "Наступила позняя осень."]
    assert task.words == words


def test_notebook_task_without_number() -> None:
    task = notebook_task([_word("Наступила", 0), _word("осень", 0, 40)])
    assert (task.number, task.number_on_page) == ("1", False)


def test_notebook_task_orders_words_by_line_then_x() -> None:
    task = notebook_task([_word("осень", 0, 40), _word("Наступила", 0, 0)])
    assert task.lines == ["Наступила осень"]
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_ru_recognize.py -v`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Реализация**

`src/hwcheck/subjects/base.py` — добавить поля:

```python
class SubjectTask(BaseModel):
    ...
    # путь фото учебника в var/kb_photos (проставляет бот): страница сохраняется в базе знаний
    photo_path: str | None = None


class SubjectPage(BaseModel):
    ...
    # OCR-сервис недоступен/упал: бот пишет событие ocr_failed, проверка не падает (спецификация §8)
    failure: Literal["ocr_failed"] | None = None
```

`prompts/ru_page/v1.md`:

```
Ты видишь фотографию страницы. Определи, что это, и, если это страница УЧЕБНИКА русского языка, перепиши печатный текст упражнений.

role:
- "textbook" — печатная страница учебника или рабочей тетради с заданиями (типографский шрифт);
- "notebook" — рукописная тетрадь ученика;
- "unknown" — ни то, ни другое, пустая страница или нечитаемое фото (в comment коротко почему).

Для "textbook" перепиши каждое упражнение:
- number — номер упражнения, как напечатан («245»), или null;
- instruction — формулировка задания («Спиши, вставляя пропущенные буквы.»);
- text — текст упражнения ТОЧНО как напечатан: пропущенные буквы обозначай одним символом «_» на каждую букву (м_шина), скобки сохраняй ((с)делать), знаки препинания и порядок слов не меняй. НЕ заполняй пропуски и НЕ исправляй текст.

Для "notebook" НИЧЕГО не переписывай — exercises должен быть пустым списком: рукописный текст ученика читает другая программа.

Верни ТОЛЬКО валидный JSON без markdown:

{"role": "textbook", "exercises": [{"number": "245", "instruction": "Спиши, вставляя пропущенные буквы.", "text": "Наступила п_здняя ос_нь."}], "comment": null}
```

`src/hwcheck/subjects/russian/recognize.py`:

```python
"""Распознавание страниц для русского языка (спецификация §6).

Учебник — печатный текст, его читает GigaChat-vision (с пропусками «как напечатано»). Тетрадь —
рукопись ученика, её vision НЕ читает (исследование 13.09: скрывает 78–80 % ошибок), только
определяет роль; слова «как написано» даёт OCR-сервис (`OcrClient`).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from hwcheck.llm.base import VisionClient, extract_json
from hwcheck.pipeline.normalize import normalize_image, rotate_image
from hwcheck.prompts import load_prompt
from hwcheck.subjects.base import SubjectTask, Usage, Word

ORIENTATIONS = (0, 270, 90)
_NUMBER = re.compile(r"(?:упр\w*\.?|№|n)\s*(\d{1,4})|^(\d{1,4})\.?$", re.IGNORECASE)


class RuExercise(BaseModel):
    number: str | None = None
    instruction: str = ""
    text: str


class RuPage(BaseModel):
    role: Literal["textbook", "notebook", "unknown"]
    exercises: list[RuExercise] = Field(default_factory=list)
    comment: str | None = None


async def recognize_page(
    client: VisionClient, image: bytes, *, model: str, prompt_version: str = "v1"
) -> tuple[RuPage, Usage]:
    """Роль страницы и печатные упражнения; на «unknown» пробуем повернуть фото (как vision.py)."""
    prompt = load_prompt("ru_page", prompt_version)
    normalized = normalize_image(image)
    usage = Usage()
    page = RuPage(role="unknown")
    for degrees in ORIENTATIONS:
        data = normalized if degrees == 0 else rotate_image(normalized, degrees)
        result = await client.analyze_image(data, prompt=prompt, model=model, filename="page.jpg")
        usage.calls += 1
        usage.tokens += result.tokens_in + result.tokens_out
        try:
            page = RuPage.model_validate_json(extract_json(result.content))
        except ValidationError:
            page = RuPage(role="unknown", comment="ответ модели не разобран")
        if page.role != "unknown":
            return page, usage
    return page, usage


def task_kind(condition: str) -> str:
    if "_" in condition:
        return "fill_letters"
    if "(" in condition:
        return "expand_brackets"
    return "copy"


def textbook_tasks(page: RuPage, photo_path: str | None) -> list[SubjectTask]:
    return [
        SubjectTask(
            number=exercise.number or str(index),
            number_on_page=exercise.number is not None,
            condition=exercise.text.strip(),
            photo_path=photo_path,
        )
        for index, exercise in enumerate(page.exercises, start=1)
        if exercise.text.strip()
    ]


def notebook_task(words: list[Word]) -> SubjectTask:
    """Страница тетради — одно задание: строки по `line`, слова слева направо."""
    ordered = sorted(words, key=lambda w: (w.line or 0, w.box.x0 if w.box else 0))
    lines: dict[int, list[str]] = {}
    for word in ordered:
        lines.setdefault(word.line or 0, []).append(word.text)
    texts = [" ".join(parts) for _, parts in sorted(lines.items())]
    number = _header_number(texts[:2])
    return SubjectTask(
        number=number or "1",
        number_on_page=number is not None,
        lines=texts,
        words=ordered,
        confidence=min((w.confidence for w in words if w.confidence is not None), default=1.0),
    )


def _header_number(lines: list[str]) -> str | None:
    for line in lines:
        for token in line.split():
            match = _NUMBER.search(token) or _NUMBER.search(line)
            if match:
                return match.group(1) or match.group(2)
    return None
```

`_header_number`: сначала по строке целиком («Упражнение 245.»), затем по одиночным токенам «245.» — оставить один проход: `match = _NUMBER.search(line)`; если нет — по токенам `^(\d{1,4})\.?$`. Упростить до этого в реализации; тест «Упражнение 245.» проходит через первый шаблон.

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_ru_recognize.py tests/test_subjects_base.py -v && uv run mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/subjects/base.py src/hwcheck/subjects/russian/recognize.py prompts/ru_page tests/test_ru_recognize.py
git commit -m "feat(russian): роль страницы и печатный текст учебника через vision, страница тетради из слов OCR"
```

---

### Task 4 (R4): Выравнивание и находки

**Files:**
- Create: `src/hwcheck/subjects/russian/align.py`, `src/hwcheck/subjects/russian/check.py`
- Test: `tests/test_ru_align.py`, `tests/test_ru_check.py`

**Interfaces:**
- Consumes: `DerivedText` (R2), `SubjectTask.words` (`Word` с `box`, `confidence`, `line`, `photo_index`), `Finding`.
- Produces:
  - `normalize(word: str) -> str` — нижний регистр, `ё → е`, без пунктуации и `_`/скобок
  - `class Pair(BaseModel): kind: Literal["match", "subst", "missing", "extra"]; expected_index: int | None; actual_index: int | None`
  - `align(expected: list[str], actual: list[str]) -> list[Pair]` — Левенштейн по словам; замена стоит `0` при равенстве нормализованных форм, `0.5` при буквенном сходстве ≥ `SIMILAR` (0.5), иначе `1.2` (дороже пары «пропуск + лишнее»)
  - `similarity(a: str, b: str) -> float` — `1 − lev/max(len)`
  - `check_words(task_index: int, task: SubjectTask, derived: DerivedText) -> list[Finding]` — находки `spelling` (в пропуске — первыми), `missing_word`, `extra_word`; все `candidate`; если расходится больше `MAX_DIFF_SHARE` (40 %) слов — одна находка `kind="uncertain"` с `detail="не смог сверить с упражнением"`
  - `KIND_DETAIL: dict[str, str]` — тексты для сводки

- [ ] **Step 1: Тесты выравнивания (RED)**

`tests/test_ru_align.py`:

```python
from hwcheck.subjects.russian.align import align, normalize, similarity


def test_normalize() -> None:
    assert normalize("Ёжик,") == "ежик" and normalize("м_шина") == "мшина"


def test_similarity() -> None:
    assert similarity("машина", "машына") == 1 - 1 / 6
    assert similarity("кот", "собака") < 0.5


def test_align_exact_and_substitution() -> None:
    pairs = align(["Наступила", "поздняя", "осень"], ["Наступила", "позняя", "осень"])
    assert [(p.kind, p.expected_index, p.actual_index) for p in pairs] == [
        ("match", 0, 0),
        ("subst", 1, 1),
        ("match", 2, 2),
    ]


def test_align_missing_and_extra() -> None:
    pairs = align(["у", "нас", "гость"], ["у", "гость", "был"])
    assert [(p.kind, p.expected_index, p.actual_index) for p in pairs] == [
        ("match", 0, 0),
        ("missing", 1, None),
        ("match", 2, 1),
        ("extra", None, 2),
    ]


def test_align_prefers_missing_plus_extra_over_unlike_substitution() -> None:
    # «кот» и «собака» не похожи: это не описка, а пропущенное и лишнее слово
    pairs = align(["кот"], ["собака"])
    assert [p.kind for p in pairs] == ["missing", "extra"]
```

- [ ] **Step 2: Тесты находок (RED)**

`tests/test_ru_check.py`:

```python
from hwcheck.subjects.base import Box, SubjectTask, Word
from hwcheck.subjects.russian.check import MAX_DIFF_SHARE, check_words
from hwcheck.subjects.russian.gaps import DerivedText


def _words(*texts: str, confidence: float = 0.9) -> list[Word]:
    return [
        Word(text=t, box=Box(x0=i * 40, y0=0, x1=i * 40 + 30, y1=20), confidence=confidence, line=0)
        for i, t in enumerate(texts)
    ]


def _derived(*words: str, gaps: list[int] | None = None) -> DerivedText:
    return DerivedText(
        words=list(words), gap_indices=gaps or [], trust="verified", derived_by="dictionary"
    )


def test_all_correct_gives_no_findings() -> None:
    task = SubjectTask(number="1", words=_words("Наступила", "поздняя", "осень"))
    assert check_words(0, task, _derived("Наступила", "поздняя", "осень", gaps=[1])) == []


def test_spelling_in_gap_is_candidate_with_word_and_expected() -> None:
    task = SubjectTask(number="1", words=_words("Наступила", "позняя", "осень"))
    [finding] = check_words(3, task, _derived("Наступила", "поздняя", "осень", gaps=[1]))
    assert (finding.task_index, finding.kind, finding.strength) == (3, "spelling", "candidate")
    assert (finding.actual, finding.expected) == ("позняя", "поздняя")
    assert finding.word is not None and finding.word.box is not None and finding.line == 1
    assert finding.detail == "проверь слово «позняя»"


def test_gap_findings_come_first_then_high_confidence() -> None:
    words = _words("Настипила", "позняя", "осинь")
    words[2] = words[2].model_copy(update={"confidence": 0.3})
    task = SubjectTask(number="1", words=words)
    findings = check_words(0, task, _derived("Наступила", "поздняя", "осень", gaps=[1]))
    assert [f.actual for f in findings] == ["позняя", "Настипила", "осинь"]


def test_missing_and_extra_words() -> None:
    task = SubjectTask(number="1", words=_words("У", "гость", "был"))
    findings = check_words(0, task, _derived("У", "нас", "гость"))
    assert [(f.kind, f.actual, f.expected) for f in findings] == [
        ("missing_word", None, "нас"),
        ("extra_word", "был", None),
    ]
    missing, extra = findings
    assert missing.word is None and extra.word is not None
    assert missing.detail == "кажется, пропущено слово после «У»"


def test_too_many_differences_means_wrong_exercise() -> None:
    task = SubjectTask(number="1", words=_words("совсем", "другой", "текст", "тут"))
    [finding] = check_words(0, task, _derived("Наступила", "поздняя", "осень", "уже"))
    assert (finding.kind, finding.strength) == ("uncertain", "candidate")
    assert finding.detail == "не смог сверить с упражнением"
    assert MAX_DIFF_SHARE == 0.4


def test_unresolved_gap_is_not_a_finding() -> None:
    derived = DerivedText(
        words=["щ_ка", "плывёт"], gap_indices=[0], trust="unverified",
        derived_by="dictionary", unresolved=[0],
    )  # fmt: skip
    task = SubjectTask(number="1", words=_words("щука", "плывёт"))
    assert check_words(0, task, derived) == []  # эталона для слова нет — не спрашиваем


def test_header_line_is_skipped() -> None:
    words = [*_words("Упражнение", "245."), *_words("Наступила", "осень")]
    for w in words[2:]:
        w.line = 1
    task = SubjectTask(number="245", lines=["Упражнение 245.", "Наступила осень"], words=words)
    assert check_words(0, task, _derived("Наступила", "осень")) == []
```

- [ ] **Step 3: Запустить — падают**

Run: `uv run pytest tests/test_ru_align.py tests/test_ru_check.py -v`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 4: Реализация выравнивания**

`src/hwcheck/subjects/russian/align.py`:

```python
"""Выравнивание слов тетради с эталоном (спецификация §6): Левенштейн по словам, замена
оценивается буквенным сходством — похожее слово считается опиской, непохожее — парой
«пропущено + лишнее»."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

SIMILAR = 0.5  # доля совпавших букв, с которой слова считаются «тем же словом с опиской»
_STRIP = re.compile(r"[^а-яa-z0-9-]")


def normalize(word: str) -> str:
    return _STRIP.sub("", word.lower().replace("ё", "е"))


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    longest = max(len(a), len(b))
    return 1.0 if longest == 0 else 1 - levenshtein(a, b) / longest


class Pair(BaseModel):
    kind: Literal["match", "subst", "missing", "extra"]
    expected_index: int | None = None
    actual_index: int | None = None


def _subst_cost(expected: str, actual: str) -> float:
    if normalize(expected) == normalize(actual):
        return 0.0
    return 0.5 if similarity(expected, actual) >= SIMILAR else 1.2


def align(expected: list[str], actual: list[str]) -> list[Pair]:
    n, m = len(expected), len(actual)
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = float(i)
    for j in range(1, m + 1):
        cost[0][j] = float(j)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = min(
                cost[i - 1][j - 1] + _subst_cost(expected[i - 1], actual[j - 1]),
                cost[i - 1][j] + 1.0,  # слово эталона пропущено
                cost[i][j - 1] + 1.0,  # лишнее слово ученика
            )
    pairs: list[Pair] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            step = _subst_cost(expected[i - 1], actual[j - 1])
            if cost[i][j] == cost[i - 1][j - 1] + step:
                kind: Literal["match", "subst"] = "match" if step == 0 else "subst"
                pairs.append(Pair(kind=kind, expected_index=i - 1, actual_index=j - 1))
                i, j = i - 1, j - 1
                continue
        if i > 0 and cost[i][j] == cost[i - 1][j] + 1.0:
            pairs.append(Pair(kind="missing", expected_index=i - 1))
            i -= 1
        else:
            pairs.append(Pair(kind="extra", actual_index=j - 1))
            j -= 1
    pairs.reverse()
    return pairs
```

- [ ] **Step 5: Реализация находок**

`src/hwcheck/subjects/russian/check.py`:

```python
"""Находки по выравниванию (спецификация §6): все — `candidate`, ребёнок подтверждает
«здесь написано …?». В пропуске — первыми, затем по уверенности OCR (уверенное расхождение
вероятнее ошибка ученика, неуверенное — шум распознавания)."""

from __future__ import annotations

from hwcheck.subjects.base import Finding, SubjectTask, Word
from hwcheck.subjects.russian.align import Pair, align
from hwcheck.subjects.russian.gaps import DerivedText
from hwcheck.subjects.russian.recognize import _header_number

MAX_DIFF_SHARE = 0.4  # больше расхождений — это не то упражнение или не та страница
KIND_DETAIL = {
    "spelling": "проверь слово «{actual}»",
    "missing_word": "кажется, пропущено слово после «{previous}»",
    "extra_word": "лишнее слово «{actual}»?",
}
UNCERTAIN_DETAIL = "не смог сверить с упражнением"


def check_words(task_index: int, task: SubjectTask, derived: DerivedText) -> list[Finding]:
    words = _body_words(task)
    expected = derived.words
    if not words or not expected:
        return []
    pairs = align(expected, [w.text for w in words])
    diffs = [p for p in pairs if p.kind != "match"]
    if len(diffs) / max(len(expected), len(words)) > MAX_DIFF_SHARE:
        return [
            Finding(
                task_index=task_index,
                kind="uncertain",
                strength="candidate",
                detail=UNCERTAIN_DETAIL,
            )
        ]
    unresolved = set(derived.unresolved)
    gaps = set(derived.gap_indices)
    scored = [
        (pair.expected_index in gaps, _finding(task_index, pair, expected, words))
        for pair in diffs
        if pair.expected_index not in unresolved
    ]
    # в пропуске → первыми; орфография раньше пропущенных/лишних слов; уверенное OCR раньше шума
    scored.sort(key=lambda item: (not item[0], item[1].kind != "spelling", -_confidence(item[1])))
    return [finding for _, finding in scored]


def _confidence(finding: Finding) -> float:
    return finding.word.confidence if finding.word and finding.word.confidence else 0.0


def _body_words(task: SubjectTask) -> list[Word]:
    """Без строки-заголовка «Упражнение 245.» — её нет в тексте упражнения."""
    first_line = [w for w in task.words if (w.line or 0) == (task.words[0].line or 0)]
    header = task.number_on_page and _header_number([" ".join(w.text for w in first_line)])
    return [w for w in task.words if not (header and w in first_line)]


def _finding(task_index: int, pair: Pair, expected: list[str], words: list[Word]) -> Finding:
    if pair.kind == "subst":
        assert pair.expected_index is not None and pair.actual_index is not None
        word = words[pair.actual_index]
        return Finding(
            task_index=task_index,
            kind="spelling",
            strength="candidate",
            expected=expected[pair.expected_index],
            actual=word.text,
            word=word,
            line=(word.line or 0) + 1,
            detail=KIND_DETAIL["spelling"].format(actual=word.text),
        )
    if pair.kind == "missing":
        assert pair.expected_index is not None
        previous = expected[pair.expected_index - 1] if pair.expected_index else "начала"
        return Finding(
            task_index=task_index,
            kind="missing_word",
            strength="candidate",
            expected=expected[pair.expected_index],
            detail=KIND_DETAIL["missing_word"].format(previous=previous),
        )
    assert pair.actual_index is not None
    word = words[pair.actual_index]
    return Finding(
        task_index=task_index,
        kind="extra_word",
        strength="candidate",
        actual=word.text,
        word=word,
        line=(word.line or 0) + 1,
        detail=KIND_DETAIL["extra_word"].format(actual=word.text),
    )
```

`_body_words`: строка-заголовок отбрасывается, только если номер записан на странице и первая строка
содержит номер (`_header_number` из R3 — сделать публичной `header_number`, импортировать без
подчёркивания).

- [ ] **Step 6: Тесты зелёные**

Run: `uv run pytest tests/test_ru_align.py tests/test_ru_check.py -v && uv run mypy && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/hwcheck/subjects/russian/align.py src/hwcheck/subjects/russian/check.py tests/test_ru_align.py tests/test_ru_check.py
git commit -m "feat(russian): выравнивание слов тетради с эталоном и находки-кандидаты с кропом слова"
```

---

### Task 5 (R5): Модуль `RussianModule` и тьютор по слову

**Files:**
- Create: `src/hwcheck/subjects/russian/module.py`, `src/hwcheck/subjects/russian/rules.py`, `prompts/ru_orthogram/v1.md`, `prompts/ru_tutor/v1.md`
- Modify: `src/hwcheck/pipeline/tutor.py` (`WordTutoring`, ветка в `tutor_reply`), `src/hwcheck/subjects/registry.py`
- Test: `tests/test_ru_module.py`, `tests/test_ru_tutor.py`, `tests/test_ru_rules.py`

**Interfaces:**
- Consumes: R2 `derive_text`, `Dictionary`; R3 `recognize_page`, `textbook_tasks`, `notebook_task`, `task_kind`; R4 `check_words`; `OcrClient`, `OcrError`; `KnowledgeBase` (`find_page`, `save_page`, `answers_for`, `save_answer`, `rule`); `KbPage`, `KbTask`, `KbAnswer`, `KbRule`.
- Produces:
  - `RussianModule(llm: VisionAndChatClient, models: CheckModels, *, ocr: OcrClient | None, dictionary: Dictionary)` с `code = "russian"`
  - `SubjectDeps` получает поля `ocr: OcrClient | None = None`, `kb: KnowledgeBase | None = None`, `dictionary: Dictionary | None = None`; `module_for("russian", deps)`
  - `RULE_CODES: dict[str, str]` (код → название) и `async classify_orthogram(llm, actual, expected, sentence, *, model) -> str | None` в `rules.py`
  - `TutorSession.word: WordTutoring | None` (`WordTutoring(actual, expected, sentence, rule_code, rule_title, rule_statement, rule_example)`); `tutor_reply` для `session.word is not None` использует `prompts/ru_tutor/v1.md`, считает разобранным реплику с ожидаемым словом, до уровня 3 не пропускает ожидаемое слово в ответе
  - `TaskResult.payload` для русского: `{"expected": [...], "gap_indices": [...], "sentence": {actual: "…"}}` — предложение вокруг каждого расхождения для тьютора

- [ ] **Step 1: Тесты модуля (RED)**

`tests/test_ru_module.py`:

```python
"""Русский через контракт SubjectModule: фейки vision, OCR и базы знаний."""

import json

import pytest

from hwcheck.bot.check import CheckModels
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.ocr_client import OcrError
from hwcheck.subjects.base import Box, SubjectTask, Word
from hwcheck.subjects.kb_models import KbRule
from hwcheck.subjects.russian.module import RussianModule
from hwcheck.subjects.registry import SubjectDeps, module_for
from tests.test_ru_gaps import WORDS
from tests.test_ru_recognize import NOTEBOOK, TEXTBOOK, FakeVision

MODELS = CheckModels(vision="v", structure="s", solver="m")


class FakeOcr:
    def __init__(self, words: list[Word] | None) -> None:
        self._words = words

    async def recognize(self, image: bytes) -> list[Word]:
        if self._words is None:
            raise OcrError("ocr: ConnectError")
        return self._words


def _w(text: str, i: int, line: int = 0) -> Word:
    return Word(text=text, box=Box(x0=i * 40, y0=line * 20, x1=i * 40 + 30, y1=line * 20 + 15),
                confidence=0.8, line=line)  # fmt: skip


async def test_recognize_textbook_and_notebook() -> None:
    module = RussianModule(FakeVision([TEXTBOOK, NOTEBOOK]), MODELS, ocr=FakeOcr([_w("осень", 0)]),
                           dictionary=WORDS)  # fmt: skip
    textbook = await module.recognize(b"a")
    assert textbook.role == "textbook" and textbook.tasks[0].condition == "Наступила п_здняя ос_нь."
    notebook = await module.recognize(b"b")
    assert notebook.role == "notebook" and notebook.tasks[0].words[0].text == "осень"
    assert notebook.usage.calls == 1  # vision один раз; OCR не считается вызовом LLM


async def test_recognize_notebook_when_ocr_fails() -> None:
    module = RussianModule(FakeVision([NOTEBOOK]), MODELS, ocr=FakeOcr(None), dictionary=WORDS)
    page = await module.recognize(b"b")
    assert page.role == "notebook" and page.failure == "ocr_failed" and page.tasks == []


async def test_recognize_notebook_without_ocr_client() -> None:
    module = RussianModule(FakeVision([NOTEBOOK]), MODELS, ocr=None, dictionary=WORDS)
    page = await module.recognize(b"b")
    assert page.failure == "ocr_failed"


async def test_resolve_reference_saves_page_and_dictionary_answer() -> None:
    kb = InMemoryKnowledgeBase()
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="245", condition="Наступила п_здняя осень.", photo_path="p.jpg")
    [reference] = await module.resolve_reference([task], kb)
    assert (reference.origin, reference.trust) == ("derived", "verified")
    assert reference.payload["words"] == ["Наступила", "поздняя", "осень"]
    page = await kb.find_page("russian", "Наступила п_здняя осень.")
    assert (
        page is not None
        and page.photo_path == "p.jpg"
        and page.tasks[0].task_kind == "fill_letters"
    )
    [answer] = await kb.answers_for(page.tasks[0].id or 0)
    assert (answer.derived_by, answer.status) == ("dictionary", "verified")
    # второй раз — из базы, без пересчёта
    [again] = await module.resolve_reference([task], kb)
    assert again.origin == "kb" and again.trust == "verified"


async def test_resolve_reference_ambiguous_goes_to_llm_and_review_queue() -> None:
    kb = InMemoryKnowledgeBase()
    llm = FakeVision([])
    llm.chat_responses = [json.dumps({"choices": [{"index": 0, "word": "щука"}]})]
    module = RussianModule(llm, MODELS, ocr=None, dictionary=WORDS)
    [reference] = await module.resolve_reference([SubjectTask(number="1", condition="щ_ка")], kb)
    assert reference.trust == "unverified" and reference.payload["words"] == ["щука"]
    [(_, answer)] = await kb.unverified_answers("russian", 10)
    assert answer.derived_by == "llm:s@v1"


async def test_resolve_reference_without_kb_still_derives() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    [reference] = await module.resolve_reference(
        [SubjectTask(number="1", condition="м_шина")], kb=None
    )
    assert reference.payload["words"] == ["машина"]


async def test_check_produces_candidate_findings_and_sentence_payload() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    [reference] = await module.resolve_reference(
        [SubjectTask(number="1", condition="Наступила п_здняя осень.")], kb=None
    )
    [result] = await module.check([task], [reference])
    [finding] = result.findings
    assert (finding.kind, finding.strength, finding.actual) == ("spelling", "candidate", "позняя")
    assert result.payload["sentence"]["позняя"] == "Наступила позняя осень"


async def test_check_without_reference_is_uncertain() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("осень", 0)])
    [result] = await module.check([task], [])
    [finding] = result.findings
    assert (
        finding.kind == "uncertain"
        and finding.detail == "нет текста упражнения — пришли фото учебника"
    )


async def test_start_tutoring_builds_word_session_with_rule() -> None:
    kb = InMemoryKnowledgeBase()
    await kb.add_rule(
        KbRule(code="ru.orth.unstressed_vowel", subject="russian", grade_from=2,
               title="Безударная гласная в корне", statement="Подбери проверочное слово.",
               example="лесá — лес", finding_kinds=["spelling"])
    )  # fmt: skip
    llm = FakeVision([])
    llm.chat_responses = [json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.9})]
    module = RussianModule(llm, MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    [reference] = await module.resolve_reference(
        [SubjectTask(number="1", condition="Наступила п_здняя осень.")], kb=None
    )
    [result] = await module.check([task], [reference])
    confirmed = result.model_copy(
        update={"findings": [result.findings[0].model_copy(update={"confirmed": True})]}
    )
    session = await module.start_tutoring(confirmed, task, kb)
    assert session.word is not None and session.word.expected == "поздняя"
    assert session.word.rule_title == "Безударная гласная в корне"
    assert session.ref.answer == "поздняя" and session.expected == "поздняя"


async def test_start_tutoring_without_error_raises() -> None:
    module = RussianModule(FakeVision([]), MODELS, ocr=None, dictionary=WORDS)
    task = SubjectTask(number="1", words=[_w("осень", 0)])
    [result] = await module.check([task], [])
    with pytest.raises(ValueError, match="нет подтверждённой ошибки"):
        await module.start_tutoring(result, task, kb=None)


def test_registry_russian() -> None:
    deps = SubjectDeps(llm=FakeVision([]), models=MODELS, cache=None, dictionary=WORDS)  # type: ignore[arg-type]
    assert isinstance(module_for("russian", deps), RussianModule)
```

`FakeVision` в `tests/test_ru_recognize.py` дополнить: атрибут `chat_responses: list[str] = []` и `chat()` возвращает `LLMResult(content=self.chat_responses.pop(0), model=model, tokens_in=5, tokens_out=5)` (вместо `AssertionError`).

- [ ] **Step 2: Тесты тьютора и классификатора (RED)**

`tests/test_ru_tutor.py`:

```python
import json

from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession, WordTutoring, tutor_reply
from tests.conftest import FakeLLMClient


def _session() -> TutorSession:
    return TutorSession(
        task_text="Слово «позняя» в предложении: «Наступила позняя осень».",
        student_steps=[],
        student_answer="позняя",
        ref=RefSolution(steps=[], answer="поздняя", units=None),
        expected="поздняя",
        word=WordTutoring(
            actual="позняя",
            expected="поздняя",
            sentence="Наступила позняя осень",
            rule_code="ru.orth.silent_consonant",
            rule_title="Непроизносимая согласная",
            rule_statement="Подбери слово, где согласная слышится: опоздать.",
            rule_example="поздно — опоздать",
        ),  # fmt: skip
    )


async def test_word_tutor_uses_ru_prompt_and_levels() -> None:
    llm = FakeLLMClient([json.dumps({"reply": "Какая орфограмма спряталась в этом слове?"})])
    reply, session = await tutor_reply(llm, _session(), "Помоги найти ошибку", model="t")
    assert session.hint_level == 1 and not session.resolved
    system = llm.calls[0][0].content
    assert "орфограмм" in system.lower()  # prompts/ru_tutor/v1.md, не математический
    assert "Непроизносимая согласная" in llm.calls[0][1].content


async def test_word_tutor_hides_expected_word_before_level_3() -> None:
    llm = FakeLLMClient(
        [
            json.dumps({"reply": "Правильно пишется «поздняя»!"}),
            json.dumps({"reply": "Подбери проверочное слово, где согласная слышится."}),
        ]
    )
    reply, _ = await tutor_reply(llm, _session(), "не знаю", model="t")
    assert "поздняя" not in reply.lower() and "проверочное" in reply


async def test_word_tutor_resolves_on_expected_word() -> None:
    llm = FakeLLMClient([json.dumps({"reply": "Верно! Опоздать — поздняя."})])
    reply, session = await tutor_reply(llm, _session(), "поздняя", model="t")
    assert session.resolved and session.hint_level == 0


async def test_word_tutor_level_3_reveals_word() -> None:
    session = _session().model_copy(update={"hint_level": 2})
    llm = FakeLLMClient([json.dumps({"reply": "Пишется «поздняя»: проверочное слово — опоздать."})])
    reply, session = await tutor_reply(llm, session, "всё равно не понимаю", model="t")
    assert session.hint_level == 3 and "поздняя" in reply
```

`tests/test_ru_rules.py`:

```python
import json
from pathlib import Path

from hwcheck.subjects.kb_models import KbRule
from hwcheck.subjects.russian.rules import RULE_CODES, classify_orthogram, load_rules
from tests.conftest import FakeLLMClient


async def test_classify_orthogram_returns_known_code() -> None:
    llm = FakeLLMClient([json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.8})])
    code = await classify_orthogram(llm, "машына", "машина", "Едет машына", model="s")
    assert code == "ru.orth.unstressed_vowel"
    assert "машына" in llm.calls[0][1].content and "машина" in llm.calls[0][1].content


async def test_classify_orthogram_unknown_or_unsure_is_none() -> None:
    llm = FakeLLMClient([json.dumps({"rule_code": "ru.orth.made_up", "confidence": 0.9})])
    assert await classify_orthogram(llm, "а", "б", "", model="s") is None
    llm = FakeLLMClient([json.dumps({"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.3})])
    assert await classify_orthogram(llm, "а", "б", "", model="s") is None


def test_rules_file_matches_codes(tmp_path: Path) -> None:
    rules = load_rules(Path("assets/kb/rules_russian.json"))
    assert {r.code for r in rules} == set(RULE_CODES)
    assert all(
        isinstance(r, KbRule) and r.subject == "russian" and r.grade_from >= 1 for r in rules
    )
```

(Файл `assets/kb/rules_russian.json` создаётся в R6; до него последний тест падает — R6 выполнять сразу после R5 или поменять порядок; в R5 положить в `assets/kb/rules_russian.json` пустой список `[]` и в `RULE_CODES` пустой словарь? Нет: `RULE_CODES` — источник истины кодов, задаётся здесь; тест `test_rules_file_matches_codes` пометить `@pytest.mark.skipif(not Path("assets/kb/rules_russian.json").exists(), reason="R6")`.)

- [ ] **Step 3: Запустить — падают**

Run: `uv run pytest tests/test_ru_module.py tests/test_ru_tutor.py tests/test_ru_rules.py -v`
Expected: FAIL (`ImportError: WordTutoring`, `ModuleNotFoundError: ...russian.module`).

- [ ] **Step 4: Тьютор по слову**

`src/hwcheck/pipeline/tutor.py` — добавить модель и ветку:

```python
class WordTutoring(BaseModel):
    """Разбор орфограммы (русский, спецификация §6): уровни 0 «какая орфограмма?», 1 правило,
    2 пример, 3 написание."""

    actual: str
    expected: str
    sentence: str
    rule_code: str | None = None
    rule_title: str | None = None
    rule_statement: str | None = None
    rule_example: str | None = None


class TutorSession(BaseModel):
    ...
    word: WordTutoring | None = None  # None — математика (прежнее поведение)
```

В `tutor_reply` в начале:

```python
    if session.word is not None:
        return await _word_reply(client, session, session.word, student_message, model=model)
```

и функции:

```python
def _mentions(message: str, word: str) -> bool:
    return normalize_word(word) in {normalize_word(t) for t in re.findall(r"[А-Яа-яЁё-]+", message)}


def normalize_word(word: str) -> str:
    return word.lower().replace("ё", "е").strip("-")


async def _word_reply(
    client: LLMClient,
    session: TutorSession,
    word: WordTutoring,
    student_message: str,
    *,
    model: str,
) -> tuple[str, TutorSession]:
    solved_now = _mentions(student_message, word.expected)
    if solved_now:
        session = session.model_copy(update={"resolved": True})
    else:
        session = session.model_copy(
            update={"hint_level": min(session.hint_level + 1, MAX_HINT_LEVEL)}
        )
    messages = [
        ChatMessage(role="system", content=load_prompt("ru_tutor", "v1")),
        ChatMessage(role="user", content=_word_context(session, word, solved_now)),
        *session.history,
        ChatMessage(role="user", content=student_message),
    ]
    try:
        turn, _ = await chat_structured(client, messages, TutorTurn, model=model)
        reply = turn.reply
    except StructuredOutputError:
        reply = SAFE_RETRY
    if not solved_now and session.hint_level < MAX_HINT_LEVEL and _mentions(reply, word.expected):
        retry = [*messages, ChatMessage(role="assistant", content=reply), ChatMessage(
            role="user",
            content="СТОП: в реплике есть правильное написание слова, а уровень подсказки ещё "
            "не 3. Переформулируй подсказку, не называя это слово и не называя букву.",
        )]  # fmt: skip
        try:
            turn, _ = await chat_structured(client, retry, TutorTurn, model=model)
            reply = turn.reply if not _mentions(turn.reply, word.expected) else WORD_REDIRECT
        except StructuredOutputError:
            reply = WORD_REDIRECT
    history = [*session.history, ChatMessage(role="user", content=student_message),
               ChatMessage(role="assistant", content=reply)]  # fmt: skip
    return reply, session.model_copy(update={"history": history})


WORD_REDIRECT = "Не спеши 🙂 Подумай, какое правило здесь работает, и напиши слово ещё раз."


def _word_context(session: TutorSession, word: WordTutoring, solved_now: bool) -> str:
    parts = [
        f"Предложение из тетради: {word.sentence}",
        f"Слово, как написал ученик: {word.actual}",
    ]
    if solved_now:
        parts.append("СИТУАЦИЯ: ученик написал слово ВЕРНО. Похвали и коротко назови правило.")
        return "\n\n".join(parts)
    parts.append(f"Уровень подсказки: {session.hint_level} из 3.")
    if word.rule_title:
        parts.append(f"Орфограмма: {word.rule_title}.")
    if session.hint_level >= 1 and word.rule_statement:
        parts.append(f"Правило (можно пересказать ребёнку): {word.rule_statement}")
    if session.hint_level >= 2 and word.rule_example:
        parts.append(f"Пример на это правило с ДРУГИМ словом: {word.rule_example}")
    if session.hint_level >= MAX_HINT_LEVEL:
        parts.append(f"Уровень 3 — назови верное написание «{word.expected}» и объясни его.")
    else:
        parts.append(f"НЕ называй верное написание («{word.expected}») и НЕ называй нужную букву.")
    return "\n\n".join(parts)
```

`prompts/ru_tutor/v1.md`:

```
Ты — добрый репетитор по русскому языку для ученика начальной школы. Ребёнок подтвердил, что написал слово с ошибкой; твоя задача — не сказать ответ, а помочь найти орфограмму и правило.

Правила:
- Одна–три коротких фразы, простым языком, обращение на «ты», без нравоучений.
- Уровень 0: спроси, какая орфограмма (опасное место) есть в этом слове, где может быть ошибка.
- Уровень 1: назови правило своими словами (оно дано в контексте).
- Уровень 2: покажи, как правило работает на другом слове (пример дан).
- Уровень 3: назови верное написание и объясни.
- До уровня 3 НЕ пиши верное написание слова и не называй нужную букву — это запрещено.
- Если ученик написал слово верно — похвали и коротко закрепи правило.

Верни ТОЛЬКО валидный JSON без markdown:

{"reply": "Посмотри на слово внимательно: в каком месте здесь можно ошибиться?"}
```

- [ ] **Step 5: Классификатор орфограммы и карточки**

`src/hwcheck/subjects/russian/rules.py`:

```python
"""Орфограммы 1–4 класса: коды карточек `kb_rules`, классификатор пары «написано → верно»,
загрузка карточек из assets/kb/rules_russian.json (`hwcheck kb load-rules`)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from hwcheck.llm.base import ChatMessage, LLMClient, StructuredOutputError, chat_structured
from hwcheck.prompts import load_prompt
from hwcheck.subjects.kb_models import KbRule

MIN_CONFIDENCE = 0.6
RULE_CODES: dict[str, str] = {
    "ru.orth.unstressed_vowel": "Безударная гласная в корне, проверяемая ударением",
    "ru.orth.unstressed_vowel_dict": "Непроверяемая безударная гласная (словарное слово)",
    "ru.orth.paired_consonant": "Парная согласная в корне и на конце слова",
    "ru.orth.silent_consonant": "Непроизносимая согласная в корне",
    "ru.orth.zhi_shi": "Сочетания жи–ши, ча–ща, чу–щу",
    "ru.orth.chk_chn": "Сочетания чк, чн, нч, щн без мягкого знака",
    "ru.orth.soft_sign": "Мягкий знак — показатель мягкости",
    "ru.orth.separating_sign": "Разделительные ь и ъ",
    "ru.orth.double_consonant": "Удвоенные согласные",
    "ru.orth.capital": "Заглавная буква в именах собственных и начале предложения",
    "ru.orth.prefix": "Приставки пишутся слитно; приставка и предлог",
    "ru.orth.preposition": "Предлог пишется отдельно от слова",
    "ru.orth.prefix_vowel": "Гласные и согласные в приставках (по-, за-, от-, под-)",
    "ru.orth.suffix": "Суффиксы -ик/-ек, -оньк/-еньк",
    "ru.orth.noun_ending": "Безударные падежные окончания существительных",
    "ru.orth.adj_ending": "Безударные окончания прилагательных",
    "ru.orth.verb_ending": "Безударные личные окончания глаголов",
    "ru.orth.tsya": "-тся и -ться в глаголах",
    "ru.orth.ne_verb": "Не с глаголами",
    "ru.orth.hissing_soft": "Мягкий знак после шипящих на конце существительных и глаголов",
}


class _Orthogram(BaseModel):
    rule_code: str
    confidence: float = Field(ge=0.0, le=1.0)


async def classify_orthogram(
    llm: LLMClient, actual: str, expected: str, sentence: str, *, model: str, version: str = "v1"
) -> str | None:
    """Код орфограммы или None (неуверен / неизвестный код) — тьютор тогда без карточки."""
    codes = "\n".join(f"- {code}: {title}" for code, title in RULE_CODES.items())
    messages = [
        ChatMessage(role="system", content=load_prompt("ru_orthogram", version)),
        ChatMessage(
            role="user",
            content=f"Написано: {actual}\nВерно: {expected}\nПредложение: {sentence}\n\n"
            f"Коды орфограмм:\n{codes}",
        ),
    ]
    try:
        answer, _ = await chat_structured(llm, messages, _Orthogram, model=model)
    except StructuredOutputError:
        return None
    if answer.rule_code not in RULE_CODES or answer.confidence < MIN_CONFIDENCE:
        return None
    return answer.rule_code


def load_rules(path: Path) -> list[KbRule]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("load-rules: ожидается JSON-список карточек")
    return [KbRule.model_validate(item) for item in data]
```

`prompts/ru_orthogram/v1.md`:

```
Ты — методист по русскому языку начальной школы. Дано слово так, как его написал ученик, верное написание и предложение. Определи, на какую орфограмму допущена ошибка, и выбери код из списка.

Правила:
- Сравни буквы: где именно расходятся написание ученика и верное — это и есть орфограмма.
- Выбирай только код из списка. Если подходящего нет или сомневаешься — ставь confidence ниже 0.6.
- confidence — уверенность от 0 до 1.

Верни ТОЛЬКО валидный JSON без markdown:

{"rule_code": "ru.orth.unstressed_vowel", "confidence": 0.85}
```

- [ ] **Step 6: Модуль**

`src/hwcheck/subjects/russian/module.py`:

```python
"""Русский язык через контракт `SubjectModule` (спецификация §6): «спиши, вставь буквы».

recognize — vision решает роль и читает печатный учебник, тетрадь читает OCR; resolve_reference —
эталон из базы знаний или словаря (LLM — только выбор из кандидатов), страница и ответ
сохраняются в базу; check — выравнивание слов, находки-кандидаты; start_tutoring — орфограмма →
карточка правила → сессия тьютора по слову.
"""

from __future__ import annotations

import logging
from typing import Any

from hwcheck.bot.check import CheckModels
from hwcheck.ocr_client import OcrClient, OcrError
from hwcheck.pipeline.solver import RefSolution
from hwcheck.pipeline.tutor import TutorSession, WordTutoring
from hwcheck.pipeline.vision import VisionAndChatClient
from hwcheck.subjects.base import (
    Finding,
    KnowledgeBase,
    Reference,
    SubjectPage,
    SubjectTask,
    TaskResult,
    Word,
)
from hwcheck.subjects.kb_models import KbAnswer, KbPage, KbTask
from hwcheck.subjects.russian.check import check_words
from hwcheck.subjects.russian.gaps import DerivedText, Dictionary, derive_text
from hwcheck.subjects.russian.recognize import (
    notebook_task,
    recognize_page,
    task_kind,
    textbook_tasks,
)
from hwcheck.subjects.russian.rules import classify_orthogram

logger = logging.getLogger(__name__)

NO_REFERENCE_DETAIL = "нет текста упражнения — пришли фото учебника"
OCR_FAILED_DETAIL = "не смог прочитать тетрадь — попробуй переснять"


class RussianModule:
    code = "russian"

    def __init__(
        self,
        llm: VisionAndChatClient,
        models: CheckModels,
        *,
        ocr: OcrClient | None,
        dictionary: Dictionary,
    ) -> None:
        self._llm = llm
        self._models = models
        self._ocr = ocr
        self._dictionary = dictionary

    async def recognize(self, image: bytes) -> SubjectPage:
        page, usage = await recognize_page(self._llm, image, model=self._models.vision)
        if page.role == "textbook":
            return SubjectPage(
                subject=self.code, role="textbook", tasks=textbook_tasks(page, None), usage=usage
            )
        if page.role != "notebook":
            return SubjectPage(
                subject=self.code, role="unknown", tasks=[], comment=page.comment, usage=usage
            )
        words = await self._ocr_words(image)
        if words is None:
            return SubjectPage(
                subject=self.code, role="notebook", tasks=[], failure="ocr_failed", usage=usage
            )
        return SubjectPage(
            subject=self.code, role="notebook", tasks=[notebook_task(words)], usage=usage
        )

    async def _ocr_words(self, image: bytes) -> list[Word] | None:
        if self._ocr is None:
            return None
        try:
            return await self._ocr.recognize(image)
        except OcrError:
            logger.warning("ocr failed", exc_info=True)
            return None

    async def resolve_reference(
        self, tasks: list[SubjectTask], kb: KnowledgeBase | None
    ) -> list[Reference]:
        references = []
        for task in tasks:
            condition = task.condition.strip()
            if not condition:
                continue
            reference = await self._from_kb(task, condition, kb) if kb is not None else None
            if reference is None:
                derived = await derive_text(
                    condition, self._dictionary, self._llm, model=self._models.structure
                )
                reference = _reference(task.number, "derived", derived)
                if kb is not None:
                    await self._save(task, condition, derived, kb)
            references.append(reference)
        return references

    async def _from_kb(
        self, task: SubjectTask, condition: str, kb: KnowledgeBase
    ) -> Reference | None:
        page = await kb.find_page(self.code, condition)
        if page is None or not page.tasks or page.tasks[0].id is None:
            return None
        answers = [a for a in await kb.answers_for(page.tasks[0].id) if a.status != "rejected"]
        if not answers:
            return None
        best = max(answers, key=lambda a: (a.trust == "verified", a.id or 0))
        derived = DerivedText.model_validate(best.answer)
        return Reference(
            task_number=task.number, origin="kb", trust=best.trust, payload=derived.model_dump()
        )

    async def _save(
        self, task: SubjectTask, condition: str, derived: DerivedText, kb: KnowledgeBase
    ) -> None:
        page = KbPage(subject=self.code, text=condition, fingerprint="", photo_path=task.photo_path)
        kb_task = KbTask(number=task.number if task.number_on_page else None, condition=condition,
                         task_kind=task_kind(condition))  # fmt: skip
        saved = await kb.save_page(page, [kb_task])
        if saved.tasks and saved.tasks[0].id is not None:
            checked_by = "dictionary" if derived.trust == "verified" else None
            await kb.save_answer(
                KbAnswer(task_id=saved.tasks[0].id, answer=derived.model_dump(),
                         derived_by=derived.derived_by, checked_by=checked_by)
            )  # fmt: skip

    async def check(
        self, tasks: list[SubjectTask], references: list[Reference]
    ) -> list[TaskResult]:
        by_number = {r.task_number: r for r in references}
        results = []
        for index, task in enumerate(tasks):
            reference = by_number.get(task.number)
            if reference is None and len(references) == 1 and len(tasks) == 1:
                reference = references[0]  # одна страница учебника и одна тетради — это пара
            if reference is None:
                detail = OCR_FAILED_DETAIL if not task.words else NO_REFERENCE_DETAIL
                findings = [Finding(task_index=index, kind="uncertain", strength="candidate",
                                    detail=detail)]  # fmt: skip
                results.append(TaskResult(task_index=index, findings=findings))
                continue
            derived = DerivedText.model_validate(reference.payload)
            findings = check_words(index, task, derived)
            payload: dict[str, Any] = {
                "words": derived.words,
                "gap_indices": derived.gap_indices,
                "sentence": {f.actual: _sentence(task, f) for f in findings if f.actual},
            }
            results.append(
                TaskResult(
                    task_index=index, findings=findings, reference=reference, payload=payload
                )
            )
        return results

    async def start_tutoring(
        self, result: TaskResult, task: SubjectTask, kb: KnowledgeBase | None
    ) -> TutorSession:
        finding = next((f for f in result.findings if f.is_error and f.expected), None)
        if finding is None or finding.actual is None or finding.expected is None:
            raise ValueError("нет подтверждённой ошибки для разбора")
        sentence = result.payload.get("sentence", {}).get(finding.actual, " ".join(task.lines))
        rule_code = await classify_orthogram(
            self._llm, finding.actual, finding.expected, sentence, model=self._models.structure
        )
        rule = await kb.rule(rule_code) if kb is not None and rule_code else None
        word = WordTutoring(
            actual=finding.actual,
            expected=finding.expected,
            sentence=sentence,
            rule_code=rule_code,
            rule_title=rule.title if rule else None,
            rule_statement=rule.statement if rule else None,
            rule_example=rule.example if rule else None,
        )
        return TutorSession(
            task_text=f"Слово «{finding.actual}» в предложении: «{sentence}».",
            student_steps=[],
            student_answer=finding.actual,
            ref=RefSolution(steps=[], answer=finding.expected, units=None),
            expected=finding.expected,
            word=word,
        )


def _reference(number: str, origin: str, derived: DerivedText) -> Reference:
    return Reference(
        task_number=number,
        origin="derived" if origin == "derived" else "kb",
        trust=derived.trust,
        payload=derived.model_dump(),
    )


def _sentence(task: SubjectTask, finding: Finding) -> str:
    """Строка тетради с этим словом — контекст для тьютора и классификатора."""
    if finding.line is None or not 1 <= finding.line <= len(task.lines):
        return " ".join(task.lines)
    return task.lines[finding.line - 1].strip(".,!?")
```

`TutorSession.word` в `start_tutoring` — на `finding.rule_code` тоже записать код: бот пишет `rule_code` в `findings` при создании (до разбора кода нет) — оставить как есть, код орфограммы попадает в событие `tutor_reply`? Нет; добавить в `Bot._start_tutoring` событие `orthogram_classified{rule_code}` при `session.word is not None` (R7).

`src/hwcheck/subjects/registry.py`:

```python
@dataclass(frozen=True)
class SubjectDeps:
    llm: VisionAndChatClient
    models: CheckModels
    cache: FileCache | None
    ocr: OcrClient | None = None
    kb: KnowledgeBase | None = None
    dictionary: Dictionary | None = None


def module_for(code: str, deps: SubjectDeps) -> SubjectModule:
    if code == "math":
        return MathModule(deps.llm, deps.models, deps.cache)
    if code == "russian":
        if deps.dictionary is None:
            raise KeyError("russian: словарь не загружен")
        return RussianModule(deps.llm, deps.models, ocr=deps.ocr, dictionary=deps.dictionary)
    raise KeyError(f"предметный модуль не реализован: {code}")
```

- [ ] **Step 7: Тесты зелёные, старые тесты тьютора не сломаны**

Run: `uv run pytest tests/test_ru_module.py tests/test_ru_tutor.py tests/test_ru_rules.py tests/test_tutor.py tests/test_math_module.py -v && uv run mypy && uv run ruff check src tests`
Expected: PASS (`test_rules_file_matches_codes` — skip до R6).

- [ ] **Step 8: Commit**

```bash
git add src/hwcheck/subjects/russian src/hwcheck/subjects/registry.py src/hwcheck/pipeline/tutor.py prompts/ru_tutor prompts/ru_orthogram tests/test_ru_module.py tests/test_ru_tutor.py tests/test_ru_rules.py tests/test_ru_recognize.py
git commit -m "feat(russian): модуль предмета — эталон из базы знаний и словаря, находки, тьютор по орфограмме"
```

---

### Task 6 (R6): Карточки орфограмм 1–4 класса и `hwcheck kb load-rules`

**Files:**
- Create: `assets/kb/rules_russian.json`
- Modify: `src/hwcheck/kb_cli.py` (`load_rules_cmd`), `src/hwcheck/cli.py` (подкоманда), `docs/deploy.md` («Проверка базы знаний»: загрузка правил при выкатке)
- Test: `tests/test_kb_cli.py` (дополнить), `tests/test_ru_rules.py` (снять skip)

**Interfaces:**
- Consumes: `RULE_CODES`, `load_rules(path) -> list[KbRule]` (R5); `KnowledgeBaseImpl.add_rule`.
- Produces: `hwcheck kb load-rules assets/kb/rules_russian.json` → печатает «Загружено N правил»; `async load_rules_into(kb, path) -> int` в `kb_cli.py`.

- [ ] **Step 1: Тест CLI (RED)** — в `tests/test_kb_cli.py`:

```python
async def test_load_rules_into_memory_kb(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            [
                {
                    "code": "ru.orth.zhi_shi", "subject": "russian", "grade_from": 1,
                    "title": "Жи–ши", "statement": "Жи и ши пиши с буквой и.",
                    "example": "жизнь, шина", "finding_kinds": ["spelling"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )  # fmt: skip
    kb = InMemoryKnowledgeBase()
    assert await load_rules_into(kb, path) == 1
    rule = await kb.rule("ru.orth.zhi_shi")
    assert rule is not None and rule.title == "Жи–ши"


async def test_load_rules_rejects_wrong_shape(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text('{"code": "x"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="JSON-список"):
        await load_rules_into(InMemoryKnowledgeBase(), path)
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_kb_cli.py -v`
Expected: FAIL, `ImportError: load_rules_into`.

- [ ] **Step 3: Реализация**

`src/hwcheck/kb_cli.py`:

```python
async def load_rules_into(kb: KnowledgeBaseImpl, path: Path) -> int:
    rules = load_rules(path)
    for rule in rules:
        await kb.add_rule(rule)
    return len(rules)
```

`src/hwcheck/cli.py`: подкоманда `load-rules` с аргументом `path` (`type=Path`); в `_run`: `elif args.kb_command == "load-rules": print(f"Загружено {await load_rules_into(kb, args.path)} правил")`.

`assets/kb/rules_russian.json` — 20 карточек, по одной на код из `RULE_CODES` (формулировки для ребёнка 2–4 класса; `finding_kinds` — `["spelling"]`, для приставки/предлога — `["spelling", "missing_word", "extra_word"]`):

```json
[
  {"code": "ru.orth.unstressed_vowel", "subject": "russian", "grade_from": 2,
   "title": "Безударная гласная в корне",
   "statement": "Чтобы проверить безударную гласную в корне, подбери такое родственное слово, где эта гласная под ударением.",
   "example": "л_са → лес, значит лес́а; в_да → во́ды, значит вода", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.unstressed_vowel_dict", "subject": "russian", "grade_from": 1,
   "title": "Словарное слово",
   "statement": "Эту гласную нельзя проверить ударением — такое слово надо запомнить или посмотреть в словаре.",
   "example": "машина, карандаш, собака, молоко", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.paired_consonant", "subject": "russian", "grade_from": 2,
   "title": "Парная согласная",
   "statement": "Чтобы проверить парную согласную (б–п, в–ф, г–к, д–т, ж–ш, з–с), измени слово так, чтобы после согласной стояла гласная.",
   "example": "сне_ → снега, значит снег; ду_ → дубы, значит дуб", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.silent_consonant", "subject": "russian", "grade_from": 3,
   "title": "Непроизносимая согласная",
   "statement": "Если согласная не слышится, подбери родственное слово, где она слышится ясно.",
   "example": "поз_ний → опоздать, значит поздний; со_нце → солнышко", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.zhi_shi", "subject": "russian", "grade_from": 1,
   "title": "Жи–ши, ча–ща, чу–щу",
   "statement": "Жи и ши пиши с буквой и, ча и ща — с буквой а, чу и щу — с буквой у.",
   "example": "жизнь, шина, чаща, чудо, щука", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.chk_chn", "subject": "russian", "grade_from": 1,
   "title": "Чк, чн, нч, щн",
   "statement": "В сочетаниях чк, чн, нч, щн мягкий знак не пишется.",
   "example": "дочка, ночной, нянчить, мощный", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.soft_sign", "subject": "russian", "grade_from": 1,
   "title": "Мягкий знак — показатель мягкости",
   "statement": "Если согласная звучит мягко в конце слова или перед другой согласной, после неё пишется мягкий знак.",
   "example": "конь, письмо, деньки", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.separating_sign", "subject": "russian", "grade_from": 2,
   "title": "Разделительные ь и ъ",
   "statement": "Разделительный ъ пишется после приставки на согласную перед е, ё, ю, я; разделительный ь — в корне перед е, ё, ю, я, и.",
   "example": "подъезд, объявление; вьюга, семья, воробьи", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.double_consonant", "subject": "russian", "grade_from": 2,
   "title": "Удвоенные согласные",
   "statement": "Слова с удвоенными согласными нужно запомнить; проверь по словарю.",
   "example": "класс, суббота, аллея, хоккей", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.capital", "subject": "russian", "grade_from": 1,
   "title": "Заглавная буква",
   "statement": "Имена, фамилии, клички животных, названия городов и рек, а также первое слово предложения пишутся с заглавной буквы.",
   "example": "Москва, Волга, кот Барсик, Маша", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.prefix", "subject": "russian", "grade_from": 2,
   "title": "Приставка пишется слитно",
   "statement": "Приставка — часть слова, она пишется слитно. Между приставкой и словом нельзя вставить другое слово.",
   "example": "сделать, побежал, написал", "finding_kinds": ["spelling", "extra_word"]},
  {"code": "ru.orth.preposition", "subject": "russian", "grade_from": 2,
   "title": "Предлог пишется отдельно",
   "statement": "Предлог — отдельное слово, между предлогом и словом можно вставить другое слово.",
   "example": "в лесу — в густом лесу; на столе — на большом столе", "finding_kinds": ["spelling", "missing_word"]},
  {"code": "ru.orth.prefix_vowel", "subject": "russian", "grade_from": 3,
   "title": "Гласные и согласные в приставках",
   "statement": "Приставки по-, до-, о-, об-, от-, под-, за-, на-, над- пишутся всегда одинаково, как бы они ни слышались.",
   "example": "подарить, отбежать, забежать, надписать", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.suffix", "subject": "russian", "grade_from": 3,
   "title": "Суффиксы -ик/-ек, -оньк/-еньк",
   "statement": "Измени слово: если гласная суффикса выпадает — пиши -ек, если остаётся — -ик. После твёрдых согласных -оньк-, после мягких -еньк-.",
   "example": "ключик — ключика, замочек — замочка; берёзонька, реченька", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.noun_ending", "subject": "russian", "grade_from": 4,
   "title": "Окончания существительных",
   "statement": "Определи склонение и падеж и подставь слово-помощник того же склонения с ударным окончанием.",
   "example": "на дорог_ → на земле́, значит на дороге; у дорог_ → у земли́, значит у дороги", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.adj_ending", "subject": "russian", "grade_from": 4,
   "title": "Окончания прилагательных",
   "statement": "Окончание прилагательного проверяй по вопросу: какое окончание у вопроса, такое и у прилагательного.",
   "example": "о доме (каком?) большом; в лесу (каком?) тёмном", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.verb_ending", "subject": "russian", "grade_from": 4,
   "title": "Личные окончания глаголов",
   "statement": "Поставь глагол в неопределённую форму: на -ить (кроме брить, стелить) и глаголы-исключения — II спряжение (-ит, -ат/-ят), остальные — I спряжение (-ет, -ут/-ют).",
   "example": "строит — строить (II); читает — читать (I)", "finding_kinds": ["spelling"]},
  {"code": "ru.orth.tsya", "subject": "russian", "grade_from": 4,
   "title": "-тся и -ться",
   "statement": "Задай вопрос к глаголу: если в вопросе есть мягкий знак (что делать?), пиши -ться; если нет (что делает?), пиши -тся.",
   "example": "хочет (что делать?) учиться; он (что делает?) учится", "finding_kinds": ["spelling", "missing_word"]},
  {"code": "ru.orth.ne_verb", "subject": "russian", "grade_from": 2,
   "title": "Не с глаголами",
   "statement": "Частица не с глаголами пишется раздельно.",
   "example": "не бегал, не знаю, не хочет", "finding_kinds": ["spelling", "missing_word", "extra_word"]},
  {"code": "ru.orth.hissing_soft", "subject": "russian", "grade_from": 3,
   "title": "Ь после шипящих",
   "statement": "У существительных женского рода после шипящих на конце пишется ь, у мужского — нет; у глаголов после шипящих ь пишется всегда.",
   "example": "ночь, мышь — но мяч, плащ; пишешь, беречь", "finding_kinds": ["spelling"]}
]
```

Черновик формулировок — Claude; **вычитка Кириллом до выкатки** (отметить в TODO). Снять `skipif` в `tests/test_ru_rules.py`.

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_kb_cli.py tests/test_ru_rules.py -v && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Runbook**

`docs/deploy.md`, раздел «Проверка базы знаний»: после миграции — `docker compose run --rm bot python -m hwcheck kb load-rules assets/kb/rules_russian.json` (идемпотентно: `ON CONFLICT (code) DO UPDATE`).

- [ ] **Step 6: Commit**

```bash
git add assets/kb/rules_russian.json src/hwcheck/kb_cli.py src/hwcheck/cli.py tests/test_kb_cli.py tests/test_ru_rules.py docs/deploy.md
git commit -m "feat(kb): карточки орфограмм 1–4 класса и hwcheck kb load-rules"
```

---

### Task 7 (R7): Бот — маршрутизация по предмету, путь языков через `SubjectPage`

Закрывает блокеры финального ревью каркаса: бот вызывает `recognize`/`resolve_reference` модуля; `CheckedTask.grade` необязателен; `clarify.py` не трогает `grade` у языков; `KnowledgeBase` и `OcrClient` подключены; `photo_index` — по позиции в альбоме; при пропуске word-вопроса ребёнку сообщается. Отложено (в TODO): `findings.confirmed`/`homework_id` в БД.

**Files:**
- Modify: `src/hwcheck/bot/fsm.py`, `src/hwcheck/bot/summary.py`, `src/hwcheck/bot/clarify.py`, `src/hwcheck/bot/onboarding/router.py`, `src/hwcheck/bot/onboarding/parent.py`, `src/hwcheck/bot/handlers.py`, `src/hwcheck/bot/runner.py`, `src/hwcheck/config.py`, `docker-compose.yml` (том `./var/kb_photos`, `OCR_URL`), `.env.example`
- Test: `tests/test_bot_russian.py`, дополнения в `tests/test_clarify_word.py`, `tests/test_summary.py`, `tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `RussianModule` через `module_for`; `SubjectDeps(ocr, kb, dictionary)`; `PhotoStore`; `plan_clarifications`, `apply_word`, `_finding` (без изменений).
- Produces:
  - `CheckPhotos(urls: list[str], subject: str = "math")` — предмет профиля (ученик — `position.profile.subject`; родитель — предмет выбранного ребёнка через `ParentSteps.homework_subject(actor, account) -> str`)
  - `CheckedTask.grade: GradeResult | None = None`, `CheckedTask.subject_task: SubjectTask | None = None`, `CheckedTask.reference: Reference | None = None`, `CheckedTask.payload: dict[str, Any] = {}`
  - `ChatState.subject: str = "math"`, `ChatState.conditions: list[SubjectTask] = []` (упражнения учебника для языков, TTL — `textbook_saved_at`)
  - `Bot._process_photos(chat_id, user_id, urls, subject)`; для `subject != "math"` — `_process_language_photos`
  - `Settings.kb_photos_dir = "var/kb_photos"`, `Settings.kb_photos_ttl_days = 365`
  - события: `ocr_failed{subject}`, `orthogram_classified{rule_code}`, `clarification_skipped` теперь с сообщением ребёнку `«{label}: вопрос снят — записал как «стоит перепроверить» 🤔»`
  - тексты: `WELCOME_BY_SUBJECT = {"math": WELCOME, "russian": "Привет! Я проверяю домашку по русскому языку. 📚\nПришли фото страницы учебника с упражнением и фото тетради — найду, где стоит перепроверить написание."}`

- [ ] **Step 1: Тесты состояния и сводки без `grade` (RED)** — в `tests/test_summary.py` добавить:

```python
def test_task_line_for_language_task_without_grade() -> None:
    word = Word(text="позняя", box=Box(x0=1, y0=1, x1=9, y1=9), confidence=0.8, line=1)
    finding = Finding(task_index=0, kind="spelling", strength="candidate", actual="позняя",
                      expected="поздняя", word=word, detail="проверь слово «позняя»")  # fmt: skip
    task = SubjectTask(number="245", words=[word])
    item = CheckedTask(task=to_vision_task(task), ref=None, subject_task=task, findings=[finding])
    line, button = task_line(0, item)
    assert line == "№245 — проверь слово «позняя» 🤔" and button is None
    confirmed = item.model_copy(
        update={"findings": [finding.model_copy(update={"confirmed": True})]}
    )
    line, button = task_line(0, confirmed)
    assert line == "№245 — есть ошибка (слово «позняя») ❌" and button is not None


def test_review_header_counts_language_tasks() -> None:
    task = SubjectTask(number="1", words=[])
    state = ChatState(tasks=[CheckedTask(task=to_vision_task(task), ref=None, subject_task=task)])
    assert review_header(state) == "Проверил! 1 из 1 верно.\n"
```

В `tests/test_clarify_word.py`:

```python
def test_plan_clarifications_skips_grade_when_missing() -> None:
    task = SubjectTask(number="1", words=[])
    item = CheckedTask(task=to_vision_task(task), ref=None, subject_task=task,
                       findings=[Finding(task_index=0, kind="spelling", strength="candidate",
                                         actual="а", word=Word(text="а", box=Box(x0=0, y0=0, x1=1, y1=1)))])  # fmt: skip
    [clarification] = plan_clarifications([item])
    assert clarification.kind == "word"
```

- [ ] **Step 2: Тест бота для русского (RED)**

`tests/test_bot_russian.py` — по образцу `tests/test_bot.py` (`FakeMax`, `InMemoryStateStore`, `EventLog` во `tmp_path`); бот собирается с `SubjectDeps(llm=FakeVision, models, cache=None, ocr=FakeOcr, kb=InMemoryKnowledgeBase(), dictionary=WORDS)` и `onboarding=None`:

```python
"""Русский в боте: альбом учебник + тетрадь → находки → «здесь написано …?» → разбор."""

import json
from pathlib import Path

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.handlers import Bot
from hwcheck.bot.onboarding.router import CheckPhotos
from hwcheck.config import Settings
from hwcheck.events import EventLog, read_events
from hwcheck.db.kb_memory import InMemoryKnowledgeBase
from hwcheck.photos import PhotoStore
from hwcheck.subjects.registry import SubjectDeps
from tests.test_bot import FakeMax, callback_update, photo_update  # существующие помощники
from tests.test_ru_gaps import WORDS
from tests.test_ru_module import MODELS, FakeOcr, _w
from tests.test_ru_recognize import NOTEBOOK, TEXTBOOK, FakeVision


def _bot(tmp_path: Path, vision: FakeVision, ocr: FakeOcr) -> tuple[Bot, FakeMax, Path]:
    events_path = tmp_path / "events.jsonl"
    max_client = FakeMax()
    settings = Settings(photos_dir=str(tmp_path / "photos"), kb_photos_dir=str(tmp_path / "kb"))
    deps = SubjectDeps(
        llm=vision, models=MODELS, cache=None, ocr=ocr, kb=InMemoryKnowledgeBase(), dictionary=WORDS
    )  # type: ignore[arg-type]
    bot = Bot(
        max_client,
        vision,
        InMemoryStateStore(),
        EventLog(events_path, "dev"),
        settings,
        photos=PhotoStore(tmp_path / "photos", 30),
        subjects=deps,
    )  # type: ignore[arg-type]
    return bot, max_client, events_path


async def test_album_textbook_and_notebook_asks_about_word(tmp_path: Path) -> None:
    vision = FakeVision([TEXTBOOK, NOTEBOOK])
    ocr = FakeOcr([_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    bot, max_client, events_path = _bot(tmp_path, vision, ocr)
    await bot._on_photo(chat_id=1, user_id=7, urls=["u1", "u2"], subject="russian")
    texts = [m.text for m in max_client.sent]
    assert any("уточню у тебя одну деталь" in t for t in texts)
    assert texts[-1] == "№245: здесь написано «позняя»?"
    assert max_client.sent[-1].image_token is not None  # кроп слова с фото №2 (photo_index=1)
    state = await bot._store.get(1)
    assert state.subject == "russian" and state.phase == "clarifying"
    assert state.tasks[0].grade is None and state.tasks[0].findings[0].word is not None
    assert state.tasks[0].findings[0].word.photo_index == 1
    kinds = [e["type"] for e in read_events(events_path)]
    assert "reference_resolved" in kinds and "finding_created" in kinds


async def test_yes_confirms_error_and_tutor_starts(tmp_path: Path) -> None:
    vision = FakeVision([TEXTBOOK, NOTEBOOK])
    vision.chat_responses = [
        json.dumps({"rule_code": "ru.orth.silent_consonant", "confidence": 0.9}),
        json.dumps({"reply": "Какая орфограмма в этом слове?"}),
    ]
    ocr = FakeOcr([_w("Наступила", 0), _w("позняя", 1), _w("осень", 2)])
    bot, max_client, events_path = _bot(tmp_path, vision, ocr)
    await bot._on_photo(chat_id=1, user_id=7, urls=["u1", "u2"], subject="russian")
    state = await bot._store.get(1)
    token = state.clarifications[0].token
    await bot._on_callback(1, 7, f"clarify:{token}:yes", "cb")
    assert max_client.sent[-1].text == "№245 — есть ошибка (слово «позняя») ❌"
    await bot._on_callback(1, 7, "tutor:0", "cb2")
    assert max_client.sent[-1].text == "Какая орфограмма в этом слове?"
    state = await bot._store.get(1)
    assert state.phase == "tutoring" and state.tutor is not None and state.tutor.word is not None
    kinds = [e["type"] for e in read_events(events_path)]
    assert "finding_confirmed" in kinds and "orthogram_classified" in kinds


async def test_ocr_failure_reports_uncertain_and_event(tmp_path: Path) -> None:
    bot, max_client, events_path = _bot(tmp_path, FakeVision([TEXTBOOK, NOTEBOOK]), FakeOcr(None))
    await bot._on_photo(chat_id=1, user_id=7, urls=["u1", "u2"], subject="russian")
    assert "не смог прочитать тетрадь" in max_client.sent[-1].text
    assert "ocr_failed" in [e["type"] for e in read_events(events_path)]


async def test_textbook_only_is_remembered_for_next_message(tmp_path: Path) -> None:
    vision = FakeVision([TEXTBOOK, NOTEBOOK])
    ocr = FakeOcr([_w("Наступила", 0), _w("поздняя", 1), _w("осень", 2)])
    bot, max_client, _ = _bot(tmp_path, vision, ocr)
    await bot._on_photo(chat_id=1, user_id=7, urls=["u1"], subject="russian")
    assert max_client.sent[-1].text.startswith("Вижу страницу учебника (№245)")
    await bot._on_photo(chat_id=1, user_id=7, urls=["u2"], subject="russian")
    assert max_client.sent[-1].text == "Проверил! 1 из 1 верно.\n№245 — верно ✅"


async def test_math_path_unchanged_for_math_subject(tmp_path: Path) -> None:
    """subject=math идёт прежним кодом: FakeVision русского не вызывается."""
    bot, max_client, _ = _bot(tmp_path, FakeVision([]), FakeOcr([]))
    await bot._on_photo(chat_id=1, user_id=7, urls=["u1"], subject="math")
    assert "Что-то пошло не так" in max_client.sent[-1].text  # math-vision не замокан → RETRY


def test_check_photos_carries_subject() -> None:
    assert (
        CheckPhotos(["u"]).subject == "math"
        and CheckPhotos(["u"], subject="russian").subject == "russian"
    )
```

Помощники из `tests/test_bot.py`: если `photo_update`/`callback_update` там названы иначе — использовать существующие имена; `FakeMax.sent[i]` должен хранить `text`, `buttons`, `image_token` (`FakeMax.send_message` уже принимает `image_token` с этапа 2 — проверить и при необходимости сохранить в запись).

- [ ] **Step 3: Запустить — падают**

Run: `uv run pytest tests/test_bot_russian.py tests/test_summary.py tests/test_clarify_word.py -v`
Expected: FAIL (`TypeError: CheckedTask grade required`, `CheckPhotos.__init__() got an unexpected keyword 'subject'`).

- [ ] **Step 4: Состояние, сводка, уточнения**

`src/hwcheck/bot/fsm.py`:

```python
class CheckedTask(BaseModel):
    task: VisionTask  # у языков — to_vision_task(subject_task): подпись, номер, строки
    ref: RefSolution | None
    grade: GradeResult | None = None  # None — предмет без пересчёта (языки)
    findings: list[Finding] = Field(default_factory=list)
    ref_status: RefStatus = "no_condition"
    subject_task: SubjectTask | None = None  # исходное задание модуля (слова с координатами)
    reference: Reference | None = None  # эталон модуля (для тьютора)
    payload: dict[str, Any] = Field(default_factory=dict)  # TaskResult.payload модуля


class ChatState(BaseModel):
    ...
    subject: str = "math"
    conditions: list[SubjectTask] = Field(default_factory=list)  # упражнения учебника (языки)
```

`src/hwcheck/bot/summary.py`: `task_findings` → `item.findings if item.grade is None else (item.findings or findings_from_grade(index, item.grade))`.

`src/hwcheck/bot/clarify.py`: в `_clarification_for` первой строкой `if item.grade is None or item.grade.verdict != "uncertain": return None`; в `question` для `answer`-вопроса `item.grade` уже не None (вопрос ставится только при `grade`), добавить `assert item.grade is not None`; `regrade` — `item.grade` не читается; `apply_text` для `kind == "word"` — без изменений.

- [ ] **Step 5: Онбординг отдаёт предмет**

`router.py`: `CheckPhotos(urls: list[str], subject: str = "math")`; `_on_photo` для ученика (`student_ready`) — `return "pass"` остаётся (без онбординга бот не знает предмета → в `Bot._dispatch` для `route == "pass"` с фото ученика нужен предмет: вернуть `CheckPhotos(urls, subject=position.profile.subject or "math")` вместо `"pass"` — так бот всегда получает предмет из онбординга). Для родителя: `chosen = await self._parents.on_photo(...)` → `CheckPhotos(chosen, subject=await self._parents.homework_subject(actor, account))`; в `_whose` — так же после `choose_owner`.

`parent.py`:

```python
    async def homework_subject(self, actor: Actor, account: Account) -> str:
        """Предмет ребёнка, чью домашку проверяем: выбранный кнопкой «Чья домашка?» или
        единственный ребёнок 1–4 класса."""
        young = await self.young_children(account)
        state = await self._ctx.states.get(actor.user_hash)
        chosen = next((c for c in young if c.id == state.child_id), None)
        child = chosen or (young[0] if len(young) == 1 else None)
        return (child.subject if child is not None else None) or "math"
```

Тест в `tests/test_onboarding_router.py`: ученик с `subject="russian"` и согласием присылает фото → `route` возвращает `CheckPhotos(urls, subject="russian")`; родитель с одним ребёнком-«russian» — тоже.

- [ ] **Step 6: Бот — путь языков**

`src/hwcheck/bot/handlers.py`:

```python
    def __init__(..., subjects: SubjectDeps | None = None, ...):
        ...
        self._deps = subjects or SubjectDeps(llm, self._models, self._cache)
        self._kb = self._deps.kb
        self._kb_photos = kb_photos  # PhotoStore | None — фото учебников (TTL 365 дней)
        self._modules: dict[str, SubjectModule] = {}

    def _module_for(self, subject: str) -> SubjectModule:
        if subject not in self._modules:
            self._modules[subject] = module_for(subject, self._deps)
        return self._modules[subject]

    @property
    def _module(self) -> SubjectModule:  # математика — как раньше
        return self._module_for("math")
```

`_dispatch`: `CheckPhotos` → `await self._on_photo(chat_id, user_id, route.urls, route.subject)`; без онбординга (`self._onboarding is None`) фото идёт в `"math"`. `_on_photo(..., subject: str = "math")` → `_process_photos(chat_id, user_id, urls, subject)`; в `_process_photos` первой строкой:

```python
        if subject != "math":
            await self._process_language_photos(chat_id, user_id, urls, subject)
            return
```

Новый код:

```python
async def _process_language_photos(
    self, chat_id: int, user_id: int | None, urls: list[str], subject: str
) -> None:
    """Языки (спецификация §6, §8): страницы через SubjectPage модуля. Учебник даёт
    упражнения (запоминаются на TTL), тетрадь — слова «как написано»; условия сопоставляются
    по номеру упражнения, единственная пара — друг с другом."""
    module = self._module_for(subject)
    state = await self._store.get(chat_id)
    known = list(state.conditions) if textbook_is_fresh(state.textbook_saved_at) else []
    pages, photo_paths = await self._recognize_language_album(module, user_id, urls)
    notebook: list[SubjectTask] = []
    conditions = {t.number: t for t in known}
    ocr_failed = False
    for index, (page, path) in enumerate(zip(pages, photo_paths, strict=True)):
        if page is None:
            continue
        if page.failure == "ocr_failed":
            ocr_failed = True
        if page.role == "textbook":
            kb_path = self._save_kb_photo(user_id, index, urls)  # см. ниже
            for task in page.tasks:
                conditions[task.number] = task.model_copy(update={"photo_path": kb_path})
        elif page.role == "notebook":
            for task in page.tasks:
                words = [w.model_copy(update={"photo_index": index}) for w in task.words]
                notebook.append(task.model_copy(update={"words": words}))
    remembered = list(conditions.values())
    if not notebook:
        if ocr_failed:
            await self._max.send_message(chat_id, OCR_FAILED)
        elif remembered and any(p is not None and p.role == "textbook" for p in pages):
            numbers = (
                ", ".join(f"№{t.number}" for t in remembered if t.number_on_page) or "без номера"
            )
            await self._max.send_message(chat_id, TEXTBOOK_ONLY.format(numbers=numbers))
        else:
            await self._max.send_message(chat_id, UNREADABLE)
        if remembered:
            await self._store.set(chat_id, state.model_copy(update={
                "conditions": remembered, "textbook_saved_at": time.time(), "subject": subject,
            }))  # fmt: skip
        return
    references = await module.resolve_reference(remembered, self._kb)
    for reference in references:
        self._events.log("reference_resolved", user_id=user_id, subject=subject,
                         origin=reference.origin, trust=reference.trust)  # fmt: skip
    results = await module.check(notebook, references)
    checked = []
    for task, result in zip(notebook, results, strict=True):
        findings = [f.model_copy(update={"task_index": result.task_index}) for f in result.findings]
        await self._record_findings(user_id, task, findings, subject=subject)
        checked.append(CheckedTask(
            task=to_vision_task(task), ref=None, subject_task=task, findings=findings,
            reference=result.reference, payload=result.payload,
        ))  # fmt: skip
    plan = plan_clarifications(checked)
    new_state = ChatState(
        phase="clarifying" if plan else "review", tasks=checked, subject=subject,
        conditions=remembered, textbook_saved_at=time.time() if remembered else None,
        clarifications=plan, photo_paths=photo_paths,
    )  # fmt: skip
    await self._store.set(chat_id, new_state)
    await self._send_review(chat_id, new_state)
    if plan:
        await self._ask_clarification(chat_id, user_id, new_state)


async def _recognize_language_album(
    self, module: SubjectModule, user_id: int | None, urls: list[str]
) -> tuple[list[SubjectPage | None], list[str]]:
    """Как `_recognize_all`: сбой одного фото не теряет остальные; индексы альбома сохраняются."""
    pages: list[SubjectPage | None] = []
    paths: list[str] = []
    failed = 0
    for url in urls:
        photo: str | None = None
        try:
            image = await self._max.download(url)
            photo = self._save_photo(user_id, image)
            page = await module.recognize(image)
            self._events.log("page_recognized", user_id=user_id, subject=module.code,
                             role=page.role, n_tasks=len(page.tasks), calls=page.usage.calls,
                             tokens=page.usage.tokens, photo=photo)  # fmt: skip
            if page.failure == "ocr_failed":
                self._events.log("ocr_failed", user_id=user_id, subject=module.code)
            pages.append(page)
        except Exception as exc:
            failed += 1
            pages.append(None)
            logger.exception("photo failed: %s", url.split("?")[0])
            self._events.log("photo_failed", user_id=user_id, error=type(exc).__name__, photo=photo)
        paths.append(photo or "")
    if urls and failed == len(urls):
        raise RuntimeError("all photos failed")
    return pages, paths
```

Фото учебника в `var/kb_photos`: `_save_kb_photo` — прочитать байты повторно не нужно: сохранять в `_recognize_language_album` при `page.role == "textbook"` через `self._kb_photos.save(anonymize(user_id), image)` и вернуть путь третьим списком (`kb_paths: list[str | None]`); в `_process_language_photos` использовать `kb_paths[index]`. Реализовать именно так (без `_save_kb_photo`).

Тексты: `OCR_FAILED = "Не смог прочитать тетрадь 😕 Попробуй переснять: страница целиком, без наклона, при хорошем свете."`; `TEXTBOOK_ONLY` переиспользуется.

`_record_findings(..., subject: str = "math")` — `subject` вместо `self._module.code` в событии и записи. `_answer_clarification`: `verdict_before=before.grade.verdict if before.grade else None` и так же `after`; `task_clarified` — только при `updated.grade is not None`, иначе событие `finding_answered{kind}` (уже есть `finding_confirmed`). `_start_tutoring`: `module = self._module_for(state.subject)`; `result = task_result_of(index, item)`; для `item.grade is None` → `TaskResult(task_index=index, findings=item.findings, reference=item.reference, payload=item.payload)`; `subject_task = item.subject_task or to_subject_task(item.task)`; `kb=self._kb`; после создания сессии, если `session.word is not None`: событие `orthogram_classified{rule_code=session.word.rule_code}`. `_on_callback` для `tutor:` — `notification` как раньше; при `ValueError` из `start_tutoring` («нет подтверждённой ошибки») — сообщение `RETRY` не подходит: отправить «Здесь нечего разбирать — ошибка не подтверждена 🙂». `_skip_clarification`: отправить ребёнку `f"{task_label(item.task)}: вопрос снят — оставлю «стоит перепроверить» 🤔"`. `WELCOME` по предмету: в `_dispatch` для `bot_started` предмет неизвестен без онбординга → оставить `WELCOME`; для `_on_text` в `phase == "idle"` — тоже; текст `WELCOME_BY_SUBJECT["russian"]` использует онбординг на шаге «готово» (`texts.STUDENT_READY`) — оставить как есть, изменение текстов онбординга не входит в объём.

`task_result_of` — принимать `CheckedTask` без `grade`:

```python
def task_result_of(index: int, item: CheckedTask) -> TaskResult:
    if item.grade is None:
        return TaskResult(task_index=index, findings=item.findings, reference=item.reference,
                          payload=item.payload)  # fmt: skip
    ...  # прежний код
```

`runner.py`: `kb_photos=PhotoStore(Path(settings.kb_photos_dir), settings.kb_photos_ttl_days)`; `SubjectDeps(llm, models, cache, ocr=OcrClient(settings.ocr_url, timeout_s=settings.ocr_timeout_s) if settings.ocr_url else None, kb=PgKnowledgeBase(pool) if pool else None, dictionary=HunspellDictionary.load())` — словарь грузится один раз при старте (лог `dictionary loaded in N s`), `OcrClient` закрывается через `resources.enter_async_context`. `config.py`: `kb_photos_dir: str = "var/kb_photos"`, `kb_photos_ttl_days: int = 365`. `docker-compose.yml`: том `./var` уже покрывает `var/kb_photos`; `OCR_URL` уже пробрасывается. `.env.example`: `OCR_URL=http://ocr:8080` (закомментировано с пояснением).

- [ ] **Step 7: Все тесты зелёные**

Run: `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS, включая все прежние тесты математики без правок логики (`tests/test_bot.py` — без изменений).

- [ ] **Step 8: Ревью**

Субагент `code-reviewer` + `security-reviewer` по диффу R7: кропы и пути (`photo_index` в границах `photo_paths`), состояние Redis старого формата (`ChatState` без `subject` → `"math"`), утечки эталона в текстах ребёнку (`expected` нигде не печатается до уровня 3).

- [ ] **Step 9: Commit**

```bash
git add src/hwcheck/bot src/hwcheck/config.py src/hwcheck/bot/onboarding docker-compose.yml .env.example tests
git commit -m "feat(bot): маршрутизация по предмету профиля — языки через SubjectPage, OCR и база знаний подключены"
```

---

### Task 8 (R8): Стенд русского — golden-кейсы с ожидаемыми находками

Спецификация §3 п.6: форма задания открывается через стенд на ≥ 10 реальных фото. Для русского
`verified` без подтверждения ребёнка не ставится, поэтому главные метрики — **точность кандидатов**
(какая доля вопросов «здесь написано …?» указывает на реальную ошибку — прообраз доли «нет» в
`finding_confirmed`) и **полнота в первых двух** (спросит ли бот про настоящую ошибку в пределах
`MAX_QUESTIONS`). Порог открытия: ложных `verified` = 0 (по построению) **и** точность кандидатов
в первых двух ≥ 50 % на ≥ 10 фото; полнота — отчётная.

**Files:**
- Create: `src/hwcheck/bench/russian.py`, `bench/golden_ru/README.md`, `bench/golden_ru/ru-*.json` (≥ 10 кейсов по фото Кирилла — TODO «Фото тетрадей по русскому»)
- Modify: `src/hwcheck/bench/cli.py` (`hwcheck bench ru`), `src/hwcheck/cli.py`
- Test: `tests/test_bench_ru.py`

**Interfaces:**
- Consumes: `RussianModule` (R5) через `SubjectDeps`; `BenchClient` (кэш vision/chat); `OcrClient` к живому контейнеру (`--ocr-url`, по умолчанию `http://127.0.0.1:8080` — на VPS через `docker compose --profile ocr up`, локально — образ R1); `index_photos` из `bench/golden.py`.
- Produces:
  - `class RuGoldenCase(BaseModel)`: `schema: 1`, `id`, `source`, `photos: [{sha256, role}]`, `exercise: {number, text}` (текст с пропусками как в учебнике), `reference: str` (текст с заполненными пропусками), `errors: list[{"written": str, "expected": str, "in_gap": bool}]` (реальные ошибки ребёнка), `notes`
  - `run_ru_case(module, kb, case, index) -> RuCaseRun` (`findings: list[dict]`, `reference_trust`, `seconds`, `error`)
  - `score_ru(cases, runs) -> RuSummary`: `errors_total`, `errors_found` (найдена находка с тем же `written`), `errors_in_top2`, `candidates_total`, `candidates_true`, `precision_top2`, `reference_verified` (доля кейсов с `verified` эталоном), `wrong_exercise` (кейсы с `uncertain`)
  - `render_ru_report(summary) -> str` — markdown-таблица

- [ ] **Step 1: Тест метрик (RED)**

`tests/test_bench_ru.py`:

```python
from hwcheck.bench.russian import RuCaseRun, RuGoldenCase, render_ru_report, score_ru


def _case() -> RuGoldenCase:
    return RuGoldenCase.model_validate(
        {
            "schema": 1,
            "id": "ru-01",
            "photos": [{"sha256": "a" * 64, "role": "textbook"}, {"sha256": "b" * 64, "role": "notebook"}],
            "exercise": {"number": "245", "text": "Наступила п_здняя ос_нь."},
            "reference": "Наступила поздняя осень.",
            "errors": [{"written": "позняя", "expected": "поздняя", "in_gap": True}],
        }
    )  # fmt: skip


def _run(*findings: tuple[str, str]) -> RuCaseRun:
    return RuCaseRun(
        case_id="ru-01",
        findings=[{"kind": k, "actual": a, "expected": None} for k, a in findings],
        reference_trust="verified",
        seconds=1.0,
    )


def test_score_counts_found_errors_and_false_candidates() -> None:
    summary = score_ru([_case()], [_run(("spelling", "Настипила"), ("spelling", "позняя"))])
    assert (summary.errors_total, summary.errors_found, summary.errors_in_top2) == (1, 1, 1)
    assert (summary.candidates_total, summary.candidates_true) == (2, 1)
    assert summary.precision_top2 == 0.5 and summary.reference_verified == 1


def test_score_error_beyond_top2_is_missed_in_top2() -> None:
    summary = score_ru(
        [_case()], [_run(("spelling", "а"), ("spelling", "б"), ("spelling", "позняя"))]
    )
    assert (summary.errors_found, summary.errors_in_top2) == (1, 0)


def test_score_uncertain_case() -> None:
    summary = score_ru([_case()], [_run(("uncertain", ""))])
    assert summary.wrong_exercise == 1 and summary.errors_found == 0


def test_report_has_threshold_line() -> None:
    text = render_ru_report(score_ru([_case()], [_run(("spelling", "позняя"))]))
    assert "Точность кандидатов в первых двух" in text and "100%" in text
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_bench_ru.py -v`
Expected: FAIL, `ModuleNotFoundError: hwcheck.bench.russian`.

- [ ] **Step 3: Реализация**

`src/hwcheck/bench/russian.py`:

```python
"""Стенд русского языка: тем же модулем, что в боте, по эталонной разметке bench/golden_ru/."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hwcheck.bench.golden import GoldenPhoto
from hwcheck.bot.clarify import MAX_QUESTIONS
from hwcheck.subjects.base import KnowledgeBase, SubjectModule, SubjectTask
from hwcheck.subjects.russian.align import normalize


class RuExerciseGold(BaseModel):
    number: str | None = None
    text: str


class RuError(BaseModel):
    written: str
    expected: str
    in_gap: bool = True


class RuGoldenCase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schema")
    id: str
    source: str = ""
    photos: list[GoldenPhoto] = Field(min_length=1, max_length=4)
    exercise: RuExerciseGold
    reference: str
    errors: list[RuError] = Field(default_factory=list)
    notes: str = ""


def load_ru_cases(directory: Path) -> list[RuGoldenCase]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(RuGoldenCase.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
    return cases


@dataclass
class RuCaseRun:
    case_id: str
    findings: list[dict[str, Any]]
    reference_trust: str | None
    seconds: float
    error: str | None = None


async def run_ru_case(
    module: SubjectModule, kb: KnowledgeBase | None, case: RuGoldenCase, index: dict[str, Path]
) -> RuCaseRun:
    started = time.monotonic()
    try:
        conditions: list[SubjectTask] = []
        notebook: list[SubjectTask] = []
        for photo in case.photos:
            page = await module.recognize(index[photo.sha256].read_bytes())
            (conditions if page.role == "textbook" else notebook).extend(page.tasks)
        references = await module.resolve_reference(conditions, kb)
        results = await module.check(notebook, references)
        findings = [f.model_dump() for r in results for f in r.findings]
        trust = references[0].trust if references else None
    except Exception as exc:  # сбой кейса — строка отчёта, не падение прогона
        return RuCaseRun(
            case.id, [], None, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
        )
    return RuCaseRun(case.id, findings, trust, time.monotonic() - started)


@dataclass
class RuSummary:
    cases: int = 0
    errors_total: int = 0
    errors_found: int = 0
    errors_in_top2: int = 0
    candidates_total: int = 0
    candidates_true: int = 0
    candidates_top2: int = 0
    candidates_top2_true: int = 0
    reference_verified: int = 0
    wrong_exercise: int = 0
    failed: int = 0
    per_case: list[str] = field(default_factory=list)

    @property
    def precision_top2(self) -> float:
        return self.candidates_top2_true / self.candidates_top2 if self.candidates_top2 else 0.0


def score_ru(cases: list[RuGoldenCase], runs: list[RuCaseRun]) -> RuSummary:
    by_id = {r.case_id: r for r in runs}
    summary = RuSummary()
    for case in cases:
        run = by_id.get(case.id)
        if run is None or run.error:
            summary.failed += 1
            continue
        summary.cases += 1
        summary.reference_verified += int(run.reference_trust == "verified")
        if any(f["kind"] == "uncertain" for f in run.findings):
            summary.wrong_exercise += 1
        truth = {normalize(e.written) for e in case.errors}
        candidates = [f for f in run.findings if f["kind"] != "uncertain"]
        summary.errors_total += len(truth)
        found = {normalize(f["actual"] or "") for f in candidates} & truth
        top2 = {normalize(f["actual"] or "") for f in candidates[:MAX_QUESTIONS]} & truth
        summary.errors_found += len(found)
        summary.errors_in_top2 += len(top2)
        summary.candidates_total += len(candidates)
        summary.candidates_true += sum(normalize(f["actual"] or "") in truth for f in candidates)
        summary.candidates_top2 += len(candidates[:MAX_QUESTIONS])
        summary.candidates_top2_true += len(top2)
        summary.per_case.append(
            f"{case.id}: ошибок {len(truth)}, найдено {len(found)}, кандидатов {len(candidates)}"
        )
    return summary


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "—"


def render_ru_report(summary: RuSummary) -> str:
    rows = [
        ("Кейсов (+ упавших)", f"{summary.cases} (+{summary.failed})"),
        ("Реальные ошибки найдены", _pct(summary.errors_found, summary.errors_total)),
        (
            "Реальные ошибки в первых двух вопросах",
            _pct(summary.errors_in_top2, summary.errors_total),
        ),
        ("Точность кандидатов (все)", _pct(summary.candidates_true, summary.candidates_total)),
        (
            "Точность кандидатов в первых двух",
            _pct(summary.candidates_top2_true, summary.candidates_top2),
        ),
        ("Эталон verified", _pct(summary.reference_verified, summary.cases)),
        ("«Не то упражнение» (uncertain)", str(summary.wrong_exercise)),
    ]
    table = "| Метрика | Значение |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)
    return table + "\n\n" + "\n".join(f"- {line}" for line in summary.per_case) + "\n"
```

`hwcheck bench ru --golden bench/golden_ru --photos data --ocr-url http://127.0.0.1:8080 --out bench/reports/<дата>-russian.md`: собирает `RussianModule` через `SubjectDeps(BenchClient(llm), models, cache=None, ocr=OcrClient(url, timeout_s=60), kb=InMemoryKnowledgeBase(), dictionary=HunspellDictionary.load())` (база в памяти — прогон не пишет в прод), печатает отчёт, JSONL прогона — `.cache/bench/runs/russian.jsonl` (находки содержат слова из тетрадей — в git не попадает).

`bench/golden_ru/README.md`: формат кейса (пример выше), правила разметки: `errors` — только реальные ошибки ребёнка, проверенные по фото; `written` — как написано; `in_gap` — в позиции пропуска учебника; фото не в git (sha256, как в `bench/README.md`).

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_bench_ru.py -v && uv run mypy && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Разметка и прогон**

Кирилл присылает ≥ 10 пар «страница учебника + тетрадь» (2–4 класс, «вставь буквы»); разметка — Claude по фото (`errors` вычитывает Кирилл, как в `bench/golden`). Прогон на VPS с живым OCR (или локально, если Docker поднимет образ R1). Отчёт в `bench/reports/2026-XX-XX-russian.md`; решение по порогу — в `docs/PROJECT_MEMORY.md` §4.

- [ ] **Step 6: Commit**

```bash
git add src/hwcheck/bench/russian.py src/hwcheck/bench/cli.py src/hwcheck/cli.py bench/golden_ru tests/test_bench_ru.py bench/reports
git commit -m "feat(bench): стенд русского — точность и полнота кандидатов по golden-кейсам"
```

---

### Task 9 (R9): Открытие предмета — OCR на VPS, флаг, выкатка, живой тест

**Files:**
- Modify: `src/hwcheck/bot/subjects.py` (`russian` → `available=True`), `docs/deploy.md` («OCR-сервис»: запуск, память, `OCR_URL`; «Обновить бота»: `kb load-rules`), `docs/PROJECT_MEMORY.md` (§2 состояние, §4 решения, §8 артефакты), `docs/legal/privacy-policy-v0.md` (фото учебников, срок 365 дней), `TODO.md`, `HISTORY.md`
- Test: `tests/test_subjects.py` (флаг), `tests/test_onboarding_texts.py` (если тексты каталога проверяют «скоро»)

**Interfaces:** — (эксплуатация).

- [ ] **Step 1: Сборка и дымовой тест OCR на VPS** (до слияния PR, из ветки)

```bash
ssh root@193.247.73.243 "free -m"                                  # available ≥ 2,5 ГБ
ssh root@193.247.73.243 "cd /opt/max-homework-ai && git fetch && git checkout feat/russian \
  && docker compose --profile ocr build ocr"                       # ~5–10 мин: клоны + веса 165 МБ
ssh root@193.247.73.243 "cd /opt/max-homework-ai && docker compose --profile ocr up -d ocr \
  && sleep 90 && docker ps --filter name=homework-ocr --format '{{.Status}}'"
# живое фото тетради (из var/photos, пережатое ботом) → слова
ssh root@193.247.73.243 "cd /opt/max-homework-ai && f=\$(ls var/photos/*/*.webp | head -1); \
  docker run --rm --network max-homework-ai_default -v \$PWD/var/photos:/p:ro python:3.12-slim \
  python -c \"import urllib.request,sys; r=urllib.request.Request('http://ocr:8080/recognize', data=open('/p/${f#var/photos/}','rb').read(), headers={'Content-Type':'image/webp'}); print(urllib.request.urlopen(r, timeout=90).read()[:600])\""
ssh root@193.247.73.243 "docker stats --no-stream homework-ocr"    # память после прогона < 1,6 ГБ
```

Ожидаемо: `words` непустой, `seconds` 5–15, память контейнера ~1,3–1,5 ГБ. Если OOM — лимит 2g в compose поднять нельзя без апгрейда VPS: остановить `ocr`, записать в PROJECT_MEMORY, вернуться к решению «Cloud.ru».

- [ ] **Step 2: Флаг и тексты**

`src/hwcheck/bot/subjects.py`: `Subject("russian", "Русский язык", range(1, 10), available=True)`. Тест `tests/test_subjects.py`: `subject_by_code("russian").available is True`. Прогнать все тесты онбординга — кнопка «скоро» для русского исчезает.

- [ ] **Step 3: Документы**

- `docs/deploy.md` «OCR-сервис»: запуск `docker compose --profile ocr up -d ocr`, `OCR_URL=http://ocr:8080` в `.env`, лимит 2 ГБ, проверка `docker stats`, откат — `docker compose stop ocr` + пустой `OCR_URL` (бот тогда отвечает «не смог прочитать тетрадь», не падает).
- `docs/deploy.md` «Обновить бота»: `kb load-rules` после `up -d`.
- `docs/legal/privacy-policy-v0.md`: фото страниц учебника хранятся до 365 дней (не содержат данных ребёнка) — пункт к вычитке юристом.
- `docs/PROJECT_MEMORY.md`: §2 «Предметы кроме математики» → русский в проде; §4 решение «русский открыт по стенду <дата>: точность кандидатов N %»; §8 артефакты (`bench/golden_ru`, отчёт); §9 грабли по ходу.
- Лист ожидания (`subject_waitlist`, спецификация онбординга §5): рассылка «русский открыт» — **отдельная задача онбординга** (этап 3–4), в объём не входит; записать в TODO.

- [ ] **Step 4: Выкатка**

PR `feat/russian` → ревью → слияние. На VPS: нет событий за 10 минут → `git pull` → `docker compose up -d --build bot` (Redis не трогать) → `OCR_URL` в `.env` → `docker compose --profile ocr up -d ocr` → `kb load-rules` → `tail var/bot.log`: `dictionary loaded`, `onboarding: required`, бот здоров.

- [ ] **Step 5: Живой тест (Кирилл)**

1. Профиль ученика/ребёнка с предметом «русский» (онбординг: смена предмета — этап 3 онбординга; до него — новый профиль).
2. Альбом: фото страницы учебника + фото тетради с упражнением «вставь буквы» с 1–2 намеренными ошибками.
3. Ожидаемо: «Проверил! …», «уточню одну деталь», вопрос «здесь написано «…»?» с кропом слова; «да» → «есть ошибка (слово …)», кнопка «Разобрать» → тьютор без верного написания до 3-го уровня.
4. Проверить по логу: `page_recognized{role}`, `reference_resolved{origin, trust}`, `finding_created`, `finding_confirmed`, `orthogram_classified`; `docker stats homework-ocr`.
5. Отрицательные сценарии: только тетрадь без учебника → «пришли фото учебника»; OCR остановлен → «не смог прочитать тетрадь», событие `ocr_failed`, бот жив.

- [ ] **Step 6: Commit и запись сессии**

```bash
git add src/hwcheck/bot/subjects.py docs TODO.md HISTORY.md tests/test_subjects.py
git commit -m "feat: русский язык открыт — флаг available, runbook OCR, политика (фото учебников)"
```

---

## Самопроверка плана по спецификации

- §3 п.1 (научить, не решить): тьютор по слову — уровни 0–3, слово не раскрывается до уровня 3, защита от утечки в `tutor_reply` (R5). п.2 (два источника): «как написано» — OCR (R1, R3), «как должно быть» — учебник + словарь + база (R2, R3, R5). п.3 (сила вердикта): все находки `candidate`, ошибка — только после «да» (R4, R7); ложных `verified` ≤ 5 % — по построению, стенд меряет точность кандидатов (R8). п.4 (модуль): `RussianModule` в реестре (R5), бот — общий путь языков (R7). п.5 (база от работы): `resolve_reference` сохраняет страницу и ответ, `unverified` — в `kb review` (R5). п.6 (стенд до флага): R8 → R9.
- §4 контракт: `recognize`/`resolve_reference`/`check`/`start_tutoring` — R5; `SubjectTask.words` с координатами — R3; `Finding.word` для кропа — R4; отступления записаны в шапке.
- §5 база: `kb_pages`/`kb_tasks`/`kb_answers` заполняются (R5), `kb_rules` — R6, `kb_words` — не используется (Hunspell из файла быстрее и не нуждается в таблице; отметить в PROJECT_MEMORY как решение), очередь `kb review` работает для `llm:*` ответов (R5), `photo_path` — `var/kb_photos` (R7).
- §6 распознавание — R1, R3; эталон — R2, R5; проверка (таблица ситуаций): совпало → нет находки; расхождение в пропуске → `candidate` первым; вне пропуска → ниже приоритетом; пропущено/лишнее слово → `candidate` с текстом «кажется, пропущено слово после …» (R4); лимит уточнений — `MAX_QUESTIONS` (R7); тьютор — классификатор орфограммы + `kb_rules`, уровни 0–3 (R5, R6). «Не входит» — не делаем.
- §8 сводка — общая форма работает без `grade` (R7); уточнения `word` с кропом — уже в каркасе, `photo_index` проставляет бот (R7); события `finding_created`, `finding_confirmed`, `reference_resolved`, `ocr_failed`, новые `page_recognized`, `orthogram_classified` (R7); инфраструктура — контейнер 2 ГБ, семафор, таймаут 60 с (R1, R9); тесты контракта на фейках (R5), стенд (R8).
- §11 риски: память VPS — R1 рецепт + R9 дымовой тест; ложные срабатывания OCR — приоритеты и лимит (R4), доля «нет» — метрика R8; правила — вычитка Кириллом (R6); авторские права — в базе только текст упражнений из фото пользователей (R5), к юристу вместе с политикой (R9); кропы — проверены живьём 17.09 (каркас).
- Блокеры ревью каркаса: закрыты в R7 (перечислены в заголовке задачи); `findings.confirmed`/`homework_id` в БД — в TODO.
- Типы сквозные: `DerivedText` (R2) → `Reference.payload` (R5) → `check_words` (R4) → `TaskResult.payload["sentence"]` (R5) → `WordTutoring.sentence` (R5); `SubjectTask.photo_path` (R3) → `KbPage.photo_path` (R5) ← `kb_paths` бота (R7); `SubjectPage.failure` (R3) → `ocr_failed` (R7); `CheckPhotos.subject` (R7) → `_process_photos(subject)`; `header_number` (R3) ← `check.py` (R4).
