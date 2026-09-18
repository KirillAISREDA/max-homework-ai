# OCR-сервис «Домашки»: ReadingPipeline (ai-forever, MIT) на ONNX/CPU без языковой модели и без
# torch. Рецепт — docs/research/2026-09-17-readingpipeline-memory.md (пик 1,46 ГБ на страницу).
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends git g++ libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ocrsvc/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# апстрим не запинен по хэшу: patch_no_torch.py падает assert'ом на сборке, если структура
# файлов изменилась (явная ошибка вместо тихо сломанного пайплайна)
RUN git clone --depth 1 https://github.com/ai-forever/ReadingPipeline.git \
    && git clone --depth 1 https://github.com/ai-forever/OCR-model.git \
    && git clone --depth 1 https://github.com/ai-forever/SEGM-model.git
RUN printf 'class CTCBeamDecoder:\n    def __init__(self, *a, **kw):\n        raise NotImplementedError("LM disabled")\n' \
        > /usr/local/lib/python3.10/site-packages/ctcdecode.py
COPY ocrsvc/patch_no_torch.py ./patch_no_torch.py
RUN python patch_no_torch.py && pip install --no-cache-dir --no-deps ./OCR-model ./SEGM-model
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('ai-forever/ReadingPipeline-notebooks', local_dir='/app/weights', \
    ignore_patterns=['*.arpa', '*.ipynb', '*.jpg', 'ocr/*.ckpt', 'segm/*.ckpt'])"

ENV PYTHONPATH=/app:/app/ReadingPipeline \
    MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=2 OCR_THREADS=2 \
    OCR_ORT_ARENA=0 OCR_ORT_MEMPATTERN=0 \
    OCR_ENGINE=readingpipeline OCR_WEIGHTS=/app/weights
COPY ocrsvc ./ocrsvc
RUN useradd --uid 1000 --create-home app && chown -R app:app /app
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "ocrsvc.server"]
