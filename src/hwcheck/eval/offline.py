"""Офлайн-прогон Vision по датасету фото (арх. §10, неделя 1).

Цели: замерить латентность vision-этапа (риск против целевых 20-60 с на проверку)
и долю валидных JSON. Сравнение с ground truth добавим, когда датасет будет размечен.
"""

import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from hwcheck.llm.base import VisionClient
from hwcheck.pipeline.vision import recognize_page

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


@dataclass
class ImageEvalResult:
    image: str
    latency_s: float
    tokens_in: int
    tokens_out: int
    json_valid: bool
    n_tasks: int | None
    min_confidence: float | None
    orientation: int
    attempts: int
    error: str | None
    raw: str


async def run_offline_eval(
    client: VisionClient,
    dataset_dir: Path,
    out_file: Path,
    *,
    model: str,
    prompt: str,
    prompt_version: str,
    limit: int | None = None,
) -> list[ImageEvalResult]:
    images = sorted(p for p in dataset_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if limit is not None:
        images = images[:limit]
    if not images:
        raise SystemExit(f"В {dataset_dir} нет изображений ({'/'.join(IMAGE_SUFFIXES)})")

    results: list[ImageEvalResult] = []
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as out:
        for i, path in enumerate(images, 1):
            print(f"[{i}/{len(images)}] {path.name} ...", flush=True)
            t0 = time.monotonic()
            try:
                rec = await recognize_page(
                    client, path.read_bytes(), prompt=prompt, model=model, filename=path.name
                )
                result = ImageEvalResult(
                    image=path.name,
                    latency_s=round(rec.latency_s, 2),
                    tokens_in=rec.tokens_in,
                    tokens_out=rec.tokens_out,
                    json_valid=rec.page is not None,
                    n_tasks=len(rec.page.tasks) if rec.page else None,
                    min_confidence=(
                        min((t.confidence for t in rec.page.tasks), default=None)
                        if rec.page
                        else None
                    ),
                    orientation=rec.orientation,
                    attempts=rec.attempts,
                    error=None if rec.page is not None else "invalid JSON in all orientations",
                    raw=rec.raw,
                )
            except Exception as exc:  # ошибки API не должны ронять весь прогон
                result = ImageEvalResult(
                    image=path.name,
                    latency_s=round(time.monotonic() - t0, 2),
                    tokens_in=0,
                    tokens_out=0,
                    json_valid=False,
                    n_tasks=None,
                    min_confidence=None,
                    orientation=0,
                    attempts=0,
                    error=f"{type(exc).__name__}: {exc}",
                    raw="",
                )
            record = asdict(result) | {"model": model, "prompt_version": prompt_version}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            results.append(result)

    _print_summary(results, model=model, prompt_version=prompt_version)
    return results


def _print_summary(results: list[ImageEvalResult], *, model: str, prompt_version: str) -> None:
    latencies = sorted(r.latency_s for r in results if r.attempts)
    valid = [r for r in results if r.json_valid]
    with_tasks = [r for r in valid if r.n_tasks]
    print(f"\n=== Итог: {len(results)} фото, model={model}, prompt={prompt_version} ===")
    print(f"Валидный JSON: {len(valid)}/{len(results)}")
    print(f"Найдены задания: {len(with_tasks)}/{len(results)}")
    if latencies:
        p90 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))]
        print(
            f"Латентность на фото (все попытки), с: медиана {statistics.median(latencies):.1f}, "
            f"p90 {p90:.1f}, max {latencies[-1]:.1f}"
        )
    if with_tasks:
        rotated = [r for r in with_tasks if r.orientation]
        attempts = statistics.mean(r.attempts for r in with_tasks)
        tokens = statistics.mean(r.tokens_in + r.tokens_out for r in with_tasks)
        print(
            f"Понадобился поворот: {len(rotated)}/{len(with_tasks)}, средних попыток {attempts:.1f}"
        )
        print(f"Средние токены на успешное фото: {tokens:.0f}")
        low_conf = sum(
            1 for r in with_tasks if r.min_confidence is not None and r.min_confidence < 0.7
        )
        print(f"Фото с заданиями confidence < 0.7: {low_conf}/{len(with_tasks)}")
