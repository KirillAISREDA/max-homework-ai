"""Прогон ReadingPipeline без LM по каталогу фото — версия для спайка памяти.

Отличия от spikes/ocr_ru/run.py (throwaway, не для прода):
  - RSS (resource.getrusage) логируется ПОСЛЕ КАЖДОЙ СТАДИИ пайплайна (не только
    после каждого фото) для самого первого фото — чтобы найти, на какой стадии
    (сегментация / OCR / постобработка) происходит скачок памяти.
  - tracemalloc — снимок топ-Python-аллокаций после первого фото (дополнительный
    сигнал; onnxruntime/opencv/torch аллоцируют нативную память мимо pymalloc,
    поэтому это НЕ основной сигнал, см. README.md).
  - Настройки через переменные окружения (см. Dockerfile ENV и README.md):
    OCR_MEM_THREADS, OCR_MEM_ORT_ARENA, OCR_MEM_ORT_MEMPATTERN, OCR_MEM_RUNTIME,
    OCR_MEM_SEGM_SIZE, OCR_MEM_SEGM_MODEL_PATH.
  - Формат /out/<имя>.json не изменён (text/box/confidence/line) — старый вывод
    spikes/ocr_ru можно диффить с новым построчно по полю "text" для проверки
    паритета качества.

См. docs/research/2026-09-17-readingpipeline-memory.md.
"""

import json
import os
import resource
import sys
import time
import tracemalloc
from pathlib import Path

WEIGHTS_DIR = "/app/weights"
os.chdir(WEIGHTS_DIR)

sys.path.insert(0, "/app/ReadingPipeline")

# --- Настройки из окружения (читаем до любых импортов torch/onnxruntime,
# чтобы OMP_NUM_THREADS/MALLOC_ARENA_MAX точно относились к этому процессу
# с самого начала; сами переменные заданы в Dockerfile ENV или через
# `docker run -e`, здесь только читаем для лога и для настройки библиотек). ---
THREADS = int(os.environ.get("OCR_MEM_THREADS", "2"))
ORT_ARENA = os.environ.get("OCR_MEM_ORT_ARENA", "1") == "1"
ORT_MEMPATTERN = os.environ.get("OCR_MEM_ORT_MEMPATTERN", "1") == "1"
RUNTIME = os.environ.get("OCR_MEM_RUNTIME", "ONNX")
SEGM_SIZE = os.environ.get("OCR_MEM_SEGM_SIZE", "").strip()
SEGM_MODEL_PATH = os.environ.get("OCR_MEM_SEGM_MODEL_PATH", "").strip()

import onnxruntime as ort  # noqa: E402

# onnxruntime.SessionOptions() создаётся внутри OCR-model/ocr/predictor.py и
# SEGM-model/segm/predictor.py (SegmONNXCPUModel/OCRONNXCPUModel.__init__) —
# эти файлы мы не патчим (throwaway, но чисто), вместо этого подменяем сам
# конструктор SessionOptions в уже импортированном модуле onnxruntime: когда
# predictor.py сделает `import onnxruntime as ort; ort.SessionOptions()`, он
# получит тот же объект модуля (кэш sys.modules) с уже подменённым атрибутом.
_ORIG_SESSION_OPTIONS = ort.SessionOptions


def _patched_session_options(*args, **kwargs):
    so = _ORIG_SESSION_OPTIONS(*args, **kwargs)
    so.enable_cpu_mem_arena = ORT_ARENA
    so.enable_mem_pattern = ORT_MEMPATTERN
    return so


ort.SessionOptions = _patched_session_options

try:
    # Попытка #4 (docs/research/2026-09-17-readingpipeline-memory.md): в
    # Dockerfile.notorch torch не установлен вовсе (~200 МБ RSS экономии на
    # импорте) — OCRTorchModel/SegmTorchModel и BeamSearcDecoder всё равно не
    # используются при runtime=ONNX и lm_path="" (см. patch_no_torch.py), так
    # что PipelinePredictor работает и без torch. Здесь просто не падаем, если
    # его нет, и не выставляем num_threads (нечему).
    import torch  # noqa: E402

    torch.set_num_threads(THREADS)
except ImportError:
    torch = None

from ocrpipeline.predictor import PipelinePredictor  # noqa: E402

TEXT_CLASS = "shrinked_text"
ORIGINAL_CONFIG = "pipeline_config.json"
RUNTIME_CONFIG = "pipeline_config.runtime.json"
SEGM_CONFIG_ORIGINAL = "segm/segm_config.json"
SEGM_CONFIG_RUNTIME = "segm/segm_config.runtime.json"


def ru_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def make_segm_config_override(size: int) -> str:
    """Патчит segm_config.json: image.width/height -> size x size.

    ВНИМАНИЕ (гипотеза, подтверждается/опровергается этим же прогоном): ONNX-граф
    segm_model.onnx экспортирован SEGM-model/scripts/torch2onnx.py с
    dynamic_axes только по batch_size (см. код скрипта в апстриме) — H и W
    зашиты в граф на момент экспорта (896x896). Патч этого конфига меняет
    только препроцессинг (resize перед подачей в сессию), а не сам граф —
    если граф действительно фиксирован по пространственным осям, onnxruntime
    должен упасть с ошибкой несовпадения shape при попытке скормить 640x640
    или 768x768. Это ожидаемый и информативный результат попытки #3 без
    переэкспорта (см. export_segm_onnx.py и README.md).
    """
    with open(SEGM_CONFIG_ORIGINAL, encoding="utf-8") as f:
        config = json.load(f)
    config["image"]["width"] = size
    config["image"]["height"] = size
    with open(SEGM_CONFIG_RUNTIME, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=1)
    return SEGM_CONFIG_RUNTIME


def make_runtime_config() -> str:
    """Патчит pipeline_config.json: GPU/Pytorch/KenLM из весов -> CPU + настройки спайка."""
    with open(ORIGINAL_CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    main_process = config["main_process"]
    for step, onnx_path in (
        ("SegmPrediction", "segm/segm_model.onnx"),
        ("OCRPrediction", "ocr/ocr_model.onnx"),
    ):
        main_process[step]["device"] = "cpu"
        main_process[step]["runtime"] = RUNTIME
        main_process[step]["model_path"] = onnx_path
        main_process[step]["num_threads"] = THREADS
    main_process["OCRPrediction"]["lm_path"] = ""

    if SEGM_SIZE:
        size = int(SEGM_SIZE)
        main_process["SegmPrediction"]["config_path"] = make_segm_config_override(size)
    if SEGM_MODEL_PATH:
        main_process["SegmPrediction"]["model_path"] = SEGM_MODEL_PATH

    with open(RUNTIME_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=1)
    return RUNTIME_CONFIG


def run_pipeline_instrumented(predictor: PipelinePredictor, image, log_prefix: str):
    """Повторяет PipelinePredictor.__call__, но логирует RSS после каждой стадии."""
    pred_img = None
    for process_func in predictor.main_process_funcs:
        image, pred_img = process_func(image, pred_img)
        print(
            f"  [{log_prefix}] after {type(process_func).__name__}: RSS={ru_mb():.0f} MB",
            flush=True,
        )
    return image, pred_img


def main() -> None:
    import cv2  # noqa: PLC0415 (импорт после sys.path.insert, как в ocr_ru)

    print(
        "config: "
        f"THREADS={THREADS} ORT_ARENA={ORT_ARENA} ORT_MEMPATTERN={ORT_MEMPATTERN} "
        f"RUNTIME={RUNTIME} SEGM_SIZE={SEGM_SIZE or 'default(896)'} "
        f"SEGM_MODEL_PATH={SEGM_MODEL_PATH or 'default'} "
        f"MALLOC_ARENA_MAX={os.environ.get('MALLOC_ARENA_MAX', '(unset)')} "
        f"OMP_NUM_THREADS={os.environ.get('OMP_NUM_THREADS', '(unset)')} "
        f"torch_installed={torch is not None}",
        flush=True,
    )

    config_path = make_runtime_config()
    predictor = PipelinePredictor(pipeline_config_path=config_path)
    loaded_mb = ru_mb()
    print(f"models loaded, RSS={loaded_mb:.0f} MB", flush=True)

    out_dir = Path("/out")
    out_dir.mkdir(exist_ok=True)
    timings: list[float] = []
    per_image: list[dict] = []

    photo_paths = sorted(Path("/data").glob("*.jp*g"))
    for i, path in enumerate(photo_paths):
        image = cv2.imread(str(path))
        started = time.perf_counter()

        if i == 0:
            tracemalloc.start()
            _rotated, pred = run_pipeline_instrumented(predictor, image, path.name)
            snapshot = tracemalloc.take_snapshot()
            tracemalloc.stop()
            print(f"  [{path.name}] tracemalloc top-5 (Python-level only, see README):", flush=True)
            for stat in snapshot.statistics("lineno")[:5]:
                print(f"    {stat}", flush=True)
        else:
            _rotated, pred = predictor(image)

        elapsed = time.perf_counter() - started
        timings.append(elapsed)
        words = [
            {
                "text": p.get("text"),
                "box": p.get("bbox"),
                "confidence": p.get("confidence"),
                "line": p.get("line_idx"),
            }
            for p in pred["predictions"]
            if p.get("class_name") == TEXT_CLASS
        ]
        (out_dir / f"{path.stem}.json").write_text(
            json.dumps({"seconds": elapsed, "words": words}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        rss_mb = ru_mb()
        per_image.append(
            {
                "file": path.name,
                "seconds": round(elapsed, 2),
                "words": len(words),
                "rss_after_mb": round(rss_mb),
            }
        )
        print(f"{path.name}: {elapsed:.1f} s, {len(words)} words, RSS={rss_mb:.0f} MB", flush=True)

    peak_mb = ru_mb()
    timings.sort()
    n = len(timings)
    p50 = timings[n // 2]
    p95 = timings[min(n - 1, int(n * 0.95))]
    summary = {
        "config": {
            "threads": THREADS,
            "ort_arena": ORT_ARENA,
            "ort_mempattern": ORT_MEMPATTERN,
            "runtime": RUNTIME,
            "segm_size": SEGM_SIZE or None,
            "segm_model_path": SEGM_MODEL_PATH or None,
        },
        "per_image": per_image,
        "p50_seconds": round(p50, 2),
        "p95_seconds": round(p95, 2),
        "peak_rss_mb": round(peak_mb),
        "loaded_rss_mb": round(loaded_mb),
    }
    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"p50={p50:.1f}s p95={p95:.1f}s")
    print(f"peak RSS={peak_mb:.0f} MB")


if __name__ == "__main__":
    main()
