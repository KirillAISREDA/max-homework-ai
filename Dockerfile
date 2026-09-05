# Бот «Домашка» (MAX, long polling). Один контейнер, без входящих портов.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Версия совпадает с локальной (формат uv.lock)
RUN pip install --no-cache-dir uv==0.12.7

WORKDIR /app

# Сначала только зависимости — слой кэшируется, пока не меняется uv.lock
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Исходники, промпты (версионированные файлы, читаются с диска) и корень НУЦ Минцифры
COPY src ./src
COPY prompts ./prompts
COPY certs ./certs
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# var/ — журнал событий и marker polling, .cache/ — кэш солвера; оба монтируются с хоста
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /app/var /app/.cache \
    && chown -R app:app /app
USER app

CMD ["python", "-m", "hwcheck", "bot"]
