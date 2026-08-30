import pytest

from hwcheck.config import Settings
from hwcheck.prompts import load_prompt


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.gigachat_scope == "GIGACHAT_API_PERS"
    assert s.gigachat_verify_ssl_certs is True
    assert "Max" in s.vision_model


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "secret")
    monkeypatch.setenv("VISION_MODEL", "GigaChat-3-Max")
    s = Settings(_env_file=None)
    assert s.gigachat_credentials == "secret"
    assert s.vision_model == "GigaChat-3-Max"


def test_vision_prompt_v1_loads() -> None:
    prompt = load_prompt("vision", "v1")
    assert "JSON" in prompt
    assert "confidence" in prompt
