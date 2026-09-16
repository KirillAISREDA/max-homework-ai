# Онбординг, согласие родителя и профиль ученика — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** первый вход в бота с ролью, классом и предметом, связка «ребёнок ↔ родитель» и согласие родителя до проверки фото, профиль с переходом в следующий класс, уведомления родителю.

**Architecture:** профили, согласия и приглашения — в PostgreSQL (asyncpg, миграции при старте бота); сырой id MAX хранится только шифротекстом Fernet, для поиска — HMAC-хэш. Шаг онбординга выводится из данных, FSM — детерминированный код в `bot/onboarding.py`; `bot/handlers.py` только маршрутизирует. Спецификация большая, поэтому работа идёт этапами: каждый этап даёт рабочий, проверенный и выкатываемый результат, подробный план следующего этапа пишется по коду предыдущего.

**Tech Stack:** Python 3.12, asyncpg, cryptography (Fernet), pydantic-settings, Redis (как сейчас), PostgreSQL 17 в Docker, pytest + pytest-asyncio, ruff, mypy strict, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-14-onboarding-design.md`

## Global Constraints

- Аудитория — только 1–9 классы (`grade BETWEEN 1 AND 9`).
- 1–4 классы — фото присылает родитель из своего MAX, у родителя может быть несколько детей; ученик 1–4 класса в своём аккаунте в базе не сохраняется (изменение 16.09, спецификация §4.7).
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
| 2. Вход ученика и согласие родителя | роль → класс → предмет (лист ожидания) → ссылка/код родителю → согласие с политикой; вход родителя первым; дети 1–4 класса через аккаунт родителя и «Чья домашка?» (решение 16.09); блокировка фото до согласия; пример проверки; инструкция; `ONBOARDING_REQUIRED`; события воронки | §4.1–4.4, §4.7, §10.2–10.3, §11, §12 | ниже, подробно (16.09) |
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
mkdir -p var/backups && chmod 700 var/backups   # дампы с данными детей — только root
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

---

# Этап 2. Вход ученика и согласие родителя

Результат этапа: при `ONBOARDING_REQUIRED=true` бот ведёт нового пользователя по онбордингу — роль, класс, предмет
(лист ожидания), связка «ребёнок 5–9 ↔ родитель» ссылкой и запасным кодом, согласие с версией политики; ребёнок
1–4 класса пользуется Домашкой через аккаунт родителя, у родителя их может быть несколько («Чья домашка?»).
До согласия фото не скачиваются. При `false` бот работает как до этапа 2 (аварийный выключатель). В prod флаг
включается на время живого теста на двух аккаунтах.

Ветка: `feat/onboarding-stage2`, один PR на этап. Спецификация в этой ветке уже обновлена (изменение 16.09).

## Решения этапа (сверх спецификации)

- **Схема этапа 1 пересоздаётся** (`002_children.sql`): профиль ребёнка больше не совпадает с аккаунтом —
  `student_profiles.id`, `user_id` необязателен; согласие привязано к профилю; `invites.grade`. В prod таблицы
  пусты (проверено 16.09), миграция падает, если это уже не так.
- **Выбор до первой записи едет в payload кнопки**: `ob:pgrade:7` → `ob:pconsent:7`. Родитель ребёнка 5–9 до
  «Согласен» в базе не появляется; ученик 1–4 класса не появляется никогда.
- **Пакет `bot/onboarding/`** вместо одного `onboarding.py`: одним файлом код превысил бы 800 строк.
  Шаги получают общий `OnboardingContext`; `router.py` выводит шаг из данных и решает, пропустить ли апдейт в
  сценарий проверки.
- **Режим уведомлений родителю — этап 4**, меню и удаление данных — этап 3. До меню родитель на текст получает
  список детей и [Добавить ребёнка].
- **Политика `v0`** — черновик с полями реквизитов ИП, их заполняет Кирилл; реальных пользователей до вычитки
  юристом не привлекаем (§14).
- **Фото родителя до «Чья домашка?»** копятся час (как условия учебника): учебник и тетрадь двумя сообщениями
  проверяются вместе; выбор ребёнка действует тот же час.

## Файлы этапа

| Файл | Ответственность |
|---|---|
| `src/hwcheck/db/migrations/002_children.sql` (нов.) | профили детей без своего аккаунта, согласие на профиль, класс в приглашении |
| `src/hwcheck/db/repo.py` (нов.) | `ProfileRepository`, `Account`, `StudentProfile`, `Invite`, `LinkOutcome`, `PgProfileRepository` |
| `src/hwcheck/db/memory.py` (нов.) | `InMemoryProfileRepository` для сценарных тестов |
| `src/hwcheck/bot/models.py`, `src/hwcheck/bot/max_api.py` (изм.) | метка `bot_started.payload`; `send_to_user` |
| `src/hwcheck/config.py` (изм.) | `onboarding_required` |
| `src/hwcheck/bot/subjects.py` (изм.) | `PARENT_SENDS_UP_TO_GRADE = 4` |
| `src/hwcheck/bot/onboarding/__init__.py` (нов.) | пакет |
| `src/hwcheck/bot/onboarding/state.py` (нов.) | `OnboardingState`, хранилища в памяти и Redis |
| `src/hwcheck/bot/onboarding/policy.py` (нов.) | `POLICY_VERSION`, `policy_messages`, `split_message` |
| `src/hwcheck/bot/onboarding/texts.py` (нов.) | тексты, выжимка согласия, клавиатуры |
| `src/hwcheck/bot/onboarding/context.py` (нов.) | `Actor`, `OnboardingContext` (ответ, журнал, сообщение второй стороне) |
| `src/hwcheck/bot/onboarding/subject.py` (нов.) | `SubjectStep` — предмет и лист ожидания |
| `src/hwcheck/bot/onboarding/student.py` (нов.) | `StudentSteps` — класс ученика, ссылка родителю, блокировка фото |
| `src/hwcheck/bot/onboarding/linking.py` (нов.) | `Linking` — ссылка и код, согласие/отказ, привязка ребёнка, лимит кода |
| `src/hwcheck/bot/onboarding/parent.py` (нов.) | `ParentSteps` — класс ребёнка, дети 1–4, приглашение 5–9, список детей, «Чья домашка?» |
| `src/hwcheck/bot/onboarding/router.py` (нов.) | `Onboarding.route`, `CheckPhotos`, `Route` |
| `src/hwcheck/bot/handlers.py`, `src/hwcheck/bot/runner.py` (изм.) | маршрутизация через онбординг; сборка и проверки при старте |
| `docs/legal/privacy-policy-v0.md` (нов.), `Dockerfile` (изм.) | черновик политики; политика в образе |
| `.env.example`, `docs/deploy.md` (изм.) | флаг и выкатка этапа |
| `tests/onboarding_kit.py` (нов.) | фейк MAX, часы, сборка контекста, апдейты |
| `tests/test_db_migrations.py` (изм.), `tests/test_profile_repo.py`, `tests/test_max_client.py`, `tests/test_onboarding_state.py`, `tests/test_onboarding_texts.py`, `tests/test_onboarding_student.py`, `tests/test_onboarding_linking.py`, `tests/test_onboarding_parent.py`, `tests/test_onboarding_router.py`, `tests/test_bot_onboarding.py`, `tests/test_onboarding_runner.py` (нов.) | тесты |

## Кнопки онбординга

| Payload | Кому доступна | Действие |
|---|---|---|
| `ob:role:student`, `ob:role:parent` | новый пользователь | роль |
| `ob:grade:<1–9>` | новый пользователь | класс ученика; 1–4 — ссылка на бота для родителя |
| `ob:pgrade:<1–9>` | новый или родитель | класс ребёнка: 1–4 — ребёнок без своего MAX, 5–9 — согласие |
| `ob:subject:<код>` | ученик без предмета; родитель с незавершённым ребёнком 1–4 | предмет или лист ожидания |
| `ob:consent` | родитель с ребёнком 1–4 без согласия | согласие, фото присылает родитель |
| `ob:pconsent:<5–9>` | новый или родитель | согласие и ссылка ребёнку |
| `ob:accept`, `ob:decline` | новый или родитель с открытой ссылкой `p_` | ответ ребёнку |
| `ob:policy`, `ob:example` | любой | полный текст политики; пример проверки |
| `ob:resend` | ученик, ждущий родителя | новая ссылка родителю |
| `ob:addchild` | новый или родитель | класс ещё одного ребёнка |
| `ob:whose:<id профиля>` | родитель с фото, ждущими выбора | чья домашка |

Кнопка не своего шага (старое сообщение, чужая роль, выдуманный аргумент) показывает текущий шаг.

Локальная база для тестов: `docker start hwcheck-test-pg`, затем в bash
`export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/hwcheck_test`. Без неё тесты
PostgreSQL пропускаются локально и обязательны в CI.

---

### Task 1: Миграция `002_children.sql` — дети без своего аккаунта

**Files:**
- Create: `src/hwcheck/db/migrations/002_children.sql`
- Modify: `tests/test_db_migrations.py`

**Interfaces:**
- Consumes: схема `001_onboarding.sql`, `apply_migrations`, `create_pool` (этап 1).
- Produces: таблицы `student_profiles(id, user_id NULL UNIQUE, parent_user_id, grade, grade_year, grade_asked_year, subject, created_at)`, `consents(id, parent_hash, student_profile_id, student_hash NULL, policy_version, given_at, revoked_at)` с уникальным активным согласием на профиль, `homeworks.student_id → student_profiles`, `invites.grade` + `CHECK` полноты приглашения родителя.

- [ ] **Step 1: Переписать тесты миграций под новую схему**

В `tests/test_db_migrations.py`:

1. Добавить импорт `from hwcheck.db.migrate import MIGRATIONS_DIR, apply_migrations` (вместо импорта одного `apply_migrations`).
2. В `test_migrations_create_schema_once` ожидать обе миграции:

```python
async def test_migrations_create_schema_once(schema: str) -> None:
    conn = await connect(schema)
    try:
        assert await apply_migrations(conn) == ["001_onboarding.sql", "002_children.sql"]
        assert await apply_migrations(conn) == []
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = $1", schema
        )
        assert {row["table_name"] for row in rows} == TABLES
    finally:
        await conn.close()
```

3. Заменить `test_constraints_protect_consent_and_cascade_profiles` целиком:

```python
async def test_constraints_protect_children_and_consents(schema: str) -> None:
    conn = await connect(schema)
    consent = (
        "INSERT INTO consents (parent_hash, student_profile_id, policy_version, given_at) "
        "VALUES ($1, $2, 'v0', now())"
    )
    young = (
        "INSERT INTO student_profiles (parent_user_id, grade, grade_year, grade_asked_year) "
        "VALUES ($1, $2, 2026, 2026) RETURNING id"
    )
    try:
        await apply_migrations(conn)
        parent = await conn.fetchval(
            "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
            "VALUES ('p1', 'x', 'parent') RETURNING id"
        )
        with pytest.raises(asyncpg.CheckViolationError):  # только 1–9 классы
            await conn.fetchval(young, parent, 10)
        with pytest.raises(asyncpg.CheckViolationError):  # у профиля всегда есть, кто шлёт фото
            await conn.execute(
                "INSERT INTO student_profiles (grade, grade_year, grade_asked_year) "
                "VALUES (3, 2026, 2026)"
            )
        with pytest.raises(asyncpg.CheckViolationError):  # ссылка ребёнку — с классом и согласием
            await conn.execute(
                "INSERT INTO invites (token_hash, code_hash, kind, created_by, expires_at) "
                "VALUES ('t', 'c', 'parent_invites_student', $1, now())",
                parent,
            )
        child = await conn.fetchval(young, parent, 3)
        await conn.execute(consent, "p1", child)
        with pytest.raises(asyncpg.UniqueViolationError):  # у ребёнка один подтвердивший родитель
            await conn.execute(consent, "p2", child)
        await conn.execute("UPDATE consents SET revoked_at = now()")
        await conn.execute(consent, "p2", child)
        await conn.execute(
            "INSERT INTO homeworks (student_id, subject, tasks_total, tasks_correct, tasks_wrong, "
            "tasks_uncertain) VALUES ($1, 'math', 3, 2, 1, 0)",
            child,
        )
        with pytest.raises(asyncpg.CheckViolationError):  # родителя не удалить раньше детей 1–4
            await conn.execute("DELETE FROM users WHERE id = $1", parent)
        await conn.execute("DELETE FROM student_profiles WHERE id = $1", child)
        await conn.execute("DELETE FROM users WHERE id = $1", parent)
        assert await conn.fetchval("SELECT count(*) FROM homeworks") == 0
        assert await conn.fetchval("SELECT count(*) FROM consents") == 2  # запись согласия остаётся
    finally:
        await conn.close()
```

4. Добавить тест защиты данных и поправить тест пула:

```python
async def test_children_migration_refuses_to_drop_data(schema: str, tmp_path: Path) -> None:
    """002 пересоздаёт таблицы этапа 1 только пустыми: данные не теряются молча."""
    for name in ("001_onboarding.sql", "002_children.sql"):
        (tmp_path / name).write_text(
            (MIGRATIONS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    second = tmp_path / "002_children.sql"
    second_sql = second.read_text(encoding="utf-8")
    second.unlink()
    conn = await connect(schema)
    try:
        await apply_migrations(conn, tmp_path)
        student = await conn.fetchval(
            "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
            "VALUES ('s1', 'x', 'student') RETURNING id"
        )
        await conn.execute("INSERT INTO student_profiles (user_id, grade) VALUES ($1, 7)", student)
        second.write_text(second_sql, encoding="utf-8")
        with pytest.raises(asyncpg.RaiseError):
            await apply_migrations(conn, tmp_path)
        assert await conn.fetchval("SELECT count(*) FROM student_profiles") == 1
    finally:
        await conn.close()


async def test_create_pool_applies_migrations(schema: str) -> None:
    pool = await create_pool(database_url(), server_settings={"search_path": schema})
    try:
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == 2
    finally:
        await pool.close()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_db_migrations.py -v` (с `TEST_DATABASE_URL`)
Expected: FAIL — нет `002_children.sql` (список миграций, `student_profiles.id`, `RaiseError` не возникает).

- [ ] **Step 3: Написать миграцию**

`src/hwcheck/db/migrations/002_children.sql`:

```sql
-- Онбординг, этап 2 (спецификация §4.7, §6; решение 16.09): дети 1–4 классов пользуются Домашкой через
-- аккаунт родителя, у родителя их может быть несколько — профиль ребёнка больше не совпадает с аккаунтом MAX.
-- Таблицы этапа 1 пусты (в них ещё не писал ни один код) и пересоздаются; если данные всё же есть —
-- миграция падает, а не теряет их.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM student_profiles) OR EXISTS (SELECT 1 FROM consents)
     OR EXISTS (SELECT 1 FROM homeworks) THEN
    RAISE EXCEPTION '002_children: в student_profiles, consents или homeworks есть данные';
  END IF;
END $$;

DROP TABLE homeworks;
DROP TABLE consents;
DROP TABLE student_profiles;

CREATE TABLE student_profiles (
  id               bigserial PRIMARY KEY,
  user_id          bigint UNIQUE REFERENCES users ON DELETE CASCADE,  -- свой MAX (5–9 класс)
  parent_user_id   bigint REFERENCES users ON DELETE SET NULL,
  grade            smallint NOT NULL CHECK (grade BETWEEN 1 AND 9),
  grade_year       smallint NOT NULL,
  grade_asked_year smallint NOT NULL,
  subject          text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  -- фото присылает свой аккаунт ребёнка или родитель; удаление родителя раньше детей 1–4 упрётся сюда,
  -- а не оставит профиль без владельца
  CHECK (user_id IS NOT NULL OR parent_user_id IS NOT NULL),
  CHECK (user_id IS DISTINCT FROM parent_user_id)
);
CREATE INDEX student_profiles_parent ON student_profiles (parent_user_id);

-- юридическая запись: без FK, переживает удаление данных
CREATE TABLE consents (
  id                 bigserial PRIMARY KEY,
  parent_hash        text NOT NULL,
  student_profile_id bigint NOT NULL,
  student_hash       text,            -- хэш MAX ребёнка; NULL — фото присылает родитель
  policy_version     text NOT NULL,
  given_at           timestamptz NOT NULL,
  revoked_at         timestamptz
);
-- у ребёнка один подтвердивший родитель
CREATE UNIQUE INDEX consents_one_active ON consents (student_profile_id) WHERE revoked_at IS NULL;

CREATE TABLE homeworks (
  id               bigserial PRIMARY KEY,
  student_id       bigint NOT NULL REFERENCES student_profiles ON DELETE CASCADE,
  subject          text NOT NULL,
  tasks_total      smallint NOT NULL,
  tasks_correct    smallint NOT NULL,
  tasks_wrong      smallint NOT NULL,
  tasks_uncertain  smallint NOT NULL,
  errors_resolved  smallint NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX homeworks_student_time ON homeworks (student_id, created_at);

-- класс из ссылки родителя: ребёнок по ней не выбирает класс заново
ALTER TABLE invites ADD COLUMN grade smallint CHECK (grade BETWEEN 1 AND 9);
ALTER TABLE invites ADD CONSTRAINT invites_parent_invite_complete CHECK (
  kind <> 'parent_invites_student'
  OR (grade IS NOT NULL AND consent_given_at IS NOT NULL AND policy_version IS NOT NULL)
);
```

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest tests/test_db_migrations.py -v`
Expected: PASS (5 тестов).

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/db/migrations/002_children.sql tests/test_db_migrations.py
git commit -m "feat: миграция 002 — дети 1–4 класса без своего аккаунта, согласие на профиль"
```

---

### Task 2: Контракт хранилища профилей и реализация в памяти

**Files:**
- Create: `src/hwcheck/db/repo.py`, `src/hwcheck/db/memory.py`, `tests/test_profile_repo.py`

**Interfaces:**
- Consumes: `NewInvite`, `InviteKind` из `hwcheck.bot.invites` (этап 1).
- Produces (в `hwcheck.db.repo`):
  - `Role = Literal["student", "parent"]`; `InviteResult = Literal["ok", "expired", "used", "invalid", "role_mismatch", "has_parent"]`
  - `Account(id: int, role: Role, user_hash: str, user_id_enc: bytes)`
  - `StudentProfile(id: int, user_id: int | None, parent_user_id: int | None, grade: int, grade_year: int, subject: str | None, has_consent: bool)` + свойство `sent_by_parent: bool`
  - `Invite(token_hash: str, kind: InviteKind, created_by: int, grade: int | None, expires_at: datetime, used: bool)`
  - `LinkOutcome(result: InviteResult, profile: StudentProfile | None = None, notify: bytes | None = None)`
  - протокол `ProfileRepository` (методы ниже), `InMemoryProfileRepository` в `hwcheck.db.memory` (+ открытые для тестов `waitlist: set[tuple[str, str]]`, `consents: list`).

- [ ] **Step 1: Контрактные тесты**

`tests/test_profile_repo.py`:

```python
"""Контракт хранилища профилей (спецификация онбординга §4.4, §4.7, §6, §11).

Одни тесты для InMemoryProfileRepository и PgProfileRepository (Task 3): фейк, на котором
проверяются сценарии бота, ведёт себя как PostgreSQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from hwcheck.bot.invites import new_invite
from hwcheck.db.memory import InMemoryProfileRepository
from hwcheck.db.repo import ProfileRepository, StudentProfile

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
WEEK = timedelta(days=7)


@pytest.fixture(params=["memory"])
async def repo(request: pytest.FixtureRequest) -> AsyncIterator[ProfileRepository]:
    yield InMemoryProfileRepository()


async def student(repo: ProfileRepository, name: str = "s1", grade: int = 7) -> StudentProfile:
    profile = await repo.create_student(name, name.encode(), grade, 2026)
    assert profile is not None
    return profile


async def parent_invite(repo: ProfileRepository, profile: StudentProfile) -> str:
    """Ссылка родителю от ученика; возвращает token_hash."""
    assert profile.user_id is not None
    invite = new_invite("student_invites_parent")
    await repo.create_invite(invite, profile.user_id, NOW + WEEK)
    return invite.token_hash


async def child_invite(
    repo: ProfileRepository, parent_hash: str = "p1", grade: int = 7
) -> tuple[str, str]:
    """Ссылка ребёнку от родителя; возвращает (token_hash, code_hash)."""
    parent = await repo.get_or_create_account(parent_hash, b"p", "parent")
    invite = new_invite("parent_invites_student")
    await repo.create_invite(
        invite, parent.id, NOW + WEEK, grade=grade, policy_version="v0", consent_at=NOW
    )
    return invite.token_hash, invite.code_hash


async def test_student_account_and_profile_created_once(repo: ProfileRepository) -> None:
    profile = await student(repo)
    assert (profile.grade, profile.grade_year, profile.subject) == (7, 2026, None)
    assert not profile.has_consent and not profile.sent_by_parent
    assert await repo.create_student("s1", b"s1", 5, 2026) is None  # повторное нажатие класса
    account = await repo.get_account("s1")
    assert account is not None and (account.role, account.user_id_enc) == ("student", b"s1")
    await repo.set_subject(profile.id, "math")
    own = await repo.own_profile(account.id)
    assert own is not None and own.subject == "math"


async def test_get_or_create_keeps_existing_role(repo: ProfileRepository) -> None:
    await student(repo)
    account = await repo.get_or_create_account("s1", b"other", "parent")
    assert (account.role, account.user_id_enc) == ("student", b"s1")
    assert await repo.get_account("nobody") is None


async def test_young_children_replace_unfinished_and_keep_consented(
    repo: ProfileRepository,
) -> None:
    parent = await repo.get_or_create_account("p1", b"p", "parent")
    first = await repo.start_child_by_parent(parent.id, 2, 2026)
    assert first.sent_by_parent and first.parent_user_id == parent.id
    replaced = await repo.start_child_by_parent(parent.id, 3, 2026)  # передумал с классом
    assert [c.grade for c in await repo.children(parent.id)] == [3]
    assert await repo.give_parent_consent(replaced.id, "p1", "v0", NOW)
    assert not await repo.give_parent_consent(replaced.id, "p1", "v0", NOW)  # второе «Согласен»
    second = await repo.start_child_by_parent(parent.id, 4, 2026)
    children = await repo.children(parent.id)
    assert [(c.grade, c.has_consent) for c in children] == [(3, True), (4, False)]
    assert children[1].id == second.id


async def test_waitlist_ignores_duplicates(repo: ProfileRepository) -> None:
    await repo.add_to_waitlist("s1", "history", 7)
    await repo.add_to_waitlist("s1", "history", 7)


async def test_parent_accepts_student_invite_once(repo: ProfileRepository) -> None:
    profile = await student(repo)
    token = await parent_invite(repo, profile)
    found = await repo.find_invite(token_hash=token)
    assert found is not None
    assert (found.kind, found.grade, found.used) == ("student_invites_parent", 7, False)

    outcome = await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)
    assert (outcome.result, outcome.notify) == ("ok", b"s1")
    assert outcome.profile is not None and outcome.profile.has_consent
    parent = await repo.get_account("p1")
    assert parent is not None and parent.role == "parent"
    assert [c.id for c in await repo.children(parent.id)] == [profile.id]
    assert (await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)).result == "used"
    used = await repo.find_invite(token_hash=token)
    assert used is not None and used.used


async def test_parent_invite_refusals(repo: ProfileRepository) -> None:
    profile = await student(repo)
    token = await parent_invite(repo, profile)
    second = await parent_invite(repo, profile)  # ребёнок отправил ссылку двоим
    expired = await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW + WEEK)
    assert expired.result == "expired"
    await student(repo, "s2")
    assert (
        await repo.accept_parent_invite(token, "s2", b"s2", "v0", NOW)
    ).result == "role_mismatch"
    assert (await repo.accept_parent_invite("nope", "p1", b"p1", "v0", NOW)).result == "invalid"
    assert (await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)).result == "ok"
    assert (await repo.accept_parent_invite(second, "p2", b"p2", "v0", NOW)).result == "has_parent"
    assert await repo.get_account("p2") is None


async def test_decline_burns_invite_without_storing_parent(repo: ProfileRepository) -> None:
    profile = await student(repo)
    token = await parent_invite(repo, profile)
    outcome = await repo.decline_parent_invite(token, NOW)
    assert (outcome.result, outcome.notify) == ("ok", b"s1")
    assert (await repo.decline_parent_invite(token, NOW)).result == "used"
    assert (await repo.accept_parent_invite(token, "p1", b"p1", "v0", NOW)).result == "used"
    assert await repo.get_account("p1") is None


async def test_child_opens_parent_invite(repo: ProfileRepository) -> None:
    token, code_hash = await child_invite(repo)
    found = await repo.find_invite(code_hash=code_hash)
    assert found is not None and (found.token_hash, found.grade) == (token, 7)
    parent = await repo.get_account("p1")
    assert parent is not None
    assert [i.token_hash for i in await repo.open_child_invites(parent.id, NOW)] == [token]

    outcome = await repo.accept_child_invite(token, "c1", b"c1", 2026, NOW)
    assert (outcome.result, outcome.notify) == ("ok", b"p")
    assert outcome.profile is not None
    linked = outcome.profile
    assert (linked.grade, linked.parent_user_id, linked.has_consent) == (7, parent.id, True)
    assert await repo.open_child_invites(parent.id, NOW) == []
    assert (await repo.accept_child_invite(token, "c2", b"c2", 2026, NOW)).result == "used"


async def test_child_invite_for_registered_student_keeps_own_grade(
    repo: ProfileRepository,
) -> None:
    await student(repo, "s1", grade=8)
    token, _ = await child_invite(repo, grade=7)
    outcome = await repo.accept_child_invite(token, "s1", b"s1", 2026, NOW)
    assert outcome.result == "ok" and outcome.profile is not None
    assert outcome.profile.grade == 8


async def test_child_invite_refusals(repo: ProfileRepository) -> None:
    token, _ = await child_invite(repo, "p1")
    assert (await repo.accept_child_invite(token, "p1", b"p", 2026, NOW)).result == "role_mismatch"
    assert (await repo.accept_parent_invite(token, "x", b"x", "v0", NOW)).result == "invalid"
    late = await repo.accept_child_invite(token, "c1", b"c1", 2026, NOW + WEEK)
    assert late.result == "expired"
    assert (await repo.accept_child_invite(token, "c1", b"c1", 2026, NOW)).result == "ok"
    other, _ = await child_invite(repo, "p2")
    assert (await repo.accept_child_invite(other, "c1", b"c1", 2026, NOW)).result == "has_parent"
    parent2 = await repo.get_account("p2")
    assert parent2 is not None
    assert await repo.children(parent2.id) == []
    assert len(await repo.open_child_invites(parent2.id, NOW)) == 1  # отказ не гасит ссылку
    assert await repo.open_child_invites(parent2.id, NOW + WEEK) == []


async def test_code_attempts_limited_per_window(repo: ProfileRepository) -> None:
    window = timedelta(hours=1)
    results = [await repo.code_attempt("u1", NOW, limit=5, window=window) for _ in range(6)]
    assert results == [True] * 5 + [False]
    assert await repo.code_attempt("u2", NOW, limit=5, window=window)
    assert await repo.code_attempt("u1", NOW + window, limit=5, window=window)
```

- [ ] **Step 2: Тесты падают**

Run: `uv run pytest tests/test_profile_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: hwcheck.db.memory`.

- [ ] **Step 3: Протокол и модели**

`src/hwcheck/db/repo.py`:

```python
"""Профили детей, согласия и приглашения (спецификация онбординга §4, §6, §7).

`ProfileRepository` — всё, что онбордингу нужно от хранилища. `PgProfileRepository` — PostgreSQL;
`InMemoryProfileRepository` (db/memory.py) — для сценарных тестов. Обе реализации проходят одни
контрактные тесты (tests/test_profile_repo.py). Всё, что решает исход гонки (погашение приглашения,
один родитель у ребёнка), делается в одной транзакции с блокировкой строк.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from hwcheck.bot.invites import InviteKind, NewInvite

Role = Literal["student", "parent"]
# исход открытия приглашения — поле result события invite_opened (§12)
InviteResult = Literal["ok", "expired", "used", "invalid", "role_mismatch", "has_parent"]


@dataclass(frozen=True)
class Account:
    id: int
    role: Role
    user_hash: str
    user_id_enc: bytes  # Fernet(USER_ID_KEY): написать пользователю первым


@dataclass(frozen=True)
class StudentProfile:
    id: int
    user_id: int | None  # свой MAX ребёнка (5–9 класс); None — фото присылает родитель
    parent_user_id: int | None
    grade: int
    grade_year: int
    subject: str | None
    has_consent: bool

    @property
    def sent_by_parent(self) -> bool:
        return self.user_id is None


@dataclass(frozen=True)
class Invite:
    token_hash: str
    kind: InviteKind
    created_by: int
    grade: int | None  # класс ребёнка: из ссылки родителя или из профиля ученика
    expires_at: datetime
    used: bool


@dataclass(frozen=True)
class LinkOutcome:
    """Итог погашения приглашения. `profile` — ребёнок после привязки; `notify` — шифротекст id
    второй стороны, которой бот пишет о результате."""

    result: InviteResult
    profile: StudentProfile | None = None
    notify: bytes | None = None


class ProfileRepository(Protocol):
    async def get_account(self, user_hash: str) -> Account | None: ...

    async def get_or_create_account(
        self, user_hash: str, user_id_enc: bytes, role: Role
    ) -> Account:
        """Новый аккаунт с ролью или существующий — со своей ролью (её проверяет вызывающий)."""
        ...

    async def create_student(
        self, user_hash: str, user_id_enc: bytes, grade: int, year: int
    ) -> StudentProfile | None:
        """Аккаунт ученика и профиль одной транзакцией; аккаунт уже есть — None."""
        ...

    async def own_profile(self, user_id: int) -> StudentProfile | None: ...

    async def children(self, parent_user_id: int) -> list[StudentProfile]:
        """Дети родителя в порядке добавления: и со своим MAX, и 1–4 класса."""
        ...

    async def start_child_by_parent(
        self, parent_user_id: int, grade: int, year: int
    ) -> StudentProfile:
        """Ребёнок 1–4 класса; незавершённый (без согласия) заменяется, а не копится."""
        ...

    async def set_subject(self, profile_id: int, subject: str) -> None: ...

    async def add_to_waitlist(self, user_hash: str, subject: str, grade: int) -> None: ...

    async def give_parent_consent(
        self, profile_id: int, parent_hash: str, policy_version: str, now: datetime
    ) -> bool:
        """Согласие на ребёнка 1–4 класса; активное уже есть — False."""
        ...

    async def create_invite(
        self,
        invite: NewInvite,
        created_by: int,
        expires_at: datetime,
        *,
        grade: int | None = None,
        policy_version: str | None = None,
        consent_at: datetime | None = None,
    ) -> None: ...

    async def find_invite(
        self, *, token_hash: str | None = None, code_hash: str | None = None
    ) -> Invite | None: ...

    async def open_child_invites(self, parent_user_id: int, now: datetime) -> list[Invite]:
        """Непогашенные живые ссылки родителя ребёнку 5–9 класса."""
        ...

    async def accept_parent_invite(
        self,
        token_hash: str,
        parent_hash: str,
        parent_id_enc: bytes,
        policy_version: str,
        now: datetime,
    ) -> LinkOutcome:
        """Родитель согласился по ссылке ученика: аккаунт родителя, связка, согласие, погашение."""
        ...

    async def decline_parent_invite(self, token_hash: str, now: datetime) -> LinkOutcome:
        """Родитель отказал: ссылка погашена, родитель не сохраняется."""
        ...

    async def accept_child_invite(
        self, token_hash: str, child_hash: str, child_id_enc: bytes, year: int, now: datetime
    ) -> LinkOutcome:
        """Ребёнок открыл ссылку родителя: аккаунт и профиль (класс из ссылки), связка, согласие."""
        ...

    async def code_attempt(
        self, user_hash: str, now: datetime, *, limit: int, window: timedelta
    ) -> bool:
        """Учитывает попытку ввода кода; False — лимит за окно исчерпан, попытка не записана."""
        ...
```

- [ ] **Step 4: Реализация в памяти**

`src/hwcheck/db/memory.py`:

```python
"""Хранилище профилей в памяти процесса — для сценарных тестов онбординга.

Повторяет поведение PgProfileRepository, включая исходы гонок; расхождение ловят контрактные тесты
tests/test_profile_repo.py, которые гоняют обе реализации.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from hwcheck.bot.invites import InviteKind, NewInvite
from hwcheck.db.repo import Account, Invite, InviteResult, LinkOutcome, Role, StudentProfile


@dataclass
class _Profile:
    id: int
    user_id: int | None
    parent_user_id: int | None
    grade: int
    grade_year: int
    subject: str | None = None


@dataclass
class _Invite:
    token_hash: str
    code_hash: str
    kind: InviteKind
    created_by: int
    expires_at: datetime
    grade: int | None
    policy_version: str | None
    consent_at: datetime | None
    used_at: datetime | None = None


@dataclass
class _Consent:
    parent_hash: str
    profile_id: int
    student_hash: str | None
    policy_version: str
    given_at: datetime
    revoked_at: datetime | None = None


class InMemoryProfileRepository:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._profiles: dict[int, _Profile] = {}
        self._invites: dict[str, _Invite] = {}
        self._attempts: dict[str, list[datetime]] = {}
        self._last_id = 0
        self.consents: list[_Consent] = []
        self.waitlist: set[tuple[str, str]] = set()

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _account_by_id(self, account_id: int) -> Account:
        return next(a for a in self._accounts.values() if a.id == account_id)

    def _own(self, user_id: int) -> _Profile | None:
        return next((p for p in self._profiles.values() if p.user_id == user_id), None)

    def _has_consent(self, profile_id: int) -> bool:
        return any(c.profile_id == profile_id and c.revoked_at is None for c in self.consents)

    def _view(self, profile: _Profile) -> StudentProfile:
        return StudentProfile(
            id=profile.id,
            user_id=profile.user_id,
            parent_user_id=profile.parent_user_id,
            grade=profile.grade,
            grade_year=profile.grade_year,
            subject=profile.subject,
            has_consent=self._has_consent(profile.id),
        )

    async def get_account(self, user_hash: str) -> Account | None:
        return self._accounts.get(user_hash)

    async def get_or_create_account(
        self, user_hash: str, user_id_enc: bytes, role: Role
    ) -> Account:
        if user_hash not in self._accounts:
            self._accounts[user_hash] = Account(self._next_id(), role, user_hash, user_id_enc)
        return self._accounts[user_hash]

    async def create_student(
        self, user_hash: str, user_id_enc: bytes, grade: int, year: int
    ) -> StudentProfile | None:
        if user_hash in self._accounts:
            return None
        account = await self.get_or_create_account(user_hash, user_id_enc, "student")
        profile = _Profile(self._next_id(), account.id, None, grade, year)
        self._profiles[profile.id] = profile
        return self._view(profile)

    async def own_profile(self, user_id: int) -> StudentProfile | None:
        profile = self._own(user_id)
        return self._view(profile) if profile is not None else None

    async def children(self, parent_user_id: int) -> list[StudentProfile]:
        ordered = sorted(self._profiles.values(), key=lambda p: p.id)
        return [self._view(p) for p in ordered if p.parent_user_id == parent_user_id]

    async def start_child_by_parent(
        self, parent_user_id: int, grade: int, year: int
    ) -> StudentProfile:
        for profile in list(self._profiles.values()):
            unfinished = profile.user_id is None and not self._has_consent(profile.id)
            if profile.parent_user_id == parent_user_id and unfinished:
                del self._profiles[profile.id]
        profile = _Profile(self._next_id(), None, parent_user_id, grade, year)
        self._profiles[profile.id] = profile
        return self._view(profile)

    async def set_subject(self, profile_id: int, subject: str) -> None:
        if profile_id in self._profiles:
            self._profiles[profile_id].subject = subject

    async def add_to_waitlist(self, user_hash: str, subject: str, grade: int) -> None:
        self.waitlist.add((user_hash, subject))

    async def give_parent_consent(
        self, profile_id: int, parent_hash: str, policy_version: str, now: datetime
    ) -> bool:
        if self._has_consent(profile_id):
            return False
        self.consents.append(_Consent(parent_hash, profile_id, None, policy_version, now))
        return True

    async def create_invite(
        self,
        invite: NewInvite,
        created_by: int,
        expires_at: datetime,
        *,
        grade: int | None = None,
        policy_version: str | None = None,
        consent_at: datetime | None = None,
    ) -> None:
        self._invites[invite.token_hash] = _Invite(
            invite.token_hash,
            invite.code_hash,
            invite.kind,
            created_by,
            expires_at,
            grade,
            policy_version,
            consent_at,
        )

    def _invite_view(self, invite: _Invite) -> Invite:
        grade = invite.grade
        if grade is None:
            own = self._own(invite.created_by)
            grade = own.grade if own is not None else None
        used = invite.used_at is not None
        return Invite(
            invite.token_hash, invite.kind, invite.created_by, grade, invite.expires_at, used
        )

    async def find_invite(
        self, *, token_hash: str | None = None, code_hash: str | None = None
    ) -> Invite | None:
        for invite in self._invites.values():
            if (token_hash is not None and invite.token_hash == token_hash) or (
                code_hash is not None and invite.code_hash == code_hash
            ):
                return self._invite_view(invite)
        return None

    async def open_child_invites(self, parent_user_id: int, now: datetime) -> list[Invite]:
        return [
            self._invite_view(i)
            for i in sorted(self._invites.values(), key=lambda i: i.expires_at)
            if i.created_by == parent_user_id
            and i.kind == "parent_invites_student"
            and i.used_at is None
            and i.expires_at > now
        ]

    def _usable(
        self, token_hash: str, kind: InviteKind, now: datetime
    ) -> tuple[_Invite | None, InviteResult]:
        invite = self._invites.get(token_hash)
        if invite is None or invite.kind != kind:
            return None, "invalid"
        if invite.used_at is not None:
            return None, "used"
        if invite.expires_at <= now:
            return None, "expired"
        return invite, "ok"

    async def accept_parent_invite(
        self,
        token_hash: str,
        parent_hash: str,
        parent_id_enc: bytes,
        policy_version: str,
        now: datetime,
    ) -> LinkOutcome:
        invite, result = self._usable(token_hash, "student_invites_parent", now)
        if invite is None:
            return LinkOutcome(result)
        existing = self._accounts.get(parent_hash)
        if existing is not None and existing.role != "parent":
            return LinkOutcome("role_mismatch")
        profile = self._own(invite.created_by)
        if profile is None:
            return LinkOutcome("invalid")
        if profile.parent_user_id is not None:
            return LinkOutcome("has_parent")
        parent = await self.get_or_create_account(parent_hash, parent_id_enc, "parent")
        child = self._account_by_id(invite.created_by)
        profile.parent_user_id = parent.id
        consent = _Consent(parent_hash, profile.id, child.user_hash, policy_version, now)
        self.consents.append(consent)
        invite.used_at = now
        return LinkOutcome("ok", self._view(profile), child.user_id_enc)

    async def decline_parent_invite(self, token_hash: str, now: datetime) -> LinkOutcome:
        invite, result = self._usable(token_hash, "student_invites_parent", now)
        if invite is None:
            return LinkOutcome(result)
        invite.used_at = now
        return LinkOutcome("ok", notify=self._account_by_id(invite.created_by).user_id_enc)

    async def accept_child_invite(
        self, token_hash: str, child_hash: str, child_id_enc: bytes, year: int, now: datetime
    ) -> LinkOutcome:
        invite, result = self._usable(token_hash, "parent_invites_student", now)
        if invite is None:
            return LinkOutcome(result)
        if invite.grade is None or invite.policy_version is None or invite.consent_at is None:
            return LinkOutcome("invalid")
        existing = self._accounts.get(child_hash)
        if existing is not None and existing.role != "student":
            return LinkOutcome("role_mismatch")
        parent = self._account_by_id(invite.created_by)
        if existing is None:
            account = await self.get_or_create_account(child_hash, child_id_enc, "student")
            profile = _Profile(self._next_id(), account.id, parent.id, invite.grade, year)
            self._profiles[profile.id] = profile
        else:
            own = self._own(existing.id)
            if own is None:
                return LinkOutcome("invalid")
            if own.parent_user_id is not None:
                return LinkOutcome("has_parent")
            own.parent_user_id = parent.id
            profile = own
        consent = _Consent(
            parent.user_hash, profile.id, child_hash, invite.policy_version, invite.consent_at
        )
        self.consents.append(consent)
        invite.used_at = now
        return LinkOutcome("ok", self._view(profile), parent.user_id_enc)

    async def code_attempt(
        self, user_hash: str, now: datetime, *, limit: int, window: timedelta
    ) -> bool:
        recent = [t for t in self._attempts.get(user_hash, []) if t > now - window]
        if len(recent) >= limit:
            self._attempts[user_hash] = recent
            return False
        self._attempts[user_hash] = [*recent, now]
        return True
```

- [ ] **Step 5: Тесты и проверки проходят**

Run: `uv run pytest tests/test_profile_repo.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS (12 тестов), mypy и ruff без ошибок.

- [ ] **Step 6: Commit**

```bash
git add src/hwcheck/db/repo.py src/hwcheck/db/memory.py tests/test_profile_repo.py
git commit -m "feat: контракт хранилища профилей и реализация в памяти"
```

---

### Task 3: `PgProfileRepository` проходит тот же контракт

**Files:**
- Modify: `src/hwcheck/db/repo.py` (дописать реализацию), `tests/test_profile_repo.py` (фикстура на обе реализации)

**Interfaces:**
- Consumes: протокол и модели Task 2; `create_pool` (этап 1); схема Task 1.
- Produces: `PgProfileRepository(pool: asyncpg.Pool[asyncpg.Record])`, реализующий `ProfileRepository`.

- [ ] **Step 1: Фикстура гоняет контракт на PostgreSQL**

В `tests/test_profile_repo.py` добавить импорты `import os`, `import uuid`, `import asyncpg`, `from hwcheck.db.pool import create_pool`, `from hwcheck.db.repo import PgProfileRepository` и заменить фикстуру:

```python
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "postgres"])
async def repo(request: pytest.FixtureRequest) -> AsyncIterator[ProfileRepository]:
    if request.param == "memory":
        yield InMemoryProfileRepository()
        return
    if TEST_DATABASE_URL is None:
        if "CI" in os.environ:
            pytest.fail("в CI нужен TEST_DATABASE_URL")
        pytest.skip("нужен TEST_DATABASE_URL (PostgreSQL)")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await create_pool(TEST_DATABASE_URL, server_settings={"search_path": schema})
    try:
        yield PgProfileRepository(pool)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
```

- [ ] **Step 2: Тесты на PostgreSQL падают**

Run: `uv run pytest tests/test_profile_repo.py -v` (с `TEST_DATABASE_URL`)
Expected: FAIL — `ImportError: cannot import name 'PgProfileRepository'`.

- [ ] **Step 3: Реализация**

В `src/hwcheck/db/repo.py` расширить импорты:

```python
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

import asyncpg

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy
```

и дописать в конец файла:

```python
_PROFILE = (
    "SELECT p.id, p.user_id, p.parent_user_id, p.grade, p.grade_year, p.subject, "
    "EXISTS (SELECT 1 FROM consents c WHERE c.student_profile_id = p.id "
    "AND c.revoked_at IS NULL) AS has_consent FROM student_profiles p"
)
# класс ссылки ученика — из его профиля, ссылки родителя — из самой ссылки
_INVITE = (
    "SELECT i.token_hash, i.kind, i.created_by, COALESCE(i.grade, p.grade) AS grade, "
    "i.expires_at, i.used_at IS NOT NULL AS used FROM invites i "
    "LEFT JOIN student_profiles p ON p.user_id = i.created_by"
)
_CONSENT = (
    "INSERT INTO consents (parent_hash, student_profile_id, student_hash, policy_version, "
    "given_at) VALUES ($1, $2, $3, $4, $5)"
)

# строкой: PoolConnectionProxy не параметризуется во время выполнения
Connection: TypeAlias = "PoolConnectionProxy[asyncpg.Record]"


def _account(row: asyncpg.Record) -> Account:
    return Account(
        id=row["id"],
        role=row["role"],
        user_hash=row["max_user_hash"],
        user_id_enc=bytes(row["max_user_id_enc"]),
    )


def _profile(row: asyncpg.Record) -> StudentProfile:
    return StudentProfile(
        id=row["id"],
        user_id=row["user_id"],
        parent_user_id=row["parent_user_id"],
        grade=row["grade"],
        grade_year=row["grade_year"],
        subject=row["subject"],
        has_consent=row["has_consent"],
    )


def _invite(row: asyncpg.Record) -> Invite:
    return Invite(
        token_hash=row["token_hash"],
        kind=row["kind"],
        created_by=row["created_by"],
        grade=row["grade"],
        expires_at=row["expires_at"],
        used=row["used"],
    )


async def _profile_by_id(conn: Connection, profile_id: int) -> StudentProfile:
    row = await conn.fetchrow(f"{_PROFILE} WHERE p.id = $1", profile_id)
    assert row is not None
    return _profile(row)


async def _get_or_create(
    conn: Connection, user_hash: str, user_id_enc: bytes, role: Role
) -> Account:
    await conn.execute(
        "INSERT INTO users (max_user_hash, max_user_id_enc, role) VALUES ($1, $2, $3) "
        "ON CONFLICT (max_user_hash) DO NOTHING",
        user_hash,
        user_id_enc,
        role,
    )
    row = await conn.fetchrow("SELECT * FROM users WHERE max_user_hash = $1", user_hash)
    assert row is not None
    return _account(row)


async def _lock_usable(
    conn: Connection, token_hash: str, kind: InviteKind, now: datetime
) -> tuple[asyncpg.Record | None, InviteResult]:
    """Приглашение под блокировкой до конца транзакции: второй родитель ждёт итога первого."""
    row = await conn.fetchrow("SELECT * FROM invites WHERE token_hash = $1 FOR UPDATE", token_hash)
    if row is None or row["kind"] != kind:
        return None, "invalid"
    if row["used_at"] is not None:
        return None, "used"
    if row["expires_at"] <= now:
        return None, "expired"
    return row, "ok"


class PgProfileRepository:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self._pool = pool

    async def get_account(self, user_hash: str) -> Account | None:
        row = await self._pool.fetchrow("SELECT * FROM users WHERE max_user_hash = $1", user_hash)
        return _account(row) if row is not None else None

    async def get_or_create_account(
        self, user_hash: str, user_id_enc: bytes, role: Role
    ) -> Account:
        async with self._pool.acquire() as conn:
            return await _get_or_create(conn, user_hash, user_id_enc, role)

    async def create_student(
        self, user_hash: str, user_id_enc: bytes, grade: int, year: int
    ) -> StudentProfile | None:
        async with self._pool.acquire() as conn, conn.transaction():
            user_id = await conn.fetchval(
                "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
                "VALUES ($1, $2, 'student') ON CONFLICT (max_user_hash) DO NOTHING RETURNING id",
                user_hash,
                user_id_enc,
            )
            if user_id is None:
                return None
            profile_id = await conn.fetchval(
                "INSERT INTO student_profiles (user_id, grade, grade_year, grade_asked_year) "
                "VALUES ($1, $2, $3, $3) RETURNING id",
                user_id,
                grade,
                year,
            )
            return await _profile_by_id(conn, profile_id)

    async def own_profile(self, user_id: int) -> StudentProfile | None:
        row = await self._pool.fetchrow(f"{_PROFILE} WHERE p.user_id = $1", user_id)
        return _profile(row) if row is not None else None

    async def children(self, parent_user_id: int) -> list[StudentProfile]:
        rows = await self._pool.fetch(
            f"{_PROFILE} WHERE p.parent_user_id = $1 ORDER BY p.id", parent_user_id
        )
        return [_profile(row) for row in rows]

    async def start_child_by_parent(
        self, parent_user_id: int, grade: int, year: int
    ) -> StudentProfile:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM student_profiles p WHERE p.parent_user_id = $1 AND p.user_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM consents c WHERE c.student_profile_id = p.id "
                "AND c.revoked_at IS NULL)",
                parent_user_id,
            )
            profile_id = await conn.fetchval(
                "INSERT INTO student_profiles (parent_user_id, grade, grade_year, "
                "grade_asked_year) VALUES ($1, $2, $3, $3) RETURNING id",
                parent_user_id,
                grade,
                year,
            )
            return await _profile_by_id(conn, profile_id)

    async def set_subject(self, profile_id: int, subject: str) -> None:
        await self._pool.execute(
            "UPDATE student_profiles SET subject = $2 WHERE id = $1", profile_id, subject
        )

    async def add_to_waitlist(self, user_hash: str, subject: str, grade: int) -> None:
        await self._pool.execute(
            "INSERT INTO subject_waitlist (user_hash, subject, grade) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            user_hash,
            subject,
            grade,
        )

    async def give_parent_consent(
        self, profile_id: int, parent_hash: str, policy_version: str, now: datetime
    ) -> bool:
        status = await self._pool.execute(
            "INSERT INTO consents (parent_hash, student_profile_id, policy_version, given_at) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (student_profile_id) WHERE revoked_at IS NULL DO NOTHING",
            parent_hash,
            profile_id,
            policy_version,
            now,
        )
        return bool(status == "INSERT 0 1")

    async def create_invite(
        self,
        invite: NewInvite,
        created_by: int,
        expires_at: datetime,
        *,
        grade: int | None = None,
        policy_version: str | None = None,
        consent_at: datetime | None = None,
    ) -> None:
        await self._pool.execute(
            "INSERT INTO invites (token_hash, code_hash, kind, created_by, grade, "
            "consent_given_at, policy_version, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            invite.token_hash,
            invite.code_hash,
            invite.kind,
            created_by,
            grade,
            consent_at,
            policy_version,
            expires_at,
        )

    async def find_invite(
        self, *, token_hash: str | None = None, code_hash: str | None = None
    ) -> Invite | None:
        if token_hash is not None:
            row = await self._pool.fetchrow(f"{_INVITE} WHERE i.token_hash = $1", token_hash)
        elif code_hash is not None:
            row = await self._pool.fetchrow(f"{_INVITE} WHERE i.code_hash = $1", code_hash)
        else:
            return None
        return _invite(row) if row is not None else None

    async def open_child_invites(self, parent_user_id: int, now: datetime) -> list[Invite]:
        rows = await self._pool.fetch(
            f"{_INVITE} WHERE i.created_by = $1 AND i.kind = 'parent_invites_student' "
            "AND i.used_at IS NULL AND i.expires_at > $2 ORDER BY i.expires_at",
            parent_user_id,
            now,
        )
        return [_invite(row) for row in rows]

    async def accept_parent_invite(
        self,
        token_hash: str,
        parent_hash: str,
        parent_id_enc: bytes,
        policy_version: str,
        now: datetime,
    ) -> LinkOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            invite, result = await _lock_usable(conn, token_hash, "student_invites_parent", now)
            if invite is None:
                return LinkOutcome(result)
            role = await conn.fetchval(
                "SELECT role FROM users WHERE max_user_hash = $1", parent_hash
            )
            if role is not None and role != "parent":
                return LinkOutcome("role_mismatch")
            profile = await conn.fetchrow(
                "SELECT id, parent_user_id FROM student_profiles WHERE user_id = $1 FOR UPDATE",
                invite["created_by"],
            )
            if profile is None:
                return LinkOutcome("invalid")
            if profile["parent_user_id"] is not None:
                return LinkOutcome("has_parent")
            parent = await _get_or_create(conn, parent_hash, parent_id_enc, "parent")
            child = await conn.fetchrow(
                "SELECT max_user_hash, max_user_id_enc FROM users WHERE id = $1",
                invite["created_by"],
            )
            assert child is not None
            await conn.execute(
                "UPDATE student_profiles SET parent_user_id = $1 WHERE id = $2",
                parent.id,
                profile["id"],
            )
            await conn.execute(
                _CONSENT, parent_hash, profile["id"], child["max_user_hash"], policy_version, now
            )
            await conn.execute(
                "UPDATE invites SET used_at = $1, used_by = $2 WHERE token_hash = $3",
                now,
                parent.id,
                token_hash,
            )
            linked = await _profile_by_id(conn, profile["id"])
            return LinkOutcome("ok", linked, bytes(child["max_user_id_enc"]))

    async def decline_parent_invite(self, token_hash: str, now: datetime) -> LinkOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            invite, result = await _lock_usable(conn, token_hash, "student_invites_parent", now)
            if invite is None:
                return LinkOutcome(result)
            await conn.execute(
                "UPDATE invites SET used_at = $1 WHERE token_hash = $2", now, token_hash
            )
            child_enc = await conn.fetchval(
                "SELECT max_user_id_enc FROM users WHERE id = $1", invite["created_by"]
            )
            return LinkOutcome("ok", notify=bytes(child_enc))

    async def accept_child_invite(
        self, token_hash: str, child_hash: str, child_id_enc: bytes, year: int, now: datetime
    ) -> LinkOutcome:
        async with self._pool.acquire() as conn, conn.transaction():
            invite, result = await _lock_usable(conn, token_hash, "parent_invites_student", now)
            if invite is None:
                return LinkOutcome(result)
            existing = await conn.fetchrow(
                "SELECT id, role FROM users WHERE max_user_hash = $1", child_hash
            )
            if existing is not None and existing["role"] != "student":
                return LinkOutcome("role_mismatch")
            if existing is None:
                child_id = await conn.fetchval(
                    "INSERT INTO users (max_user_hash, max_user_id_enc, role) "
                    "VALUES ($1, $2, 'student') RETURNING id",
                    child_hash,
                    child_id_enc,
                )
                profile_id = await conn.fetchval(
                    "INSERT INTO student_profiles (user_id, parent_user_id, grade, grade_year, "
                    "grade_asked_year) VALUES ($1, $2, $3, $4, $4) RETURNING id",
                    child_id,
                    invite["created_by"],
                    invite["grade"],
                    year,
                )
            else:
                child_id = existing["id"]
                own = await conn.fetchrow(
                    "SELECT id, parent_user_id FROM student_profiles WHERE user_id = $1 FOR UPDATE",
                    child_id,
                )
                if own is None:
                    return LinkOutcome("invalid")
                if own["parent_user_id"] is not None:
                    return LinkOutcome("has_parent")
                profile_id = own["id"]
                await conn.execute(
                    "UPDATE student_profiles SET parent_user_id = $1 WHERE id = $2",
                    invite["created_by"],
                    profile_id,
                )
            parent = await conn.fetchrow(
                "SELECT max_user_hash, max_user_id_enc FROM users WHERE id = $1",
                invite["created_by"],
            )
            assert parent is not None
            await conn.execute(
                _CONSENT,
                parent["max_user_hash"],
                profile_id,
                child_hash,
                invite["policy_version"],
                invite["consent_given_at"],
            )
            await conn.execute(
                "UPDATE invites SET used_at = $1, used_by = $2 WHERE token_hash = $3",
                now,
                child_id,
                token_hash,
            )
            linked = await _profile_by_id(conn, profile_id)
            return LinkOutcome("ok", linked, bytes(parent["max_user_id_enc"]))

    async def code_attempt(
        self, user_hash: str, now: datetime, *, limit: int, window: timedelta
    ) -> bool:
        # апдейты обрабатываются последовательно (runner.py) — гонки попыток одного пользователя нет
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM login_attempts WHERE user_hash = $1 AND attempted_at <= $2",
                user_hash,
                now - window,
            )
            count = await conn.fetchval(
                "SELECT count(*) FROM login_attempts WHERE user_hash = $1", user_hash
            )
            if count >= limit:
                return False
            await conn.execute(
                "INSERT INTO login_attempts (user_hash, attempted_at) VALUES ($1, $2)",
                user_hash,
                now,
            )
            return True
```

- [ ] **Step 4: Тесты на обеих реализациях проходят**

Run: `uv run pytest tests/test_profile_repo.py tests/test_db_migrations.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS — 24 теста контракта (12 × memory, postgres) и миграции; mypy, ruff чистые.

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/db/repo.py tests/test_profile_repo.py
git commit -m "feat: PgProfileRepository — транзакции согласия, привязки и лимита кода"
```


---

### Task 4: Метка `bot_started`, сообщение по id пользователя, флаг и временное состояние онбординга

**Files:**
- Modify: `src/hwcheck/bot/models.py`, `src/hwcheck/bot/max_api.py`, `src/hwcheck/config.py`
- Create: `src/hwcheck/bot/onboarding/__init__.py`, `src/hwcheck/bot/onboarding/state.py`, `tests/test_max_client.py`, `tests/test_onboarding_state.py`

**Interfaces:**
- Consumes: `MaxClient`, `MaxUpdate`, `Settings` (текущие).
- Produces:
  - `MaxUpdate.payload: str | None` — метка `?start=` из `bot_started`
  - `MaxClient.send_to_user(user_id: int, text: str, *, buttons: Buttons | None = None) -> None`
  - `Settings.onboarding_required: bool = False`
  - в `hwcheck.bot.onboarding.state`: `OnboardingState(pending_invite: str | None, pending_photos: list[str], pending_at: float | None, child_id: int | None, child_chosen_at: float | None)`, протокол `OnboardingStateStore` (`get(user_hash) -> OnboardingState`, `set(user_hash, state)`), `InMemoryOnboardingStateStore`, `RedisOnboardingStateStore(client, *, ttl_s=24*3600)` с ключом `onb:<хэш>`.

- [ ] **Step 1: Тесты клиента MAX**

`tests/test_max_client.py`:

```python
"""Клиент MAX: сообщение пользователю по id (вторая сторона связки, спецификация §4.2) и метка
`bot_started` (deep link `?start=`, §4.4)."""

import json

import httpx

from hwcheck.bot.max_api import MaxClient, callback_button
from hwcheck.bot.models import MaxUpdate


async def test_send_to_user_uses_user_id_and_send_message_chat_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with MaxClient("token") as client:
        await client._http.aclose()
        client._http = httpx.AsyncClient(
            base_url="https://max.test", transport=httpx.MockTransport(handler)
        )
        await client.send_to_user(42, "привет", buttons=[[callback_button("Да", "ob:accept")]])
        await client.send_message(7, "в чат")

    assert [dict(r.url.params) for r in requests] == [{"user_id": "42"}, {"chat_id": "7"}]
    body = json.loads(requests[0].content)
    assert body["text"] == "привет"
    assert body["attachments"][0]["payload"]["buttons"][0][0]["payload"] == "ob:accept"


def test_bot_started_payload_is_parsed() -> None:
    update = MaxUpdate.model_validate(
        {
            "update_type": "bot_started",
            "chat_id": 70,
            "user": {"user_id": 7},
            "payload": "p_abcdefghijklmnopqrstuv",
            "user_locale": "ru",
        }
    )
    assert update.payload == "p_abcdefghijklmnopqrstuv"
    assert (update.effective_chat_id, update.effective_user_id) == (70, 7)
    assert MaxUpdate.model_validate({"update_type": "bot_started"}).payload is None
```

- [ ] **Step 2: Тесты состояния онбординга**

`tests/test_onboarding_state.py`:

```python
"""Временное состояние онбординга в Redis (спецификация §4): TTL, ключ без id, сброс битого."""

import fakeredis

from hwcheck.bot.onboarding.state import (
    InMemoryOnboardingStateStore,
    OnboardingState,
    RedisOnboardingStateStore,
)


def sample() -> OnboardingState:
    return OnboardingState(
        pending_invite="t" * 64,
        pending_photos=["https://files/1.jpg"],
        pending_at=1_789_300_000.0,
        child_id=5,
        child_chosen_at=1_789_300_100.0,
    )


async def test_state_survives_new_client_and_has_ttl() -> None:
    server = fakeredis.FakeServer()
    client = fakeredis.FakeAsyncRedis(server=server)
    await RedisOnboardingStateStore(client, ttl_s=3600).set("abc123", sample())
    restored = await RedisOnboardingStateStore(fakeredis.FakeAsyncRedis(server=server)).get(
        "abc123"
    )
    assert restored == sample()
    assert [k.decode() for k in await client.keys("*")] == ["onb:abc123"]
    assert 0 < await client.ttl("onb:abc123") <= 3600


async def test_missing_or_broken_state_is_empty() -> None:
    client = fakeredis.FakeAsyncRedis()
    store = RedisOnboardingStateStore(client)
    assert await store.get("nobody") == OnboardingState()
    await client.set("onb:broken", '{"pending_photos": "не список"}')
    assert await store.get("broken") == OnboardingState()


async def test_in_memory_store() -> None:
    store = InMemoryOnboardingStateStore()
    assert await store.get("u") == OnboardingState()
    await store.set("u", sample())
    assert await store.get("u") == sample()
```

- [ ] **Step 3: Тесты падают**

Run: `uv run pytest tests/test_max_client.py tests/test_onboarding_state.py -v`
Expected: FAIL — нет `send_to_user`, `payload`, модуля `hwcheck.bot.onboarding.state`.

- [ ] **Step 4: Модель, клиент, настройка**

В `src/hwcheck/bot/models.py`, класс `MaxUpdate`, после `user: MaxUser | None = None`:

```python
    # метка deep link `?start=` из bot_started (приглашение родителя/ребёнка, онбординг §4.4)
    payload: str | None = None
```

В `src/hwcheck/bot/max_api.py` заменить `send_message` на пару методов с общим телом:

```python
async def send_message(
    self,
    chat_id: int,
    text: str,
    *,
    buttons: Buttons | None = None,
) -> None:
    await self._post_message({"chat_id": chat_id}, text, buttons)


async def send_to_user(
    self,
    user_id: int,
    text: str,
    *,
    buttons: Buttons | None = None,
) -> None:
    """Сообщение пользователю по id MAX, а не в чат, где идёт диалог: второй стороне связки
    «ребёнок ↔ родитель» (онбординг §4.2)."""
    await self._post_message({"user_id": user_id}, text, buttons)


async def _post_message(self, params: dict[str, int], text: str, buttons: Buttons | None) -> None:
    body: dict[str, Any] = {"text": text}
    if buttons:
        body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
    response = await self._http.post("/messages", params=params, json=body)
    response.raise_for_status()
```

В `src/hwcheck/config.py` после `database_url`:

```python
    # онбординг и согласие родителя до проверки (спецификация 2026-09-14); false — аварийный
    # выключатель: бот проверяет фото без онбординга, как до этапа 2
    onboarding_required: bool = False
```

- [ ] **Step 5: Пакет и состояние**

`src/hwcheck/bot/onboarding/__init__.py`:

```python
"""Онбординг: роль, класс, предмет, связка «ребёнок ↔ родитель», согласие (спецификация 2026-09-14).

Точка входа — `router.Onboarding`; здесь ничего не импортируется, чтобы шаги не зацикливались.
"""
```

`src/hwcheck/bot/onboarding/state.py`:

```python
"""Временное состояние онбординга (спецификация §4, §4.7): Redis, TTL 24 ч, ключ — хэш пользователя.

Здесь то, чему не место в PostgreSQL: ссылка ребёнка, открытая родителем, до «Согласен»/«Отказать»;
фото родителя до ответа «Чья домашка?»; выбранный ребёнок на время одной домашки.
"""

import logging
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

ONBOARDING_TTL_S = 24 * 3600


class OnboardingState(BaseModel):
    pending_invite: str | None = None  # token_hash ссылки ученика, открытой родителем
    pending_photos: list[str] = Field(default_factory=list)  # фото родителя до «Чья домашка?»
    pending_at: float | None = None  # когда пришло первое из этих фото
    child_id: int | None = None  # ребёнок 1–4 класса, выбранный для текущей домашки
    child_chosen_at: float | None = None


class OnboardingStateStore(Protocol):
    async def get(self, user_hash: str) -> OnboardingState: ...

    async def set(self, user_hash: str, state: OnboardingState) -> None: ...


class InMemoryOnboardingStateStore:
    def __init__(self) -> None:
        self._states: dict[str, OnboardingState] = {}

    async def get(self, user_hash: str) -> OnboardingState:
        return self._states.get(user_hash, OnboardingState())

    async def set(self, user_hash: str, state: OnboardingState) -> None:
        self._states[user_hash] = state


class RedisOnboardingStateStore:
    """JSON в `onb:<хэш пользователя>` с TTL, продлевается при записи; сырой id MAX не хранится."""

    def __init__(self, client: Redis, *, ttl_s: int = ONBOARDING_TTL_S) -> None:
        self._client = client
        self._ttl_s = ttl_s

    async def get(self, user_hash: str) -> OnboardingState:
        raw = await self._client.get(f"onb:{user_hash}")
        if raw is None:
            return OnboardingState()
        try:
            return OnboardingState.model_validate_json(raw)
        except ValidationError:
            # схема поменялась между деплоями: пользователь откроет ссылку ещё раз
            logger.warning("onboarding state unreadable, reset")
            return OnboardingState()

    async def set(self, user_hash: str, state: OnboardingState) -> None:
        await self._client.set(f"onb:{user_hash}", state.model_dump_json(), ex=self._ttl_s)
```

- [ ] **Step 6: Тесты проходят, старые не сломаны**

Run: `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/hwcheck/bot/models.py src/hwcheck/bot/max_api.py src/hwcheck/config.py src/hwcheck/bot/onboarding tests/test_max_client.py tests/test_onboarding_state.py
git commit -m "feat: метка bot_started, сообщение по id пользователя, флаг и состояние онбординга"
```

---

### Task 5: Тексты, клавиатуры и черновик политики

**Files:**
- Create: `src/hwcheck/bot/onboarding/policy.py`, `src/hwcheck/bot/onboarding/texts.py`, `docs/legal/privacy-policy-v0.md`, `tests/test_onboarding_texts.py`
- Modify: `Dockerfile`

**Interfaces:**
- Consumes: `Buttons`, `callback_button` (max_api); `subjects_for` (subjects); `StudentProfile`, `InviteResult` (Task 2); `InviteKind` (invites).
- Produces:
  - `hwcheck.bot.onboarding.policy`: `POLICY_VERSION = "v0"`, `MAX_MESSAGE_LEN = 4000`, `split_message(text: str, limit: int = 4000) -> list[str]`, `policy_messages(version: str = POLICY_VERSION) -> list[str]`
  - `hwcheck.bot.onboarding.texts`: константы `HELLO, STUDENT_GRADE, PARENT_GRADE, YOUNG_STUDENT, BOT_LINK_FOR_PARENT, SUBJECT_STUDENT, SUBJECT_PARENT, SUBJECT_SOON, NEED_PARENT, PARENT_INVITE_MESSAGE, WAITING_PARENT, PHOTO_BLOCKED, EXAMPLE, PARENT_ALLOWED, PARENT_NOT_ALLOWED, INSTRUCTION_STUDENT, INSTRUCTION_PARENT, CONSENT_SUMMARY, CHILD_ASKS_CONSENT, PARENT_FIRST_CONSENT, PARENT_SENDS_CONSENT, CONSENT_THANKS, DECLINED, FORWARD_TO_CHILD, CHILD_INVITE_MESSAGE, CHILD_LINKED, CHILD_JOINED, LINK_GONE, PARENT_STATUS_HEADER, PARENT_PHOTO_NO_YOUNG, WHOSE_HOMEWORK, PHOTOS_EXPIRED`; функции `bot_link(username) -> str`, `consent_text(intro) -> str`, `invite_refusal(kind, result) -> str`, `code_invalid(formal: bool) -> str`, `code_rate_limited(formal: bool) -> str`, `parent_status(children, waiting_grades) -> str`; клавиатуры `role_keyboard()`, `grade_keyboard(action: str)`, `subject_keyboard(grade: int)`, `consent_keyboard(accept: str, decline: str | None = None)`, `example_keyboard()`, `waiting_parent_keyboard()`, `add_child_keyboard()`, `whose_keyboard(children)` — все `-> Buttons`.

- [ ] **Step 1: Тесты**

`tests/test_onboarding_texts.py`:

```python
"""Тексты и клавиатуры онбординга, политика сообщениями MAX (спецификация §4, §10.3)."""

import pytest

from hwcheck.bot.max_api import Buttons
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.policy import (
    MAX_MESSAGE_LEN,
    POLICY_VERSION,
    policy_messages,
    split_message,
)
from hwcheck.db.repo import StudentProfile


def payloads(buttons: Buttons) -> list[str]:
    return [button["payload"] for row in buttons for button in row]


def profile(profile_id: int, grade: int, *, own: bool = False) -> StudentProfile:
    return StudentProfile(
        id=profile_id,
        user_id=100 + profile_id if own else None,
        parent_user_id=9,
        grade=grade,
        grade_year=2026,
        subject="math",
        has_consent=True,
    )


def test_split_message_by_paragraphs_and_limit() -> None:
    a, b = "а" * 10, "б" * 10
    assert split_message(f"{a}\n\n{b}", limit=15) == [a, b]
    assert split_message(f"{a}\n\n\n\n{b}", limit=25) == [f"{a}\n\n{b}"]
    assert split_message("в" * 35, limit=15) == ["в" * 15, "в" * 15, "в" * 5]


def test_policy_fits_max_messages() -> None:
    messages = policy_messages()
    assert messages and all(0 < len(m) <= MAX_MESSAGE_LEN for m in messages)
    assert POLICY_VERSION in messages[0]
    assert "GigaChat" in "\n".join(messages)


def test_consent_summary_names_transfer_and_version() -> None:
    summary = texts.CONSENT_SUMMARY
    assert "GigaChat API (ПАО Сбербанк)" in summary and POLICY_VERSION in summary
    text = texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=7))
    assert text.startswith("Ваш ребёнок (7 класс)") and len(text) <= MAX_MESSAGE_LEN


def test_grade_and_role_keyboards() -> None:
    assert payloads(texts.role_keyboard()) == ["ob:role:student", "ob:role:parent"]
    grades = texts.grade_keyboard("pgrade")
    assert [len(row) for row in grades] == [3, 3, 3]
    assert payloads(grades) == [f"ob:pgrade:{g}" for g in range(1, 10)]


def test_subject_keyboard_marks_unavailable_and_fits_grade() -> None:
    buttons = texts.subject_keyboard(3)
    titles = {b["payload"]: b["text"] for row in buttons for b in row}
    assert titles["ob:subject:math"] == "Математика"
    assert titles["ob:subject:world_around"] == "🔜 Окружающий мир"
    assert "ob:subject:history" not in titles
    assert all(len(row) <= 2 for row in buttons)


def test_consent_and_waiting_keyboards() -> None:
    assert payloads(texts.consent_keyboard("ob:accept", "ob:decline")) == [
        "ob:policy",
        "ob:accept",
        "ob:decline",
    ]
    assert payloads(texts.consent_keyboard("ob:pconsent:7")) == ["ob:policy", "ob:pconsent:7"]
    assert payloads(texts.waiting_parent_keyboard()) == ["ob:resend", "ob:example"]
    assert payloads(texts.add_child_keyboard()) == ["ob:addchild"]


def test_whose_keyboard_numbers_children_of_same_grade() -> None:
    buttons = texts.whose_keyboard([profile(1, 2), profile(2, 4), profile(3, 2)])
    assert [b["text"] for row in buttons for b in row] == [
        "2 класс, ребёнок 1",
        "4 класс",
        "2 класс, ребёнок 2",
    ]
    assert payloads(buttons) == ["ob:whose:1", "ob:whose:2", "ob:whose:3"]


def test_parent_status_lists_children_and_waiting_links() -> None:
    status = texts.parent_status([profile(1, 2), profile(2, 7, own=True)], [8])
    assert status.splitlines()[:4] == [
        texts.PARENT_STATUS_HEADER,
        "• 2 класс — фото присылаете вы",
        "• 7 класс — свой MAX",
        "• 8 класс — ждём, когда ребёнок откроет ссылку",
    ]
    assert "пришлите фото" in status
    assert "пришлите фото" not in texts.parent_status([profile(2, 7, own=True)], [])


@pytest.mark.parametrize("kind", ["student_invites_parent", "parent_invites_student"])
@pytest.mark.parametrize("result", ["expired", "used", "invalid", "role_mismatch", "has_parent"])
def test_every_refusal_has_text(kind: str, result: str) -> None:
    assert texts.invite_refusal(kind, result) != "Ссылка не подошла."  # type: ignore[arg-type]


def test_code_texts_address_parent_and_child() -> None:
    assert "Проверьте" in texts.code_invalid(formal=True)
    assert "Проверь " in texts.code_invalid(formal=False)
    assert "Попробуйте" in texts.code_rate_limited(formal=True)
    assert "Попробуй " in texts.code_rate_limited(formal=False)
```

- [ ] **Step 2: Тесты падают**

Run: `uv run pytest tests/test_onboarding_texts.py -v`
Expected: FAIL — нет модулей `policy`, `texts`.

- [ ] **Step 3: Политика**

`src/hwcheck/bot/onboarding/policy.py`:

```python
"""Политика обработки персональных данных (спецификация онбординга §10.3).

Полный текст — `docs/legal/privacy-policy-<версия>.md`, версия пишется в каждое согласие. Новый
текст — новый файл и новая POLICY_VERSION: старые согласия остаются со своей версией.
"""

from pathlib import Path

POLICY_VERSION = "v0"  # черновик: до вычитки юристом реальных пользователей не привлекаем (§14)
LEGAL_DIR = Path(__file__).resolve().parents[4] / "docs" / "legal"
MAX_MESSAGE_LEN = 4000  # лимит текста сообщения MAX


def policy_messages(version: str = POLICY_VERSION) -> list[str]:
    text = (LEGAL_DIR / f"privacy-policy-{version}.md").read_text(encoding="utf-8")
    return split_message(text)


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Сообщения не длиннее лимита: режутся по абзацам, слишком длинный абзац — по лимиту."""
    chunks: list[str] = []
    current = ""
    for paragraph in (p.strip() for p in text.split("\n\n")):
        for start in range(0, len(paragraph), limit):
            piece = paragraph[start : start + limit]
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks
```

`parents[4]`: `policy.py` → `onboarding` → `bot` → `hwcheck` → `src` → корень проекта (в контейнере `/app`).

`docs/legal/privacy-policy-v0.md` (обычный текст без разметки — показывается в чате как есть):

```text
Политика обработки персональных данных сервиса «Домашка», версия v0

Черновик: не вычитан юристом. Поля в квадратных скобках заполняет оператор.

1. Кто обрабатывает данные
Оператор — индивидуальный предприниматель [ФИО], ИНН [ИНН], ОГРНИП [ОГРНИП]. Связь с оператором: [адрес электронной почты]. «Домашка» — бот в мессенджере MAX, который проверяет домашние задания школьников 1–9 классов и помогает разобраться с ошибками подсказками, без готовых ответов.

2. Какие данные обрабатываются
— идентификатор пользователя MAX: хранится в зашифрованном виде, для поиска — необратимый хэш;
— роль (ученик или родитель), класс ребёнка, выбранный предмет;
— фотографии домашних заданий и распознанный текст заданий;
— результаты проверок и разборов ошибок;
— отметка о согласии родителя: дата и версия этой политики.
Имя, фамилию, школу и контакты ребёнка сервис не запрашивает. Пожалуйста, не присылайте фото, на которых видны личные данные, например подпись на обложке тетради.

3. Зачем
Проверка домашнего задания, разбор ошибок с подсказками, уведомления и сводки родителю, учёт обращений к сервису и улучшение качества проверки.

4. Основание
Согласие родителя (законного представителя) ребёнка. Ученик 5–9 класса пользуется сервисом из своего аккаунта MAX только после согласия родителя. За ученика 1–4 класса фото присылает родитель из своего аккаунта.

5. Кому передаются данные
Фотографии и текст заданий передаются в GigaChat API (ПАО Сбербанк) для распознавания и проверки. Идентификатор MAX и данные профиля в GigaChat не передаются. Другим лицам данные не передаются и не продаются.

6. Где и сколько хранятся
Данные хранятся на серверах в России (Москва).
— фотографии домашних заданий — 30 дней;
— профиль и результаты проверок — пока действует согласие;
— резервные копии базы данных — 7 дней;
— отметка о согласии и его отзыве — без идентификатора MAX, как подтверждение законной обработки;
— обезличенный журнал обращений (без идентификатора MAX, фотографий и текста заданий) — для отчётности конкурса Sber500xDisrupt и анализа качества.

7. Как отозвать согласие и удалить данные
В меню бота — «Отозвать согласие и удалить данные». Профиль, результаты проверок и фотографии удаляются, вторая сторона (ребёнок или родитель) получает уведомление. Также можно написать оператору (п. 1).

8. Как защищены данные
Идентификатор MAX шифруется, в журналах используются только необратимые хэши. База данных недоступна из интернета, доступ к серверу есть только у оператора.

9. Изменения политики
При изменении текста меняется версия политики. Новая версия действует после нового согласия родителя.
```

В `Dockerfile` после `COPY prompts ./prompts`:

```dockerfile
# полный текст политики обработки данных показывается в чате (онбординг §10.3)
COPY docs/legal ./docs/legal
```

- [ ] **Step 4: Тексты и клавиатуры**

`src/hwcheck/bot/onboarding/texts.py`:

```python
"""Тексты и клавиатуры онбординга (спецификация §4, §4.7, §10.3). Логика — в шагах пакета.

Payload кнопок начинается с `ob:`: по нему маршрутизатор забирает нажатие у сценария проверки.
Payload недоверенный — разбирает и проверяет его router.py.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from hwcheck.bot.invites import InviteKind
from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.bot.subjects import subjects_for
from hwcheck.db.repo import InviteResult, StudentProfile

HELLO = "Привет! Я Домашка — проверяю домашку и помогаю разобраться с ошибками. Кто ты?"
STUDENT_GRADE = "В каком ты классе?"
PARENT_GRADE = "В каком классе ваш ребёнок?"
YOUNG_STUDENT = (
    "В 1–4 классе домашку мне присылает мама или папа из своего MAX. Перешли им ссылку на меня 👇"
)
BOT_LINK_FOR_PARENT = (
    "Домашка — бот, который проверяет домашку по фото и помогает ребёнку разобраться с ошибками "
    "подсказками: {link}"
)
SUBJECT_STUDENT = "Какой предмет сейчас?"
SUBJECT_PARENT = "Какой предмет проверяем?"
SUBJECT_SOON = (
    "Проверку по предмету «{title}» пока учу — сообщу, когда появится. "
    "А пока могу проверить математику."
)

NEED_PARENT = "Чтобы начать, нужно разрешение родителя. Перешли маме или папе сообщение ниже 👇"
PARENT_INVITE_MESSAGE = (
    "Здравствуйте! Ваш ребёнок хочет проверять домашку в Домашке — это бот, который находит ошибки "
    "и помогает разобраться с ними подсказками, без готовых ответов. Чтобы разрешить, откройте "
    "ссылку: {link}\nЕсли ссылка не открылась — отправьте боту код {code}"
)
WAITING_PARENT = (
    "Проверка откроется, когда родитель разрешит 🙏 Если ссылка потерялась — пришлю новую."
)
PHOTO_BLOCKED = "Сначала нужно разрешение родителя 🙏 До него я не смотрю фото."
EXAMPLE = (
    "Вот как проходит проверка 👇\n\n"
    "1. Ты присылаешь фото страницы тетради. Например, там решено: «№17: 803 + 169 = 753».\n"
    "2. Через минуту я отвечаю: «Проверил! 0 из 1 верно. №17 — есть ошибка (строка 1) ❌» "
    "и показываю кнопку «Разобрать №17».\n"
    "3. Нажимаешь — и я не называю ответ, а подсказываю: «Сложи сначала единицы: 3 + 9. "
    "Сколько получилось и что нужно запомнить?»\n"
    "4. Ошибку находишь ты сам — так и учимся 💪"
)
PARENT_ALLOWED = "Родитель разрешил! 🎉"
PARENT_NOT_ALLOWED = "Родитель пока не разрешил. Можно отправить ссылку ещё раз."
INSTRUCTION_STUDENT = (
    "Пришли фото страницы тетради с решением — целиком, при хорошем свете. Можно вместе с фото "
    "страницы учебника, до 4 фото за раз. Проверка занимает около минуты. Ошибки разберём вместе "
    "подсказками — готовых ответов я не даю 😉"
)
INSTRUCTION_PARENT = (
    "Готово! 🎉 Присылайте фото страницы тетради с решением — целиком, при хорошем свете. Можно "
    "вместе с фото страницы учебника, до 4 фото за раз. Проверка занимает около минуты. Ошибки бот "
    "разбирает подсказками, без готовых ответов — удобно пройти вместе с ребёнком."
)

CONSENT_SUMMARY = (
    "Что обрабатываем: класс, выбранный предмет, фото домашних заданий (хранятся 30 дней), "
    "результаты проверок, идентификатор MAX (в зашифрованном виде). Имя и школу не спрашиваем.\n"
    "Зачем: проверка домашки, разбор ошибок, уведомления родителю.\n"
    "Кому передаём: фото и текст заданий — в GigaChat API (ПАО Сбербанк) для распознавания и "
    "проверки.\n"
    "Где: серверы в России.\n"
    "Как отозвать: меню → «Отозвать согласие и удалить данные».\n"
    f"Полный текст — кнопка «Полный текст» (политика {POLICY_VERSION})."
)
CHILD_ASKS_CONSENT = (
    "Ваш ребёнок ({grade} класс) хочет пользоваться Домашкой — ботом, который проверяет домашку и "
    "помогает разобраться с ошибками подсказками, без готовых ответов."
)
PARENT_FIRST_CONSENT = (
    "Ребёнок {grade} класса пользуется Домашкой из своего MAX, а вы даёте разрешение."
)
PARENT_SENDS_CONSENT = (
    "В 1–4 классе фото домашки присылаете вы — из своего MAX, можно вместе с ребёнком."
)
CONSENT_THANKS = "Спасибо! Ребёнок получил доступ и может присылать домашку ✅"
DECLINED = "Хорошо, доступ не открыт. Если передумаете — попросите ребёнка прислать новую ссылку."
FORWARD_TO_CHILD = "Спасибо! Перешлите ребёнку сообщение ниже 👇"
CHILD_INVITE_MESSAGE = (
    "Открой ссылку, чтобы подключиться к Домашке: {link}\n"
    "Если ссылка не открылась — отправь боту код {code}"
)
CHILD_LINKED = "Привет! Родитель подключил тебя к Домашке 🎉"
CHILD_JOINED = "Ребёнок ({grade} класс) подключился к Домашке ✅"
LINK_GONE = "Эта ссылка уже не ждёт ответа. Откройте её ещё раз или попросите новую."

PARENT_STATUS_HEADER = "Ваши дети в Домашке:"
PARENT_PHOTO_NO_YOUNG = (
    "Фото домашки 5–9 класса присылает ребёнок в своём MAX. Если ребёнок в 1–4 классе — "
    "добавьте его и присылайте фото сами."
)
WHOSE_HOMEWORK = "Чья это домашка?"
PHOTOS_EXPIRED = "Не нашёл фото для проверки — пришлите их ещё раз 📸"

# ссылку ученика открывает родитель — на «вы», ссылку родителя открывает ребёнок — на «ты»
_PARENT_REFUSALS: dict[InviteResult, str] = {
    "expired": "Ссылка устарела — попросите ребёнка прислать новую.",
    "used": "Эта ссылка уже использована — попросите ребёнка прислать новую.",
    "invalid": "Ссылка не сработала — попросите ребёнка прислать новую.",
    "role_mismatch": "Эту ссылку нужно открыть в MAX родителя.",
    "has_parent": "У ребёнка уже подключён родитель.",
}
_CHILD_REFUSALS: dict[InviteResult, str] = {
    "expired": "Ссылка устарела — попроси родителя прислать новую.",
    "used": "Эта ссылка уже использована — попроси родителя прислать новую.",
    "invalid": "Ссылка не сработала — попроси родителя прислать новую.",
    "role_mismatch": "Эту ссылку нужно открыть в MAX ребёнка.",
    "has_parent": "У тебя уже подключён родитель 👍",
}


def bot_link(username: str) -> str:
    return f"https://max.ru/{username}"


def consent_text(intro: str) -> str:
    return f"{intro}\n\n{CONSENT_SUMMARY}"


def invite_refusal(kind: InviteKind, result: InviteResult) -> str:
    refusals = _PARENT_REFUSALS if kind == "student_invites_parent" else _CHILD_REFUSALS
    return refusals.get(result, "Ссылка не подошла.")


def code_invalid(formal: bool) -> str:
    """formal — родитель или новый пользователь (код от ребёнка получает родитель), иначе ученик."""
    if formal:
        return "Код не подошёл 🤔 Проверьте его или попросите новую ссылку."
    return "Код не подошёл 🤔 Проверь его или попроси новую ссылку."


def code_rate_limited(formal: bool) -> str:
    if formal:
        return "Слишком много попыток ввода кода. Попробуйте через час."
    return "Слишком много попыток ввода кода. Попробуй через час."


def parent_status(children: Sequence[StudentProfile], waiting_grades: Sequence[int | None]) -> str:
    lines = [PARENT_STATUS_HEADER]
    for child in children:
        how = "фото присылаете вы" if child.sent_by_parent else "свой MAX"
        lines.append(f"• {child.grade} класс — {how}")
    lines += [f"• {grade} класс — ждём, когда ребёнок откроет ссылку" for grade in waiting_grades]
    if any(child.sent_by_parent for child in children):
        lines.append("\nЧтобы проверить домашку 1–4 класса, пришлите фото страницы тетради.")
    return "\n".join(lines)


def role_keyboard() -> Buttons:
    return [
        [
            callback_button("Я ученик", "ob:role:student"),
            callback_button("Я родитель", "ob:role:parent"),
        ]
    ]


def grade_keyboard(action: str) -> Buttons:
    """Классы 3×3; action `grade` — ученик о себе, `pgrade` — родитель о ребёнке."""
    return [
        [callback_button(str(grade), f"ob:{action}:{grade}") for grade in range(row, row + 3)]
        for row in (1, 4, 7)
    ]


def subject_keyboard(grade: int) -> Buttons:
    buttons = [
        callback_button(s.title if s.available else f"🔜 {s.title}", f"ob:subject:{s.code}")
        for s in subjects_for(grade)
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def consent_keyboard(accept: str, decline: str | None = None) -> Buttons:
    answer = [callback_button("Согласен", accept)]
    if decline is not None:
        answer.append(callback_button("Отказать", decline))
    return [[callback_button("Полный текст", "ob:policy")], answer]


def example_keyboard() -> Buttons:
    return [[callback_button("Посмотреть пример проверки", "ob:example")]]


def waiting_parent_keyboard() -> Buttons:
    return [
        [callback_button("Отправить ссылку ещё раз", "ob:resend")],
        [callback_button("Посмотреть пример проверки", "ob:example")],
    ]


def add_child_keyboard() -> Buttons:
    return [[callback_button("Добавить ребёнка", "ob:addchild")]]


def whose_keyboard(children: Sequence[StudentProfile]) -> Buttons:
    """Дети 1–4 класса по классу; одинаковый класс — «ребёнок N» по порядку добавления."""
    total = Counter(child.grade for child in children)
    seen: Counter[int] = Counter()
    rows: Buttons = []
    for child in children:
        seen[child.grade] += 1
        label = f"{child.grade} класс"
        if total[child.grade] > 1:
            label += f", ребёнок {seen[child.grade]}"
        rows.append([callback_button(label, f"ob:whose:{child.id}")])
    return rows
```

- [ ] **Step 5: Тесты проходят**

Run: `uv run pytest tests/test_onboarding_texts.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hwcheck/bot/onboarding/policy.py src/hwcheck/bot/onboarding/texts.py docs/legal/privacy-policy-v0.md Dockerfile tests/test_onboarding_texts.py
git commit -m "feat: тексты и клавиатуры онбординга, черновик политики обработки данных"
```

---

### Task 6: Контекст шагов, шаг «предмет», ученик 5–9 и ученик 1–4

**Files:**
- Create: `src/hwcheck/bot/onboarding/context.py`, `src/hwcheck/bot/onboarding/subject.py`, `src/hwcheck/bot/onboarding/student.py`, `tests/onboarding_kit.py`, `tests/test_onboarding_student.py`
- Modify: `src/hwcheck/bot/subjects.py`

**Interfaces:**
- Consumes: `ProfileRepository` (Task 2), `OnboardingStateStore` (Task 4), тексты и клавиатуры (Task 5), `StateStore` (fsm), `EventLog`, `anonymize`, `UserIdCipher`, `new_invite`, `INVITE_TTL_DAYS`, `school_year`, `subject_by_code`.
- Produces:
  - `hwcheck.bot.subjects.PARENT_SENDS_UP_TO_GRADE = 4`
  - `hwcheck.bot.onboarding.context`: `MSK`, `utc_now() -> datetime`, `Actor(chat_id: int, user_id: int, user_hash: str)` + `Actor.of(chat_id, user_id)`, `OnboardingContext(max, repo, states, dialogs, events, cipher, bot_username, clock=utc_now)` с методами `now() -> datetime`, `school_year() -> int`, `encrypted_id(actor) -> bytes`, `reply(actor, text, buttons=None)`, `log(event, actor, *, user_initiated=True, **fields)`, `notify(actor, user_id_enc, text, *, kind, buttons=None)`
  - `SubjectStep(ctx)`: `ask(actor, profile) -> None`, `choose(actor, profile, code) -> bool`
  - `StudentSteps(ctx, subjects)`: `choose_grade(actor, grade) -> None`, `after_subject(actor, profile) -> None`, `invite_parent(actor, profile) -> None`, `remind_waiting(actor) -> None`, `block_photo(actor) -> None`
  - `tests/onboarding_kit.py`: `NOW`, `BOT`, `FakeMax`, `Clock`, `Kit`, `make_kit(tmp_path)`, `actor(user_id)`, `chat(user_id)`, апдейты `start`, `text`, `photo`, `press`, разбор `payloads`, `link_token`, `code_of`, `ready_student(kit, user_id=1, grade=7)`.

- [ ] **Step 1: Набор для сценарных тестов**

`tests/onboarding_kit.py`:

```python
"""Набор для сценарных тестов онбординга: фейк MAX, часы, контекст в памяти, апдейты."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.invites import new_invite
from hwcheck.bot.max_api import Buttons
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.state import InMemoryOnboardingStateStore
from hwcheck.crypto import UserIdCipher, new_user_id_key
from hwcheck.db.memory import InMemoryProfileRepository
from hwcheck.db.repo import StudentProfile
from hwcheck.events import EventLog

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
BOT = "domashka_bot"


class FakeMax:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Buttons | None]] = []  # (chat_id, текст, кнопки)
        self.to_users: list[tuple[int, str, Buttons | None]] = []  # (user_id, текст, кнопки)
        self.callbacks: list[str] = []
        self.downloads: list[str] = []
        self.blocked_users: set[int] = set()  # заблокировали бота: send_to_user падает

    async def send_message(
        self, chat_id: int, text: str, *, buttons: Buttons | None = None
    ) -> None:
        self.sent.append((chat_id, text, buttons))

    async def send_to_user(
        self, user_id: int, text: str, *, buttons: Buttons | None = None
    ) -> None:
        if user_id in self.blocked_users:
            raise RuntimeError("blocked by user")
        self.to_users.append((user_id, text, buttons))

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
        self.callbacks.append(callback_id)

    async def download(self, url: str) -> bytes:
        self.downloads.append(url)
        return b"fake-image"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Kit:
    ctx: OnboardingContext
    max: FakeMax
    repo: InMemoryProfileRepository
    clock: Clock
    events_path: Path

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows = [json.loads(line) for line in self.events_path.read_text("utf-8").splitlines()]
        return [row for row in rows if event_type is None or row["type"] == event_type]

    def texts(self, user_id: int) -> list[str]:
        return [text for chat_id, text, _ in self.max.sent if chat_id == chat(user_id)]

    def last(self, user_id: int) -> tuple[str, Buttons | None]:
        _, text, buttons = [m for m in self.max.sent if m[0] == chat(user_id)][-1]
        return text, buttons


def make_kit(tmp_path: Path) -> Kit:
    fake = FakeMax()
    repo = InMemoryProfileRepository()
    clock = Clock()
    events_path = tmp_path / "events.jsonl"
    ctx = OnboardingContext(
        max=fake,  # type: ignore[arg-type]
        repo=repo,
        states=InMemoryOnboardingStateStore(),
        dialogs=InMemoryStateStore(),
        events=EventLog(events_path, "dev"),
        cipher=UserIdCipher(new_user_id_key()),
        bot_username=BOT,
        clock=clock,
    )
    return Kit(ctx, fake, repo, clock, events_path)


def chat(user_id: int) -> int:
    """Чат диалога с ботом отличается от id пользователя, как в MAX."""
    return user_id * 10


def actor(user_id: int) -> Actor:
    return Actor.of(chat(user_id), user_id)


def start(user_id: int, payload: str | None = None) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "bot_started",
            "chat_id": chat(user_id),
            "user": {"user_id": user_id},
            "payload": payload,
        }
    )


def text(user_id: int, body: str) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": user_id},
                "recipient": {"chat_id": chat(user_id)},
                "body": {"mid": "m", "text": body, "attachments": []},
            },
        }
    )


def photo(user_id: int, *urls: str) -> MaxUpdate:
    attachments = [{"type": "image", "payload": {"url": url}} for url in urls]
    return MaxUpdate.model_validate(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": user_id},
                "recipient": {"chat_id": chat(user_id)},
                "body": {"mid": "m", "text": None, "attachments": attachments},
            },
        }
    )


def press(user_id: int, payload: str) -> MaxUpdate:
    return MaxUpdate.model_validate(
        {
            "update_type": "message_callback",
            "chat_id": chat(user_id),
            "callback": {
                "callback_id": f"cb-{payload}",
                "payload": payload,
                "user": {"user_id": user_id},
            },
        }
    )


def payloads(buttons: Buttons | None) -> list[str]:
    return [button["payload"] for row in buttons or [] for button in row]


def link_token(message: str) -> str:
    match = re.search(r"\?start=[pc]_([A-Za-z0-9_-]+)", message)
    assert match is not None, message
    return match.group(1)


def code_of(message: str) -> str:
    """Запасной код из сообщения со ссылкой, как его вводит пользователь: «4F7K-92QD»."""
    match = re.search(r"код ([A-Z0-9]{4}-[A-Z0-9]{4})", message)
    assert match is not None, message
    return match.group(1)


async def ready_student(kit: Kit, user_id: int = 1, grade: int = 7) -> StudentProfile:
    """Ученик с предметом и подключённым родителем (родитель — user_id + 100)."""
    me, parent = actor(user_id), actor(user_id + 100)
    profile = await kit.repo.create_student(
        me.user_hash, kit.ctx.encrypted_id(me), grade, kit.ctx.school_year()
    )
    assert profile is not None and profile.user_id is not None
    await kit.repo.set_subject(profile.id, "math")
    invite = new_invite("student_invites_parent")
    await kit.repo.create_invite(invite, profile.user_id, kit.clock.now + timedelta(days=7))
    outcome = await kit.repo.accept_parent_invite(
        invite.token_hash, parent.user_hash, kit.ctx.encrypted_id(parent), "v0", kit.clock.now
    )
    assert outcome.profile is not None
    return outcome.profile
```

- [ ] **Step 2: Тесты шагов ученика**

`tests/test_onboarding_student.py`:

```python
"""Ученик в своём MAX (спецификация §4.1, §4.7 п.1, §10.2): класс, предмет, ссылка родителю."""

from datetime import timedelta
from pathlib import Path

from onboarding_kit import BOT, Kit, actor, code_of, link_token, make_kit, payloads

from hwcheck.bot.invites import digest
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.student import StudentSteps
from hwcheck.bot.onboarding.subject import SubjectStep


def steps(kit: Kit) -> tuple[SubjectStep, StudentSteps]:
    subjects = SubjectStep(kit.ctx)
    return subjects, StudentSteps(kit.ctx, subjects)


async def test_young_student_gets_bot_link_for_parent_and_nothing_is_stored(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    _, students = steps(kit)
    await students.choose_grade(actor(1), 3)
    link = texts.BOT_LINK_FOR_PARENT.format(link=f"https://max.ru/{BOT}")
    assert kit.texts(1) == [texts.YOUNG_STUDENT, link]
    assert await kit.repo.get_account(actor(1).user_hash) is None
    assert kit.events("onboarding_parent_required")[0]["grade"] == 3


async def test_student_grade_and_subject_with_waitlist(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    subjects, students = steps(kit)
    me = actor(1)
    await students.choose_grade(me, 7)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None and account.role == "student"
    profile = await kit.repo.own_profile(account.id)
    assert profile is not None and (profile.grade, profile.grade_year) == (7, 2026)
    question, buttons = kit.last(1)
    assert question == texts.SUBJECT_STUDENT and "ob:subject:physics" in payloads(buttons)
    assert kit.events("onboarding_grade_chosen")[0]["role"] == "student"

    assert not await subjects.choose(me, profile, "history")
    assert (me.user_hash, "history") in kit.repo.waitlist
    assert texts.SUBJECT_SOON.format(title="История") in kit.texts(1)
    assert not await subjects.choose(me, profile, "world_around")  # предмет 1–4 классов
    assert not await subjects.choose(me, profile, "../../etc")
    assert kit.last(1)[0] == texts.SUBJECT_STUDENT
    assert await subjects.choose(me, profile, "math")
    chosen = await kit.repo.own_profile(account.id)
    assert chosen is not None and chosen.subject == "math"
    events = kit.events("onboarding_subject_chosen")
    assert [(e["subject"], e["available"]) for e in events] == [
        ("history", False),
        ("math", True),
    ]


async def test_student_without_parent_gets_message_to_forward(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, students = steps(kit)
    me = actor(1)
    await students.choose_grade(me, 7)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None
    profile = await kit.repo.own_profile(account.id)
    assert profile is not None
    await students.after_subject(me, profile)

    need, message = kit.texts(1)[-2:]
    assert need == texts.NEED_PARENT
    assert f"https://max.ru/{BOT}?start=p_" in message
    invite = await kit.repo.find_invite(token_hash=digest(link_token(message)))
    assert invite is not None
    assert (invite.kind, invite.grade) == ("student_invites_parent", 7)
    assert invite.expires_at == kit.clock.now + timedelta(days=7)
    by_code = await kit.repo.find_invite(code_hash=digest(code_of(message).replace("-", "")))
    assert by_code == invite
    assert kit.events("invite_created")[0]["kind"] == "student_invites_parent"


async def test_waiting_student_reminded_and_photo_blocked(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, students = steps(kit)
    await students.remind_waiting(actor(1))
    assert kit.last(1)[0] == texts.WAITING_PARENT
    await students.block_photo(actor(1))
    blocked, buttons = kit.last(1)
    assert blocked == texts.PHOTO_BLOCKED and payloads(buttons) == ["ob:resend", "ob:example"]
```

- [ ] **Step 3: Тесты падают**

Run: `uv run pytest tests/test_onboarding_student.py -v`
Expected: FAIL — нет `hwcheck.bot.onboarding.context`.

- [ ] **Step 4: Граница «фото присылает родитель»**

В `src/hwcheck/bot/subjects.py` после `MAX_GRADE = 9`:

```python
# 1–4 класс: фото домашки присылает родитель из своего MAX — рекомендации Минпросвещения 13.09.2026
# (спецификация онбординга §4.7)
PARENT_SENDS_UP_TO_GRADE = 4
```

- [ ] **Step 5: Контекст**

`src/hwcheck/bot/onboarding/context.py`:

```python
"""Общее для шагов онбординга (спецификация §4, §7, §11): кто пишет, чем ответить, где хранить.

Шаги (subject, student, parent, linking) получают один OnboardingContext; часы подменяются в тестах.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from hwcheck.bot.fsm import StateStore
from hwcheck.bot.max_api import Buttons, MaxClient
from hwcheck.bot.onboarding.state import OnboardingStateStore
from hwcheck.bot.subjects import school_year
from hwcheck.crypto import UserIdCipher
from hwcheck.db.repo import ProfileRepository
from hwcheck.events import EventLog, anonymize

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))  # учебный год — по московской дате


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Actor:
    """Автор апдейта: чат для ответа, id MAX для журнала и шифра, хэш для поиска в базе."""

    chat_id: int
    user_id: int
    user_hash: str

    @classmethod
    def of(cls, chat_id: int, user_id: int) -> Actor:
        user_hash = anonymize(user_id)
        assert user_hash is not None  # None только для user_id=None
        return cls(chat_id, user_id, user_hash)


@dataclass(frozen=True)
class OnboardingContext:
    max: MaxClient
    repo: ProfileRepository
    states: OnboardingStateStore
    dialogs: StateStore  # состояние проверки (bot/fsm.py): текст родителя в разборе идёт тьютору
    events: EventLog
    cipher: UserIdCipher
    bot_username: str
    clock: Callable[[], datetime] = utc_now

    def now(self) -> datetime:
        return self.clock()

    def school_year(self) -> int:
        return school_year(self.clock().astimezone(MSK).date())

    def encrypted_id(self, actor: Actor) -> bytes:
        return self.cipher.encrypt(actor.user_id)

    async def reply(self, actor: Actor, text: str, buttons: Buttons | None = None) -> None:
        await self.max.send_message(actor.chat_id, text, buttons=buttons)

    def log(self, event: str, actor: Actor, *, user_initiated: bool = True, **fields: Any) -> None:
        self.events.log(event, user_id=actor.user_id, user_initiated=user_initiated, **fields)

    async def notify(
        self,
        actor: Actor,
        user_id_enc: bytes,
        text: str,
        *,
        kind: str,
        buttons: Buttons | None = None,
    ) -> None:
        """Сообщение второй стороне связки. Не дошло (бот заблокирован) — событие notify_failed,
        а не сбой апдейта: у автора действие уже выполнено (§9.4, §11)."""
        try:
            await self.max.send_to_user(self.cipher.decrypt(user_id_enc), text, buttons=buttons)
        except Exception as exc:
            logger.warning("notify failed: %s (%s)", kind, type(exc).__name__)
            self.log(
                "notify_failed",
                actor,
                user_initiated=False,
                component="notifier",
                kind=kind,
                error=type(exc).__name__,
            )
            return
        self.log("notify_sent", actor, user_initiated=False, component="notifier", kind=kind)
```

- [ ] **Step 6: Шаг «предмет» и шаги ученика**

`src/hwcheck/bot/onboarding/subject.py`:

```python
"""Шаг «предмет» (спецификация §4.1 п.3, §4.7 п.1, §5) — для ученика и для ребёнка 1–4 класса."""

from __future__ import annotations

from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.subjects import subject_by_code
from hwcheck.db.repo import StudentProfile


class SubjectStep:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    async def ask(self, actor: Actor, profile: StudentProfile) -> None:
        question = texts.SUBJECT_PARENT if profile.sent_by_parent else texts.SUBJECT_STUDENT
        await self._ctx.reply(actor, question, texts.subject_keyboard(profile.grade))

    async def choose(self, actor: Actor, profile: StudentProfile, code: str) -> bool:
        """True — предмет записан. Недоступный — лист ожидания и выбор заново; предмет не своего
        класса или выдуманный payload — просто выбор заново."""
        ctx = self._ctx
        subject = subject_by_code(code)
        if subject is None or profile.grade not in subject.grades:
            await self.ask(actor, profile)
            return False
        ctx.log(
            "onboarding_subject_chosen",
            actor,
            subject=code,
            available=subject.available,
            sender="parent" if profile.sent_by_parent else "student",
        )
        if not subject.available:
            await ctx.repo.add_to_waitlist(actor.user_hash, code, profile.grade)
            ctx.log("subject_waitlist", actor, subject=code, grade=profile.grade)
            await ctx.reply(actor, texts.SUBJECT_SOON.format(title=subject.title))
            await self.ask(actor, profile)
            return False
        await ctx.repo.set_subject(profile.id, code)
        return True
```

`src/hwcheck/bot/onboarding/student.py`:

```python
"""Ученик в своём MAX (спецификация §4.1, §10.2): класс, ссылка родителю, ожидание согласия.

1–4 класс дальше выбора класса не идёт: ссылка на бота для родителя, в базе ничего (§4.7).
"""

from __future__ import annotations

from datetime import timedelta

from hwcheck.bot.invites import INVITE_TTL_DAYS, new_invite
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.bot.subjects import PARENT_SENDS_UP_TO_GRADE
from hwcheck.db.repo import StudentProfile


class StudentSteps:
    def __init__(self, ctx: OnboardingContext, subjects: SubjectStep) -> None:
        self._ctx = ctx
        self._subjects = subjects

    async def choose_grade(self, actor: Actor, grade: int) -> None:
        ctx = self._ctx
        if grade <= PARENT_SENDS_UP_TO_GRADE:
            ctx.log("onboarding_parent_required", actor, grade=grade)
            await ctx.reply(actor, texts.YOUNG_STUDENT)
            link = texts.bot_link(ctx.bot_username)
            await ctx.reply(actor, texts.BOT_LINK_FOR_PARENT.format(link=link))
            return
        profile = await ctx.repo.create_student(
            actor.user_hash, ctx.encrypted_id(actor), grade, ctx.school_year()
        )
        if profile is None:  # аккаунт уже есть: старая кнопка, маршрутизатор покажет текущий шаг
            return
        ctx.log("onboarding_grade_chosen", actor, grade=grade, role="student")
        await self._subjects.ask(actor, profile)

    async def after_subject(self, actor: Actor, profile: StudentProfile) -> None:
        """Предмет выбран: родитель уже подключён (ребёнок пришёл по его ссылке) — инструкция."""
        if profile.has_consent:
            await self._ctx.reply(actor, texts.INSTRUCTION_STUDENT)
        else:
            await self.invite_parent(actor, profile)

    async def invite_parent(self, actor: Actor, profile: StudentProfile) -> None:
        """Новая ссылка родителю: в базе только HMAC токена и кода, сами они — в сообщении."""
        ctx = self._ctx
        assert profile.user_id is not None  # ссылку родителю создаёт ученик со своим MAX
        invite = new_invite("student_invites_parent")
        expires_at = ctx.now() + timedelta(days=INVITE_TTL_DAYS)
        await ctx.repo.create_invite(invite, profile.user_id, expires_at)
        ctx.log("invite_created", actor, kind=invite.kind)
        await ctx.reply(actor, texts.NEED_PARENT, texts.example_keyboard())
        message = texts.PARENT_INVITE_MESSAGE.format(
            link=invite.link(ctx.bot_username), code=invite.display_code
        )
        await ctx.reply(actor, message)

    async def remind_waiting(self, actor: Actor) -> None:
        await self._ctx.reply(actor, texts.WAITING_PARENT, texts.waiting_parent_keyboard())

    async def block_photo(self, actor: Actor) -> None:
        """До согласия фото не скачивается, не сохраняется и не уходит в GigaChat (§10.2)."""
        await self._ctx.reply(actor, texts.PHOTO_BLOCKED, texts.waiting_parent_keyboard())
```

- [ ] **Step 7: Тесты проходят**

Run: `uv run pytest tests/test_onboarding_student.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS (4 теста).

- [ ] **Step 8: Commit**

```bash
git add src/hwcheck/bot/subjects.py src/hwcheck/bot/onboarding/context.py src/hwcheck/bot/onboarding/subject.py src/hwcheck/bot/onboarding/student.py tests/onboarding_kit.py tests/test_onboarding_student.py
git commit -m "feat: шаги онбординга ученика — класс, предмет, лист ожидания, ссылка родителю"
```


---

### Task 7: Связка по ссылке и коду — согласие, отказ, привязка ребёнка

**Files:**
- Create: `src/hwcheck/bot/onboarding/linking.py`, `tests/test_onboarding_linking.py`

**Interfaces:**
- Consumes: `OnboardingContext`, `Actor`, `SubjectStep`, `StudentSteps` (Task 6, в тестах); `ProfileRepository`, `Account`, `Invite`, `InviteResult` (Task 2); `digest`, `InviteKind` (invites); `POLICY_VERSION`, тексты (Task 5).
- Produces: `Linking(ctx, subjects)`: `open_link(actor, account: Account | None, kind: InviteKind, token: str) -> None`, `open_code(actor, account: Account | None, code: str) -> None` (code — результат `parse_code`, без дефиса), `accept(actor) -> None`, `decline(actor) -> None`; константы `CODE_ATTEMPTS = 5`, `CODE_WINDOW = timedelta(hours=1)`.

- [ ] **Step 1: Тесты**

`tests/test_onboarding_linking.py`:

```python
"""Связка «ребёнок ↔ родитель» по ссылке и коду (спецификация §4.2, §4.3 п.6, §4.4, §11)."""

from datetime import timedelta
from pathlib import Path

from onboarding_kit import Kit, actor, code_of, link_token, make_kit, payloads

from hwcheck.bot.invites import new_invite
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.linking import Linking
from hwcheck.bot.onboarding.student import StudentSteps
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.db.repo import StudentProfile


def linking(kit: Kit) -> Linking:
    return Linking(kit.ctx, SubjectStep(kit.ctx))


async def waiting_student(kit: Kit, user_id: int = 1) -> tuple[StudentProfile, str]:
    """Ученик 7 класса с предметом и ссылкой родителю: (профиль, сообщение для пересылки)."""
    me = actor(user_id)
    profile = await kit.repo.create_student(me.user_hash, kit.ctx.encrypted_id(me), 7, 2026)
    assert profile is not None
    await kit.repo.set_subject(profile.id, "math")
    return profile, await forward_again(kit, profile, user_id)


async def forward_again(kit: Kit, profile: StudentProfile, user_id: int = 1) -> str:
    await StudentSteps(kit.ctx, SubjectStep(kit.ctx)).invite_parent(actor(user_id), profile)
    return kit.texts(user_id)[-1]


async def test_parent_opens_link_and_consents(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile, message = await waiting_student(kit)
    parent = actor(2)
    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message))
    consent, buttons = kit.last(2)
    assert consent == texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=7))
    assert payloads(buttons) == ["ob:policy", "ob:accept", "ob:decline"]

    await linking(kit).accept(parent)
    assert kit.last(2)[0] == texts.CONSENT_THANKS
    allowed = f"{texts.PARENT_ALLOWED}\n\n{texts.INSTRUCTION_STUDENT}"
    assert kit.max.to_users == [(1, allowed, None)]
    assert profile.user_id is not None
    linked = await kit.repo.own_profile(profile.user_id)
    assert linked is not None and linked.has_consent
    assert [e["scenario"] for e in kit.events("consent_given")] == ["child_link"]
    assert [e["result"] for e in kit.events("invite_opened")] == ["ok"]
    assert kit.events("notify_sent")[0]["kind"] == "consent_given"

    await linking(kit).accept(parent)  # повторное «Согласен»
    assert kit.last(2)[0] == texts.LINK_GONE
    assert len(kit.max.to_users) == 1


async def test_parent_declines_and_child_can_resend(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    parent = actor(2)
    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message))
    await linking(kit).decline(parent)
    assert kit.last(2)[0] == texts.DECLINED
    [(child_id, notice, buttons)] = kit.max.to_users
    assert (child_id, notice) == (1, texts.PARENT_NOT_ALLOWED)
    assert "ob:resend" in payloads(buttons)
    assert await kit.repo.get_account(parent.user_hash) is None
    assert len(kit.events("consent_declined")) == 1

    await linking(kit).open_link(parent, None, "student_invites_parent", link_token(message))
    assert kit.last(2)[0] == texts.invite_refusal("student_invites_parent", "used")


async def test_child_opens_parent_link_and_parent_is_told(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    parent = actor(2)
    account = await kit.repo.get_or_create_account(
        parent.user_hash, kit.ctx.encrypted_id(parent), "parent"
    )
    invite = new_invite("parent_invites_student")
    await kit.repo.create_invite(
        invite,
        account.id,
        kit.clock.now + timedelta(days=7),
        grade=6,
        policy_version="v0",
        consent_at=kit.clock.now,
    )
    await linking(kit).open_link(actor(3), None, "parent_invites_student", invite.token)
    assert kit.texts(3) == [texts.CHILD_LINKED, texts.SUBJECT_STUDENT]
    assert kit.max.to_users == [(2, texts.CHILD_JOINED.format(grade=6), None)]
    assert kit.events("child_linked")[0]["grade"] == 6


async def test_link_refusals(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    token = link_token(message)
    student = await kit.repo.get_account(actor(1).user_hash)
    await linking(kit).open_link(actor(1), student, "student_invites_parent", token)
    assert kit.last(1)[0] == texts.invite_refusal("student_invites_parent", "role_mismatch")
    await linking(kit).open_link(actor(2), None, "parent_invites_student", token)  # не тот вид
    assert kit.last(2)[0] == texts.invite_refusal("parent_invites_student", "invalid")
    kit.clock.now += timedelta(days=8)
    await linking(kit).open_link(actor(2), None, "student_invites_parent", token)
    assert kit.last(2)[0] == texts.invite_refusal("student_invites_parent", "expired")
    results = [e["result"] for e in kit.events("invite_opened")]
    assert results == ["role_mismatch", "invalid", "expired"]


async def test_second_parent_is_refused(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    profile, first = await waiting_student(kit)
    second = await forward_again(kit, profile)  # ребёнок отправил ссылку второму родителю
    await linking(kit).open_link(actor(2), None, "student_invites_parent", link_token(first))
    await linking(kit).accept(actor(2))
    await linking(kit).open_link(actor(4), None, "student_invites_parent", link_token(second))
    assert kit.last(4)[0] == texts.invite_refusal("student_invites_parent", "has_parent")


async def test_code_opens_invite_with_attempt_limit(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    code = code_of(message).replace("-", "")
    parent = actor(2)
    for _ in range(5):
        await linking(kit).open_code(parent, None, "AAAAAAAA")
    assert kit.last(2)[0] == texts.code_invalid(formal=True)
    await linking(kit).open_code(parent, None, code)
    assert kit.last(2)[0] == texts.code_rate_limited(formal=True)
    kit.clock.now += timedelta(hours=1)
    await linking(kit).open_code(parent, None, code)
    assert kit.last(2)[0] == texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=7))
    results = [(e["via"], e["result"]) for e in kit.events("invite_opened")]
    assert results == [("code", "invalid")] * 5 + [("code", "rate_limited"), ("code", "ok")]


async def test_blocked_child_does_not_break_consent(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    _, message = await waiting_student(kit)
    kit.max.blocked_users.add(1)
    await linking(kit).open_link(actor(2), None, "student_invites_parent", link_token(message))
    await linking(kit).accept(actor(2))
    assert kit.last(2)[0] == texts.CONSENT_THANKS
    [failed] = kit.events("notify_failed")
    assert (failed["kind"], failed["component"], failed["error"]) == (
        "consent_given",
        "notifier",
        "RuntimeError",
    )
```

- [ ] **Step 2: Тесты падают**

Run: `uv run pytest tests/test_onboarding_linking.py -v`
Expected: FAIL — нет `hwcheck.bot.onboarding.linking`.

- [ ] **Step 3: Реализация**

`src/hwcheck/bot/onboarding/linking.py`:

```python
"""Ссылки и запасные коды «ребёнок ↔ родитель» (спецификация §4.2, §4.3 п.6, §4.4, §11).

Открытие проверяет ссылку и роль до показа согласия, но исход решает транзакция хранилища: между
открытием и «Согласен» ссылку мог погасить другой родитель. Ссылка ученика, открытая родителем, ждёт
ответа в Redis; ссылку родителя ребёнок погашает сразу — согласие дано при её создании.
"""

from __future__ import annotations

from datetime import timedelta

from hwcheck.bot.invites import InviteKind, digest
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.db.repo import Account, Invite, InviteResult

CODE_ATTEMPTS = 5
CODE_WINDOW = timedelta(hours=1)


class Linking:
    def __init__(self, ctx: OnboardingContext, subjects: SubjectStep) -> None:
        self._ctx = ctx
        self._subjects = subjects

    async def open_link(
        self, actor: Actor, account: Account | None, kind: InviteKind, token: str
    ) -> None:
        invite = await self._ctx.repo.find_invite(token_hash=digest(token))
        await self._open(actor, account, invite, kind, via="link")

    async def open_code(self, actor: Actor, account: Account | None, code: str) -> None:
        ctx = self._ctx
        formal = account is None or account.role == "parent"
        allowed = await ctx.repo.code_attempt(
            actor.user_hash, ctx.now(), limit=CODE_ATTEMPTS, window=CODE_WINDOW
        )
        if not allowed:
            ctx.log("invite_opened", actor, kind=None, via="code", result="rate_limited")
            await ctx.reply(actor, texts.code_rate_limited(formal))
            return
        invite = await ctx.repo.find_invite(code_hash=digest(code))
        if invite is None:
            ctx.log("invite_opened", actor, kind=None, via="code", result="invalid")
            await ctx.reply(actor, texts.code_invalid(formal))
            return
        await self._open(actor, account, invite, invite.kind, via="code")

    async def _open(
        self,
        actor: Actor,
        account: Account | None,
        invite: Invite | None,
        kind: InviteKind,
        via: str,
    ) -> None:
        ctx = self._ctx
        result = await self._precheck(account, invite, kind)
        if invite is None or result != "ok":
            ctx.log("invite_opened", actor, kind=kind, via=via, result=result)
            await ctx.reply(actor, texts.invite_refusal(kind, result))
            return
        if kind == "parent_invites_student":
            await self._join_child(actor, invite, via)
            return
        ctx.log("invite_opened", actor, kind=kind, via=via, result="ok")
        state = await ctx.states.get(actor.user_hash)
        await ctx.states.set(
            actor.user_hash, state.model_copy(update={"pending_invite": invite.token_hash})
        )
        intro = texts.CHILD_ASKS_CONSENT.format(grade=invite.grade)
        buttons = texts.consent_keyboard("ob:accept", "ob:decline")
        await ctx.reply(actor, texts.consent_text(intro), buttons)

    async def _precheck(
        self, account: Account | None, invite: Invite | None, kind: InviteKind
    ) -> InviteResult:
        """Исход до транзакции — чтобы не показывать согласие по заведомо негодной ссылке."""
        if invite is None or invite.kind != kind:
            return "invalid"
        if invite.used:
            return "used"
        if invite.expires_at <= self._ctx.now():
            return "expired"
        wanted = "parent" if kind == "student_invites_parent" else "student"
        if account is not None and account.role != wanted:
            return "role_mismatch"
        # чей профиль проверять: ссылку ученика создал ребёнок, ссылку родителя открывает ребёнок
        child_account = invite.created_by if kind == "student_invites_parent" else None
        if kind == "parent_invites_student" and account is not None:
            child_account = account.id
        if child_account is not None:
            child = await self._ctx.repo.own_profile(child_account)
            if child is not None and child.parent_user_id is not None:
                return "has_parent"
        return "ok"

    async def _join_child(self, actor: Actor, invite: Invite, via: str) -> None:
        """Ребёнок открыл ссылку родителя: привязка и согласие одной транзакцией, затем предмет."""
        ctx = self._ctx
        outcome = await ctx.repo.accept_child_invite(
            invite.token_hash,
            actor.user_hash,
            ctx.encrypted_id(actor),
            ctx.school_year(),
            ctx.now(),
        )
        ctx.log("invite_opened", actor, kind=invite.kind, via=via, result=outcome.result)
        profile, parent = outcome.profile, outcome.notify
        if outcome.result != "ok" or profile is None or parent is None:
            await ctx.reply(actor, texts.invite_refusal(invite.kind, outcome.result))
            return
        ctx.log("child_linked", actor, grade=profile.grade)
        await ctx.reply(actor, texts.CHILD_LINKED)
        if profile.subject is None:
            await self._subjects.ask(actor, profile)
        else:
            await ctx.reply(actor, texts.INSTRUCTION_STUDENT)
        joined = texts.CHILD_JOINED.format(grade=profile.grade)
        await ctx.notify(actor, parent, joined, kind="child_linked")

    async def accept(self, actor: Actor) -> None:
        ctx = self._ctx
        state = await ctx.states.get(actor.user_hash)
        if state.pending_invite is None:
            await ctx.reply(actor, texts.LINK_GONE)
            return
        outcome = await ctx.repo.accept_parent_invite(
            state.pending_invite,
            actor.user_hash,
            ctx.encrypted_id(actor),
            POLICY_VERSION,
            ctx.now(),
        )
        await ctx.states.set(actor.user_hash, state.model_copy(update={"pending_invite": None}))
        profile, child = outcome.profile, outcome.notify
        if outcome.result != "ok" or profile is None or child is None:
            await ctx.reply(actor, texts.invite_refusal("student_invites_parent", outcome.result))
            return
        ctx.log("consent_given", actor, policy_version=POLICY_VERSION, scenario="child_link")
        ctx.log("child_linked", actor, grade=profile.grade)
        await ctx.reply(actor, texts.CONSENT_THANKS)
        allowed = f"{texts.PARENT_ALLOWED}\n\n{texts.INSTRUCTION_STUDENT}"
        await ctx.notify(actor, child, allowed, kind="consent_given")

    async def decline(self, actor: Actor) -> None:
        ctx = self._ctx
        state = await ctx.states.get(actor.user_hash)
        if state.pending_invite is None:
            await ctx.reply(actor, texts.LINK_GONE)
            return
        outcome = await ctx.repo.decline_parent_invite(state.pending_invite, ctx.now())
        await ctx.states.set(actor.user_hash, state.model_copy(update={"pending_invite": None}))
        if outcome.result != "ok" or outcome.notify is None:
            await ctx.reply(actor, texts.invite_refusal("student_invites_parent", outcome.result))
            return
        ctx.log("consent_declined", actor)
        await ctx.reply(actor, texts.DECLINED)
        await ctx.notify(
            actor,
            outcome.notify,
            texts.PARENT_NOT_ALLOWED,
            kind="consent_declined",
            buttons=texts.waiting_parent_keyboard(),
        )
```

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest tests/test_onboarding_linking.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS (7 тестов).

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/bot/onboarding/linking.py tests/test_onboarding_linking.py
git commit -m "feat: связка ребёнка и родителя по ссылке и коду, согласие и отказ"
```

---

### Task 8: Родитель — класс ребёнка, дети 1–4, ссылка ребёнку 5–9, «Чья домашка?»

**Files:**
- Create: `src/hwcheck/bot/onboarding/parent.py`, `tests/test_onboarding_parent.py`

**Interfaces:**
- Consumes: `OnboardingContext`, `Actor`, `SubjectStep` (Task 6); `Account`, `StudentProfile` (Task 2); `OnboardingState` (Task 4); тексты, `POLICY_VERSION` (Task 5); `TEXTBOOK_TTL_S` (`bot/pages.py`); `PARENT_SENDS_UP_TO_GRADE`, `new_invite`, `INVITE_TTL_DAYS`.
- Produces: `OWNER_TTL_S = TEXTBOOK_TTL_S`; `ParentSteps(ctx, subjects)`: `ask_grade(actor)`, `choose_grade(actor, account: Account | None, grade: int)`, `ask_consent(actor)`, `give_consent(actor, child: StudentProfile)`, `invite_child(actor, account: Account | None, grade: int)`, `show_status(actor, account: Account)`, `young_children(account) -> list[StudentProfile]`, `on_photo(actor, account, urls: list[str]) -> list[str] | None`, `choose_owner(actor, account, child_id: int) -> list[str] | None`.

- [ ] **Step 1: Тесты**

`tests/test_onboarding_parent.py`:

```python
"""Родитель (спецификация §4.3, §4.7): дети 1–4 класса с фото от родителя, ссылка ребёнку
5–9 класса, список детей, «Чья домашка?»."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from onboarding_kit import BOT, Kit, actor, link_token, make_kit, payloads

from hwcheck.bot.invites import digest
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.parent import OWNER_TTL_S, ParentSteps
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.db.repo import Account, StudentProfile


def parents(kit: Kit) -> ParentSteps:
    return ParentSteps(kit.ctx, SubjectStep(kit.ctx))


async def young_child(kit: Kit, grade: int, user_id: int = 2) -> tuple[Account, StudentProfile]:
    """Родитель добавил ребёнка 1–4 класса, выбрал математику и дал согласие."""
    me = actor(user_id)
    await parents(kit).choose_grade(me, await kit.repo.get_account(me.user_hash), grade)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None
    child = (await kit.repo.children(account.id))[-1]
    await kit.repo.set_subject(child.id, "math")
    await parents(kit).give_consent(me, replace(child, subject="math"))
    [consented] = [c for c in await kit.repo.children(account.id) if c.id == child.id]
    return account, consented


async def test_parent_of_young_child_sends_photos_himself(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    me = actor(2)
    await parents(kit).choose_grade(me, None, 3)
    account = await kit.repo.get_account(me.user_hash)
    assert account is not None and account.role == "parent"
    [child] = await kit.repo.children(account.id)
    assert child.sent_by_parent and (child.grade, child.has_consent) == (3, False)
    question, buttons = kit.last(2)
    assert question == texts.SUBJECT_PARENT and "ob:subject:world_around" in payloads(buttons)

    await parents(kit).ask_consent(me)
    consent, buttons = kit.last(2)
    assert consent == texts.consent_text(texts.PARENT_SENDS_CONSENT)
    assert payloads(buttons) == ["ob:policy", "ob:consent"]
    await parents(kit).give_consent(me, child)
    await parents(kit).give_consent(me, child)  # второе «Согласен»
    assert kit.last(2) == (texts.INSTRUCTION_PARENT, texts.add_child_keyboard())
    assert [e["scenario"] for e in kit.events("consent_given")] == ["parent_sends"]
    [consented] = await kit.repo.children(account.id)
    assert consented.has_consent


async def test_parent_of_older_child_consents_then_forwards_link(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    me = actor(2)
    await parents(kit).choose_grade(me, None, 7)
    assert await kit.repo.get_account(me.user_hash) is None  # до «Согласен» ничего не хранится
    consent, buttons = kit.last(2)
    assert consent == texts.consent_text(texts.PARENT_FIRST_CONSENT.format(grade=7))
    assert payloads(buttons) == ["ob:policy", "ob:pconsent:7"]

    await parents(kit).invite_child(me, None, 7)
    forward, message = kit.texts(2)[-2:]
    assert forward == texts.FORWARD_TO_CHILD
    assert f"https://max.ru/{BOT}?start=c_" in message
    invite = await kit.repo.find_invite(token_hash=digest(link_token(message)))
    assert invite is not None and (invite.kind, invite.grade) == ("parent_invites_student", 7)
    assert [e["scenario"] for e in kit.events("consent_given")] == ["parent_first"]


async def test_status_lists_children_and_waiting_links(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    account, child = await young_child(kit, 2)
    await parents(kit).invite_child(actor(2), account, 7)
    await parents(kit).show_status(actor(2), account)
    assert kit.last(2) == (texts.parent_status([child], [7]), texts.add_child_keyboard())

    lonely = await kit.repo.get_or_create_account("p9", b"x", "parent")
    await parents(kit).show_status(actor(9), lonely)
    assert kit.last(9) == (texts.PARENT_GRADE, texts.grade_keyboard("pgrade"))


async def test_photo_goes_to_check_with_one_young_child(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    older = await kit.repo.get_or_create_account(actor(3).user_hash, b"x", "parent")
    assert await parents(kit).on_photo(actor(3), older, ["u0"]) is None
    assert kit.last(3) == (texts.PARENT_PHOTO_NO_YOUNG, texts.add_child_keyboard())

    account, _ = await young_child(kit, 2)
    assert await parents(kit).on_photo(actor(2), account, ["u1"]) == ["u1"]


async def test_two_young_children_ask_whose_homework(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    me = actor(2)
    account, second_grade = await young_child(kit, 2)
    _, fourth_grade = await young_child(kit, 4)
    steps = parents(kit)

    assert await steps.on_photo(me, account, ["u1"]) is None
    question, buttons = kit.last(2)
    assert question == texts.WHOSE_HOMEWORK
    assert payloads(buttons) == [f"ob:whose:{second_grade.id}", f"ob:whose:{fourth_grade.id}"]
    sent = len(kit.max.sent)
    assert await steps.on_photo(me, account, ["u2"]) is None  # тетрадь вслед за учебником
    assert len(kit.max.sent) == sent  # второй раз не спрашиваем
    assert kit.events("homework_owner_asked")[0]["n_children"] == 2

    assert await steps.choose_owner(me, account, 999) is None  # чужой профиль
    assert await steps.choose_owner(me, account, fourth_grade.id) == ["u1", "u2"]
    assert await steps.on_photo(me, account, ["u3"]) == ["u3"]  # выбор действует час

    kit.clock.now += timedelta(seconds=OWNER_TTL_S)
    assert await steps.on_photo(me, account, ["u4"]) is None
    assert kit.last(2)[0] == texts.WHOSE_HOMEWORK
    kit.clock.now += timedelta(seconds=OWNER_TTL_S)
    assert await steps.choose_owner(me, account, second_grade.id) is None  # фото устарели
    assert kit.last(2)[0] == texts.PHOTOS_EXPIRED
```

- [ ] **Step 2: Тесты падают**

Run: `uv run pytest tests/test_onboarding_parent.py -v`
Expected: FAIL — нет `hwcheck.bot.onboarding.parent`.

- [ ] **Step 3: Реализация**

`src/hwcheck/bot/onboarding/parent.py`:

```python
"""Родитель (спецификация §4.3, §4.7): класс ребёнка; дети 1–4 класса, за которых фото присылает
родитель; ссылка ребёнку 5–9 класса; список детей; «Чья домашка?» при нескольких детях 1–4."""

from __future__ import annotations

from datetime import timedelta

from hwcheck.bot.invites import INVITE_TTL_DAYS, new_invite
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.bot.pages import TEXTBOOK_TTL_S
from hwcheck.bot.subjects import PARENT_SENDS_UP_TO_GRADE
from hwcheck.db.repo import Account, StudentProfile

# фото одной домашки приходят рядом (учебник, затем тетрадь): выбор ребёнка и ожидающие фото живут
# столько же, сколько условия учебника
OWNER_TTL_S = TEXTBOOK_TTL_S


class ParentSteps:
    def __init__(self, ctx: OnboardingContext, subjects: SubjectStep) -> None:
        self._ctx = ctx
        self._subjects = subjects

    async def _parent(self, actor: Actor, account: Account | None) -> Account:
        if account is not None:
            return account
        return await self._ctx.repo.get_or_create_account(
            actor.user_hash, self._ctx.encrypted_id(actor), "parent"
        )

    async def ask_grade(self, actor: Actor) -> None:
        await self._ctx.reply(actor, texts.PARENT_GRADE, texts.grade_keyboard("pgrade"))

    async def choose_grade(self, actor: Actor, account: Account | None, grade: int) -> None:
        """1–4 класс — ребёнок без своего MAX сразу в базе; 5–9 — согласие, класс едет в payload."""
        ctx = self._ctx
        ctx.log("onboarding_grade_chosen", actor, grade=grade, role="parent")
        if grade <= PARENT_SENDS_UP_TO_GRADE:
            parent = await self._parent(actor, account)
            child = await ctx.repo.start_child_by_parent(parent.id, grade, ctx.school_year())
            await self._subjects.ask(actor, child)
            return
        intro = texts.PARENT_FIRST_CONSENT.format(grade=grade)
        buttons = texts.consent_keyboard(f"ob:pconsent:{grade}")
        await ctx.reply(actor, texts.consent_text(intro), buttons)

    async def ask_consent(self, actor: Actor) -> None:
        buttons = texts.consent_keyboard("ob:consent")
        await self._ctx.reply(actor, texts.consent_text(texts.PARENT_SENDS_CONSENT), buttons)

    async def give_consent(self, actor: Actor, child: StudentProfile) -> None:
        ctx = self._ctx
        if await ctx.repo.give_parent_consent(child.id, actor.user_hash, POLICY_VERSION, ctx.now()):
            ctx.log("consent_given", actor, policy_version=POLICY_VERSION, scenario="parent_sends")
        await ctx.reply(actor, texts.INSTRUCTION_PARENT, texts.add_child_keyboard())

    async def invite_child(self, actor: Actor, account: Account | None, grade: int) -> None:
        """«Согласен» за ребёнка 5–9 класса: аккаунт родителя и ссылка с классом и согласием."""
        ctx = self._ctx
        parent = await self._parent(actor, account)
        invite = new_invite("parent_invites_student")
        now = ctx.now()
        await ctx.repo.create_invite(
            invite,
            parent.id,
            now + timedelta(days=INVITE_TTL_DAYS),
            grade=grade,
            policy_version=POLICY_VERSION,
            consent_at=now,
        )
        ctx.log("consent_given", actor, policy_version=POLICY_VERSION, scenario="parent_first")
        ctx.log("invite_created", actor, kind=invite.kind)
        await ctx.reply(actor, texts.FORWARD_TO_CHILD)
        message = texts.CHILD_INVITE_MESSAGE.format(
            link=invite.link(ctx.bot_username), code=invite.display_code
        )
        await ctx.reply(actor, message)

    async def show_status(self, actor: Actor, account: Account) -> None:
        """До меню (этап 3): список детей и [Добавить ребёнка]; никого нет — класс ребёнка."""
        ctx = self._ctx
        children = [c for c in await ctx.repo.children(account.id) if c.has_consent]
        waiting = await ctx.repo.open_child_invites(account.id, ctx.now())
        if not children and not waiting:
            await self.ask_grade(actor)
            return
        status = texts.parent_status(children, [invite.grade for invite in waiting])
        await ctx.reply(actor, status, texts.add_child_keyboard())

    async def young_children(self, account: Account) -> list[StudentProfile]:
        children = await self._ctx.repo.children(account.id)
        return [c for c in children if c.sent_by_parent and c.has_consent]

    async def on_photo(self, actor: Actor, account: Account, urls: list[str]) -> list[str] | None:
        """Фото для проверки или None. Несколько детей 1–4 — «Чья домашка?», фото ждут ответа."""
        ctx = self._ctx
        young = await self.young_children(account)
        if not young:
            await ctx.reply(actor, texts.PARENT_PHOTO_NO_YOUNG, texts.add_child_keyboard())
            return None
        state = await ctx.states.get(actor.user_hash)
        now = ctx.now().timestamp()
        chosen = state.child_id in {c.id for c in young} and _fresh(state.child_chosen_at, now)
        if len(young) == 1 or chosen:
            return urls
        waiting = bool(state.pending_photos) and _fresh(state.pending_at, now)
        pending = [*state.pending_photos, *urls] if waiting else list(urls)
        pending_at = state.pending_at if waiting else now
        update = {"pending_photos": pending, "pending_at": pending_at}
        await ctx.states.set(actor.user_hash, state.model_copy(update=update))
        if not waiting:
            ctx.log("homework_owner_asked", actor, n_children=len(young))
            await ctx.reply(actor, texts.WHOSE_HOMEWORK, texts.whose_keyboard(young))
        return None

    async def choose_owner(self, actor: Actor, account: Account, child_id: int) -> list[str] | None:
        ctx = self._ctx
        if child_id not in {c.id for c in await self.young_children(account)}:
            return None
        state = await ctx.states.get(actor.user_hash)
        now = ctx.now().timestamp()
        if not state.pending_photos or not _fresh(state.pending_at, now):
            await ctx.reply(actor, texts.PHOTOS_EXPIRED)
            return None
        update = {
            "pending_photos": [],
            "pending_at": None,
            "child_id": child_id,
            "child_chosen_at": now,
        }
        await ctx.states.set(actor.user_hash, state.model_copy(update=update))
        ctx.log("homework_owner_chosen", actor)
        return state.pending_photos


def _fresh(saved_at: float | None, now: float) -> bool:
    return saved_at is not None and now - saved_at < OWNER_TTL_S
```

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest tests/test_onboarding_parent.py -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS (5 тестов).

- [ ] **Step 5: Commit**

```bash
git add src/hwcheck/bot/onboarding/parent.py tests/test_onboarding_parent.py
git commit -m "feat: онбординг родителя — дети 1–4 класса, ссылка ребёнку, «Чья домашка?»"
```

---

### Task 9: Маршрутизатор онбординга и встраивание в бота

**Files:**
- Create: `src/hwcheck/bot/onboarding/router.py`, `tests/test_onboarding_router.py`, `tests/test_bot_onboarding.py`
- Modify: `src/hwcheck/bot/handlers.py`

**Interfaces:**
- Consumes: всё из Task 4–8; `parse_start_payload`, `parse_code` (invites); `policy_messages`; `MaxUpdate`; `ChatState` (fsm, для проверки фазы диалога).
- Produces:
  - `hwcheck.bot.onboarding.router`: `CheckPhotos(urls: list[str])`, `Route = Literal["handled", "pass"] | CheckPhotos`, `Step`, `Position(step, account, profile=None)`, `Onboarding(ctx)` с `async route(update: MaxUpdate) -> Route`
  - `Bot.__init__(..., photos=None, onboarding: Onboarding | None = None)`: при `onboarding` апдейт сначала идёт в `route`; `"handled"` — готово, `CheckPhotos` — проверка этих фото, `"pass"` — текущий сценарий.

- [ ] **Step 1: Сценарные тесты маршрутизатора**

`tests/test_onboarding_router.py`:

```python
"""Сценарии онбординга целиком через маршрутизатор (спецификация §4, §12, §15)."""

from pathlib import Path

from onboarding_kit import (
    Kit,
    actor,
    chat,
    code_of,
    link_token,
    make_kit,
    payloads,
    photo,
    press,
    ready_student,
    start,
    text,
)

from hwcheck.bot.fsm import ChatState
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.policy import policy_messages
from hwcheck.bot.onboarding.router import CheckPhotos, Onboarding


def journey_types(kit: Kit, user_id: int) -> list[str]:
    user = actor(user_id).user_hash
    return [e["type"] for e in kit.events() if e["user"] == user and e["type"] != "button_pressed"]


async def test_student_journey_until_parent_consents(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    assert await ob.route(start(1)) == "handled"
    assert kit.last(1) == (texts.HELLO, texts.role_keyboard())
    await ob.route(press(1, "ob:role:student"))
    assert kit.last(1) == (texts.STUDENT_GRADE, texts.grade_keyboard("grade"))
    await ob.route(press(1, "ob:grade:7"))
    await ob.route(press(1, "ob:subject:history"))
    await ob.route(press(1, "ob:subject:math"))
    invite_message = kit.texts(1)[-1]
    assert await ob.route(photo(1, "https://files/1.jpg")) == "handled"
    assert kit.last(1)[0] == texts.PHOTO_BLOCKED
    assert await ob.route(text(1, "ну когда уже")) == "handled"
    assert kit.last(1)[0] == texts.WAITING_PARENT
    assert kit.max.callbacks == ["cb-ob:role:student", "cb-ob:grade:7"] + [
        "cb-ob:subject:history",
        "cb-ob:subject:math",
    ]

    assert await ob.route(start(2, f"p_{link_token(invite_message)}")) == "handled"
    await ob.route(press(2, "ob:accept"))
    assert kit.last(2)[0] == texts.CONSENT_THANKS
    assert kit.max.to_users[-1][0] == 1
    assert await ob.route(photo(1, "https://files/2.jpg")) == "pass"
    assert await ob.route(text(1, "4F7K-92QD")) == "pass"  # у ученика с родителем — просто текст

    assert journey_types(kit, 1) == [
        "bot_started",
        "onboarding_role_chosen",
        "onboarding_grade_chosen",
        "onboarding_subject_chosen",
        "subject_waitlist",
        "onboarding_subject_chosen",
        "invite_created",
        "photo_blocked_no_consent",
    ]
    assert journey_types(kit, 2) == [
        "bot_started",
        "invite_opened",
        "consent_given",
        "child_linked",
        "notify_sent",
    ]


async def test_young_student_and_foreign_buttons(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    await ob.route(press(1, "ob:grade:3"))  # кнопка из старого сообщения — шаг «роль» ещё идёт
    assert kit.texts(1)[-2] == texts.YOUNG_STUDENT
    assert await kit.repo.get_account(actor(1).user_hash) is None

    await ob.route(press(5, "ob:grade:7"))
    subject_step = (texts.SUBJECT_STUDENT, texts.subject_keyboard(7))
    for payload in ("ob:pgrade:3", "ob:whose:1", "ob:consent", "ob:bogus", "ob:grade:²"):
        assert await ob.route(press(5, payload)) == "handled"
        assert kit.last(5) == subject_step, payload
    assert await ob.route(press(5, "tutor:0")) == "handled"  # кнопка проверки до онбординга
    assert kit.max.callbacks[-1] == "cb-tutor:0"
    assert await ob.route(MaxUpdate.model_validate({"update_type": "chat_title_changed"})) == "pass"


async def test_parent_with_two_young_children(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await ob.route(press(2, payload))
    assert kit.last(2)[0] == texts.INSTRUCTION_PARENT
    assert await ob.route(photo(2, "u1")) == CheckPhotos(["u1"])

    for payload in ("ob:addchild", "ob:pgrade:4", "ob:subject:math", "ob:consent"):
        await ob.route(press(2, payload))
    assert await ob.route(photo(2, "u2")) == "handled"
    question, buttons = kit.last(2)
    assert question == texts.WHOSE_HOMEWORK
    assert await ob.route(press(2, payloads(buttons)[1])) == CheckPhotos(["u2"])
    assert await ob.route(photo(2, "u3")) == CheckPhotos(["u3"])

    assert await ob.route(text(2, "что дальше?")) == "handled"
    assert kit.last(2)[0].startswith(texts.PARENT_STATUS_HEADER)
    await kit.ctx.dialogs.set(chat(2), ChatState(phase="tutoring"))
    assert await ob.route(text(2, "получилось 12")) == "pass"  # родитель отвечает тьютору


async def test_parent_first_then_child_joins_by_link(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:role:parent", "ob:pgrade:7"):
        await ob.route(press(2, payload))
    assert await kit.repo.get_account(actor(2).user_hash) is None
    await ob.route(press(2, "ob:pconsent:7"))
    child_message = kit.texts(2)[-1]

    assert await ob.route(start(3, f"c_{link_token(child_message)}")) == "handled"
    assert kit.texts(3)[-2:] == [texts.CHILD_LINKED, texts.SUBJECT_STUDENT]
    assert kit.max.to_users == [(2, texts.CHILD_JOINED.format(grade=7), None)]
    await ob.route(press(3, "ob:subject:math"))
    assert kit.last(3)[0] == texts.INSTRUCTION_STUDENT
    assert await ob.route(photo(3, "u")) == "pass"

    await ob.route(text(2, "как там ребёнок?"))
    assert "• 7 класс — свой MAX" in kit.last(2)[0]
    assert await ob.route(photo(2, "u")) == "handled"
    assert kit.last(2)[0] == texts.PARENT_PHOTO_NO_YOUNG


async def test_code_policy_example_and_resend(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    for payload in ("ob:grade:8", "ob:subject:math"):
        await ob.route(press(1, payload))
    first_message = kit.texts(1)[-1]
    await ob.route(press(1, "ob:resend"))
    assert kit.texts(1)[-2] == texts.NEED_PARENT and kit.texts(1)[-1] != first_message
    await ob.route(press(1, "ob:example"))
    assert kit.last(1)[0] == texts.EXAMPLE

    assert await ob.route(text(2, code_of(first_message).lower())) == "handled"
    assert kit.last(2)[0] == texts.consent_text(texts.CHILD_ASKS_CONSENT.format(grade=8))
    await ob.route(press(2, "ob:policy"))
    assert kit.texts(2)[-len(policy_messages()) :] == policy_messages()
    await ob.route(press(2, "ob:decline"))
    assert kit.max.to_users[-1][1] == texts.PARENT_NOT_ALLOWED
    await ob.route(press(1, "ob:resend"))  # ребёнок отправляет ссылку ещё раз
    assert kit.texts(1)[-2] == texts.NEED_PARENT


async def test_start_with_broken_payload_and_ready_student_start(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    ob = Onboarding(kit.ctx)
    assert await ob.route(start(1, "p_!!")) == "handled"
    assert kit.last(1)[0] == texts.HELLO
    assert kit.events("bot_started")[0]["invite"] is False
    await ready_student(kit, user_id=4)
    assert await ob.route(start(4)) == "handled"
    assert kit.last(4)[0] == texts.INSTRUCTION_STUDENT
```

- [ ] **Step 2: Тесты встраивания в бота**

`tests/test_bot_onboarding.py`:

```python
"""Бот с онбордингом (спецификация §7 «Встраивание», §10.2): до согласия фото не скачивается,
фото с выбранным ребёнком и апдейты готового ученика идут в проверку."""

from pathlib import Path

from onboarding_kit import Kit, make_kit, photo, press, ready_student, text

from hwcheck.bot.handlers import WELCOME, Bot
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.config import Settings


def make_bot(tmp_path: Path) -> tuple[Bot, Kit]:
    kit = make_kit(tmp_path)
    bot = Bot(
        kit.max,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]  # без LLM распознавание упадёт: важен маршрут
        kit.ctx.dialogs,
        kit.ctx.events,
        Settings(_env_file=None),
        onboarding=Onboarding(kit.ctx),
    )
    return bot, kit


async def test_photo_before_consent_is_not_downloaded(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await bot.handle_update(photo(1, "https://files/1.jpg"))
    assert kit.max.downloads == []
    assert kit.last(1)[0] == texts.HELLO
    types = [e["type"] for e in kit.events()]
    assert "photo_blocked_no_consent" in types and "homework_uploaded" not in types


async def test_parent_photo_with_young_child_goes_to_check(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    for payload in ("ob:role:parent", "ob:pgrade:2", "ob:subject:math", "ob:consent"):
        await bot.handle_update(press(2, payload))
    await bot.handle_update(photo(2, "https://files/2.jpg"))
    assert kit.max.downloads == ["https://files/2.jpg"]
    checks = [e["type"] for e in kit.events() if e["type"] in ("homework_uploaded", "check_failed")]
    assert checks == ["homework_uploaded", "check_failed"]


async def test_ready_student_text_reaches_check_dialog(tmp_path: Path) -> None:
    bot, kit = make_bot(tmp_path)
    await ready_student(kit)
    await bot.handle_update(text(1, "привет"))
    assert kit.last(1)[0] == WELCOME
```

- [ ] **Step 3: Тесты падают**

Run: `uv run pytest tests/test_onboarding_router.py tests/test_bot_onboarding.py -v`
Expected: FAIL — нет `hwcheck.bot.onboarding.router`; `Bot` не принимает `onboarding`.

- [ ] **Step 4: Маршрутизатор**

`src/hwcheck/bot/onboarding/router.py`:

```python
"""Маршрутизатор онбординга (спецификация §4, §7): шаг выводится из данных, апдейт идёт в нужный шаг
или дальше — в сценарий проверки (bot/handlers.py).

Кнопки онбординга — `ob:<действие>[:<аргумент>]`, аргумент недоверенный. Кнопка не своего шага
(старое сообщение, чужая роль, выдуманный аргумент) показывает текущий шаг, а не действие.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from hwcheck.bot.invites import parse_code, parse_start_payload
from hwcheck.bot.models import MaxUpdate
from hwcheck.bot.onboarding import texts
from hwcheck.bot.onboarding.context import Actor, OnboardingContext
from hwcheck.bot.onboarding.linking import Linking
from hwcheck.bot.onboarding.parent import ParentSteps
from hwcheck.bot.onboarding.policy import policy_messages
from hwcheck.bot.onboarding.student import StudentSteps
from hwcheck.bot.onboarding.subject import SubjectStep
from hwcheck.bot.subjects import PARENT_SENDS_UP_TO_GRADE
from hwcheck.db.repo import Account, StudentProfile

Step = Literal[
    "role",  # новый пользователь
    "student_subject",  # ученик без предмета
    "waiting_parent",  # ученик без подключённого родителя
    "student_ready",
    "child_subject",  # у родителя ребёнок 1–4 без предмета
    "child_consent",  # у родителя ребёнок 1–4 без согласия
    "parent_ready",
]
_UPDATES = frozenset({"bot_started", "message_callback", "message_created"})
_GRADE = re.compile(r"[1-9]")
_PROFILE_ID = re.compile(r"[0-9]{1,18}")


@dataclass(frozen=True)
class CheckPhotos:
    """Фото, которые онбординг отдаёт в проверку (родитель выбрал, чья домашка)."""

    urls: list[str]


Route = Literal["handled", "pass"] | CheckPhotos


@dataclass(frozen=True)
class Position:
    step: Step
    account: Account | None
    profile: StudentProfile | None = None  # профиль, к которому относится шаг


Action = Callable[[Actor, Position, str], Awaitable[Route | None]]


class Onboarding:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx
        self._subjects = SubjectStep(ctx)
        self._students = StudentSteps(ctx, self._subjects)
        self._parents = ParentSteps(ctx, self._subjects)
        self._linking = Linking(ctx, self._subjects)
        self._actions: dict[str, Action] = {
            "role": self._role,
            "grade": self._grade,
            "pgrade": self._parent_grade,
            "subject": self._subject,
            "consent": self._consent,
            "pconsent": self._parent_consent,
            "accept": self._accept,
            "decline": self._decline,
            "policy": self._policy,
            "example": self._example,
            "resend": self._resend,
            "addchild": self._add_child,
            "whose": self._whose,
        }

    async def route(self, update: MaxUpdate) -> Route:
        chat_id, user_id = update.effective_chat_id, update.effective_user_id
        if update.update_type not in _UPDATES or chat_id is None or user_id is None:
            return "pass"
        actor = Actor.of(chat_id, user_id)
        position = await self._position(await self._ctx.repo.get_account(actor.user_hash))
        if update.update_type == "bot_started":
            return await self._on_start(actor, position, update.payload)
        if update.callback is not None:
            payload, callback_id = update.callback.payload or "", update.callback.callback_id or ""
            return await self._on_callback(actor, position, payload, callback_id)
        if update.message is None:
            return "pass"
        if update.message.image_urls:
            return await self._on_photo(actor, position, update.message.image_urls)
        body = update.message.body
        if body is not None and body.text:
            return await self._on_text(actor, position, body.text)
        return "pass"

    async def _position(self, account: Account | None) -> Position:
        repo = self._ctx.repo
        if account is None:
            return Position("role", None)
        if account.role == "student":
            profile = await repo.own_profile(account.id)
            if profile is None:  # аккаунт ученика и профиль создаются одной транзакцией
                raise RuntimeError("student account without profile")
            if profile.subject is None:
                return Position("student_subject", account, profile)
            step: Step = "student_ready" if profile.has_consent else "waiting_parent"
            return Position(step, account, profile)
        children = await repo.children(account.id)
        unfinished = [c for c in children if c.sent_by_parent and not c.has_consent]
        if unfinished:  # незавершённый ребёнок 1–4 у родителя один (start_child_by_parent)
            child = unfinished[-1]
            step = "child_subject" if child.subject is None else "child_consent"
            return Position(step, account, child)
        return Position("parent_ready", account)

    async def _show(self, actor: Actor, position: Position) -> None:
        """Текущий шаг ещё раз: ответ на текст, фото, старую или чужую кнопку."""
        profile, account = position.profile, position.account
        if position.step == "role":
            await self._ctx.reply(actor, texts.HELLO, texts.role_keyboard())
        elif position.step in ("student_subject", "child_subject") and profile is not None:
            await self._subjects.ask(actor, profile)
        elif position.step == "waiting_parent":
            await self._students.remind_waiting(actor)
        elif position.step == "student_ready":
            await self._ctx.reply(actor, texts.INSTRUCTION_STUDENT)
        elif position.step == "child_consent":
            await self._parents.ask_consent(actor)
        elif position.step == "parent_ready" and account is not None:
            await self._parents.show_status(actor, account)

    async def _on_start(self, actor: Actor, position: Position, payload: str | None) -> Route:
        invite = parse_start_payload(payload)
        self._ctx.log("bot_started", actor, invite=invite is not None)
        if invite is None:
            await self._show(actor, position)
        else:
            kind, token = invite
            await self._linking.open_link(actor, position.account, kind, token)
        return "handled"

    async def _on_text(self, actor: Actor, position: Position, message: str) -> Route:
        if position.step == "student_ready":
            return "pass"  # похожий на код текст тоже: у ученика с родителем код уже не нужен
        code = parse_code(message)
        if code is not None:
            await self._linking.open_code(actor, position.account, code)
            return "handled"
        if position.step == "parent_ready":
            dialog = await self._ctx.dialogs.get(actor.chat_id)
            if dialog.phase != "idle":
                return "pass"  # родитель отвечает на уточнение или в разборе ошибки
        await self._show(actor, position)
        return "handled"

    async def _on_photo(self, actor: Actor, position: Position, urls: list[str]) -> Route:
        if position.step == "student_ready":
            return "pass"
        if position.step == "parent_ready" and position.account is not None:
            chosen = await self._parents.on_photo(actor, position.account, urls)
            return CheckPhotos(chosen) if chosen else "handled"
        self._ctx.log("photo_blocked_no_consent", actor, step=position.step)
        if position.step == "waiting_parent":
            await self._students.block_photo(actor)
        else:
            await self._show(actor, position)
        return "handled"

    async def _on_callback(
        self, actor: Actor, position: Position, payload: str, callback_id: str
    ) -> Route:
        ready = position.step in ("student_ready", "parent_ready")
        if not payload.startswith("ob:") and ready:
            return "pass"
        await self._ctx.max.answer_callback(callback_id)
        if not payload.startswith("ob:"):  # кнопка проверки у того, кто онбординг не прошёл
            await self._show(actor, position)
            return "handled"
        self._ctx.log("button_pressed", actor, payload=payload)
        name, _, arg = payload.removeprefix("ob:").partition(":")
        action = self._actions.get(name)
        route = await action(actor, position, arg) if action is not None else None
        if route is None:
            await self._show(actor, position)
            return "handled"
        return route

    # --- действия кнопок: None — кнопка не для этого шага, маршрутизатор покажет текущий ---

    async def _role(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "role" or arg not in ("student", "parent"):
            return None
        self._ctx.log("onboarding_role_chosen", actor, role=arg)
        if arg == "student":
            await self._ctx.reply(actor, texts.STUDENT_GRADE, texts.grade_keyboard("grade"))
        else:
            await self._parents.ask_grade(actor)
        return "handled"

    async def _grade(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "role" or not _GRADE.fullmatch(arg):
            return None
        await self._students.choose_grade(actor, int(arg))
        return "handled"

    async def _parent_grade(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position) or not _GRADE.fullmatch(arg):
            return None
        await self._parents.choose_grade(actor, position.account, int(arg))
        return "handled"

    async def _subject(self, actor: Actor, position: Position, arg: str) -> Route | None:
        profile = position.profile
        if position.step not in ("student_subject", "child_subject") or profile is None:
            return None
        if await self._subjects.choose(actor, profile, arg):
            if position.step == "student_subject":
                await self._students.after_subject(actor, replace(profile, subject=arg))
            else:
                await self._parents.ask_consent(actor)
        return "handled"

    async def _consent(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "child_consent" or position.profile is None:
            return None
        await self._parents.give_consent(actor, position.profile)
        return "handled"

    async def _parent_consent(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position) or not _GRADE.fullmatch(arg):
            return None
        if int(arg) <= PARENT_SENDS_UP_TO_GRADE:  # за 1–4 класс ссылка ребёнку не создаётся
            return None
        await self._parents.invite_child(actor, position.account, int(arg))
        return "handled"

    async def _accept(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position):
            return None
        await self._linking.accept(actor)
        return "handled"

    async def _decline(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position):
            return None
        await self._linking.decline(actor)
        return "handled"

    async def _policy(self, actor: Actor, position: Position, arg: str) -> Route | None:
        for message in policy_messages():
            await self._ctx.reply(actor, message)
        return "handled"

    async def _example(self, actor: Actor, position: Position, arg: str) -> Route | None:
        await self._ctx.reply(actor, texts.EXAMPLE)
        return "handled"

    async def _resend(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if position.step != "waiting_parent" or position.profile is None:
            return None
        await self._students.invite_parent(actor, position.profile)
        return "handled"

    async def _add_child(self, actor: Actor, position: Position, arg: str) -> Route | None:
        if not _parent_side(position):
            return None
        await self._parents.ask_grade(actor)
        return "handled"

    async def _whose(self, actor: Actor, position: Position, arg: str) -> Route | None:
        account = position.account
        if position.step != "parent_ready" or account is None or not _PROFILE_ID.fullmatch(arg):
            return None
        urls = await self._parents.choose_owner(actor, account, int(arg))
        return CheckPhotos(urls) if urls else "handled"


def _parent_side(position: Position) -> bool:
    """Действие родителя доступно новому пользователю и родителю, но не ученику."""
    return position.account is None or position.account.role == "parent"
```

- [ ] **Step 5: Встраивание в `handlers.py`**

В `src/hwcheck/bot/handlers.py`:

1. Импорт: `from hwcheck.bot.onboarding.router import CheckPhotos, Onboarding`.
2. Конструктор — новый именованный параметр и поле:

```python
    def __init__(
        self,
        max_client: MaxClient,
        llm: GigaChatClient,
        store: StateStore,
        events: EventLog,
        settings: Settings,
        *,
        photos: PhotoStore | None = None,
        onboarding: Onboarding | None = None,
    ) -> None:
        self._max = max_client
        self._llm = llm
        self._store = store
        self._events = events
        self._settings = settings
        self._photos = photos
        # None — ONBOARDING_REQUIRED=false: проверка без онбординга, как до этапа 2
        self._onboarding = onboarding
        self._cache = FileCache(Path(".cache/solver"))
```

3. Начало `_dispatch`:

```python
    async def _dispatch(self, update: MaxUpdate) -> None:
        chat_id = update.effective_chat_id
        if chat_id is None:
            return
        user_id = update.effective_user_id
        if self._onboarding is not None:
            # онбординг первым: до согласия фото не скачивается (спецификация онбординга §10.2)
            route = await self._onboarding.route(update)
            if isinstance(route, CheckPhotos):
                await self._on_photo(chat_id, user_id, route.urls)
                return
            if route == "handled":
                return
        if update.update_type == "bot_started":
```

(дальше `_dispatch` без изменений).

- [ ] **Step 6: Тесты проходят, старые сценарии не изменились**

Run: `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS — новые 9 тестов и все прежние (включая `tests/test_bot.py` без онбординга).

- [ ] **Step 7: Commit**

```bash
git add src/hwcheck/bot/onboarding/router.py src/hwcheck/bot/handlers.py tests/test_onboarding_router.py tests/test_bot_onboarding.py
git commit -m "feat: маршрутизатор онбординга перед сценарием проверки"
```


---

### Task 10: Раннер собирает онбординг; проверки при старте, `.env.example`, runbook

**Files:**
- Modify: `src/hwcheck/bot/runner.py`, `.env.example`, `docs/deploy.md`
- Create: `tests/test_onboarding_runner.py`

**Interfaces:**
- Consumes: `Onboarding` (Task 9), `OnboardingContext` (Task 6), хранилища состояния (Task 4), `PgProfileRepository` (Task 3), `Settings.onboarding_required` (Task 4).
- Produces: в `hwcheck.bot.runner` — `check_onboarding_settings(settings: Settings) -> None` (SystemExit без базы или ключей), `make_onboarding(settings, *, pool, redis_client, dialogs, max_client, events, me) -> Onboarding | None`; в логе старта `onboarding: required` / `onboarding: off`.

- [ ] **Step 1: Тесты**

`tests/test_onboarding_runner.py`:

```python
"""Старт бота с онбордингом (спецификация §11, §13): без базы, ключей, username — не стартует."""

from pathlib import Path
from typing import Any

import fakeredis
import pytest

from hwcheck.bot.fsm import InMemoryStateStore
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.bot.onboarding.state import RedisOnboardingStateStore
from hwcheck.bot.runner import check_onboarding_settings, make_onboarding
from hwcheck.config import Settings
from hwcheck.crypto import new_user_id_key
from hwcheck.events import EventLog


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "onboarding_required": True,
        "database_url": "postgresql://homework@db/homework",
        "id_hash_key": "secret",
        "user_id_key": new_user_id_key(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_onboarding_needs_database_and_id_keys() -> None:
    check_onboarding_settings(Settings(_env_file=None))  # флаг выключен — ничего не нужно
    check_onboarding_settings(settings())
    with pytest.raises(SystemExit, match="DATABASE_URL, USER_ID_KEY"):
        check_onboarding_settings(settings(database_url=None, user_id_key=""))


def test_make_onboarding(tmp_path: Path) -> None:
    # тесты не проверяются mypy: вместо пула и клиента MAX — заглушки
    common: dict[str, Any] = {
        "redis_client": fakeredis.FakeAsyncRedis(),
        "dialogs": InMemoryStateStore(),
        "max_client": object(),
        "events": EventLog(tmp_path / "events.jsonl", "dev"),
    }
    off = make_onboarding(Settings(_env_file=None), pool=None, me={"username": "bot"}, **common)
    assert off is None
    with pytest.raises(SystemExit, match="username"):
        make_onboarding(settings(), pool=object(), me={"name": "Домашка"}, **common)
    with pytest.raises(SystemExit, match="PostgreSQL"):
        make_onboarding(settings(), pool=None, me={"username": "bot"}, **common)
    onboarding = make_onboarding(
        settings(), pool=object(), me={"username": "domashka_bot"}, **common
    )
    assert isinstance(onboarding, Onboarding)
    assert onboarding._ctx.bot_username == "domashka_bot"
    assert isinstance(onboarding._ctx.states, RedisOnboardingStateStore)
```

- [ ] **Step 2: Тесты падают**

Run: `uv run pytest tests/test_onboarding_runner.py -v`
Expected: FAIL — `ImportError: cannot import name 'check_onboarding_settings'`.

- [ ] **Step 3: Раннер**

В `src/hwcheck/bot/runner.py` добавить импорты:

```python
from typing import Any

import asyncpg

from hwcheck.bot.onboarding.context import OnboardingContext
from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.bot.onboarding.state import (
    InMemoryOnboardingStateStore,
    OnboardingStateStore,
    RedisOnboardingStateStore,
)
from hwcheck.db.repo import PgProfileRepository
```

и функции после `configure_ids`:

```python
def check_onboarding_settings(settings: Settings) -> None:
    """ONBOARDING_REQUIRED без базы или ключей id — бот не стартует с понятной ошибкой (§11)."""
    if not settings.onboarding_required:
        return
    required = {
        "DATABASE_URL": settings.database_url,
        "ID_HASH_KEY": settings.id_hash_key,
        "USER_ID_KEY": settings.user_id_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"ONBOARDING_REQUIRED=true: не заданы {', '.join(missing)}")


def make_onboarding(
    settings: Settings,
    *,
    pool: asyncpg.Pool[asyncpg.Record] | None,
    redis_client: Redis | None,
    dialogs: StateStore,
    max_client: MaxClient,
    events: EventLog,
    me: dict[str, Any],
) -> Onboarding | None:
    """Онбординг перед проверкой; None — флаг выключен (аварийный выключатель, спецификация §7)."""
    if not settings.onboarding_required:
        return None
    username = me.get("username")
    if not isinstance(username, str) or not username:
        raise SystemExit(
            "ONBOARDING_REQUIRED=true: у бота нет username в GET /me — ссылки не собрать"
        )
    if pool is None:
        raise SystemExit("ONBOARDING_REQUIRED=true: нет подключения к PostgreSQL")
    states: OnboardingStateStore = (
        RedisOnboardingStateStore(redis_client)
        if redis_client is not None
        else InMemoryOnboardingStateStore()
    )
    ctx = OnboardingContext(
        max=max_client,
        repo=PgProfileRepository(pool),
        states=states,
        dialogs=dialogs,
        events=events,
        cipher=UserIdCipher(settings.user_id_key),
        bot_username=username,
    )
    return Onboarding(ctx)
```

В `run_polling`:

1. После блока `configure_ids` — `check_onboarding_settings(settings)`.
2. Перед `async with contextlib.AsyncExitStack() as resources:` — `pool: asyncpg.Pool[asyncpg.Record] | None = None`.
3. Создание бота заменить на:

```python
        onboarding = make_onboarding(
            settings,
            pool=pool,
            redis_client=redis_client,
            dialogs=store,
            max_client=max_client,
            events=events,
            me=me,
        )
        logger.info("onboarding: %s", "required" if onboarding is not None else "off")
        bot = Bot(
            max_client,
            llm,
            store,
            events,
            settings,
            photos=_make_photo_store(settings),
            onboarding=onboarding,
        )
```

- [ ] **Step 4: `.env.example` и runbook**

В `.env.example` после строки `# DATABASE_URL=...`:

```bash
# Онбординг и согласие родителя до проверки: true — фото проверяются только после согласия;
# false — аварийный выключатель, бот работает без онбординга. При true нужны DATABASE_URL, ID_HASH_KEY,
# USER_ID_KEY и username бота (GET /me)
# ONBOARDING_REQUIRED=false
```

В `docs/deploy.md` после раздела «PostgreSQL и ключи id (онбординг, этап 1)» — новый раздел:

````markdown
## Онбординг: включение и аварийный выключатель (этап 2)

Миграция `002_children.sql` применяется при старте бота и пересоздаёт пустые таблицы профилей и согласий;
если в них уже есть данные — бот не стартует (в логе `002_children: в student_profiles…`).

```bash
# на VPS, в /opt/max-homework-ai; перед этим — нет событий за 10 минут
grep -q '^ONBOARDING_REQUIRED=' .env \
  && sed -i 's/^ONBOARDING_REQUIRED=.*/ONBOARDING_REQUIRED=true/' .env \
  || echo 'ONBOARDING_REQUIRED=true' >> .env
docker compose up -d --force-recreate bot        # env_file перечитывается только при пересоздании
docker compose logs --tail 20 bot                # «onboarding: required»
```

Выключить (поломка онбординга мешает проверке): то же с `ONBOARDING_REQUIRED=false`, в логе
`onboarding: off`. Профили и согласия в базе остаются.

Сбросить тестовый аккаунт для повторного прохода (хэш — поле `user` в `var/events.jsonl`):

```bash
docker exec homework-postgres psql -U homework -c "
  DELETE FROM student_profiles WHERE user_id IS NULL
    AND parent_user_id = (SELECT id FROM users WHERE max_user_hash = '<хэш>');
  DELETE FROM users WHERE max_user_hash = '<хэш>';"
```
````

- [ ] **Step 5: Все проверки**

Run: `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hwcheck/bot/runner.py .env.example docs/deploy.md tests/test_onboarding_runner.py
git commit -m "feat: бот собирает онбординг при ONBOARDING_REQUIRED, проверки при старте"
```

---

### Task 11: Ревью, слияние, выкатка и живой тест этапа 2

**Files:**
- Modify: `HISTORY.md`, `TODO.md`, `docs/PROJECT_MEMORY.md`, при необходимости `docs/legal/privacy-policy-v0.md` (реквизиты от Кирилла)

**Interfaces:**
- Consumes: всё из Task 1–10.
- Produces: этап 2 в prod; живой тест на двух аккаунтах; решение, оставлять ли флаг включённым.

- [ ] **Step 1: Ревью** — параллельно агенты `ecc:python-reviewer` и `ecc:security-reviewer` на `git diff main...HEAD` (без команд, меняющих дерево; фокус безопасности: payload кнопок, коды и лимит, сырые id, гонки согласия). Исправить CRITICAL/HIGH, MEDIUM — по возможности; прогнать `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`.

- [ ] **Step 2: PR** — `git push -u origin feat/onboarding-stage2`, `gh pr create` (тело: что сделано, изменение спецификации 16.09, пересоздание схемы миграцией 002, флаг по умолчанию выключен, план проверки); дождаться зелёного CI (`gh pr checks --watch`), включая тесты PostgreSQL.

- [ ] **Step 3: Тишина в боте и слияние** — на VPS нет событий за 10 минут (скрипт из `docs/deploy.md`), затем слияние (`gh pr merge <номер> --merge`; при GraphQL-ошибке — `gh api -X PUT repos/KirillAISREDA/max-homework-ai/pulls/<номер>/merge -f merge_method=merge`).

- [ ] **Step 4: Выкатка с выключенным флагом**

```bash
ssh root@193.247.73.243
cd /opt/max-homework-ai && git pull
cp .env .env.bak-$(date +%F)-stage2 && chmod 600 .env.bak-*
docker compose up -d --build bot            # Redis и PostgreSQL не трогаем
docker compose logs --tail 20 bot
docker exec homework-postgres psql -U homework -c 'SELECT name FROM schema_migrations'
```

Expected: в логе `migrations applied: 002_children.sql`, `onboarding: off`, `bot started`; в `schema_migrations` — обе миграции; прежний сценарий проверки работает (Кирилл присылает фото).

- [ ] **Step 5: Кирилл — реквизиты в политике** (если есть до теста): ФИО, ИНН, ОГРНИП и контакт в `docs/legal/privacy-policy-v0.md` — коммит в main через PR `docs:`; без них живой тест идёт на черновике с полями в скобках.

- [ ] **Step 6: Включить онбординг** — раздел «Онбординг: включение» в `docs/deploy.md`; в логе `onboarding: required`.

- [ ] **Step 7: Живой тест (Кирилл + второй аккаунт)** — отмечать результат каждого пункта; спорное — фото экрана и `trace_id`:
  0. До Step 6 (включения флага): `docker compose build bot` на VPS проходит — образ собирается с `docs/legal` (финальное ревью, F1); иначе бот с флагом не стартует без текста политики.
  1. Аккаунт A (бот уже запущен ранее) → любое сообщение → «Кто ты?» → Я ученик → 7 → История («пока учу», лист ожидания) → Математика → «нужно разрешение» + сообщение со ссылкой и кодом. Фото → «Сначала нужно разрешение», фото не в `var/photos`.
  2. Аккаунт B открывает ссылку из сообщения A: **приходит ли метка `p_` при уже запущенном у B боте** (риск §16) — если нет, B отправляет код текстом. «Полный текст» — политика сообщениями. «Согласен» → B: «Спасибо!», A: «Родитель разрешил! 🎉». A присылает фото → проверка как раньше.
  3. B → любой текст → список детей → [Добавить ребёнка] → 2 класс → Математика → «Согласен» → фото → проверка.
  4. B → [Добавить ребёнка] → 4 класс → Математика → «Согласен» → фото → «Чья это домашка?» → «4 класс» → проверка; следующее фото в течение часа — без вопроса.
  5. Сброс A (раздел deploy.md), затем B → [Добавить ребёнка] → 8 класс → «Согласен» → пересылает сообщение A → A открывает ссылку → предмет → инструкция; B: «Ребёнок (8 класс) подключился ✅».
  6. Сброс A, A снова ученик 7 класса; B открывает ссылку и жмёт «Отказать» → A: «Родитель пока не разрешил» + [Отправить ссылку ещё раз] → новая ссылка приходит.
  7. Сброс A, A — «Я ученик» → 3 класс → ссылка на бота для родителя; в базе A нет (`SELECT count(*) FROM users WHERE max_user_hash = '<хэш A>'` → 0).
  8. Воронка в `var/events.jsonl`: события §12 с `env=test` для обоих аккаунтов (`TEST_USERS`).
  9. B (дети 2 и 4 класса из пунктов 3–4) → [Добавить ребёнка] → 3 класс, предмет не выбирать → фото → «Чья это домашка?» → «2 класс» → проверка: незавершённый третий ребёнок не мешает.
  10. Двойное нажатие «Согласен»: B доводит третьего ребёнка из пункта 9 (Математика) и жмёт «Согласен» быстро два раза; в пункте 12 так же — «Согласен» по ссылке A. Второе нажатие не создаёт второго активного согласия (`SELECT count(*) FROM consents WHERE revoked_at IS NULL`) и не даёт ошибок в `var/bot.log`.
  11. Ответ на «Чья домашка?» через ~45 минут: B присылает фото, выбирает ребёнка спустя ~45 минут (фото ждут до часа) → проверка проходит, то есть URL фото MAX живут столько; если скачивание падает — записать `trace_id`.
  12. Сброс и повторная связка с другим родителем: сброс A и B (оба хэша, раздел `docs/deploy.md`) → A — ученик 7 класса, ссылку открывает B → «Согласен» → сброс только B → у A снова «ждём родителя» → [Отправить ссылку ещё раз] → ссылку открывает другой аккаунт-родитель (третий аккаунт или B после сброса) → «Согласен» → A: «Родитель разрешил! 🎉», без ошибки `consents_one_active` в `var/bot.log`.

- [ ] **Step 8: Решение по флагу** — `ONBOARDING_REQUIRED=true` в prod держится только на время живого теста, пока нет меню отзыва согласия (этап 3), вычитки политики юристом и реквизитов оператора в политике (§14); после теста — `false`, если Кирилл не решил иначе (раздел «Онбординг: включение» в `docs/deploy.md`). Записать решение в `HISTORY.md`.

- [ ] **Step 9: Документы сессии** — `HISTORY.md` (этап 2: что сделано, результаты живого теста по пунктам, метка deep link у запущенного бота, решение по флагу), `TODO.md` (этап 2 закрыт; следующий — план этапа 3; реквизиты ИП в политику — Кирилл), `docs/PROJECT_MEMORY.md` (§2 состояние онбординга, §4 решения 16.09: 1–4 через родителя и несколько детей, §8 артефакты `bot/onboarding/`, `docs/legal/`, §9 новые грабли); commit `docs: …` через PR.

---

## Самопроверка плана этапа 2 по спецификации

- §2 решения 16.09 (1–4 через родителя, несколько детей) — Task 1 (схема), Task 2–3 (`start_child_by_parent`, `children`, `give_parent_consent`), Task 8 (`choose_grade`, `on_photo`, `choose_owner`), Task 9 (кнопки `pgrade`, `consent`, `addchild`, `whose`).
- §4 шаг из данных, Redis только для временного — Task 9 (`_position`), Task 4 (`OnboardingState`: ссылка, фото, выбранный ребёнок).
- §4.1 ученик: роль, класс (1–4 — ссылка на бота, ничего не хранится), предмет и лист ожидания, ссылка и код родителю, блокировка фото, пример, инструкция после согласия — Task 6, Task 9.
- §4.2 родитель по ссылке: согласие одной транзакцией, отказ без сохранения родителя, сообщение ребёнку — Task 2–3, Task 7. Режим уведомлений (п.3) — этап 4.
- §4.3 родитель первым: класс → 1–4 (§4.7) / 5–9 согласие с классом в payload → ссылка `c_` с классом → ребёнок выбирает только предмет, родителю «подключился» — Task 8, Task 7 (`_join_child`), Task 9. [Добавить ребёнка] — Task 8–9.
- §4.4 правила: одноразовые ссылки на 7 дней, устаревшая/использованная, один родитель у ребёнка и гонка (блокировка строк), роль не та, код с лимитом 5 в час, битая метка → обычный старт — Task 2–3, Task 7, Task 9. Самосвязка недостижима — зафиксировано в спецификации, `CHECK` в схеме.
- §4.5 до меню: родитель без детей 1–4 прислал фото; список детей на текст — Task 8–9. Меню, `/menu`, `/help` — этап 3.
- §4.6 переход класса — этап 3 (`grade_year`, `grade_asked_year` заполняются уже сейчас — Task 2–3).
- §4.7 дети 1–4: незавершённый один, «Чья домашка?» с копящимися фото и выбором на час, одинаковые классы — Task 2–3, Task 5 (`whose_keyboard`), Task 8.
- §6 схема — Task 1; `code_hash` вместо `short_code` — отступление этапа 1 сохраняется.
- §7 компоненты пакета `bot/onboarding/`, `db/repo.py`, `db/memory.py`, встраивание в `handlers.py`, `ONBOARDING_REQUIRED` — Task 2–10.
- §10.2 до согласия фото не скачивается — Task 9 (`_on_photo`, тест `test_photo_before_consent_is_not_downloaded`); §10.3 выжимка и полный текст политики — Task 5; §10.4 удаление — этап 3.
- §11 транзакции и идемпотентность повторных нажатий, `notify_failed`, бот не стартует без ключей/базы — Task 2–3, Task 6 (`notify`), Task 7–8, Task 10.
- §12 события: `onboarding_role_chosen`, `onboarding_grade_chosen`, `onboarding_parent_required`, `onboarding_subject_chosen`, `subject_waitlist`, `invite_created`, `invite_opened` (с `rate_limited`), `consent_given` (сценарий), `consent_declined`, `child_linked`, `homework_owner_asked`, `homework_owner_chosen`, `photo_blocked_no_consent`, `notify_sent`, `notify_failed` — Task 6–9. `notify_mode_set`, `grade_confirmed`, `graduated`, `data_deleted` — этапы 3–4.
- §13 `ONBOARDING_REQUIRED` — Task 4, Task 10.
- §14 живой тест за флагом — Task 11; юрист и реальные пользователи — этап 5.
- §15 юнит (тексты, политика, состояние, клиент), сценарные (фейк-хранилище + фейк MAX: все пути §4.1–4.7), интеграционные (контракт хранилища на PostgreSQL, миграция) — Task 1–10.
