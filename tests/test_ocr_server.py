"""OCR-сервис: HTTP-обёртка над движком; движок подменяется фейком."""

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest
from ocr.engine import FakeEngine
from ocr.server import make_handler


@pytest.fixture
def server_url() -> Iterator[str]:
    engine = FakeEngine(words=[{"text": "cat", "box": [1, 2, 3, 4], "confidence": 0.9, "line": 0}])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()


def test_health_and_recognize(server_url: str) -> None:
    with urllib.request.urlopen(f"{server_url}/health") as response:
        assert json.loads(response.read()) == {"status": "ok", "engine": "fake"}
    request = urllib.request.Request(
        f"{server_url}/recognize", data=b"img", headers={"Content-Type": "image/jpeg"}
    )
    with urllib.request.urlopen(request) as response:
        body = json.loads(response.read())
    assert body["words"][0]["text"] == "cat" and body["seconds"] >= 0


def raw_post(url: str, headers: str) -> tuple[int, dict[str, object]]:
    """Запрос «как есть»: urllib не даст подделать Content-Length, а проверять надо именно его."""
    host, port = url.removeprefix("http://").split(":")
    request = f"POST /recognize HTTP/1.0\r\nHost: {host}\r\n{headers}\r\n\r\n".encode()
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def test_content_length_not_a_number_is_400(server_url: str) -> None:
    status, body = raw_post(server_url, "Content-Length: abc")
    assert (status, body) == (400, {"error": "bad content-length"})


def test_engine_failure_hides_details_from_client(server_url: str) -> None:
    """Текст исключения движка (пути, версии) остаётся в логе, клиенту — общая формулировка."""

    class Broken:
        name = "broken"

        def recognize(self, image: bytes) -> list[dict[str, object]]:
            raise RuntimeError("/app/weights/secret.pth missing")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Broken()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/recognize", data=b"x"
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        payload = exc.value.read().decode()
        assert exc.value.code == 500
        assert json.loads(payload) == {"error": "recognize failed"}
        assert "secret.pth" not in payload
    finally:
        httpd.shutdown()


def test_second_recognize_while_busy_is_503() -> None:
    """Пик ReadingPipeline ~3 ГБ: два прогона разом — OOM контейнера, лучше честный 503."""
    started, release = threading.Event(), threading.Event()

    class Slow:
        name = "slow"

        def recognize(self, image: bytes) -> list[dict[str, object]]:
            started.set()
            release.wait(timeout=5)
            return []

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Slow(), busy_timeout_s=0.1))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/recognize"
    try:
        first = threading.Thread(
            target=lambda: urllib.request.urlopen(urllib.request.Request(url, data=b"img")).read()
        )
        first.start()
        assert started.wait(timeout=5)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(url, data=b"img2"))
        assert exc.value.code == 503 and json.loads(exc.value.read()) == {"error": "busy"}
    finally:
        release.set()
        first.join(timeout=5)
        httpd.shutdown()


def test_socket_timeout_is_set() -> None:
    """Без таймаута сокета молчащий клиент держит тред обработчика вечно."""
    assert make_handler(FakeEngine()).timeout == 30
