"""Реестр предметных модулей: код предмета из профиля → модуль. Новый предмет — строка здесь."""

from __future__ import annotations

from dataclasses import dataclass

from hwcheck.bot.check import CheckModels
from hwcheck.pipeline.solver import FileCache
from hwcheck.pipeline.vision import VisionAndChatClient
from hwcheck.subjects.base import SubjectModule
from hwcheck.subjects.math.module import MathModule


@dataclass(frozen=True)
class SubjectDeps:
    llm: VisionAndChatClient
    models: CheckModels
    cache: FileCache | None


def module_for(code: str, deps: SubjectDeps) -> SubjectModule:
    if code == "math":
        return MathModule(deps.llm, deps.models, deps.cache)
    raise KeyError(f"предметный модуль не реализован: {code}")
