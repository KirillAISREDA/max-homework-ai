"""Метрики стенда: точность строк, вердикты против эталона, расхождения двух расшифровок.

Главная метрика — ложные «ошибки»: верное задание ребёнка названо неверным.
"""

from dataclasses import dataclass, field
from typing import Literal

from hwcheck.bench.golden import GoldenTask

PredictedVerdict = Literal["correct", "wrong", "uncertain"]
# задание сопоставляется с эталоном по строкам, если доля символьных ошибок не больше этого
MATCH_MAX_CER = 0.5

_SIGNS = str.maketrans({"·": "*", "×": "*", "∙": "*", "−": "-", "–": "-", "—": "-", "÷": ":"})


def normalize_line(line: str) -> str:
    """Без пробелов и регистра, одинаковое написание знаков: сравниваем содержание строки."""
    text = line.translate(_SIGNS).lower().replace(",", ".").replace("х", "x")
    return "".join(text.split())


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


@dataclass
class LineScore:
    truth_lines: int = 0
    exact: int = 0
    char_errors: int = 0
    truth_chars: int = 0

    @property
    def exact_rate(self) -> float:
        return self.exact / self.truth_lines if self.truth_lines else 0.0

    @property
    def cer(self) -> float:
        return self.char_errors / self.truth_chars if self.truth_chars else 0.0

    def add(self, other: "LineScore") -> None:
        self.truth_lines += other.truth_lines
        self.exact += other.exact
        self.char_errors += other.char_errors
        self.truth_chars += other.truth_chars


def _best_match(target: str, candidates: list[str], used: set[int]) -> tuple[int | None, int]:
    best, best_distance = None, len(target)
    for i, candidate in enumerate(candidates):
        if i in used:
            continue
        distance = levenshtein(target, candidate)
        if best is None or distance < best_distance:
            best, best_distance = i, distance
    return best, best_distance


def score_lines(truth: list[str], predicted: list[str]) -> LineScore:
    """Порядок строк не важен: у разных моделей колонки примеров идут в разном порядке."""
    candidates = [normalize_line(p) for p in predicted]
    used: set[int] = set()
    score = LineScore()
    for line in truth:
        target = normalize_line(line)
        index, distance = _best_match(target, candidates, used)
        if index is not None:
            used.add(index)
        score.truth_lines += 1
        score.truth_chars += len(target)
        score.char_errors += min(distance, len(target))
        score.exact += int(index is not None and distance == 0)
    return score


@dataclass
class PredictedTask:
    number: int
    number_on_page: bool
    lines: list[str]
    verdict: PredictedVerdict


def match_tasks(
    truth: list[GoldenTask], predicted: list[PredictedTask]
) -> list[tuple[GoldenTask, PredictedTask | None]]:
    """Номер со страницы у обоих — сопоставление по номеру; иначе — по самым похожим строкам."""
    free = list(range(len(predicted)))
    pairs: list[tuple[GoldenTask, PredictedTask | None]] = []
    for task in truth:
        chosen = next(
            (
                i
                for i in free
                if task.number_on_page
                and predicted[i].number_on_page
                and predicted[i].number == task.number
            ),
            None,
        )
        if chosen is None:
            scored = [(score_lines(task.lines, predicted[i].lines).cer, i) for i in free]
            scored = [(cer, i) for cer, i in scored if cer <= MATCH_MAX_CER]
            chosen = min(scored)[1] if scored else None
        if chosen is not None:
            free.remove(chosen)
        pairs.append((task, predicted[chosen] if chosen is not None else None))
    return pairs


@dataclass
class VerdictTally:
    correct_ok: int = 0  # верно → «верно»
    caught: int = 0  # ошибка → «ошибка»
    false_error: int = 0  # верно → «ошибка» (худший сбой)
    missed_error: int = 0  # ошибка → «верно»
    uncertain_on_correct: int = 0
    uncertain_on_wrong: int = 0
    not_found: int = 0  # задание эталона бот не увидел

    @property
    def truth_correct(self) -> int:
        return self.correct_ok + self.false_error + self.uncertain_on_correct

    @property
    def truth_wrong(self) -> int:
        return self.caught + self.missed_error + self.uncertain_on_wrong

    @property
    def found(self) -> int:
        return self.truth_correct + self.truth_wrong

    def add(self, other: "VerdictTally") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


def tally_verdicts(pairs: list[tuple[GoldenTask, PredictedTask | None]]) -> VerdictTally:
    tally = VerdictTally()
    for truth, predicted in pairs:
        if predicted is None:
            tally.not_found += 1
        elif truth.verdict == "correct":
            if predicted.verdict == "correct":
                tally.correct_ok += 1
            elif predicted.verdict == "wrong":
                tally.false_error += 1
            else:
                tally.uncertain_on_correct += 1
        elif predicted.verdict == "wrong":
            tally.caught += 1
        elif predicted.verdict == "correct":
            tally.missed_error += 1
        else:
            tally.uncertain_on_wrong += 1
    return tally


@dataclass
class DisagreementScore:
    """Две расшифровки: попадают ли ошибки распознавания в места, где модели разошлись."""

    lines: int = 0
    errors: int = 0  # строки, где расшифровка A не совпала с эталоном
    flagged: int = 0  # строки, где A и B разошлись — там бот задал бы вопрос
    errors_flagged: int = 0
    per_case_flagged: list[int] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.errors_flagged / self.errors if self.errors else 0.0

    @property
    def precision(self) -> float:
        return self.errors_flagged / self.flagged if self.flagged else 0.0

    def add(self, other: "DisagreementScore") -> None:
        self.lines += other.lines
        self.errors += other.errors
        self.flagged += other.flagged
        self.errors_flagged += other.errors_flagged
        self.per_case_flagged.append(other.flagged)


def disagreement(truth: list[str], run_a: list[str], run_b: list[str]) -> DisagreementScore:
    a_lines = [normalize_line(x) for x in run_a]
    b_lines = [normalize_line(x) for x in run_b]
    used_a: set[int] = set()
    used_b: set[int] = set()
    score = DisagreementScore()
    for line in truth:
        target = normalize_line(line)
        ia, _ = _best_match(target, a_lines, used_a)
        ib, _ = _best_match(target, b_lines, used_b)
        a = a_lines[ia] if ia is not None else ""
        b = b_lines[ib] if ib is not None else ""
        if ia is not None:
            used_a.add(ia)
        if ib is not None:
            used_b.add(ib)
        error = a != target
        flagged = a != b
        score.lines += 1
        score.errors += int(error)
        score.flagged += int(flagged)
        score.errors_flagged += int(error and flagged)
    return score
