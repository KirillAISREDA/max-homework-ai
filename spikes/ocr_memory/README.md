# Спайк памяти: ReadingPipeline ≤ 1,5 ГБ на CPU VPS

Throwaway-код для follow-up спайка `docs/research/2026-09-17-readingpipeline-memory.md`: снизить
пиковую память ReadingPipeline (ai-forever, без LM, CPU/ONNX) с ~2,5–3 ГБ (см.
`docs/research/2026-09-17-readingpipeline-vps.md`) до ≤ 1,5 ГБ без потери качества распознавания.
Не для прода. Все выводы, цифры и таблица попыток — в файле исследования.

## Файлы

- `Dockerfile` — базовый образ (как `spikes/ocr_ru`, с torch), инструментированный `run.py`,
  env-переключатели для экспериментов (попытки 1–3).
- `Dockerfile.notorch` — **лучшая найденная конфигурация**: тот же пайплайн без torch/torchvision
  вообще (патч `patch_no_torch.py`) + arena/mem-pattern onnxruntime выключены по умолчанию. Дал
  **1454–1457 МБ** пика на 5 живых фото, текст побайтово идентичен неоптимизированному прогону.
- `run.py` — прогон по каталогу фото; логирует RSS **после каждой стадии пайплайна** для первого фото
  (не только после каждого фото целиком, как в `ocr_ru`), плюс snapshot `tracemalloc` (см. оговорку
  ниже). Настройки — через переменные окружения:
  - `OCR_MEM_THREADS` (default `2`) — `num_threads` для обеих ONNX-сессий и `torch.set_num_threads`
    (если torch установлен).
  - `OCR_MEM_ORT_ARENA` / `OCR_MEM_ORT_MEMPATTERN` (`1`/`0`, default `1` в базовом `Dockerfile`, `0` в
    `Dockerfile.notorch`) — `enable_cpu_mem_arena` / `enable_mem_pattern` на
    `onnxruntime.SessionOptions`, патчатся через монки-патч `onnxruntime.SessionOptions` в `run.py` (до
    создания `PipelinePredictor`) — апстримный код `OCR-model`/`SEGM-model` не трогаем.
  - `OCR_MEM_RUNTIME` (default `ONNX`) — пробрасывается в `pipeline_config.runtime.json`
    (`main_process.*.runtime`); поддерживает `OpenVino`, не понадобилось (см. отчёт, попытка #5).
  - `OCR_MEM_SEGM_SIZE` — если задано, патчит `image.width/height` в `segm_config.json`
    **и ожидает**, что `OCR_MEM_SEGM_MODEL_PATH` указывает на ONNX-граф, переэкспортированный под этот
    размер (см. ниже) — без переэкспорта onnxruntime упадёт `INVALID_ARGUMENT` (граф сегментации
    зафиксирован по H/W при экспорте апстримом, не динамический).
  - `OCR_MEM_SEGM_MODEL_PATH` — переопределить путь к `.onnx` весам сегментации (для переэкспортированных
    графов).
- `export_segm_onnx.py` — переэкспорт `segm_model.ckpt` в ONNX с другим `H=W` входа (тот же код, что
  апстримный `SEGM-model/scripts/torch2onnx.py`, параметризован). Нужен только чтобы проверить попытку
  #3 (снижение разрешения сегментации) — **отклонена по качеству** (10–31 % меньше распознанных слов на
  640/768 против 896), несмотря на то что по памяти работает (1420/1490 МБ).
- `patch_no_torch.py` — убирает безусловные `import torch`/`import torchvision` из клонов
  `OCR-model`/`SEGM-model` (они не нужны на ONNX-пути без LM — см. докстринг файла с построчным
  обоснованием по коду апстрима) и подменяет `torchvision.transforms.Compose` тривиальным классом.
  Запускается в `Dockerfile.notorch` между `git clone` и `pip install --no-deps`.

## Как повторить лучший результат

```bash
ssh root@<host> "free -m"   # available должно быть заметно больше выбранного --memory
scp -r spikes/ocr_memory root@<host>:/opt/spike-ocr-memory
scp data/live0913/*.jpg data/live0914/*.jpg root@<host>:/opt/spike-ocr-memory/data/   # см. ниже про набор фото
ssh root@<host> "cd /opt/spike-ocr-memory && docker build -t ocr-ru-lowmem -f Dockerfile.notorch . \
  && docker run --rm --memory=1750m --cpus=2 -v \$PWD/data:/data -v \$PWD/out:/out ocr-ru-lowmem"
scp -r root@<host>:/opt/spike-ocr-memory/out spikes/ocr_memory/out
ssh root@<host> "docker rmi ocr-ru-lowmem; rm -rf /opt/spike-ocr-memory"   # фото детей с VPS убрать
```

Набор фото — **тот же 5 рукописных фото**, что и в `spikes/ocr_ru` (для сравнимости чисел): 3
уникальных фото страницы №52 (`live0913`) + 2 фото страницы №55/57 (`live0914`), сконвертированные из
`.webp` в `.jpg`. См. dedup по md5 в `docs/research/2026-09-17-readingpipeline-vps.md`. Печатные
страницы учебника (были в первом спайке) сюда намеренно не включены — не входят в целевой сценарий этого
контейнера (см. решение в первом спайке, п.6) и не участвовали в измерении памяти этого follow-up.

## Экспериментальные конфигурации (для попыток 1–3, базовый `Dockerfile`)

```bash
docker build -t spike-ocr-memory .
# попытка 1 (baseline + инструментация по стадиям):
docker run --rm --memory=3g --cpus=2 -v $PWD/data:/data -v $PWD/out:/out spike-ocr-memory
# попытка 2 (arena/mem-pattern off):
docker run --rm --memory=3g --cpus=2 \
  -e OCR_MEM_ORT_ARENA=0 -e OCR_MEM_ORT_MEMPATTERN=0 \
  -v $PWD/data:/data -v $PWD/out:/out spike-ocr-memory
# попытка 3 (переэкспорт сегментации под 640, ОТКЛОНЕНО ПО КАЧЕСТВУ — см. отчёт):
docker run --rm --memory=3g --cpus=2 --entrypoint python \
  -v $PWD/out:/out spike-ocr-memory /app/export_segm_onnx.py --size 640 \
  --ckpt /app/weights/segm/segm_model.ckpt --out /out/segm_model_640.onnx
docker run --rm --memory=3g --cpus=2 \
  -e OCR_MEM_ORT_ARENA=0 -e OCR_MEM_ORT_MEMPATTERN=0 \
  -e OCR_MEM_SEGM_SIZE=640 -e OCR_MEM_SEGM_MODEL_PATH=/segm640/segm_model_640.onnx \
  -v $PWD/data:/data -v $PWD/out:/out -v $PWD/out:/segm640:ro spike-ocr-memory
```

## Проверка качества (паритет с baseline)

`run.py` пишет тот же формат JSON, что `spikes/ocr_ru/run.py` (`text`/`box`/`confidence`/`line`) — любые
два прогона диффятся построчно по полю `words`. В этом спайке так проверено: попытка 2 (arena/mem-pattern
off) и попытка 4 (без torch) дают **побайтово идентичный** список слов (текст+бокс+confidence+line) на
всех 5 фото по сравнению с попыткой 1 (baseline); попытки 3a/3b (640/768) — нет, см. отчёт.

## Оговорка про `tracemalloc`

`run.py` берёт snapshot `tracemalloc` после первого фото — он показывает только Python-уровневые
аллокации (объекты, списки контуров и т.п.), не нативную память `onnxruntime`/`opencv`/`torch` (та
аллоцируется мимо `pymalloc`). В этом спайке главным сигналом было **не** `tracemalloc`, а
`resource.getrusage().ru_maxrss`, снятый после каждой стадии пайплайна — именно так нашли, что скачок
памяти происходит на первом реальном вызове `onnxruntime.InferenceSession.run()` в `SegmPrediction` и
`OCRPrediction`, а не в постобработке. `tracemalloc` оставлен в коде как дополнительный, второстепенный
сигнал (и как ответ на прямое указание в брифе попробовать оба инструмента).
