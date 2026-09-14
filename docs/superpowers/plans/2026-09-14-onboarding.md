# Онбординг, согласие родителя и профиль ученика — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** первый вход в бота с ролью, классом и предметом, связка «ребёнок ↔ родитель» и согласие родителя до проверки фото, профиль с переходом в следующий класс, уведомления родителю.

**Architecture:** профили, согласия и приглашения — в PostgreSQL (asyncpg, миграции при старте бота); сырой id MAX хранится только шифротекстом Fernet, для поиска — HMAC-хэш. Шаг онбординга выводится из данных, FSM — детерминированный код в `bot/onboarding.py`; `bot/handlers.py` только маршрутизирует. Спецификация большая, поэтому работа идёт этапами: каждый этап даёт рабочий, проверенный и выкатываемый результат, подробный план следующего этапа пишется по коду предыдущего.

**Tech Stack:** Python 3.12, asyncpg, cryptography (Fernet), pydantic-settings, Redis (как сейчас), PostgreSQL 17 в Docker, pytest + pytest-asyncio, ruff, mypy strict, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-14-onboarding-design.md`

## Global Constraints

- Аудитория — только 1–9 классы (`grade BETWEEN 1 AND 9`).
- Сырой id MAX нигде не лежит в открытом виде: поиск — `HMAC-SHA256(ID_HASH_KEY, str(id)).hexdigest()[:16]`, отправка — Fernet(`USER_ID_KEY`) шифротекст в `users.max_user_id_enc`.
- Ключи `USER_ID_KEY`, `ID_HASH_KEY` — в `.env` на VPS **и копия у Кирилла вне VPS**; секреты не печатаются в чат и логи, не попадают в git.
- До согласия фото не скачиваются, не сохраняются в `var/photos` и не уходят в GigaChat.
- PostgreSQL: `postgres:17-alpine`, контейнер `homework-postgres`, том `postgres-data`, без портов наружу, healthcheck `pg_isready`, лимит памяти 256 МБ; бот `depends_on: service_healthy`; ежедневный `pg_dump` в `var/backups`, хранение 7 дней.
- Новые зависимости (`asyncpg`, `cryptography`) — строкой в `docs/components.md`.
- Приглашение: токен `secrets.token_urlsafe(16)`, код 8 знаков из `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, ссылка `https://max.ru/<username>?start=<p|c>_<токен>`, метка ≤ 128 символов, живёт 7 дней, одноразовое.
- VPS общий с другими проектами: не трогать чужие контейнеры, Caddy, firewall; перед рестартом бота — нет событий за 10 минут; Redis не перезапускать; один poller на токен MAX (не запускать `hwcheck bot` локально, пока работает контейнер).
- Фото детей — никогда в git; имён детей в данных нет.
- Код: TDD, `ruff check`, `ruff format`, `mypy` (strict), conventional commits на русском, комментарии и тексты — по-русски в стиле кода.

**Отступление от спецификации (сознательное):** в `invites` хранится `code_hash` (sha256 кода), а не `short_code` открытым текстом — утечка таблицы не даёт погасить чужое приглашение; поведение для пользователя то же.

## Этапы

| Этап | Что даёт | Разделы спецификации | План |
|---|---|---|---|
| 1. Фундамент | HMAC-хэш id, шифр id, каталог предметов и учебный год, приглашения (токен, код, ссылка), PostgreSQL со схемой, миграциями и бэкапами, ключи в prod. Бот для пользователя не меняется | §5, §6, §7 (db, crypto, subjects, invites), §8, §10.1, §13 | ниже, подробно |
| 2. Вход ученика и согласие родителя | роль → класс → предмет (лист ожидания) → ссылка/код родителю → согласие с политикой; вход родителя первым; блокировка фото до согласия; пример проверки; инструкция; `ONBOARDING_REQUIRED`; события воронки | §4.1–4.4, §10.2–10.3, §11, §12 | пишется после этапа 1 |
| 3. Меню и жизненный цикл | `/menu`, `/help`, смена предмета и класса, переход в следующий класс, выпускник, отзыв согласия и удаление данных | §4.5, §4.6, §10.4 | после этапа 2 |
| 4. Уведомления родителю | выбор режима при согласии и в меню, `homeworks`, «сразу», сводка по времени и поясу | §9 | после этапа 3 |
| 5. Выкатка для реальных пользователей | живой чек-лист на двух аккаунтах, вычитка юристом, флаг для всех | §14 | после этапа 4 |

Живой тест на аккаунтах Кирилла и тестера возможен после этапа 2 (за флагом); реальные пользователи — только после этапа 5.

---

# Этап 1. Фундамент

Результат этапа: в prod работают PostgreSQL (схема §6, миграции, ежедневный бэкап) и HMAC-хэши id; в `.env` на VPS есть `ID_HASH_KEY`, `USER_ID_KEY`, `POSTGRES_PASSWORD`; библиотеки каталога предметов, учебного года, шифра и приглашений покрыты тестами. Сценарий бота для пользователя не меняется, кроме однократного сброса открытых разборов (ключи Redis переходят на новый хэш).

Ветка: `feat/onboarding-foundation`, один PR на этап.

## Файлы этапа

| Файл | Ответственность |
|---|---|
| `src/hwcheck/events.py` (изм.) | `set_id_hash_key`, `anonymize` → HMAC, `legacy_anonymize`; `EventLog` узнаёт тестеров по обоим хэшам |
| `src/hwcheck/crypto.py` (нов.) | `UserIdCipher` (Fernet), `UserIdCipherError`, `new_user_id_key` |
| `src/hwcheck/config.py` (изм.) | `id_hash_key`, `user_id_key`, `database_url` |
| `src/hwcheck/cli.py` (изм.) | команда `hwcheck keys` |
| `src/hwcheck/bot/subjects.py` (нов.) | каталог §5, `subjects_for`, `subject_by_code`, `school_year`, `current_grade` |
| `src/hwcheck/bot/invites.py` (нов.) | `new_invite`, `NewInvite`, `start_payload`, `parse_start_payload`, `parse_code`, `digest` |
| `src/hwcheck/db/__init__.py` (нов.) | пакет базы |
| `src/hwcheck/db/migrate.py` (нов.) | `apply_migrations` (advisory lock, транзакция на файл, `schema_migrations`) |
| `src/hwcheck/db/pool.py` (нов.) | `create_pool` (пул + миграции при старте) |
| `src/hwcheck/db/migrations/001_onboarding.sql` (нов.) | схема §6 |
| `src/hwcheck/bot/runner.py` (изм.) | `configure_ids`, пул при `DATABASE_URL` |
| `docker-compose.yml`, `.env.example`, `.github/workflows/ci.yml` (изм.) | `homework-postgres`, `homework-pgbackup`, сервис PostgreSQL в CI |
| `docs/deploy.md`, `docs/components.md` (изм.) | выкатка этапа, новые зависимости |
| `tests/conftest.py` (изм.), `tests/test_events.py` (изм.), `tests/test_crypto.py`, `tests/test_subjects.py`, `tests/test_invites.py`, `tests/test_db_migrations.py` (нов.) | тесты |

---

### Task 1: HMAC-хэш id и совместимость TEST_USERS

**Files:**
- Modify: `src/hwcheck/events.py` (функция `anonymize` в конце файла, `EventLog.log`)
- Modify: `tests/conftest.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: —
- Produces: `set_id_hash_key(key: str | None) -> None`; `anonymize(user_id: int | None) -> str | None` (HMAC при заданном ключе, иначе legacy); `legacy_anonymize(user_id: int | None) -> str | None`. Существующие вызовы `anonymize` в `bot/fsm.py`, `bot/handlers.py` не меняются.

- [ ] **Step 1: Сброс ключа между тестами** — в `tests/conftest.py` добавить импорт `pytest` и фикстуру (ключ — глобальное состояние процесса, тест не должен влиять на соседей):

```python
import pytest

from hwcheck.events import set_id_hash_key


@pytest.fixture(autouse=True)
def _reset_id_hash_key() -> Iterator[None]:
    yield
    set_id_hash_key(None)
```

и `from collections.abc import Iterator, Sequence` вместо текущего `from collections.abc import Sequence`.

- [ ] **Step 2: Написать падающие тесты** — дописать в `tests/test_events.py` (импорт `hashlib`, `hmac` и `legacy_anonymize`, `set_id_hash_key` — вверху файла):

```python
def test_anonymize_without_key_keeps_legacy_hash() -> None:
    legacy = hashlib.sha256(b"hwcheck:42").hexdigest()[:16]
    assert anonymize(42) == legacy_anonymize(42) == legacy


def test_anonymize_with_key_is_hmac() -> None:
    """Спецификация онбординга §8: sha256 от id обратим перебором диапазона id MAX."""
    set_id_hash_key("secret-key")
    expected = hmac.new(b"secret-key", b"42", hashlib.sha256).hexdigest()[:16]
    assert anonymize(42) == expected
    assert anonymize(42) != legacy_anonymize(42)
    assert anonymize(None) is None


def test_tester_listed_by_legacy_hash_is_still_test(tmp_path: Path) -> None:
    set_id_hash_key("secret-key")
    path = tmp_path / "events.jsonl"
    legacy = legacy_anonymize(42)
    assert legacy is not None
    log = EventLog(path, "prod", test_users={legacy})
    log.log("message_received", user_id=42)
    log.log("message_received", user_id=43)
    events = read_events(path)
    assert [e["env"] for e in events] == ["test", "prod"]
    assert events[0]["user"] == anonymize(42)  # в журнал пишется уже новый хэш
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_events.py -q`
Expected: FAIL — `ImportError: cannot import name 'legacy_anonymize'`.

- [ ] **Step 4: Реализация** — в `src/hwcheck/events.py` добавить `import hmac`, заменить `anonymize` и строку определения среды в `EventLog.log`:

```python
_id_hash_key: bytes | None = None


def set_id_hash_key(key: str | None) -> None:
    """Секрет HMAC для обезличивания id (`ID_HASH_KEY`); None — legacy-хэш (локально, тесты)."""
    global _id_hash_key
    _id_hash_key = key.encode() if key else None


def anonymize(user_id: int | None) -> str | None:
    """152-ФЗ и антифрод: наружу — только необратимый хэш идентификатора.

    С ключом — HMAC (спецификация онбординга §8): простой sha256 от id обратим перебором
    диапазона id MAX. Тот же хэш — поле user в events.jsonl, ключи Redis и имена фото.
    """
    if user_id is None:
        return None
    if _id_hash_key is None:
        return legacy_anonymize(user_id)
    return hmac.new(_id_hash_key, str(user_id).encode(), hashlib.sha256).hexdigest()[:16]


def legacy_anonymize(user_id: int | None) -> str | None:
    """Хэш до перехода на HMAC — только чтобы узнать тестеров из старого TEST_USERS."""
    if user_id is None:
        return None
    return hashlib.sha256(f"hwcheck:{user_id}".encode()).hexdigest()[:16]
```

В `EventLog.log`:

```python
        user = anonymize(user_id)
        # TEST_USERS мог быть собран до перехода на HMAC — узнаём тестера и по старому хэшу
        is_tester = user in self._test_users or legacy_anonymize(user_id) in self._test_users
        record = {
            "ts": time.time(),
            "env": "test" if is_tester else self._environment,
```

- [ ] **Step 5: Тесты зелёные**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: все тесты PASS (существующие тесты без ключа видят прежний хэш), ruff и mypy чистые.

- [ ] **Step 6: Commit**

```bash
git add src/hwcheck/events.py tests/conftest.py tests/test_events.py
git commit -m "feat: HMAC-хэш id пользователя, тестеры узнаются и по старому хэшу"
```

---

### Task 2: Шифр id MAX, ключи в настройках, `hwcheck keys`

**Files:**
- Create: `src/hwcheck/crypto.py`
- Modify: `src/hwcheck/config.py` (после `test_users`), `src/hwcheck/cli.py` (`main`), `src/hwcheck/bot/runner.py` (новая функция `configure_ids`, вызов в `run_polling`), `.env.example`, `pyproject.toml`/`uv.lock` (через `uv add`)
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: `set_id_hash_key` (Task 1).
- Produces: `UserIdCipher(key: str)` с `encrypt(user_id: int) -> bytes`, `decrypt(token: bytes) -> int`; `UserIdCipherError(ValueError)`; `new_user_id_key() -> str`; `Settings.id_hash_key: str`, `Settings.user_id_key: str`, `Settings.database_url: str | None`; `configure_ids(settings: Settings) -> None` в `bot/runner.py`.

- [ ] **Step 1: Зависимость**

Run: `uv add cryptography`
Expected: `cryptography` в `[project].dependencies`, `uv.lock` обновлён.

- [ ] **Step 2: Написать падающие тесты** — `tests/test_crypto.py`:

```python
"""Шифр id MAX и ключи при старте бота (спецификация онбординга §10.1, §13)."""

import pytest

from hwcheck.bot.runner import configure_ids
from hwcheck.config import Settings
from hwcheck.crypto import UserIdCipher, UserIdCipherError, new_user_id_key
from hwcheck.events import anonymize, legacy_anonymize


def test_cipher_roundtrip_with_random_ciphertext() -> None:
    cipher = UserIdCipher(new_user_id_key())
    first, second = cipher.encrypt(123456789), cipher.encrypt(123456789)
    assert first != second  # одинаковые id не выдают себя одинаковым шифротекстом
    assert b"123456789" not in first
    assert cipher.decrypt(first) == cipher.decrypt(second) == 123456789


def test_other_key_cannot_decrypt() -> None:
    token = UserIdCipher(new_user_id_key()).encrypt(42)
    with pytest.raises(UserIdCipherError):
        UserIdCipher(new_user_id_key()).decrypt(token)


def test_bad_key_is_reported_at_construction() -> None:
    with pytest.raises(UserIdCipherError, match="USER_ID_KEY"):
        UserIdCipher("not-a-fernet-key")


def test_configure_ids_switches_hash_and_checks_cipher_key() -> None:
    configure_ids(Settings(_env_file=None, id_hash_key="k", user_id_key=new_user_id_key()))
    assert anonymize(42) != legacy_anonymize(42)
    with pytest.raises(UserIdCipherError):
        configure_ids(Settings(_env_file=None, user_id_key="broken"))


def test_keys_command_prints_env_lines(capsys: pytest.CaptureFixture[str]) -> None:
    from hwcheck.cli import main

    main(["keys"])
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines())
    assert set(lines) == {"ID_HASH_KEY", "USER_ID_KEY", "POSTGRES_PASSWORD"}
    UserIdCipher(lines["USER_ID_KEY"])
    assert len(lines["ID_HASH_KEY"]) >= 40
    # пароль идёт в DATABASE_URL — только безопасные для URL символы
    assert all(ch.isalnum() or ch in "-_" for ch in lines["POSTGRES_PASSWORD"])
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_crypto.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hwcheck.crypto'`.

- [ ] **Step 4: `src/hwcheck/crypto.py`**

```python
"""Шифрование id MAX (152-ФЗ, спецификация онбординга §10.1): в базе — только шифротекст.

Для поиска пользователя — HMAC-хэш (`events.anonymize`), для отправки сообщения — расшифровка.
Ключ — `USER_ID_KEY` (Fernet). Без него бот не сможет писать пользователям: копия ключа хранится
вне VPS.
"""

from cryptography.fernet import Fernet, InvalidToken


class UserIdCipherError(ValueError):
    pass


class UserIdCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise UserIdCipherError(
                "USER_ID_KEY: нужен ключ Fernet (новый — python -m hwcheck keys)"
            ) from exc

    def encrypt(self, user_id: int) -> bytes:
        return self._fernet.encrypt(str(user_id).encode())

    def decrypt(self, token: bytes) -> int:
        try:
            return int(self._fernet.decrypt(token).decode())
        except InvalidToken as exc:
            raise UserIdCipherError("id не расшифровывается этим USER_ID_KEY") from exc


def new_user_id_key() -> str:
    return Fernet.generate_key().decode()
```

- [ ] **Step 5: Настройки** — в `src/hwcheck/config.py` после `test_users`:

```python
    # секрет HMAC для обезличенных id (события, ключи Redis, имена фото); пусто — legacy-хэш
    id_hash_key: str = ""
    # ключ Fernet: id MAX в базе хранится только шифротекстом (python -m hwcheck keys)
    user_id_key: str = ""
    # PostgreSQL: профили, согласия, приглашения; пусто — бот без базы (как до онбординга)
    database_url: str | None = None
```

- [ ] **Step 6: `configure_ids` в `src/hwcheck/bot/runner.py`** — импорты `from hwcheck.crypto import UserIdCipher` и `from hwcheck.events import EventLog, set_id_hash_key`; функция перед `run_polling`:

```python
def configure_ids(settings: Settings) -> None:
    """Ключи id при старте: HMAC для обезличенных id; ключ шифра проверяется сразу, а не на
    первом пользователе (спецификация онбординга §10.1)."""
    set_id_hash_key(settings.id_hash_key or None)
    if settings.user_id_key:
        UserIdCipher(settings.user_id_key)
    if settings.environment == "prod" and not settings.id_hash_key:
        logger.warning("ID_HASH_KEY не задан: id обезличены legacy-хэшем, обратимым перебором")
```

В `run_polling` сразу после проверки `MAX_TOKEN`: `configure_ids(settings)`.

- [ ] **Step 7: Команда `keys` в `src/hwcheck/cli.py`** — импорты `import secrets` и `from hwcheck.crypto import new_user_id_key`; парсер рядом с `report`:

```python
    sub.add_parser("keys", help="Новые секреты для .env: ID_HASH_KEY, USER_ID_KEY, POSTGRES_PASSWORD")
```

и перед веткой `report` в `main`:

```python
    if args.command == "keys":
        # без кредов GigaChat; вывод дописывают прямо в .env на сервере — не в чат и не в лог
        print(f"ID_HASH_KEY={secrets.token_urlsafe(32)}")
        print(f"USER_ID_KEY={new_user_id_key()}")
        print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}")
        return
```

- [ ] **Step 8: `.env.example`** — в конец:

```
# Онбординг (спецификация 2026-09-14). Секреты: python -m hwcheck keys >> .env
# Копию ID_HASH_KEY и USER_ID_KEY хранить вне сервера: без них бот не найдёт и не напишет пользователям
# ID_HASH_KEY=
# USER_ID_KEY=
# POSTGRES_PASSWORD=
# PostgreSQL; в docker-compose задан сам. Пусто — бот без базы
# DATABASE_URL=postgresql://homework:<пароль>@localhost:5432/homework
```

- [ ] **Step 9: Тесты зелёные**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: PASS, чисто.

- [ ] **Step 10: Commit**

```bash
git add src/hwcheck/crypto.py src/hwcheck/config.py src/hwcheck/cli.py src/hwcheck/bot/runner.py .env.example pyproject.toml uv.lock tests/test_crypto.py
git commit -m "feat: шифр id MAX (Fernet), ключи id в настройках и команда hwcheck keys"
```

---

### Task 3: Каталог предметов и учебный год

**Files:**
- Create: `src/hwcheck/bot/subjects.py`
- Test: `tests/test_subjects.py`

**Interfaces:**
- Consumes: —
- Produces: `Subject(code: str, title: str, grades: range, available: bool)` (frozen dataclass); `SUBJECTS: tuple[Subject, ...]`; `MIN_GRADE = 1`, `MAX_GRADE = 9`; `subjects_for(grade: int) -> list[Subject]`; `subject_by_code(code: str) -> Subject | None`; `school_year(day: date) -> int`; `current_grade(grade: int, grade_year: int, today: date) -> int`.

- [ ] **Step 1: Написать падающие тесты** — `tests/test_subjects.py`:

```python
"""Каталог предметов 1–9 классов и учебный год (спецификация онбординга §4.6, §5)."""

from datetime import date

import pytest

from hwcheck.bot.subjects import (
    MAX_GRADE,
    MIN_GRADE,
    SUBJECTS,
    current_grade,
    school_year,
    subject_by_code,
    subjects_for,
)


def codes(grade: int) -> list[str]:
    return [s.code for s in subjects_for(grade)]


def test_primary_school_subjects() -> None:
    assert codes(1) == ["math", "russian", "literary_reading", "world_around"]
    assert codes(2) == ["math", "russian", "literary_reading", "foreign_language", "world_around"]


def test_middle_school_subjects() -> None:
    assert codes(5) == [
        "math", "russian", "literature", "foreign_language", "history", "geography", "biology",
    ]  # fmt: skip
    assert "social_studies" in codes(6) and "social_studies" not in codes(5)
    assert "physics" in codes(7) and "informatics" in codes(7) and "chemistry" not in codes(7)
    assert len(codes(9)) == 11 and "chemistry" in codes(9)


def test_only_math_is_available_and_grades_stay_in_range() -> None:
    assert [s.code for s in SUBJECTS if s.available] == ["math"]
    assert all(MIN_GRADE <= g <= MAX_GRADE for s in SUBJECTS for g in s.grades)
    assert subject_by_code("history") is not None
    assert subject_by_code("english") is None


@pytest.mark.parametrize(
    ("day", "year"),
    [(date(2026, 8, 31), 2025), (date(2026, 9, 1), 2026), (date(2027, 1, 15), 2026)],
)
def test_school_year_starts_on_september_first(day: date, year: int) -> None:
    assert school_year(day) == year


def test_current_grade_moves_with_school_year() -> None:
    assert current_grade(4, 2025, date(2026, 8, 31)) == 4
    assert current_grade(4, 2025, date(2026, 9, 1)) == 5
    assert current_grade(9, 2026, date(2027, 9, 1)) == 10  # выпускник Домашки
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_subjects.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hwcheck.bot.subjects'`.

- [ ] **Step 3: `src/hwcheck/bot/subjects.py`**

```python
"""Каталог предметов 1–9 классов и учебный год (спецификация онбординга §4.6, §5).

Источник — федеральные учебные планы ФОП НОО и ФОП ООО. Математика 7–9 классов (алгебра,
геометрия, вероятность и статистика) — одной кнопкой. `available` — проверка работает; остальные
предметы показываются как «скоро» и пишутся в лист ожидания.
"""

from dataclasses import dataclass
from datetime import date

MIN_GRADE = 1
MAX_GRADE = 9


@dataclass(frozen=True)
class Subject:
    code: str
    title: str
    grades: range
    available: bool


SUBJECTS: tuple[Subject, ...] = (
    Subject("math", "Математика", range(1, 10), available=True),
    Subject("russian", "Русский язык", range(1, 10), available=False),
    Subject("literary_reading", "Литературное чтение", range(1, 5), available=False),
    Subject("literature", "Литература", range(5, 10), available=False),
    Subject("foreign_language", "Иностранный язык", range(2, 10), available=False),
    Subject("world_around", "Окружающий мир", range(1, 5), available=False),
    Subject("history", "История", range(5, 10), available=False),
    Subject("social_studies", "Обществознание", range(6, 10), available=False),
    Subject("geography", "География", range(5, 10), available=False),
    Subject("biology", "Биология", range(5, 10), available=False),
    Subject("informatics", "Информатика", range(7, 10), available=False),
    Subject("physics", "Физика", range(7, 10), available=False),
    Subject("chemistry", "Химия", range(8, 10), available=False),
)
_BY_CODE = {subject.code: subject for subject in SUBJECTS}


def subjects_for(grade: int) -> list[Subject]:
    return [subject for subject in SUBJECTS if grade in subject.grades]


def subject_by_code(code: str) -> Subject | None:
    return _BY_CODE.get(code)


def school_year(day: date) -> int:
    """Учебный год по году начала: 2026/27 → 2026, новый год — с 1 сентября."""
    return day.year if day.month >= 9 else day.year - 1


def current_grade(grade: int, grade_year: int, today: date) -> int:
    """Класс сегодня: указанный класс плюс прошедшие учебные годы; больше 9 — выпускник Домашки."""
    return grade + (school_year(today) - grade_year)
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_subjects.py -q && uv run ruff check . && uv run mypy`
Expected: PASS, чисто.

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/bot/subjects.py tests/test_subjects.py
git commit -m "feat: каталог предметов 1–9 классов и расчёт учебного года"
```

---

### Task 4: Приглашения — токен, запасной код, ссылка

**Files:**
- Create: `src/hwcheck/bot/invites.py`
- Test: `tests/test_invites.py`

**Interfaces:**
- Consumes: —
- Produces: `InviteKind = Literal["student_invites_parent", "parent_invites_student"]`; `NewInvite(kind, token, code)` с `token_hash`, `code_hash`, `display_code`, `link(bot_username: str) -> str`; `new_invite(kind: InviteKind) -> NewInvite`; `start_payload(kind, token) -> str`; `parse_start_payload(payload: str | None) -> tuple[InviteKind, str] | None`; `parse_code(text: str) -> str | None`; `digest(secret: str) -> str`; константы `CODE_ALPHABET`, `CODE_LENGTH = 8`, `INVITE_TTL_DAYS = 7`, `MAX_START_PAYLOAD = 128`.

- [ ] **Step 1: Написать падающие тесты** — `tests/test_invites.py`:

```python
"""Приглашения «ребёнок ↔ родитель»: ссылка и запасной код (спецификация онбординга §4.4, §7)."""

import pytest

from hwcheck.bot.invites import (
    CODE_ALPHABET,
    CODE_LENGTH,
    MAX_START_PAYLOAD,
    InviteKind,
    digest,
    new_invite,
    parse_code,
    parse_start_payload,
    start_payload,
)


def test_new_invite_has_url_safe_token_and_readable_code() -> None:
    first, second = new_invite("student_invites_parent"), new_invite("student_invites_parent")
    assert first.token != second.token and first.code != second.code
    assert len(first.code) == CODE_LENGTH and set(first.code) <= set(CODE_ALPHABET)
    assert first.display_code == f"{first.code[:4]}-{first.code[4:]}"


def test_link_carries_kind_prefix_within_max_limit() -> None:
    invite = new_invite("student_invites_parent")
    link = invite.link("domashka_bot")
    assert link == f"https://max.ru/domashka_bot?start=p_{invite.token}"
    assert len(start_payload(invite.kind, invite.token)) <= MAX_START_PAYLOAD
    child = new_invite("parent_invites_student")
    assert child.link("domashka_bot").endswith(f"?start=c_{child.token}")


def test_start_payload_roundtrip() -> None:
    kinds: tuple[InviteKind, ...] = ("student_invites_parent", "parent_invites_student")
    for kind in kinds:
        invite = new_invite(kind)
        assert parse_start_payload(start_payload(invite.kind, invite.token)) == (kind, invite.token)


@pytest.mark.parametrize(
    "payload", [None, "", "p_", "x_abcdefghijklmnopqrstu", "p_short", "p_bad token!"]
)
def test_broken_start_payload_is_ignored(payload: str | None) -> None:
    assert parse_start_payload(payload) is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("4F7K-92QD", "4F7K92QD"),
        (" 4f7k92qd ", "4F7K92QD"),
        ("4F7К-92QD", "4F7K92QD"),  # «К» кириллицей — ребёнок набирает в русской раскладке
    ],
)
def test_code_is_parsed_leniently(text: str, code: str) -> None:
    assert parse_code(text) == code


@pytest.mark.parametrize("text", ["4F7K-92Q0", "привет", "4F7K-92QDX", "1234 5678"])
def test_not_a_code(text: str) -> None:
    assert parse_code(text) is None


def test_hashes_do_not_reveal_secrets() -> None:
    invite = new_invite("parent_invites_student")
    assert invite.token_hash == digest(invite.token) and len(invite.token_hash) == 64
    assert invite.code not in invite.code_hash and invite.token not in invite.token_hash
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_invites.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hwcheck.bot.invites'`.

- [ ] **Step 3: `src/hwcheck/bot/invites.py`**

```python
"""Приглашения «ребёнок ↔ родитель» (спецификация онбординга §4.4, §7): ссылка и запасной код.

Ссылка `https://max.ru/<бот>?start=p_<токен>` (ребёнок зовёт родителя) или `c_<токен>` (родитель
зовёт ребёнка); запасной код — 8 знаков без похожих 0/O и 1/I. В базе — только sha256 токена и
кода: утечка таблицы не даёт погасить чужое приглашение.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Literal

InviteKind = Literal["student_invites_parent", "parent_invites_student"]

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
INVITE_TTL_DAYS = 7
MAX_START_PAYLOAD = 128  # лимит MAX на метку ?start=

_PREFIX: dict[InviteKind, str] = {"student_invites_parent": "p", "parent_invites_student": "c"}
_KIND_BY_PREFIX: dict[str, InviteKind] = {prefix: kind for kind, prefix in _PREFIX.items()}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_CODE = re.compile(rf"^([{CODE_ALPHABET}]{{4}})-?([{CODE_ALPHABET}]{{4}})$")
# кириллица, похожая на латиницу кода: ребёнок набирает код в русской раскладке
_LOOKALIKES = str.maketrans("АВЕКМНРСТХ", "ABEKMHPCTX")


@dataclass(frozen=True)
class NewInvite:
    kind: InviteKind
    token: str  # в ссылку; в базу — token_hash
    code: str  # показывается как XXXX-XXXX; в базу — code_hash

    @property
    def token_hash(self) -> str:
        return digest(self.token)

    @property
    def code_hash(self) -> str:
        return digest(self.code)

    @property
    def display_code(self) -> str:
        return f"{self.code[:4]}-{self.code[4:]}"

    def link(self, bot_username: str) -> str:
        return f"https://max.ru/{bot_username}?start={start_payload(self.kind, self.token)}"


def new_invite(kind: InviteKind) -> NewInvite:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return NewInvite(kind=kind, token=secrets.token_urlsafe(16), code=code)


def start_payload(kind: InviteKind, token: str) -> str:
    return f"{_PREFIX[kind]}_{token}"


def parse_start_payload(payload: str | None) -> tuple[InviteKind, str] | None:
    """Метка из `bot_started`; неизвестная или битая — None (обычный старт)."""
    prefix, separator, token = (payload or "").partition("_")
    kind = _KIND_BY_PREFIX.get(prefix)
    if not separator or kind is None or not _TOKEN.match(token):
        return None
    return kind, token


def parse_code(text: str) -> str | None:
    """Запасной код из сообщения: «4F7K-92QD», « 4f7k92qd » → «4F7K92QD»; не код — None."""
    match = _CODE.match(text.strip().upper().translate(_LOOKALIKES))
    return match.group(1) + match.group(2) if match else None


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_invites.py -q && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: PASS, чисто (длинную строку параметров `test_broken_start_payload_is_ignored` форматирует `ruff format`).

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/bot/invites.py tests/test_invites.py
git commit -m "feat: приглашения родителя и ребёнка — токен ссылки и запасной код"
```

---

### Task 5: PostgreSQL — схема, миграции, пул, CI

**Files:**
- Create: `src/hwcheck/db/__init__.py`, `src/hwcheck/db/migrate.py`, `src/hwcheck/db/pool.py`, `src/hwcheck/db/migrations/001_onboarding.sql`
- Modify: `.github/workflows/ci.yml`, `pyproject.toml`/`uv.lock` (через `uv add`)
- Test: `tests/test_db_migrations.py`

**Interfaces:**
- Consumes: —
- Produces: `apply_migrations(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]`; `create_pool(dsn: str, *, server_settings: dict[str, str] | None = None) -> asyncpg.Pool` (миграции применены). Таблицы §6: `users`, `student_profiles`, `parent_settings`, `consents`, `invites` (с `code_hash`), `homeworks`, `subject_waitlist`, `login_attempts`.

- [ ] **Step 1: Зависимости**

Run: `uv add asyncpg` и `uv add --dev asyncpg-stubs`
Expected: оба в `pyproject.toml`. Если `asyncpg-stubs` не ставится с текущей версией asyncpg — удалить его (`uv remove --dev asyncpg-stubs`) и добавить в `pyproject.toml` к `[[tool.mypy.overrides]]` модуль `"asyncpg.*"`; тогда в аннотациях ниже писать `asyncpg.Pool` без параметра. Если со стабами mypy требует параметр и у `Connection` — писать `asyncpg.Connection[asyncpg.Record]` (в модулях с аннотациями уже есть `from __future__ import annotations`; в `migrate.py` и тестах базы добавить его же).

- [ ] **Step 2: Локальная база для тестов**

Run: `docker run -d --name hwcheck-test-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=hwcheck_test -p 55432:5432 postgres:17-alpine`
Expected: контейнер запущен; дальше тесты базы — с `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/hwcheck_test`. Это локальная машина разработчика, не VPS.

- [ ] **Step 3: Написать падающие тесты** — `tests/test_db_migrations.py`:

```python
"""Миграции PostgreSQL на пустой схеме (спецификация онбординга §6, §15).

Локально без TEST_DATABASE_URL — пропуск; в CI база обязательна, иначе пропуск спрятал бы
непроверенные миграции.
"""

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from hwcheck.db.migrate import apply_migrations
from hwcheck.db.pool import create_pool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None and "CI" not in os.environ,
    reason="нужен TEST_DATABASE_URL (PostgreSQL)",
)
TABLES = {
    "schema_migrations", "users", "student_profiles", "parent_settings", "consents", "invites",
    "homeworks", "subject_waitlist", "login_attempts",
}  # fmt: skip


def database_url() -> str:
    assert TEST_DATABASE_URL is not None, "в CI нужен TEST_DATABASE_URL"
    return TEST_DATABASE_URL


@pytest.fixture
async def schema() -> AsyncIterator[str]:
    """Своя схема на тест: миграции проверяются на пустой базе, тесты не мешают друг другу."""
    name = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(database_url())
    await admin.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()


async def connect(schema: str) -> asyncpg.Connection:
    return await asyncpg.connect(database_url(), server_settings={"search_path": schema})


async def test_migrations_create_schema_once(schema: str) -> None:
    conn = await connect(schema)
    try:
        assert await apply_migrations(conn) == ["001_onboarding.sql"]
        assert await apply_migrations(conn) == []
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = $1", schema
        )
        assert {row["table_name"] for row in rows} == TABLES
    finally:
        await conn.close()


async def test_constraints_protect_consent_and_cascade_profiles(schema: str) -> None:
    conn = await connect(schema)
    consent = (
        "INSERT INTO consents (parent_hash, student_hash, policy_version, given_at) "
        "VALUES ($1, 's1', 'v1', now())"
    )
    try:
        await apply_migrations(conn)
        student = await conn.fetchval(
            "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
            "VALUES ('s1', 'x', 'student') RETURNING id"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO student_profiles (user_id, grade) VALUES ($1, 10)", student
            )
        await conn.execute(consent, "p1")
        with pytest.raises(asyncpg.UniqueViolationError):  # у ребёнка один подтвердивший родитель
            await conn.execute(consent, "p2")
        await conn.execute("UPDATE consents SET revoked_at = now() WHERE parent_hash = 'p1'")
        await conn.execute(consent, "p2")
        await conn.execute(
            "INSERT INTO student_profiles (user_id, grade, subject) VALUES ($1, 4, 'math')", student
        )
        await conn.execute(
            "INSERT INTO homeworks (student_user_id, subject, tasks_total, tasks_correct, "
            "tasks_wrong, tasks_uncertain) VALUES ($1, 'math', 3, 2, 1, 0)",
            student,
        )
        await conn.execute("DELETE FROM users WHERE id = $1", student)
        assert await conn.fetchval("SELECT count(*) FROM student_profiles") == 0
        assert await conn.fetchval("SELECT count(*) FROM homeworks") == 0
        assert await conn.fetchval("SELECT count(*) FROM consents") == 2  # запись согласия остаётся
    finally:
        await conn.close()


async def test_failed_migration_rolls_back_whole_file(schema: str, tmp_path: Path) -> None:
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE first_table (id int);", encoding="utf-8")
    (tmp_path / "002_broken.sql").write_text(
        "CREATE TABLE half_table (id int);\nSELECT broken(;", encoding="utf-8"
    )
    conn = await connect(schema)
    try:
        with pytest.raises(asyncpg.PostgresSyntaxError):
            await apply_migrations(conn, tmp_path)
        names = [row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")]
        assert names == ["001_ok.sql"]
        assert await conn.fetchval("SELECT to_regclass('half_table')") is None
    finally:
        await conn.close()


async def test_create_pool_applies_migrations(schema: str) -> None:
    pool = await create_pool(database_url(), server_settings={"search_path": schema})
    try:
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == 1
    finally:
        await pool.close()
```

- [ ] **Step 4: Убедиться, что тесты падают**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/hwcheck_test uv run pytest tests/test_db_migrations.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hwcheck.db'`. Без переменной: `uv run pytest tests/test_db_migrations.py -q` → skipped (локально).

- [ ] **Step 5: Схема** — `src/hwcheck/db/migrations/001_onboarding.sql`:

```sql
-- Онбординг, согласие родителя, профиль ученика (спецификация 2026-09-14, §6).
-- Сырой id MAX не хранится: max_user_hash — HMAC (events.anonymize), max_user_id_enc — Fernet.

CREATE TABLE users (
  id               bigserial PRIMARY KEY,
  max_user_hash    text NOT NULL UNIQUE,
  max_user_id_enc  bytea NOT NULL,
  role             text NOT NULL CHECK (role IN ('student', 'parent')),
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE student_profiles (
  user_id          bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  grade            smallint CHECK (grade BETWEEN 1 AND 9),
  grade_year       smallint,
  grade_asked_year smallint,
  subject          text,
  parent_user_id   bigint REFERENCES users ON DELETE SET NULL,
  CHECK (parent_user_id IS DISTINCT FROM user_id)
);

CREATE TABLE parent_settings (
  user_id          bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  notify_mode      text NOT NULL DEFAULT 'digest' CHECK (notify_mode IN ('instant', 'digest', 'off')),
  digest_time      time NOT NULL DEFAULT '20:00',
  utc_offset_min   smallint NOT NULL DEFAULT 180,
  last_digest_at   timestamptz
);

-- юридическая запись: без FK, переживает удаление данных
CREATE TABLE consents (
  id               bigserial PRIMARY KEY,
  parent_hash      text NOT NULL,
  student_hash     text NOT NULL,
  policy_version   text NOT NULL,
  given_at         timestamptz NOT NULL,
  revoked_at       timestamptz
);
CREATE UNIQUE INDEX consents_one_active_parent ON consents (student_hash) WHERE revoked_at IS NULL;

-- в базе только sha256 токена ссылки и запасного кода
CREATE TABLE invites (
  token_hash       text PRIMARY KEY,
  code_hash        text NOT NULL UNIQUE,
  kind             text NOT NULL CHECK (kind IN ('student_invites_parent', 'parent_invites_student')),
  created_by       bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  consent_given_at timestamptz,
  policy_version   text,
  expires_at       timestamptz NOT NULL,
  used_at          timestamptz,
  used_by          bigint REFERENCES users ON DELETE SET NULL
);

CREATE TABLE homeworks (
  id               bigserial PRIMARY KEY,
  student_user_id  bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  subject          text NOT NULL,
  tasks_total      smallint NOT NULL,
  tasks_correct    smallint NOT NULL,
  tasks_wrong      smallint NOT NULL,
  tasks_uncertain  smallint NOT NULL,
  errors_resolved  smallint NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX homeworks_student_time ON homeworks (student_user_id, created_at);

CREATE TABLE subject_waitlist (
  user_hash        text NOT NULL,
  subject          text NOT NULL,
  grade            smallint NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_hash, subject)
);

-- лимит ввода запасного кода
CREATE TABLE login_attempts (
  user_hash        text NOT NULL,
  attempted_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_attempts_user_time ON login_attempts (user_hash, attempted_at);
```

- [ ] **Step 6: `src/hwcheck/db/__init__.py`**

```python
"""PostgreSQL: профили, согласия, приглашения (спецификация онбординга §6–7)."""
```

- [ ] **Step 7: `src/hwcheck/db/migrate.py`**

```python
"""Миграции PostgreSQL при старте бота (спецификация онбординга §7).

Файлы `migrations/NNN_*.sql` применяются по порядку имён, каждый — в своей транзакции, учёт — в
`schema_migrations`. `pg_advisory_lock`: два процесса бота не применят одну миграцию дважды.
"""

from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x686F6D65  # «home»


async def apply_migrations(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Применяет новые миграции и возвращает их имена; упавшая откатывается целиком."""
    await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
        applied: list[str] = []
        for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
        return applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
```

- [ ] **Step 8: `src/hwcheck/db/pool.py`**

```python
"""Пул PostgreSQL (спецификация онбординга §7): открывается в раннере, миграции — при старте."""

from __future__ import annotations

import logging

import asyncpg

from hwcheck.db.migrate import apply_migrations

logger = logging.getLogger(__name__)


async def create_pool(
    dsn: str, *, server_settings: dict[str, str] | None = None
) -> asyncpg.Pool[asyncpg.Record]:
    """База недоступна или миграция упала — исключение: бот не стартует (health контейнера),
    а не работает без профилей и согласий."""
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, command_timeout=10, server_settings=server_settings
    )
    try:
        async with pool.acquire() as conn:
            applied = await apply_migrations(conn)
    except BaseException:
        await pool.close()
        raise
    if applied:
        logger.info("migrations applied: %s", ", ".join(applied))
    return pool
```

- [ ] **Step 9: Тесты зелёные локально**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/hwcheck_test uv run pytest tests/test_db_migrations.py -q && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: 4 теста базы PASS; весь набор PASS (тесты базы без переменной — skipped); ruff и mypy чистые.

- [ ] **Step 10: PostgreSQL в CI** — в `.github/workflows/ci.yml` у job `checks` перед `steps:`:

```yaml
    services:
      postgres:
        image: postgres:17-alpine
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: hwcheck_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      TEST_DATABASE_URL: postgresql://postgres:postgres@localhost:5432/hwcheck_test
```

- [ ] **Step 11: Commit**

```bash
git add src/hwcheck/db tests/test_db_migrations.py .github/workflows/ci.yml pyproject.toml uv.lock
git commit -m "feat: PostgreSQL — схема онбординга, миграции при старте, тесты базы в CI"
```

---

### Task 6: Бот поднимает базу; compose, бэкапы, документация

**Files:**
- Modify: `src/hwcheck/bot/runner.py` (`run_polling`), `docker-compose.yml`, `docs/deploy.md`, `docs/components.md`

**Interfaces:**
- Consumes: `create_pool` (Task 5), `configure_ids` (Task 2).
- Produces: пул `asyncpg` в раннере при заданном `DATABASE_URL` (этап 2 передаст его в репозиторий профилей); контейнеры `homework-postgres`, `homework-pgbackup`.

- [ ] **Step 1: Раннер** — в `src/hwcheck/bot/runner.py` импорт `from hwcheck.db.pool import create_pool`; в `run_polling` после `await redis_client.ping()`:

```python
    # PostgreSQL недоступен или миграция упала — не стартуем: health покажет, профили не потеряются
    pool = await create_pool(settings.database_url) if settings.database_url else None
```

а блок `finally` внутри `async with` — закрывать и пул:

```python
        try:
            await _poll_loop(max_client, bot, marker_path, stop)
        finally:
            if redis_client is not None:
                await redis_client.aclose()
            if pool is not None:
                await pool.close()
```

- [ ] **Step 2: Проверка раннера** — пул создаётся тем же `create_pool`, что покрыт `test_create_pool_applies_migrations`; локально бот не запускается (poller на VPS).

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: PASS, чисто.

- [ ] **Step 3: `docker-compose.yml`** — у сервиса `bot` в `environment` добавить строку и в `depends_on` — базу:

```yaml
      # профили, согласия, приглашения; пароль — из .env (python -m hwcheck keys)
      DATABASE_URL: postgresql://homework:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD не задан в .env}@postgres:5432/homework
```

```yaml
      postgres:
        condition: service_healthy
```

новые сервисы после `redis`:

```yaml
  postgres:
    image: postgres:17-alpine
    container_name: homework-postgres
    restart: unless-stopped
    # портов наружу нет: база доступна только боту в сети compose
    environment:
      POSTGRES_USER: homework
      POSTGRES_DB: homework
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD не задан в .env}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    deploy:
      resources:
        limits:
          memory: 256m
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U homework -d homework"]
      interval: 10s
      timeout: 3s
      retries: 5

  # ежедневный pg_dump в var/backups, хранение 7 дней (спецификация онбординга §7)
  pgbackup:
    image: postgres:17-alpine
    container_name: homework-pgbackup
    restart: unless-stopped
    environment:
      PGHOST: postgres
      PGUSER: homework
      PGDATABASE: homework
      PGPASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD не задан в .env}
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        while true; do
          pg_dump --format=custom --file=/backups/homework-$$(date -u +%F).dump \
            && find /backups -name 'homework-*.dump' -mtime +7 -delete
          sleep 86400
        done
    volumes:
      - ./var/backups:/backups
    depends_on:
      postgres:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 128m
```

и в `volumes:` внизу — `postgres-data:`.

- [ ] **Step 4: Проверить compose**

Run: `POSTGRES_PASSWORD=check docker compose config -q`
Expected: без ошибок (синтаксис и подстановка переменных). Без `POSTGRES_PASSWORD` — понятная ошибка «POSTGRES_PASSWORD не задан в .env».

- [ ] **Step 5: `docs/components.md`** — в таблицу «Python-зависимости runtime» по алфавиту (версии — из `uv.lock`, лицензии: asyncpg — Apache-2.0, cryptography — Apache-2.0 OR BSD-3-Clause):

```markdown
| asyncpg | <версия из uv.lock> | Apache-2.0 | Асинхронный драйвер PostgreSQL: пул и миграции (`db/pool.py`, `db/migrate.py`) — профили, согласия, приглашения онбординга |
| cryptography | <версия из uv.lock> | Apache-2.0 OR BSD-3-Clause | Fernet: id MAX в базе хранится только шифротекстом (`crypto.py`) |
```

и в «Dev-зависимости» — `asyncpg-stubs` (если остался после Task 5), лицензия BSD-3-Clause, «типы asyncpg для mypy strict». Версию подставить командой `uv tree --depth 1 | grep -E "asyncpg|cryptography"`.

- [ ] **Step 6: `docs/deploy.md`** — раздел «PostgreSQL и ключи id (онбординг, этап 1)» с командами из Task 7 (шаги 2–7) и строками диагностики:

```bash
docker exec homework-postgres psql -U homework -c '\dt'                   # таблицы онбординга
docker exec homework-postgres psql -U homework -c 'SELECT * FROM schema_migrations'
ls -la var/backups                                                       # ежедневные дампы, 7 дней
```

- [ ] **Step 7: Commit**

```bash
git add src/hwcheck/bot/runner.py docker-compose.yml docs/deploy.md docs/components.md
git commit -m "feat: бот поднимает PostgreSQL при старте; контейнеры базы и бэкапа в compose"
```

---

### Task 7: Ревью, слияние и выкатка этапа 1

**Files:**
- Modify: `HISTORY.md`, `TODO.md`, `docs/PROJECT_MEMORY.md`, память `vps-shared-host.md`

**Interfaces:**
- Consumes: всё из Task 1–6.
- Produces: prod с PostgreSQL и HMAC-хэшами; ключи на VPS и у Кирилла.

- [ ] **Step 1: Ревью и PR** — агент `ecc:python-reviewer` на `git diff main...HEAD` (без команд, меняющих дерево); исправить CRITICAL/HIGH; `gh pr create` (тело — что сделано, отступление `code_hash`, план проверки); дождаться зелёного CI (`gh pr checks --watch`), включая тесты базы.

- [ ] **Step 2: Тишина в боте** — на VPS нет событий за 10 минут (скрипт из `docs/deploy.md`); иначе ждать.

- [ ] **Step 3: Слияние и сборка на VPS**

```bash
gh pr merge <номер> --merge
ssh root@193.247.73.243
cd /opt/max-homework-ai && git pull
cp .env .env.bak-$(date +%F) && chmod 600 .env.bak-*
docker build -t max-homework-ai-bot .
docker run --rm max-homework-ai-bot python -m hwcheck keys >> .env   # секреты сразу в .env, не на экран
grep -o '^[A-Z_]*=' .env                                               # проверить только имена
mkdir -p var/backups
```

Expected: в списке имён есть `ID_HASH_KEY=`, `USER_ID_KEY=`, `POSTGRES_PASSWORD=` ровно по одному разу.

- [ ] **Step 4: Запуск без рестарта Redis**

```bash
docker compose up -d postgres pgbackup bot
docker compose ps
docker compose logs --tail 20 bot
```

Expected: `homework-postgres` и `homework-pgbackup` Up, `homework-bot` healthy после start_period, в логе бота `migrations applied: 001_onboarding.sql` и `bot started`; `homework-redis` не пересоздан (Up с прежним временем).

- [ ] **Step 5: Проверка базы и бэкапа**

```bash
docker exec homework-postgres psql -U homework -c 'SELECT name FROM schema_migrations'
ls -la var/backups
```

Expected: `001_onboarding.sql`; файл `homework-<дата>.dump`.

- [ ] **Step 6: Живая проверка хэша** — Кирилл пишет боту любое сообщение; на VPS в последнем событии `env` = `test`, а `user` отличается от прежнего хэша тестера (`88095b…`). Открытые разборы тестеров сбрасываются один раз — предупредить Кирилла заранее.

- [ ] **Step 7: Копия ключей у Кирилла** — Кирилл сам выполняет у себя и сохраняет вывод в менеджер паролей (агент эту команду не запускает — секреты не должны попасть в чат):

```bash
ssh root@193.247.73.243 "grep -E '^(ID_HASH_KEY|USER_ID_KEY|POSTGRES_PASSWORD)=' /opt/max-homework-ai/.env"
```

- [ ] **Step 8: Документы сессии** — `HISTORY.md` (этап 1: что сделано, отступление `code_hash`, ключи), `TODO.md` (этап 1 закрыт, следующий шаг — план этапа 2), `docs/PROJECT_MEMORY.md` (PostgreSQL в prod, ключи и где копия, бэкапы), память `vps-shared-host.md` (контейнеры `homework-postgres`, `homework-pgbackup`, `var/backups`); commit `docs: …` в main через PR или вместе со следующей веткой.

---

## Самопроверка плана этапа 1 по спецификации

- §5 каталог — Task 3; §4.6 формулы `school_year`/`current_grade` — Task 3 (вопрос о классе — этап 3).
- §6 схема — Task 5 (все 8 таблиц, индексы, CHECK, частичный уникальный индекс согласий; `code_hash` вместо `short_code` — отмечено).
- §7 `db/pool.py`, `db/migrate.py`, `crypto.py`, `events.py`, `bot/subjects.py`, `bot/invites.py` — Task 1–5; `bot/onboarding.py`, `bot/notifier.py`, `bot/texts.py`, `docs/legal/` — этапы 2–4. Инфраструктура compose и бэкап — Task 6.
- §8 HMAC и legacy `TEST_USERS` — Task 1; сброс ключей Redis и имён фото — следствие, предупреждение в Task 7.
- §10.1 ключи и копия вне VPS — Task 2, Task 7.
- §13 `DATABASE_URL`, `POSTGRES_PASSWORD`, `USER_ID_KEY`, `ID_HASH_KEY` — Task 2, Task 6; `ONBOARDING_REQUIRED` и проверка ключей при нём — этап 2.
- §15 юнит-тесты каталога, границ учебного года, приглашений, шифра, HMAC — Task 1–4; интеграционные тесты миграций и ограничений — Task 5 (репозиторий `PgProfileRepository` — этап 2).
