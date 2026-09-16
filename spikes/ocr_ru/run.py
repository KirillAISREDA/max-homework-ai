"""Прогон ReadingPipeline без LM по каталогу фото: слова с координатами, время, память.

Спайк: throwaway-код для измерений, не для прода.
См. docs/research/2026-09-17-readingpipeline-vps.md.
"""

import json
import os
import resource
import sys
import time
from pathlib import Path

WEIGHTS_DIR = "/app/weights"
# pipeline_config.json из ai-forever/ReadingPipeline-notebooks хранит model_path
# и config_path относительными путями ("segm/segm_model.onnx" и т.п.) — они
# резолвятся от текущей рабочей директории процесса, поэтому переходим сюда
# до создания PipelinePredictor.
os.chdir(WEIGHTS_DIR)

sys.path.insert(0, "/app/ReadingPipeline")
from ocrpipeline.predictor import PipelinePredictor  # noqa: E402

# Класс слов проверен по факту в pipeline_config.json весов: "shrinked_text"
# (а не "text" из примера в README репозитория и не "handwritten_text_shrinked_mask1"
# из черновика в задаче — тот класс в этих весах не встречается).
TEXT_CLASS = "shrinked_text"
ORIGINAL_CONFIG = "pipeline_config.json"
RUNTIME_CONFIG = "pipeline_config.runtime.json"


def make_runtime_config() -> str:
    """Патчит pipeline_config.json: GPU/Pytorch/KenLM из весов -> CPU/ONNX без LM.

    Исходный конфиг в весах рассчитан на GPU (device=cuda, runtime=Pytorch,
    model_path указывает на .ckpt, lm_path на KenLM). Правим на CPU + ONNX-веса,
    без LM (пустой lm_path -> BestPathDecoder вместо BeamSearcDecoder, см.
    OCR-model/ocr/predictor.py), потоки — по числу CPU контейнера (--cpus=2).
    """
    with open(ORIGINAL_CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    main_process = config["main_process"]
    for step, onnx_path in (
        ("SegmPrediction", "segm/segm_model.onnx"),
        ("OCRPrediction", "ocr/ocr_model.onnx"),
    ):
        main_process[step]["device"] = "cpu"
        main_process[step]["runtime"] = "ONNX"
        main_process[step]["model_path"] = onnx_path
        main_process[step]["num_threads"] = 2
    main_process["OCRPrediction"]["lm_path"] = ""
    with open(RUNTIME_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=1)
    return RUNTIME_CONFIG


def main() -> None:
    import cv2  # noqa: PLC0415 (импорт после sys.path.insert, как в чернoвике задачи)

    config_path = make_runtime_config()
    predictor = PipelinePredictor(pipeline_config_path=config_path)
    loaded_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"models loaded, RSS={loaded_mb:.0f} MB", flush=True)

    out_dir = Path("/out")
    out_dir.mkdir(exist_ok=True)
    timings: list[float] = []
    per_image: list[dict] = []

    for path in sorted(Path("/data").glob("*.jp*g")):
        image = cv2.imread(str(path))
        started = time.perf_counter()
        _rotated, pred = predictor(image)
        elapsed = time.perf_counter() - started
        timings.append(elapsed)
        words = [
            {
                "text": p.get("text"),
                # 'bbox' — координаты в исходном (неповёрнутом) фото; если для
                # Task 9 нужен кроп именно слова, используйте 'rotated_bbox'
                # вместе с повёрнутым изображением, которое возвращает predictor().
                "box": p.get("bbox"),
                # OCR-model/ocr/predictor.py не отдаёт confidence по умолчанию
                # (BestPathDecoder возвращает только текст) — поле всегда None,
                # пока пайплайн не доработан отдельно.
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
        # RSS печатаем после каждого фото, а не только в конце: если контейнер
        # упрётся в --memory и его убьют (OOM), в логе всё равно останется
        # память по уже обработанным фото, а не только время.
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        per_image.append({
            "file": path.name,
            "seconds": round(elapsed, 2),
            "words": len(words),
            "rss_after_mb": round(rss_mb),
        })
        print(f"{path.name}: {elapsed:.1f} s, {len(words)} words, RSS={rss_mb:.0f} MB", flush=True)

    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    timings.sort()
    n = len(timings)
    p50 = timings[n // 2]
    p95 = timings[min(n - 1, int(n * 0.95))]
    summary = {
        "per_image": per_image,
        "p50_seconds": round(p50, 2),
        "p95_seconds": round(p95, 2),
        "peak_rss_mb": round(peak_mb),
    }
    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"p50={p50:.1f}s p95={p95:.1f}s")
    print(f"peak RSS={peak_mb:.0f} MB")


if __name__ == "__main__":
    main()
