"""Журнал вызовов модели: событие `llm_call` на каждый вызов `chat` / `analyze_image`.

События шагов (`vision_recognized`, `solver_call`…) — суммы по шагу, а счёт от GigaChat и
конкурсный учёт обращений (Положение, Прил. 2 п. 2.2 и п. 5) идут по вызовам: нужны модель,
версия промпта, токены входа и выхода, время ответа и код ошибки — в том числе у упавших.

Обёртка не знает ни шага, ни пользователя: шаг объявляет пайплайн (`llm_step`), пользователя и
trace_id — обработчик апдейта (`events.trace`). Текст промптов и ответов в журнал не пишется —
в них записи из тетради ребёнка (152-ФЗ).
"""

import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal

from hwcheck.events import EventLog, current_user_id
from hwcheck.llm.base import ChatMessage, LLMResult

if TYPE_CHECKING:
    # шаги пайплайна сами импортируют `llm_step` отсюда — в рантайме был бы цикл импорта
    from hwcheck.pipeline.vision import VisionAndChatClient

logger = logging.getLogger(__name__)

UNKNOWN_STEP = "unknown"
CallKind = Literal["chat", "vision"]

_step: ContextVar[tuple[str, str | None]] = ContextVar("llm_step", default=(UNKNOWN_STEP, None))


@contextmanager
def llm_step(step: str, prompt_version: str | None) -> Iterator[None]:
    """Вызовы модели внутри блока пишутся с этим шагом и версией промпта.

    Имя шага — каталог промпта в `prompts/`: по паре «шаг, версия» находится текст промпта.
    """
    token = _step.set((step, prompt_version))
    try:
        yield
    finally:
        _step.reset(token)


class JournaledLLM:
    """LLMClient и VisionClient поверх любого клиента: тот же вызов плюс запись в журнал."""

    def __init__(self, inner: "VisionAndChatClient", events: EventLog) -> None:
        self._inner = inner
        self._events = events

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float = 0.1,
    ) -> LLMResult:
        return await self._journaled(
            "chat", model, lambda: self._inner.chat(messages, model=model, temperature=temperature)
        )

    async def analyze_image(
        self,
        image: bytes,
        *,
        prompt: str,
        model: str,
        filename: str = "image.jpg",
    ) -> LLMResult:
        return await self._journaled(
            "vision",
            model,
            lambda: self._inner.analyze_image(image, prompt=prompt, model=model, filename=filename),
        )

    async def _journaled(
        self, kind: CallKind, model: str, call: Callable[[], Awaitable[LLMResult]]
    ) -> LLMResult:
        # perf_counter: у monotonic на Windows шаг ~16 мс; время меряем сами, а не берём из
        # результата — у упавшего вызова результата нет
        started = time.perf_counter()
        try:
            result = await call()
        except BaseException as exc:
            # BaseException: отмена по SIGTERM посреди запроса — тоже состоявшееся обращение
            self._log(kind, model, started, error=type(exc).__name__)
            raise
        self._log(kind, model, started, tokens_in=result.tokens_in, tokens_out=result.tokens_out)
        return result

    def _log(
        self,
        kind: CallKind,
        model: str,
        started: float,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: str | None = None,
    ) -> None:
        step, prompt_version = _step.get()
        try:
            self._events.log(
                "llm_call",
                user_id=current_user_id(),
                component="llm",
                step=step,
                prompt_version=prompt_version,
                model=model,
                kind=kind,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=round((time.perf_counter() - started) * 1000),
                status="ok" if error is None else "error",
                error=error,
            )
        except Exception as exc:
            # полный диск не должен стоить ребёнку проверки: ответ модели важнее записи о нём
            logger.warning("llm_call не записан в журнал: %s (step=%s)", type(exc).__name__, step)
