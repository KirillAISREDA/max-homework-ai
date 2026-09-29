"""Параллельные чаты не должны ждать чужой тяжёлой работы (ревью 29.09).

Диспетчер обрабатывает чаты параллельно, но цикл событий один: синхронная обработка фото или
зависшая очистка в нём останавливает всех. Тесты с управляемыми задержками этого не видят —
здесь работа настоящая, блокирующая.
"""

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hwcheck.bot.onboarding.router import Onboarding
from hwcheck.config import Settings
from hwcheck.llm import gigachat_client
from hwcheck.llm.gigachat_client import GigaChatClient
from hwcheck.pipeline import vision
from hwcheck.subjects.russian import recognize as ru_recognize
from onboarding_kit import make_kit, start
from test_vision_two_stage import STRUCTURED, TRANSCRIPT, FakeTwoStageClient, make_image


@pytest.mark.parametrize("module", [vision, ru_recognize])
async def test_image_processing_runs_outside_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, module: Any
) -> None:
    """Декодирование и сжатие фото — в потоке: кнопка другого пользователя не ждёт чужой альбом."""
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = module.normalize_image

    def spy(image: bytes) -> bytes:
        threads.append(threading.get_ident())
        return real(image)

    monkeypatch.setattr(module, "normalize_image", spy)
    client = FakeTwoStageClient([TRANSCRIPT], [STRUCTURED])
    if module is vision:
        await vision.recognize_page_two_stage(
            client, make_image(), vision_model="v", structure_model="s"
        )
    else:
        with pytest.raises(Exception):  # noqa: B017, PT011 — ответ не тот, важен только поток
            await ru_recognize.recognize_page(client, make_image(), model="v")
    assert threads and loop_thread not in threads


async def test_slow_file_cleanup_is_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """Удаление фото из хранилища GigaChat при остановке не держит процесс: осиротевшая задача
    писала бы в уже закрытые клиенты."""
    monkeypatch.setattr(gigachat_client, "CLEANUP_TIMEOUT_S", 0.05)
    client = GigaChatClient(Settings(gigachat_credentials="test", _env_file=None))
    inner: Any = client._client

    async def upload(_file: Any) -> Any:
        return type("Uploaded", (), {"id_": "file-1"})()

    async def chat(_payload: Any) -> Any:
        message = type("Message", (), {"content": "ok"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice], "usage": None})()

    async def delete(_file_id: str) -> None:
        await asyncio.sleep(30)

    inner.aupload_file, inner.achat, inner.adelete_file = upload, chat, delete
    started = time.perf_counter()
    result = await client.analyze_image(b"image", prompt="p", model="m")
    assert result.content == "ok"
    assert time.perf_counter() - started < 5


async def test_onboarding_of_one_user_is_serialized(tmp_path: Path) -> None:
    """Состояние онбординга ключуется пользователем, очередь диспетчера — чатом: два апдейта
    одного пользователя из разных чатов не должны идти через онбординг одновременно."""
    kit = make_kit(tmp_path)
    onboarding = Onboarding(kit.ctx)
    inside = 0
    peak = 0
    real = kit.ctx.repo.get_account

    async def slow(user_hash: str) -> Any:
        nonlocal inside, peak
        inside += 1
        peak = max(peak, inside)
        await asyncio.sleep(0.01)
        inside -= 1
        return await real(user_hash)

    kit.ctx.repo.get_account = slow  # type: ignore[method-assign]
    same_user = [start(1), start(1), start(1)]
    await asyncio.gather(*(onboarding.route(update) for update in same_user))
    assert peak == 1

    peak = 0
    await asyncio.gather(onboarding.route(start(2)), onboarding.route(start(3)))
    assert peak == 2  # разные пользователи друг друга не ждут
