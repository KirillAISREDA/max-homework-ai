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
