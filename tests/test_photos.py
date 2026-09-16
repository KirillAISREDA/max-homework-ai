"""Хранилище фото пользователей: без исходников спорные случаи OCR не перепроверить."""

import os
from pathlib import Path

import pytest

from hwcheck.photos import PhotoStore

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
PNG = b"\x89PNG\r\n\x1a\nfake-png"
DAY = 86400.0


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_save_writes_file_under_day_dir_with_anonymized_user(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path, ttl_days=30, clock=Clock(1_789_300_000.0))
    key = store.save("a1b2c3d4e5f60718", JPEG)
    path = tmp_path / key
    assert path.read_bytes() == JPEG
    assert path.suffix == ".jpg"
    assert path.parent.name == "2026-09-13"
    assert path.name.startswith("a1b2c3d4e5f60718-")
    assert store.save(None, PNG).endswith(".png")
    assert "anon-" in store.save(None, b"unknown")


def test_saves_of_same_user_do_not_overwrite(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path, ttl_days=30, clock=Clock(1_789_300_000.0))
    assert store.save("u1", JPEG) != store.save("u1", JPEG)


def test_purge_removes_expired_files_and_empty_dirs(tmp_path: Path) -> None:
    clock = Clock(1_789_300_000.0)
    store = PhotoStore(tmp_path, ttl_days=30, clock=clock)
    old = tmp_path / store.save("u1", JPEG)
    fresh = tmp_path / store.save("u2", JPEG)
    os.utime(old, (clock.now - 31 * DAY, clock.now - 31 * DAY))
    os.utime(fresh, (clock.now - 29 * DAY, clock.now - 29 * DAY))
    other_day = tmp_path / "2026-08-01"
    other_day.mkdir()
    (other_day / "u3-x.jpg").write_bytes(JPEG)
    os.utime(other_day / "u3-x.jpg", (clock.now - 40 * DAY, clock.now - 40 * DAY))

    assert store.purge_expired() == 2
    assert not old.exists()
    assert fresh.exists()
    assert not other_day.exists()


def test_save_purges_at_most_once_per_hour(tmp_path: Path) -> None:
    clock = Clock(1_789_300_000.0)
    store = PhotoStore(tmp_path, ttl_days=1, clock=clock)
    first = tmp_path / store.save("u1", JPEG)  # первый save сразу чистит (после рестарта)
    os.utime(first, (clock.now - 2 * DAY, clock.now - 2 * DAY))

    clock.now += 60
    store.save("u1", JPEG)
    assert first.exists()  # чистка была минуту назад — не повторяем на каждое фото

    clock.now += 3600
    store.save("u1", JPEG)
    assert not first.exists()


def test_purge_tolerates_file_removed_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ручное `rm` по запросу пользователя во время чистки не прерывает её."""
    clock = Clock(1_789_300_000.0)
    store = PhotoStore(tmp_path, ttl_days=1, clock=clock)
    gone = tmp_path / store.save("u1", JPEG)
    old = tmp_path / store.save("u2", JPEG)
    for path in (gone, old):
        os.utime(path, (clock.now - 2 * DAY, clock.now - 2 * DAY))
    real_unlink = Path.unlink

    def racing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == gone:
            real_unlink(self)
            raise FileNotFoundError(self)  # файл удалили между iterdir и unlink
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", racing_unlink)
    store.purge_expired()
    assert not old.exists()


def test_save_writes_photo_even_if_purge_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PhotoStore(tmp_path, ttl_days=1, clock=Clock(1_789_300_000.0))

    def broken_purge() -> int:
        raise PermissionError("чужой файл в каталоге фото")

    monkeypatch.setattr(store, "purge_expired", broken_purge)
    key = store.save("u1", JPEG)
    assert (tmp_path / key).read_bytes() == JPEG


def test_load_reads_back_saved_photo(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path / "photos", ttl_days=30)
    key = store.save("u1", JPEG)
    assert store.load(key) == JPEG


def test_load_rejects_path_traversal_outside_root(tmp_path: Path) -> None:
    """`rel_path` может прийти из недоверенного состояния — выход за `root` не должен читать
    произвольный файл на диске (fix round 1, code review 17.09)."""
    store = PhotoStore(tmp_path / "photos", ttl_days=30)
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")

    with pytest.raises(FileNotFoundError):
        store.load("../secret.txt")
