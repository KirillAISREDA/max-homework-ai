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
    extra_lines: int = 0  # строки модели без пары в эталоне (выдуманные или раздвоенные)

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
        self.extra_lines += other.extra_lines


def _hungarian(cost: list[list[int]]) -> list[int]:
    """Оптимальное назначение для квадратной матрицы: row → col с минимальной суммой.

    Жадное сопоставление на однотипных примерах («NNN + NNN = NNN») занимает строку, нужную
    другой строке эталона, и завышает ошибки (ревью). Потенциалы, O(n³), n — строк на кейс.
    """
    n = len(cost)
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    owner = [0] * (n + 1)  # owner[col] = row (1-based)
    way = [0] * (n + 1)
    for row in range(1, n + 1):
        owner[0] = row
        col0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[col0] = True
            row0 = owner[col0]
            delta = inf
            col1 = 0
            for col in range(1, n + 1):
                if used[col]:
                    continue
                current = cost[row0 - 1][col - 1] - u[row0] - v[col]
                if current < minv[col]:
                    minv[col] = current
                    way[col] = col0
                if minv[col] < delta:
                    delta = minv[col]
                    col1 = col
            for col in range(n + 1):
                if used[col]:
                    u[owner[col]] += delta
                    v[col] -= delta
                else:
                    minv[col] -= delta
            col0 = col1
            if owner[col0] == 0:
                break
        while col0:
            col1 = way[col0]
            owner[col0] = owner[col1]
            col0 = col1
    assignment = [0] * n
    for col in range(1, n + 1):
        assignment[owner[col] - 1] = col - 1
    return assignment


def _assign_lines(targets: list[str], candidates: list[str]) -> list[tuple[int | None, int]]:
    """Для каждой строки эталона — (индекс строки модели или None, расстояние не больше длины)."""
    size = max(len(targets), len(candidates))
    if size == 0:
        return []
    cost = [
        [
            (
                min(levenshtein(targets[i], candidates[j]), len(targets[i]))
                if j < len(candidates)
                else len(targets[i])
            )
            if i < len(targets)
            else 0
            for j in range(size)
        ]
        for i in range(size)
    ]
    assignment = _hungarian(cost)
    return [
        (assignment[i] if assignment[i] < len(candidates) else None, cost[i][assignment[i]])
        for i in range(len(targets))
    ]


def score_lines(truth: list[str], predicted: list[str]) -> LineScore:
    """Порядок строк не важен: у разных моделей колонки примеров идут в разном порядке."""
    targets = [normalize_line(line) for line in truth]
    candidates = [normalize_line(p) for p in predicted]
    score = LineScore()
    matched = 0
    for target, (index, distance) in zip(targets, _assign_lines(targets, candidates), strict=True):
        score.truth_lines += 1
        score.truth_chars += len(target)
        score.char_errors += distance
        score.exact += int(index is not None and distance == 0)
        matched += int(index is not None)
    score.extra_lines = len(candidates) - matched
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
        same_number = [
            i
            for i in free
            if task.number_on_page
            and predicted[i].number_on_page
            and predicted[i].number == task.number
        ]
        chosen: int | None
        if same_number:
            # номер может повториться (задвоение при распознавании) — среди них по содержимому
            chosen = min(same_number, key=lambda i: score_lines(task.lines, predicted[i].lines).cer)
        else:
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
    targets = [normalize_line(line) for line in truth]
    a_lines = [normalize_line(x) for x in run_a]
    b_lines = [normalize_line(x) for x in run_b]
    score = DisagreementScore()
    pairs = zip(
        targets, _assign_lines(targets, a_lines), _assign_lines(targets, b_lines), strict=True
    )
    for target, (ia, _), (ib, _) in pairs:
        a = a_lines[ia] if ia is not None else ""
        b = b_lines[ib] if ib is not None else ""
        error = a != target
        flagged = a != b
        score.lines += 1
        score.errors += int(error)
        score.flagged += int(flagged)
        score.errors_flagged += int(error and flagged)
    return score
