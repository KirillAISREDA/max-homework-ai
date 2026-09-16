"""Строит spikes/ocr_en/gold.json из COCO-разметки ai-forever/school_notebooks_EN.

20 листов = все 10 test + все 10 val (train держим в стороне: как в исследовании 13.09,
val/test — контроль, не обучающая выборка). На лист берём первые ~50 слов category
"pupil_text" в порядке чтения (по группам строк сверху вниз, внутри строки слева направо) —
бюджет CPU-инференса TrOCR ограничен по времени (см. task-3-brief.md).

Кандидаты в ошибки ученика — слова вне словаря pyspellchecker (эвристика уровня RU-спайка
13.09, там — pymorphy3). Первый прогон даёт сырые кандидаты с полем "error"/"correct" —
их нужно свериться визуально по кропам (контрольный лист) и вручную поправить gold.json:
в этом спайке из 31 кандидата подтверждено 14 (см. отчёт, §caveats — типы шума разметки).

Запуск: uv run --project /d/Dev/mha-spike-ocr-en --with huggingface_hub \
    --with pyspellchecker python spikes/ocr_en/build_gold.py
"""

import json
import re
import sys
import zipfile
from pathlib import Path

from huggingface_hub import hf_hub_download
from spellchecker import SpellChecker

REPO = "ai-forever/school_notebooks_EN"
OUT = Path("spikes/ocr_en/gold.json")
IMAGES_DIR = Path("spikes/ocr_en/images")
WORDS_PER_SHEET = 50
PUPIL_TEXT = 0
PAD = 6  # px — полигон плотно облегает букву, паддинг спасает выносные элементы

SHEETS = {
    "test": [
        "en_hw2022_01_11.jpg",
        "en_hw2022_01_3.jpg",
        "en_hw2022_02_19.jpg",
        "en_hw2022_03_IMG_20211001_134547.jpg",
        "en_hw2022_07_IMG_20211001_135103.jpg",
        "en_hw2022_08_IMG_20211001_134113.jpg",
        "en_hw2022_08_IMG_20211001_134309.jpg",
        "en_hw2022_10_336.jpg",
        "en_hw2022_10_338.jpg",
        "en_hw2022_14_167.jpg",
    ],
    "val": [
        "en_hw2022_01_1.jpg",
        "en_hw2022_03_IMG_20211001_134505.jpg",
        "en_hw2022_06_IMG_20211001_135140.jpg",
        "en_hw2022_06_IMG_20211001_135217.jpg",
        "en_hw2022_06_IMG_20211001_135500.jpg",
        "en_hw2022_08_IMG_20211001_134030.jpg",
        "en_hw2022_11_372.jpg",
        "en_hw2022_11_373.jpg",
        "en_hw2022_11_IMG_20211001_135115.jpg",
        "en_hw2022_14_154.jpg",
    ],
}

# spellchecker не знает частые в этом датасете корректные слова/имена — не считаем их ошибками
ALLOWLIST = {
    "ielts",
    "internet",
    "wifi",
    "smartphone",
    "online",
    "app",
    "apps",
    "email",
    "facebook",
    "instagram",
    "whatsapp",
    "covid",
    "co",
}

_WORD_RE = re.compile(r"[A-Za-z']+")


def _clean(text: str) -> str | None:
    match = _WORD_RE.fullmatch(text.strip())
    if match is None:
        return None
    return match.group(0)


def _bbox(segmentation: list[list[float]], width: int, height: int) -> list[int]:
    xs = segmentation[0][0::2]
    ys = segmentation[0][1::2]
    x0 = max(0, int(min(xs)) - PAD)
    y0 = max(0, int(min(ys)) - PAD)
    x1 = min(width, int(max(xs)) + PAD)
    y1 = min(height, int(max(ys)) + PAD)
    return [x0, y0, x1, y1]


def _reading_order(anns: list[dict]) -> list[dict]:
    groups: dict[int, list[dict]] = {}
    for ann in anns:
        groups.setdefault(ann["group_id"], []).append(ann)

    def center(a: dict) -> tuple[float, float]:
        xs = a["segmentation"][0][0::2]
        ys = a["segmentation"][0][1::2]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    group_order = sorted(
        groups.items(),
        key=lambda kv: sum(center(a)[1] for a in kv[1]) / len(kv[1]),
    )
    ordered = []
    for _, members in group_order:
        ordered.extend(sorted(members, key=lambda a: center(a)[0]))
    return ordered


def _extract_images(wanted: list[str]) -> None:
    """Достаёт только нужные 20 листов из images.zip (356 МБ на весь датасет) — не хранит
    в репозитории (см. spikes/ocr_en/.gitignore), скачивает заново при каждом запуске."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    missing = [name for name in wanted if not (IMAGES_DIR / name).exists()]
    if not missing:
        return
    zip_path = Path(hf_hub_download(REPO, "images.zip", repo_type="dataset"))
    with zipfile.ZipFile(zip_path) as archive:
        by_basename = {
            name.split("/")[-1]: name
            for name in archive.namelist()
            if name.startswith("images/") and name.endswith(".jpg")
        }
        for name in missing:
            (IMAGES_DIR / name).write_bytes(archive.read(by_basename[name]))


def main() -> None:
    spell = SpellChecker()
    gold = []
    stats = {"words": 0, "errors": 0}
    _extract_images([name for files in SHEETS.values() for name in files])
    for split, files in SHEETS.items():
        annot_path = Path(hf_hub_download(REPO, f"annotations_{split}.json", repo_type="dataset"))
        data = json.loads(annot_path.read_text(encoding="utf-8"))
        images = {im["id"]: im for im in data["images"]}
        by_image: dict[int, list[dict]] = {}
        for ann in data["annotations"]:
            if ann["category_id"] == PUPIL_TEXT:
                by_image.setdefault(ann["image_id"], []).append(ann)
        for file_name in files:
            image_id = next(i for i, im in images.items() if im["file_name"] == file_name)
            image_info = images[image_id]
            ordered = _reading_order(by_image.get(image_id, []))[:WORDS_PER_SHEET]
            words = []
            for ann in ordered:
                text = ann["attributes"]["translation"]
                box = _bbox(ann["segmentation"], image_info["width"], image_info["height"])
                cleaned = _clean(text)
                is_error = False
                correct = None
                if cleaned and len(cleaned) >= 3 and cleaned[0].islower():
                    lower = cleaned.lower()
                    if lower not in ALLOWLIST and spell.unknown([lower]):
                        is_error = True
                        correct = spell.correction(lower)
                words.append({"text": text, "box": box, "error": is_error, "correct": correct})
                stats["words"] += 1
                stats["errors"] += is_error
            gold.append({"image": file_name, "split": split, "words": words})
    OUT.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"листов: {len(gold)}, слов: {stats['words']}, кандидатов в ошибки: {stats['errors']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    main()
