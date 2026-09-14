"""Эталонная разметка (bench/golden/*.json) и поиск фото по sha256 — сами фото в git не хранятся."""

import hashlib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class GoldenPhoto(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["notebook", "textbook", "empty"]


class GoldenTask(BaseModel):
    number: int
    number_on_page: bool = True
    lines: list[str]
    answer: str | None = None
    verdict: Literal["correct", "wrong"]
    error_lines: list[int] = []
    unsure: list[str] = []

    @model_validator(mode="after")
    def _error_lines_match_verdict(self) -> Self:
        if any(not 1 <= n <= len(self.lines) for n in self.error_lines):
            raise ValueError(f"error_lines вне 1..{len(self.lines)}: {self.error_lines}")
        if self.verdict == "correct" and self.error_lines:
            raise ValueError("у верного задания не может быть error_lines")
        return self


class GoldenTextbookTask(BaseModel):
    number: int
    condition: str


class GoldenCase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schema")
    id: str
    source: str = ""
    photos: list[GoldenPhoto] = Field(min_length=1, max_length=4)
    notebook_tasks: list[GoldenTask] = []
    textbook_tasks: list[GoldenTextbookTask] = []
    notes: str = ""


def load_cases(directory: Path) -> list[GoldenCase]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(GoldenCase.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
    return sorted(cases, key=lambda c: c.id)


def index_photos(roots: list[Path]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in PHOTO_SUFFIXES:
                index.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), path)
    return index


def missing_photos(cases: list[GoldenCase], index: dict[str, Path]) -> list[tuple[str, str]]:
    return [(c.id, p.sha256) for c in cases for p in c.photos if p.sha256 not in index]
