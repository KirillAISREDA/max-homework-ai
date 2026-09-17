"""HTTP-обёртка OCR: GET /health, POST /recognize (байты изображения → слова). Стандартная
библиотека: сервис маленький, а зависимости движка и так тяжёлые."""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ocr.engine import Engine, engine_from_env

logger = logging.getLogger("ocr")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SOCKET_TIMEOUT_S = 30  # молчащий клиент иначе держит тред обработчика вечно
BUSY_TIMEOUT_S = 5.0  # столько ждём освобождения движка, дальше честный 503


def make_handler(
    engine: Engine, *, busy_timeout_s: float = BUSY_TIMEOUT_S
) -> type[BaseHTTPRequestHandler]:
    # один прогон движка за раз: пик 1,46 ГБ при лимите 2 ГБ (спайк памяти 17.09) — два
    # параллельных прогона не помещаются, то есть потеря и второй проверки, и первой
    slot = threading.Semaphore(1)

    class Handler(BaseHTTPRequestHandler):
        timeout = SOCKET_TIMEOUT_S

        def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
            if self.path != "/health":
                self._json(404, {"error": "not found"})
                return
            self._json(200, {"status": "ok", "engine": engine.name})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/recognize":
                self._json(404, {"error": "not found"})
                return
            length = self._content_length()
            if length is None:
                self._json(400, {"error": "bad content-length"})
                return
            if not 0 < length <= MAX_IMAGE_BYTES:
                self._json(413, {"error": "image size"})
                return
            image = self.rfile.read(length)
            if not slot.acquire(timeout=busy_timeout_s):
                self._json(503, {"error": "busy"})
                return
            started = time.perf_counter()
            try:
                words = engine.recognize(image)
            except Exception:
                # подробность (пути к весам, версии) — только в лог, клиенту общая формулировка
                logger.exception("recognize failed")
                self._json(500, {"error": "recognize failed"})
                return
            finally:
                slot.release()
            self._json(200, {"words": words, "seconds": time.perf_counter() - started})

        def _content_length(self) -> int | None:
            """Длина тела; None — заголовок есть, но это не число (запрос не наш)."""
            raw = self.headers.get("Content-Length")
            if raw is None:
                return 0
            try:
                return int(raw)
            except ValueError:
                return None

        def _json(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.info("%s " + format, self.address_string(), *args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    engine = engine_from_env()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), make_handler(engine))
    logger.info("ocr started: engine=%s", engine.name)
    server.serve_forever()


if __name__ == "__main__":
    main()
