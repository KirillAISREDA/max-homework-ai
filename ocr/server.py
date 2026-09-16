"""HTTP-обёртка OCR: GET /health, POST /recognize (байты изображения → слова). Стандартная
библиотека: сервис маленький, а зависимости движка и так тяжёлые."""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ocr.engine import Engine, engine_from_env

logger = logging.getLogger("ocr")
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def make_handler(engine: Engine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
            if self.path != "/health":
                self._json(404, {"error": "not found"})
                return
            self._json(200, {"status": "ok", "engine": engine.name})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/recognize":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_IMAGE_BYTES:
                self._json(413, {"error": "image size"})
                return
            image = self.rfile.read(length)
            started = time.perf_counter()
            try:
                words = engine.recognize(image)
            except Exception as exc:
                logger.exception("recognize failed")
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"words": words, "seconds": time.perf_counter() - started})

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
