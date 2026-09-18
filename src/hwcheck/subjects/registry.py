"""Реестр предметных модулей: код предмета из профиля → модуль. Новый предмет — строка здесь."""

from __future__ import annotations

from dataclasses import dataclass

from hwcheck.bot.check import CheckModels
from hwcheck.ocr_client import OcrClient
from hwcheck.pipeline.solver import FileCache
from hwcheck.pipeline.vision import VisionAndChatClient
from hwcheck.subjects.base import KnowledgeBase, SubjectModule
from hwcheck.subjects.math.module import MathModule
from hwcheck.subjects.russian.gaps import Dictionary
from hwcheck.subjects.russian.module import RussianModule


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
