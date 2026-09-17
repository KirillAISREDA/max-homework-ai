# Спайк памяти: ReadingPipeline (ai-forever) без LM на CPU — та же сборка, что и
# spikes/ocr_ru/Dockerfile (см. его комментарии для обоснования версий), плюс
# инструментация RSS по стадиям пайплайна и env-переключатели для эксперимента
# со снижением пиковой памяти. Не для прода — только измерения.
# См. docs/research/2026-09-17-readingpipeline-memory.md.
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git g++ libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir torch==2.2.2 torchvision==0.17.2 \
    --extra-index-url https://download.pytorch.org/whl/cpu

# onnx==1.14.1 — не нужен основному пайплайну (он читает готовые .onnx через
# onnxruntime), нужен только export_segm_onnx.py (попытка #3: torch.onnx.export
# зовёт `import onnx` для сериализации графа) — совместим с numpy<2/python3.10.
RUN pip install --no-cache-dir \
        numpy==1.23.1 \
        opencv-python==4.6.0.66 \
        tqdm==4.62.3 \
        matplotlib==3.5.0 \
        Pillow==8.4.0 \
        pyclipper==1.3.0 \
        shapely==1.8.0 \
        onnxruntime==1.17.3 \
        openvino==2024.6.0 \
        pandas==1.3.4 \
        scikit-learn==1.3.2 \
        scipy==1.10.1 \
        albumentations==1.1.0 \
        huggingface_hub \
        onnx==1.14.1

RUN git clone --depth 1 https://github.com/ai-forever/ReadingPipeline.git \
    && git clone --depth 1 https://github.com/ai-forever/OCR-model.git \
    && git clone --depth 1 https://github.com/ai-forever/SEGM-model.git
RUN printf 'class CTCBeamDecoder:\n    def __init__(self, *a, **kw):\n        raise NotImplementedError("ctcdecode stub: LM disabled in spike")\n' \
        > /usr/local/lib/python3.10/site-packages/ctcdecode.py \
    && pip install --no-cache-dir --no-deps ./OCR-model ./SEGM-model

# Веса: ONNX + конфиги, как в ocr_ru. Плюс segm_model.ckpt (PyTorch-чекпоинт) —
# нужен только для попытки #3 (переэкспорт сегментации в ONNX с меньшим входом);
# на измерения baseline/attempt 1-2 не влияет (не грузится, пока не запрошен export.py).
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('ai-forever/ReadingPipeline-notebooks', local_dir='/app/weights', \
    ignore_patterns=['*.arpa', '*.ipynb', '*.jpg', 'ocr/*.ckpt'])"

# Дефолты для тюнинга аллокатора (переопределяются через `docker run -e ...`,
# см. README.md этого спайка). MALLOC_ARENA_MAX должен быть в окружении процесса
# на момент старта python — glibc читает его лениво при первом создании арены,
# но до первого мультипоточного malloc, поэтому ставим через ENV Dockerfile,
# а не через os.environ внутри run.py (там уже поздно).
ENV MALLOC_ARENA_MAX=2
ENV OMP_NUM_THREADS=2
ENV OCR_MEM_THREADS=2
ENV OCR_MEM_ORT_ARENA=1
ENV OCR_MEM_ORT_MEMPATTERN=1
ENV OCR_MEM_RUNTIME=ONNX
ENV OCR_MEM_SEGM_SIZE=
ENV OCR_MEM_SEGM_MODEL_PATH=

COPY run.py /app/run.py
COPY export_segm_onnx.py /app/export_segm_onnx.py
ENTRYPOINT ["python", "/app/run.py"]
