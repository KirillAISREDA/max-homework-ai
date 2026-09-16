"""Спайк: три пути OCR латиницы на школьных тетрадях; метрики как в исследовании 13.09.

Пути:
  (б) TrOCR-base-handwritten по кропам слов из разметки (`spikes/ocr_en/gold.json`).
  (в) GigaChat-2-Max и GigaChat-3-Ultra — две расшифровки «дословно», через
      `hwcheck.bench.client.BenchClient` (кэш, retry на 429, лимит вызовов) поверх
      `hwcheck.llm.gigachat_client.GigaChatClient`. Расхождение двух расшифровок — сигнал
      «здесь может быть ошибка ученика».
  (а) ReadingPipeline с русскими весами — не измеряется в этом прогоне, см. README/отчёт:
      контейнер строит параллельный спайк (Task 1); полноценная установка на Windows
      (клон репозитория, веса, обходы openvino/ctcdecode) — не «быстрый pip install».
      `align_by_iou` оставлена как заготовка на потом.

Запуск (см. README.md):
  uv run --project /d/Dev/mha-spike-ocr-en --with transformers --with torch --with rapidfuzz \
      --with pillow --with datasets python spikes/ocr_en/compare.py
"""

import argparse
import asyncio
import difflib
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image
from rapidfuzz.distance import Levenshtein

GOLD_PATH = Path("spikes/ocr_en/gold.json")
IMAGES_DIR = Path("spikes/ocr_en/images")
RESULTS_DIR = Path("spikes/ocr_en/results")

TRANSCRIBE_PROMPT = (
    "На фото — рукописный текст ученика на английском языке (тетрадь или письменная работа). "
    "Перепиши его дословно, буква в букву, сохраняя все орфографические ошибки ученика. "
    "Не исправляй и не улучшай текст, не пропускай зачёркнутое. "
    "Ответь только текстом транскрипции, без заголовков и пояснений."
)

_TOKEN_RE = re.compile(r"[A-Za-z']+")


def load_gold() -> list[dict]:
    return json.loads(GOLD_PATH.read_text(encoding="utf-8"))


def cer(predicted: str, expected: str) -> float:
    return Levenshtein.distance(predicted, expected) / max(1, len(expected))


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def align_words(gold_texts: list[str], hyp_tokens: list[str]) -> list[str]:
    """Выравнивание слов разметки с токенами гипотезы (SequenceMatcher по нижнему регистру).

    Гипотеза — сплошной текст (GigaChat транскрибирует лист целиком, а не по словам), поэтому
    поштучное сравнение требует выравнивания. Для «выпавших» слов (нет пары в гипотезе)
    возвращается "" — это честно считается полным промахом в cer()/exact-match.
    """
    a = [w.lower() for w in gold_texts]
    b = [t.lower() for t in hyp_tokens]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    result: list[str] = [""] * len(gold_texts)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                result[i1 + k] = hyp_tokens[j1 + k]
        elif tag == "replace":
            hyp_len = j2 - j1
            for k in range(i2 - i1):
                if k < hyp_len:
                    result[i1 + k] = hyp_tokens[j1 + k]
        # "delete" -> слово разметки выпало из гипотезы, оставляем ""
        # "insert" -> лишние слова гипотезы вне поштучного сравнения (не влияют на CER по словам)
    return result


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


def sheet_crop_box(sheet: dict) -> tuple[int, int, int, int]:
    """Прямоугольник, накрывающий все размеченные слова листа (+ отступ) — тот же кусок фото,
    что видит TrOCR по кропам, чтобы пути (б) и (в) сравнивались на одном материале."""
    pad = 25
    xs0 = [w["box"][0] for w in sheet["words"]]
    ys0 = [w["box"][1] for w in sheet["words"]]
    xs1 = [w["box"][2] for w in sheet["words"]]
    ys1 = [w["box"][3] for w in sheet["words"]]
    return (max(0, min(xs0) - pad), max(0, min(ys0) - pad), max(xs1) + pad, max(ys1) + pad)


@dataclass
class PathMetrics:
    name: str
    total: int = 0
    exact: int = 0
    cers: list[float] = field(default_factory=list)
    error_total: int = 0
    error_preserved: int = 0
    error_corrected: int = 0
    seconds: float = 0.0
    sheets: int = 0


def score(
    name: str,
    predicted: dict[str, list[str]],
    gold: list[dict],
    seconds: float = 0.0,
) -> PathMetrics:
    """predicted: image -> слова в порядке разметки. Считаем CER по словам, точность,
    и отдельно судьбу слов, отмеченных как ошибка ученика (word["error"])."""
    metrics = PathMetrics(name=name, seconds=seconds, sheets=len(gold))
    for sheet in gold:
        for word, guess in zip(sheet["words"], predicted[sheet["image"]], strict=True):
            expected = word["text"].lower()
            got = guess.lower()
            metrics.total += 1
            metrics.exact += got == expected
            metrics.cers.append(cer(got, expected))
            if word.get("error"):
                metrics.error_total += 1
                if got == expected:
                    metrics.error_preserved += 1
                elif word.get("correct") and got == str(word["correct"]).lower():
                    metrics.error_corrected += 1
    return metrics


def render(m: PathMetrics) -> str:
    cer_avg = sum(m.cers) / len(m.cers) if m.cers else 0.0
    line = (
        f"{m.name}: точных {m.exact}/{m.total} ({m.exact / max(1, m.total):.0%}), CER {cer_avg:.2f}"
    )
    if m.error_total:
        preserved = m.error_preserved / m.error_total
        corrected = m.error_corrected / m.error_total
        line += (
            f"; ошибок ученика {m.error_total}: сохранено {preserved:.0%}, "
            f"исправлено {corrected:.0%}, скрыто {1 - preserved:.0%}"
        )
    if m.seconds and m.sheets:
        line += f"; время {m.seconds:.0f} с ({m.seconds / m.sheets:.1f} с/лист)"
    return line


def disagreement_signal(
    gold: list[dict], a: dict[str, list[str]], b: dict[str, list[str]]
) -> tuple[int, int, int]:
    """Расхождение двух расшифровок как сигнал «здесь может быть ошибка ученика».

    Возвращает (flagged, error_total, error_flagged): всего расхождений, всего размеченных
    ошибок, и сколько из них попали в расхождение (полнота = error_flagged / error_total,
    точность = error_flagged / flagged).
    """
    flagged = error_total = error_flagged = 0
    for sheet in gold:
        guesses_a = a[sheet["image"]]
        guesses_b = b[sheet["image"]]
        for word, ga, gb in zip(sheet["words"], guesses_a, guesses_b, strict=True):
            disagree = ga.lower() != gb.lower()
            flagged += disagree
            if word.get("error"):
                error_total += 1
                error_flagged += disagree
    return flagged, error_total, error_flagged


async def gigachat_transcribe(
    gold: list[dict], model: str, cache_dir: Path, max_calls: int
) -> tuple[dict[str, list[str]], object]:
    from hwcheck.bench.client import BenchClient
    from hwcheck.config import Settings
    from hwcheck.llm.gigachat_client import GigaChatClient
    from hwcheck.pipeline.normalize import normalize_image

    predicted: dict[str, list[str]] = {}
    settings = Settings()
    async with GigaChatClient(settings) as llm:
        client = BenchClient(llm, cache_dir, max_calls=max_calls)
        for sheet in gold:
            image = Image.open(IMAGES_DIR / sheet["image"]).convert("RGB")
            crop = image.crop(sheet_crop_box(sheet))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=92)
            data = normalize_image(buf.getvalue())
            print(f"[{model}] {sheet['image']} ...", flush=True)
            result = await client.analyze_image(
                data, prompt=TRANSCRIBE_PROMPT, model=model, filename="sheet.jpg"
            )
            tokens = tokenize(result.content)
            gold_texts = [w["text"] for w in sheet["words"]]
            predicted[sheet["image"]] = align_words(gold_texts, tokens)
        return predicted, client.stats


def align_by_iou(pred_words: list[dict], gold_words: list[dict]) -> list[str]:
    """Выравнивание слов ReadingPipeline с разметкой по пересечению боксов (IoU > 0.5).

    Не используется в этом прогоне — путь (а) не измерялся в спайке Task 3 (см. отчёт
    docs/research/2026-09-17-en-handwriting-ocr.md, §caveats): контейнер ReadingPipeline
    строит параллельный спайк (Task 1); полноценная установка на Windows (клон репозитория,
    веса ~165 МБ, обходы для openvino/ctcdecode) — не «быстрый pip install» из брифа.
    Оставлена как заготовка на момент, когда контейнер станет доступен.
    """

    def iou(a: list[int], b: list[int]) -> float:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
        area_a = max(0, ax1 - ax0) * max(0, ay1 - ay0)
        area_b = max(0, bx1 - bx0) * max(0, by1 - by0)
        union = area_a + area_b - inter
        return inter / union if union else 0.0

    result: list[str] = []
    for gold_word in gold_words:
        best_text, best_iou = "", 0.5
        for pred_word in pred_words:
            score_iou = iou(pred_word["box"], gold_word["box"])
            if score_iou > best_iou:
                best_text, best_iou = pred_word["text"], score_iou
        result.append(best_text)
    return result


def run_trocr(gold: list[dict]) -> PathMetrics:
    started = time.monotonic()
    predicted = {
        sheet["image"]: trocr_words(
            Image.open(IMAGES_DIR / sheet["image"]).convert("RGB"),
            [w["box"] for w in sheet["words"]],
        )
        for sheet in gold
    }
    elapsed = time.monotonic() - started
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "trocr.json").write_text(
        json.dumps(predicted, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metrics = score("TrOCR-base-handwritten (кропы по разметке)", predicted, gold, elapsed)
    print(render(metrics))
    return metrics


def run_gigachat(gold: list[dict], max_calls: int) -> None:
    cache_dir = Path("spikes/ocr_en/.cache/gigachat")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    max_predicted, max_stats = asyncio.run(
        gigachat_transcribe(gold, "GigaChat-2-Max", cache_dir, max_calls)
    )
    elapsed_max = time.monotonic() - started
    (RESULTS_DIR / "gigachat_2max.json").write_text(
        json.dumps(max_predicted, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    m_max = score("GigaChat-2-Max (дословно)", max_predicted, gold, elapsed_max)
    print(render(m_max))
    print(
        f"  вызовы: свежих {max_stats.fresh_calls}, из кэша {max_stats.cached_calls}, "
        f"429: {max_stats.rate_limited}, токенов {max_stats.tokens}"
    )

    started = time.monotonic()
    ultra_predicted, ultra_stats = asyncio.run(
        gigachat_transcribe(gold, "GigaChat-3-Ultra", cache_dir, max_calls)
    )
    elapsed_ultra = time.monotonic() - started
    (RESULTS_DIR / "gigachat_3ultra.json").write_text(
        json.dumps(ultra_predicted, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    m_ultra = score("GigaChat-3-Ultra (дословно)", ultra_predicted, gold, elapsed_ultra)
    print(render(m_ultra))
    print(
        f"  вызовы: свежих {ultra_stats.fresh_calls}, из кэша {ultra_stats.cached_calls}, "
        f"429: {ultra_stats.rate_limited}, токенов {ultra_stats.tokens}"
    )

    flagged, error_total, error_flagged = disagreement_signal(gold, max_predicted, ultra_predicted)
    precision = error_flagged / flagged if flagged else 0.0
    recall = error_flagged / error_total if error_total else 0.0
    total_words = sum(len(s["words"]) for s in gold)
    print(
        f"Расхождение 2-Max/3-Ultra: {flagged} расхождений на {total_words} слов, "
        f"полнота {recall:.0%} ({error_flagged}/{error_total}), точность {precision:.0%}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-trocr", action="store_true", help="Пропустить путь (б)")
    parser.add_argument("--skip-gigachat", action="store_true", help="Пропустить путь (в)")
    parser.add_argument(
        "--max-calls", type=int, default=45, help="Лимит свежих вызовов GigaChat на модель"
    )
    args = parser.parse_args()

    gold = load_gold()
    print(f"Листов: {len(gold)}, слов: {sum(len(s['words']) for s in gold)}")

    if not args.skip_trocr:
        run_trocr(gold)
    if not args.skip_gigachat:
        run_gigachat(gold, args.max_calls)

    # (а) ReadingPipeline: не измерялось в этом прогоне — см. docstring и align_by_iou выше.


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    main()
