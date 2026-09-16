# Спайк: ReadingPipeline (ai-forever) без языковой модели на CPU. Не для прода — только измерения.
# См. docs/research/2026-09-17-readingpipeline-vps.md — там же расхождения с исходным планом.
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git g++ libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch/torchvision — CPU-колёса с отдельного индекса PyTorch.
# --extra-index-url, а не --index-url: с --index-url pip не видит на PyPI
# чистые python-пакеты вроде flit_core, которые нужны как build-зависимость
# для транзитивных пакетов torch/torchvision, и падает ещё до установки torch.
RUN pip install --no-cache-dir torch==2.2.2 torchvision==0.17.2 \
    --extra-index-url https://download.pytorch.org/whl/cpu

# requirements.txt в ReadingPipeline/OCR-model/SEGM-model — от 2022 года, часть
# версий не собирается на Python 3.10 (проверено сборкой на VPS 16.09):
#   - scikit-learn==1.0.1, scipy==1.4.1 — нет колеса под cp310, сборка из исходников
#     падает (numpy.distutils зовёт distutils.msvccompiler); заменены на ближайшие
#     версии с готовым колесом и тем же API.
#   - openvino==2022.2.0 снят с PyPI; заменён на актуальный 2024.6.0
#     (openvino.runtime — тот же неймспейс, импорт нужен, даже если рантайм не ONNX-Vino).
#   - onnxruntime==1.13.1 на этом ядре VPS (Ubuntu 22.04, 5.15) падает при импорте:
#     "cannot enable executable stack as shared object requires: Invalid argument" —
#     баг старого manylinux-колеса; onnxruntime==1.17.3 импортируется нормально.
#   - numpy закреплён <2 (1.23.1, как в оригинале): torch==2.2.2 и это старое
#     колесо onnxruntime/opencv собраны под NumPy 1.x ABI и падают на NumPy 2.
#   - pyclipper==1.3.0 собирается из исходников (C++-расширение) — нужен g++
#     (добавлен выше), это единственная реальная компиляция в образе.
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
        huggingface_hub

# Клонируем ReadingPipeline + его подмодули OCR-model/SEGM-model как обычные
# репозитории, а не через "pip install -r ReadingPipeline/requirements.txt"
# (как в исходном плане задачи): requirements.txt пакета OCR-model тянет
# `ctcdecode @ git+https://github.com/parlance/ctcdecode` — C++-расширение,
# которое клонирует и собирает kenlm+openfst. Оно нужно только для beam search
# с языковой моделью, которую мы сознательно не используем (см. §ReadingPipeline
# в docs/research/2026-09-13-ru-handwriting-ocr.md: LM выключать). Вместо тяжёлой
# и хрупкой сборки — заглушка модуля ctcdecode: класс импортируется на уровне
# модуля в OCR-model/ocr/tokenizer.py, но не вызывается, пока lm_path пуст.
RUN git clone --depth 1 https://github.com/ai-forever/ReadingPipeline.git \
    && git clone --depth 1 https://github.com/ai-forever/OCR-model.git \
    && git clone --depth 1 https://github.com/ai-forever/SEGM-model.git
RUN printf 'class CTCBeamDecoder:\n    def __init__(self, *a, **kw):\n        raise NotImplementedError("ctcdecode stub: LM disabled in spike")\n' \
        > /usr/local/lib/python3.10/site-packages/ctcdecode.py \
    && pip install --no-cache-dir --no-deps ./OCR-model ./SEGM-model

# Веса: только ONNX + конфиги (сегментация ~110 МБ + OCR ~48 МБ), без .ckpt
# (Pytorch-веса, не нужны на CPU-инференсе через ONNX) и без KenLM (*.arpa, LM выключен).
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('ai-forever/ReadingPipeline-notebooks', local_dir='/app/weights', \
    ignore_patterns=['*.ckpt', '*.arpa', '*.ipynb', '*.jpg'])"

COPY run.py /app/run.py
ENTRYPOINT ["python", "/app/run.py"]
