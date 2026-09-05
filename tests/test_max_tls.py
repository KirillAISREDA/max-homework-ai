"""TLS-доверие клиента MAX: platform-api2.max.ru подписан НУЦ Минцифры, стандартного
хранилища certifi недостаточно (сессия 9). Доп. корень добавляется к certifi, а не
заменяет его — медиа могут отдаваться с хостов под публичными CA."""

import ssl
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot import max_api
from hwcheck.bot.max_api import MaxClient, ssl_verify
from hwcheck.config import Settings

RU_ROOT = Path("certs/russian_trusted_root_ca.cer")


def _common_names(ctx: ssl.SSLContext) -> set[str]:
    names: set[str] = set()
    for cert in ctx.get_ca_certs():
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    names.add(value)
    return names


def test_ssl_verify_without_bundle_uses_httpx_default() -> None:
    assert ssl_verify(None) is True


def test_ssl_verify_adds_russian_root_on_top_of_certifi() -> None:
    ctx = ssl_verify(str(RU_ROOT))
    assert isinstance(ctx, ssl.SSLContext)
    names = _common_names(ctx)
    assert "Russian Trusted Root CA" in names
    # certifi-корни остались: без них упадут хосты под публичными CA
    assert len(names) > 50


def test_ssl_verify_missing_file_is_loud() -> None:
    with pytest.raises(FileNotFoundError):
        ssl_verify("certs/no_such_file.cer")


@pytest.mark.asyncio
async def test_max_client_passes_verify_to_both_http_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(max_api.httpx, "AsyncClient", FakeAsyncClient)
    async with MaxClient("token", ca_bundle=str(RU_ROOT)):
        pass
    assert len(captured) == 2  # API-клиент и клиент для скачивания медиа
    assert captured[0]["verify"] is captured[1]["verify"]  # один контекст на оба
    for kwargs in captured:
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        assert "Russian Trusted Root CA" in _common_names(kwargs["verify"])


def test_settings_read_max_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_CA_BUNDLE", "certs/russian_trusted_root_ca.cer")
    assert Settings(_env_file=None).max_ca_bundle == "certs/russian_trusted_root_ca.cer"  # type: ignore[call-arg]
