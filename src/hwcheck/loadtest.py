"""Нагрузочный тест бота: поток сообщений виртуальных семей и замеры по шагам нагрузки.

Критерий приёмки MVP конкурса — отчёт с нагрузкой до 10 запросов в секунду, токенами в минуту и
стресс-тестом. У бота нет HTTP-входа (long polling), поэтому нагрузка подаётся туда же, куда её
подаёт раннер: в диспетчер апдейтов. Модели, очередь, состояние диалога и журнал — настоящие;
подменён только мессенджер: MAX нагрузочно тестировать нельзя.

Виртуальная семья ведёт себя как живая: присылает домашку, ждёт ответа, нажимает «Разобрать»,
отвечает тьютору и присылает следующую домашку. Пока бот ей не ответил, она молчит — следующее
сообщение потока приходит от другой семьи. Фото — страницы открытого датасета, не работы детей.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
import time
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hwcheck.bot.dispatch import DispatchLimits, UpdateDispatcher
from hwcheck.bot.handlers import RETRY
from hwcheck.bot.max_api import Buttons
from hwcheck.bot.models import MaxUpdate
from hwcheck.events import read_events

logger = logging.getLogger(__name__)

Handler = Callable[[MaxUpdate], Coroutine[Any, Any, None]]
FIRST_CHAT_ID = 9_000_000_000  # вне диапазона настоящих чатов: записи теста видны в журнале
DRAIN_TIMEOUT_S = 300.0  # сколько ждать ответов после конца шага, прежде чем считать их потерей
KINDS = ("photo", "button", "text")


@dataclass(frozen=True)
class Step:
    rps: float  # сообщений в секунду
    seconds: float


def parse_steps(text: str) -> list[Step]:
    """«1:60,5:60» — шаги «сообщений в секунду : секунд»."""
    steps = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        try:
            rps, seconds = (float(value) for value in part.split(":"))
        except ValueError:
            raise ValueError(f"шаг «{part}»: нужен вид «сообщений в секунду:секунд»") from None
        if rps <= 0 or seconds <= 0:
            raise ValueError(f"шаг «{part}»: нагрузка и длительность больше нуля")
        steps.append(Step(rps, seconds))
    if not steps:
        raise ValueError("шаг не задан: нужен вид «сообщений в секунду:секунд»")
    return steps


class LoadMax:
    """Мессенджер-заглушка: отдаёт фото, запоминает ответы бота."""

    def __init__(self, photos: Sequence[bytes]) -> None:
        if not photos:
            raise ValueError("нужно хотя бы одно фото страницы")
        self._photos = list(photos)
        self._buttons: dict[int, Buttons] = {}
        self._failed: set[int] = set()
        self.replies = 0

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Buttons | None = None,
        image_token: str | None = None,
    ) -> None:
        self.replies += 1
        self._buttons[chat_id] = buttons or []
        if text == RETRY:
            self._failed.add(chat_id)  # «попробуй ещё раз» — бот не справился

    async def send_to_user(
        self, user_id: int, text: str, *, buttons: Buttons | None = None
    ) -> None:
        self.replies += 1

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
        return None

    async def upload_image(self, image: bytes) -> str:
        return "load-image"

    async def download(self, url: str) -> bytes:
        index = int(url.rsplit("/", 1)[-1])
        return self._photos[index % len(self._photos)]

    def last_buttons(self, chat_id: int) -> list[str]:
        """Payload кнопок под последним сообщением бота в чате."""
        rows = self._buttons.get(chat_id, [])
        return [str(button.get("payload", "")) for row in rows for button in row]

    def take_failure(self, chat_id: int) -> bool:
        failed = chat_id in self._failed
        self._failed.discard(chat_id)
        return failed


@dataclass
class _Family:
    chat_id: int
    answers_left: int = 0  # ответов тьютору до следующей домашки
    tutoring: bool = False
    photos_sent: int = 0


class VirtualUsers:
    """Семьи, пишущие боту. Занятая (ждёт ответа) молчит; свободных нет — приходит новая."""

    def __init__(self, load_max: LoadMax, *, tutor_answers: int = 2) -> None:
        self._max = load_max
        self._tutor_answers = tutor_answers
        self._families: dict[int, _Family] = {}
        self._idle: list[int] = []
        self._photo_index = itertools.count()

    @property
    def count(self) -> int:
        return len(self._families)

    def done(self, chat_id: int) -> None:
        """Бот ответил: семья может писать снова."""
        if chat_id in self._families and chat_id not in self._idle:
            self._idle.append(chat_id)

    def next_update(self) -> MaxUpdate:
        if self._idle:
            family = self._families[self._idle.pop(0)]
        else:
            family = _Family(FIRST_CHAT_ID + len(self._families))
            self._families[family.chat_id] = family
        return self._act(family)

    def _act(self, family: _Family) -> MaxUpdate:
        buttons = self._max.last_buttons(family.chat_id)
        press = next((b for b in buttons if b.startswith(("tutor:", "clarify:"))), None)
        if press is not None:
            family.tutoring = press.startswith("tutor:")
            family.answers_left = self._tutor_answers if family.tutoring else 0
            return _callback(family.chat_id, press)
        if family.tutoring and family.answers_left > 0:
            family.answers_left -= 1
            return _message(family.chat_id, text="не знаю")
        family.tutoring = False
        family.photos_sent += 1
        return _message(family.chat_id, photo=f"load://photo/{next(self._photo_index)}")


def _sender(chat_id: int) -> dict[str, Any]:
    return {"user_id": chat_id}


def _message(chat_id: int, *, text: str = "", photo: str | None = None) -> MaxUpdate:
    attachments = [{"type": "image", "payload": {"url": photo}}] if photo else []
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": _sender(chat_id),
                "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
                "body": {"mid": "load", "text": text, "attachments": attachments},
            },
        }
    )


def _callback(chat_id: int, payload: str) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "message_callback",
            "chat_id": chat_id,
            "callback": {"callback_id": "load", "payload": payload, "user": _sender(chat_id)},
        }
    )


def _kind(update: MaxUpdate) -> str:
    if update.callback is not None:
        return "button"
    if update.message is not None and update.message.image_urls:
        return "photo"
    return "text"


@dataclass(frozen=True)
class Sample:
    kind: str
    queued_at: float
    started_at: float
    finished_at: float
    ok: bool

    @property
    def latency(self) -> float:
        """От прихода сообщения до конца обработки: столько ждал человек."""
        return self.finished_at - self.queued_at

    @property
    def queue_wait(self) -> float:
        return self.started_at - self.queued_at


def percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True)
class Latency:
    n: int
    p50: float | None
    p95: float | None
    worst: float | None


@dataclass(frozen=True)
class StepResult:
    step: Step
    sent: int
    completed: int
    failed: int  # бот ответил «попробуй ещё раз» или обработка упала
    unfinished: int  # не дождались ответа за DRAIN_TIMEOUT_S
    wall_s: float  # от начала шага до последнего ответа
    throughput: float  # обработано сообщений в секунду
    latency: dict[str, Latency]
    queue_wait_p95: float | None
    peak_pending: int
    llm_calls: int
    llm_errors: int
    tokens: int
    tokens_per_minute: float
    cost: float
    families: int = 0


def _latency(samples: Iterable[Sample]) -> Latency:
    values = [s.latency for s in samples]
    worst = max(values) if values else None
    return Latency(len(values), percentile(values, 50), percentile(values, 95), worst)


def summarize_step(
    step: Step,
    samples: Sequence[Sample],
    llm_calls: Iterable[dict[str, Any]],
    started: float,
    finished: float,
    *,
    sent: int,
    peak: int,
    families: int = 0,
) -> StepResult:
    """`started`/`finished` — время по часам журнала событий: по нему отбираются вызовы модели."""
    calls = [c for c in llm_calls if started <= float(c.get("ts") or 0) <= finished]
    wall = max(finished - started, 1e-9)
    tokens = sum(int(c.get("tokens_in") or 0) + int(c.get("tokens_out") or 0) for c in calls)
    return StepResult(
        step=step,
        sent=sent,
        completed=len(samples),
        failed=sum(1 for s in samples if not s.ok),
        unfinished=sent - len(samples),
        wall_s=wall,
        throughput=len(samples) / wall,
        latency={kind: _latency(s for s in samples if s.kind == kind) for kind in KINDS},
        queue_wait_p95=percentile([s.queue_wait for s in samples], 95),
        peak_pending=peak,
        llm_calls=len(calls),
        llm_errors=sum(1 for c in calls if c.get("status") == "error"),
        tokens=tokens,
        tokens_per_minute=tokens / wall * 60,
        cost=sum(float(c["cost"]) for c in calls if isinstance(c.get("cost"), int | float)),
        families=families,
    )


@dataclass
class _Run:
    """Один шаг: что отправили, что вернулось."""

    samples: list[Sample] = field(default_factory=list)
    sent: int = 0
    peak: int = 0


async def run_load(
    handler: Handler,
    load_max: LoadMax,
    steps: Sequence[Step],
    *,
    events_path: Path,
    limits: DispatchLimits | None = None,
    users: VirtualUsers | None = None,
    drain_timeout_s: float = DRAIN_TIMEOUT_S,
) -> list[StepResult]:
    users = users or VirtualUsers(load_max)
    queued: dict[int, float] = {}

    def timed_into(run: _Run) -> Handler:
        async def timed(update: MaxUpdate) -> None:
            chat_id = update.effective_chat_id
            assert chat_id is not None
            started, failed = time.monotonic(), False
            try:
                await handler(update)
            except Exception:
                failed = True
                logger.exception("load: update failed")
            failed = load_max.take_failure(chat_id) or failed
            now = time.monotonic()
            run.samples.append(Sample(_kind(update), queued.pop(chat_id), started, now, not failed))
            users.done(chat_id)

        return timed

    results = []
    for step in steps:
        # у шага свой диспетчер: что бот не разобрал за срок, отменяется и считается «без
        # ответа», а не доезжает в замеры следующего шага
        run = _Run()
        dispatcher = UpdateDispatcher(timed_into(run), limits or DispatchLimits())
        began = time.time()
        try:
            await _send(step, dispatcher, users, queued, run)
            await _drain(dispatcher, drain_timeout_s)
        finally:
            await dispatcher.abort()
        calls = [e for e in read_events(events_path) if e.get("type") == "llm_call"]
        results.append(
            summarize_step(
                step,
                list(run.samples),
                calls,
                began,
                time.time(),
                sent=run.sent,
                peak=run.peak,
                families=users.count,
            )  # fmt: skip
        )
        logger.info("load: шаг %g/с — %d из %d", step.rps, len(run.samples), run.sent)
    return results


async def _send(
    step: Step,
    dispatcher: UpdateDispatcher,
    users: VirtualUsers,
    queued: dict[int, float],
    run: _Run,
) -> None:
    """Открытый поток: сообщения приходят по расписанию, не дожидаясь ответов на прежние."""
    start = time.monotonic()
    total = round(step.rps * step.seconds)
    for number in range(total):
        delay = start + number / step.rps - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        update = users.next_update()
        chat_id = update.effective_chat_id
        assert chat_id is not None
        queued[chat_id] = time.monotonic()
        dispatcher.submit(update)
        run.sent += 1
        run.peak = max(run.peak, dispatcher.pending)


async def _drain(dispatcher: UpdateDispatcher, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while dispatcher.pending and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def peak_memory_mb() -> float | None:
    """Пик памяти процесса; на Windows модуля `resource` нет — замера нет."""
    if sys.platform == "win32":
        return None
    import resource

    # Linux отдаёт килобайты
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}".replace(".", ",")


def render_report(
    results: Sequence[StepResult], *, settings_note: str, peak_memory_mb: float | None = None
) -> str:
    head = (
        "| Сообщений в секунду | Длительность, с | Отправлено | Обработано | Сбоев | Без ответа "
        "| Обработано в секунду | Фото: p50 / p95 / макс, с | Кнопка: p50 / p95, с "
        "| Текст: p50 / p95, с | Ожидание в очереди p95, с | Пик очереди | Семей "
        "| Вызовов модели | Ошибок модели | Токенов в минуту | Стоимость, ₽ |"
    )
    columns = head.count("|") - 1
    lines = [f"Настройки: {settings_note}", "", head, "|" + "---|" * columns]
    for r in results:
        photo, button, text = (r.latency[kind] for kind in KINDS)
        cells = [
            f"{r.step.rps:g}",
            f"{r.step.seconds:g}",
            str(r.sent),
            str(r.completed),
            str(r.failed),
            str(r.unfinished),
            f"{r.throughput:.2f}".replace(".", ","),
            f"{_seconds(photo.p50)} / {_seconds(photo.p95)} / {_seconds(photo.worst)}",
            f"{_seconds(button.p50)} / {_seconds(button.p95)}",
            f"{_seconds(text.p50)} / {_seconds(text.p95)}",
            _seconds(r.queue_wait_p95),
            str(r.peak_pending),
            str(r.families),
            str(r.llm_calls),
            str(r.llm_errors),
            f"{r.tokens_per_minute:,.0f}".replace(",", " "),
            f"{r.cost:.0f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if peak_memory_mb is not None:
        lines += ["", f"Пик памяти процесса: {peak_memory_mb:.0f} МБ"]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hwcheck.loadtest",
        description="Нагрузочный тест бота: настоящие модели, мессенджер подменён",
    )
    parser.add_argument("--photos", type=Path, required=True, help="Каталог страниц (jpg, png)")
    parser.add_argument("--steps", default="1:60,2:60,5:60,10:60", help="«в секунду:секунд», …")
    parser.add_argument("--events", type=Path, default=Path("var/loadtest/events.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("var/loadtest/report.md"))
    parser.add_argument("--tutor-answers", type=int, default=2, help="Ответов тьютору на разбор")
    parser.add_argument(
        "--concurrency", type=int, help="Одновременных обработок (по умолчанию — как у бота)"
    )
    parser.add_argument(
        "--drain-timeout", type=float, default=DRAIN_TIMEOUT_S, help="Ждать ответов после шага, с"
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> str:
    # импорт здесь: модуль замеров не должен тянуть бота и клиентов моделей при разборе отчёта
    from hwcheck.bot.fsm import InMemoryStateStore
    from hwcheck.bot.handlers import Bot, models_for
    from hwcheck.config import load_settings
    from hwcheck.events import EventLog
    from hwcheck.llm.journal import JournaledLLM
    from hwcheck.llm.router import make_llm
    from hwcheck.subjects.registry import SubjectDeps

    settings = load_settings()
    # страницы теста — открытый датасет, не работы детей: их читает модель прода. Боту без
    # онбординга согласие неизвестно, и он взял бы отечественную модель — подменяем её
    settings = settings.model_copy(update={"vision_model_domestic": settings.vision_model})
    files = sorted(
        p for p in args.photos.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )
    load_max = LoadMax([path.read_bytes() for path in files])
    # своя среда: записи теста не попадают ни в prod, ни в test журнала бота
    events = EventLog(args.events, "loadtest")
    concurrency = args.concurrency or settings.update_concurrency
    limits = DispatchLimits(concurrency=concurrency, queue_limit=1_000_000)
    async with make_llm(settings) as router:
        llm = JournaledLLM(router, events)
        bot = Bot(
            load_max,  # type: ignore[arg-type]
            llm,
            InMemoryStateStore(),
            events,
            settings,
            # без кэша эталонов: страниц теста мало, а у живых семей домашки разные —
            # с кэшем нагрузка на модель была бы заниженной
            subjects=SubjectDeps(llm, models_for(settings), None),
        )
        results = await run_load(
            bot.handle_update,
            load_max,
            parse_steps(args.steps),
            events_path=args.events,
            limits=limits,
            users=VirtualUsers(load_max, tutor_answers=args.tutor_answers),
            drain_timeout_s=args.drain_timeout,
        )
    note = (
        f"чтение фото {settings.vision_model}, разбор {settings.tutor_model}, эталон "
        f"{settings.solver_model}; одновременных обработок {concurrency}, "
        f"вызовов шлюза {settings.llm_gateway_concurrency}; страниц {len(files)}, кэш эталонов "
        f"выключен; "
        f"ожидание ответов после шага {args.drain_timeout:g} с"
    )
    return render_report(results, settings_note=note, peak_memory_mb=peak_memory_mb())


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    report = asyncio.run(_run_cli(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print(report)


if __name__ == "__main__":
    main()
