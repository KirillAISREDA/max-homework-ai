# Спайк: ReadingPipeline на CPU VPS

Throwaway-код для измерения времени/памяти/качества ReadingPipeline (ai-forever) без языковой
модели, на CPU, в Docker на общем VPS. Не для прода. Числа и выводы — в
`docs/research/2026-09-17-readingpipeline-vps.md`.

## Расхождения с черновиком задачи (Task 1) и почему

Черновик Dockerfile/run.py в `task-1-brief.md` — приблизительный, с пометкой «версии и пути сверить
при сборке». Сверили сборкой на VPS 16.09–17.09, отличия:

1. **Веса `ai-forever/ReadingPipeline-notebooks` тянем без `.ckpt` и `.arpa`**
   (`snapshot_download(..., ignore_patterns=[...])`) — нужны только ONNX-веса (сегментация ~110 МБ +
   OCR ~48 МБ) и конфиги; PyTorch-чекпоинты (ещё ~165 МБ) и KenLM (12 МБ, LM выключен) не нужны.
2. **Класс слов в весах — `shrinked_text`**, не `"text"` (пример из README репозитория) и не
   `"handwritten_text_shrinked_mask1"` (предположение в черновике). Проверено по фактическому
   `pipeline_config.json` из весов: `"classes_to_ocr": ["shrinked_text"]`.
3. **`pred["predictions"]` не содержит `confidence`.** `OcrPredictor.__call__` (OCR-model) без LM
   зовёт `BestPathDecoder.decode_numpy`, которая возвращает только строку текста. Поле `confidence`
   в JSON — всегда `null`, пока пайплайн не доработан отдельно (нужно менять `ocr/tokenizer.py`,
   чтобы decoder отдавал ещё и уверенность по softmax).
4. **`pipeline_config.json` в весах рассчитан на GPU** (`device: cuda`, `runtime: Pytorch`,
   `model_path` на `.ckpt`, `lm_path` на KenLM). `run.py` патчит его в рантайме
   (`make_runtime_config`) на `device: cpu`, `runtime: ONNX`, `model_path` на `.onnx`, `lm_path: ""`,
   `num_threads: 2` (под `--cpus=2`) — оригинальный файл не трогаем, пишем
   `pipeline_config.runtime.json` рядом.
5. **Пути `model_path`/`config_path` в `pipeline_config.json` относительные** (`"segm/segm_model.onnx"`
   и т.п.) и резолвятся от текущей рабочей директории процесса, не от расположения самого конфига —
   `run.py` делает `os.chdir("/app/weights")` до создания `PipelinePredictor`.
6. **`pip install -r ReadingPipeline/requirements.txt` не используем.** Он тянет
   `ocrmodel @ git+.../OCR-model.git`, чей `requirements.txt` (он же `install_requires` в
   `setup.py`) требует `ctcdecode @ git+https://github.com/parlance/ctcdecode` — C++-расширение,
   которое клонирует и собирает kenlm+openfst. Нужен только для beam search с LM, которую сознательно
   не используем (см. `docs/research/2026-09-13-ru-handwriting-ocr.md`: LM выключать — с ней доля
   скрытых ошибок ученика растёт вдвое). Вместо сборки — клонируем ReadingPipeline/OCR-model/SEGM-model
   репозиториями и ставим их `pip install --no-deps` поверх вручную поставленных зависимостей, а
   `ctcdecode` подменяем модулем-заглушкой (класс поднимает `NotImplementedError`, но код его не
   вызывает, пока `lm_path` пуст).
7. **Версии зависимостей не совпадают с оригинальными requirements.txt (2022 год)** — часть версий
   не собирается/не ставится на Python 3.10 на этом образе и ядре (проверено сборкой на VPS 16.09):
   - `scikit-learn==1.0.1`, `scipy==1.4.1` → нет колеса под `cp310`, сборка из исходников падает
     (`numpy.distutils` зовёт `distutils.msvccompiler`, которого нет). Заменены на
     `scikit-learn==1.3.2`, `scipy==1.10.1` — тот же публичный API, есть готовые колёса.
   - `openvino==2022.2.0` снят с PyPI. Заменён на `openvino==2024.6.0` — `openvino.runtime`
     (единственное, что нужно для импорта в OCR-model/SEGM-model) на месте.
   - `onnxruntime==1.13.1` на этом VPS (Ubuntu 22.04.5, ядро 5.15.0-191) падает при импорте:
     `ImportError: ...onnxruntime_pybind11_state...: cannot enable executable stack as shared object
     requires: Invalid argument` — известная проблема старого manylinux-колеса на этом ядре.
     `onnxruntime==1.17.3` импортируется и работает нормально.
   - `numpy` закреплён `<2` (`1.23.1`, как в оригинале): `torch==2.2.2` и старые колёса
     `opencv-python`/`onnxruntime` собраны под NumPy 1.x ABI, с NumPy 2.x падают на импорте
     (`_ARRAY_API not found`).
   - `pyclipper==1.3.0` не имеет колеса под `cp310` на этой платформе — собирается из исходников
     (C++), нужен `g++` (единственная реальная компиляция в образе, быстрая).
   - `--index-url https://download.pytorch.org/whl/cpu` (как в черновике) ломает установку torch:
     pip тогда не видит на PyPI чистые python-пакеты вида `flit_core`, нужные как build-зависимость
     транзитивным пакетам. Исправлено на `--extra-index-url`.

## Формат вывода на фото (`/out/<имя>.json`)

```json
{
  "seconds": 5.8,
  "words": [
    {"text": "651", "box": [x0, y0, x1, y1], "confidence": null, "line": 0},
    ...
  ]
}
```

`box` — координаты в системе координат исходного (неповёрнутого) фото (ключ `bbox` в
`pred["predictions"]`). Если для Task 9 нужен кроп именно слова (а не проверка на глаз по
исходному фото), берите `rotated_bbox` вместе с повёрнутым изображением, которое возвращает
`predictor(image)` первым элементом — `RestoreImageAngle` может повернуть картинку, и тогда
`bbox` (до поворота) и `rotated_bbox` (после) — в разных системах координат.

## Прогон на VPS

```bash
ssh root@193.247.73.243 "free -m"   # свободно должно быть ≥ 2.5 ГБ
scp -r spikes/ocr_ru root@193.247.73.243:/opt/spike-ocr-ru
scp data/live0913/*.jpg data/live0914/*.jpg root@193.247.73.243:/opt/spike-ocr-ru/data/
ssh root@193.247.73.243 "cd /opt/spike-ocr-ru && docker build -t spike-ocr-ru . \
  && docker run --rm --memory=2g --cpus=2 -v \$PWD/data:/data -v \$PWD/out:/out spike-ocr-ru"
scp -r root@193.247.73.243:/opt/spike-ocr-ru/out spikes/ocr_ru/out
ssh root@193.247.73.243 "docker rmi spike-ocr-ru; rm -rf /opt/spike-ocr-ru"   # фото детей с VPS убрать
```

Фото — реальные из MAX (`data/live0913`, `data/live0914` в этом воркtree, не в git — см. `.gitignore`).
Конвертированы из `.webp` (как реально приходят из MAX) в `.jpg` локально перед заливкой на VPS —
`run.py` ищет `*.jp*g`. Среди исходных файлов в `data/live0913/*.webp` часть — побайтовые дубликаты
(несколько фото одной и той же страницы тетради, присланные повторно) — на прогон и метрики взяты
только уникальные по содержимому файлы.
