"""OCR-сервис: HTTP-обёртка над движком; движок подменяется фейком."""

import json
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


def test_engine_failure_is_500(server_url: str) -> None:
    class Broken:
        name = "broken"

        def recognize(self, image: bytes) -> list[dict[str, object]]:
            raise RuntimeError("no weights")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Broken()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/recognize", data=b"x",
            headers={"Content-Type": "image/png"},
        )  # fmt: skip
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code == 500 and json.loads(exc.value.read())["error"] == "no weights"
    finally:
        httpd.shutdown()
