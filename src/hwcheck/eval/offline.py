"""Офлайн-прогон Vision по датасету фото (арх. §10, неделя 1).

Цели: замерить латентность vision-этапа (риск против целевых 20-60 с на проверку)
и долю валидных JSON. Сравнение с ground truth добавим, когда датасет будет размечен.
"""

import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import ValidationError

from hwcheck.llm.base import VisionClient, extract_json
from hwcheck.pipeline.schemas import VisionPage

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
                llm_result = await client.analyze_image(
                    path.read_bytes(), prompt=prompt, model=model, filename=path.name
                )
                page, page_error = _parse_page(llm_result.content)
                result = ImageEvalResult(
                    image=path.name,
                    latency_s=round(llm_result.latency_s, 2),
                    tokens_in=llm_result.tokens_in,
                    tokens_out=llm_result.tokens_out,
                    json_valid=page is not None,
                    n_tasks=len(page.tasks) if page else None,
                    min_confidence=(
                        min((t.confidence for t in page.tasks), default=None) if page else None
                    ),
                    error=page_error,
                    raw=llm_result.content,
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
                    error=f"{type(exc).__name__}: {exc}",
                    raw="",
                )
            record = asdict(result) | {"model": model, "prompt_version": prompt_version}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            results.append(result)

    _print_summary(results, model=model, prompt_version=prompt_version)
    return results


def _parse_page(content: str) -> tuple[VisionPage | None, str | None]:
    try:
        return VisionPage.model_validate_json(extract_json(content)), None
    except ValidationError as exc:
        return None, f"invalid JSON: {exc.error_count()} errors"


def _print_summary(results: list[ImageEvalResult], *, model: str, prompt_version: str) -> None:
    latencies = sorted(r.latency_s for r in results if r.raw)
    valid = [r for r in results if r.json_valid]
    print(f"\n=== Итог: {len(results)} фото, model={model}, prompt={prompt_version} ===")
    print(f"Валидный JSON: {len(valid)}/{len(results)}")
    if latencies:
        p90 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))]
        print(
            f"Латентность vision, с: медиана {statistics.median(latencies):.1f}, "
            f"p90 {p90:.1f}, max {latencies[-1]:.1f}"
        )
    if valid:
        tokens = statistics.mean(r.tokens_in + r.tokens_out for r in valid)
        print(f"Средние токены на фото: {tokens:.0f}")
        low_conf = sum(1 for r in valid if (r.min_confidence or 1.0) < 0.7)
        print(f"Фото с заданиями confidence < 0.7: {low_conf}/{len(valid)}")
