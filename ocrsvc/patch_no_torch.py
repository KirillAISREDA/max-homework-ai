"""Патч клонов OCR-model/SEGM-model: убрать жёсткую зависимость от torch/torchvision
для инференс-пути через ONNX (попытка #4 спайка памяти).

Почему это безопасно (проверено чтением апстрима, не предположение):
  - `OCRTorchModel`/`SegmTorchModel` (реально используют torch: .to(device),
    torch.no_grad(), state_dict) никогда не инстанцируются в этом спайке —
    пайплайн всегда конфигурируется с runtime="ONNX", device="cpu"
    (см. run.py:make_runtime_config), выбор класса — `OCRONNXCPUModel`/
    `SegmONNXCPUModel`.
  - `BeamSearcDecoder.decode_numpy` — единственное место в ocr/tokenizer.py,
    которое реально вызывает `torch.from_numpy` — не используется, пока
    lm_path="" (у нас всегда, LM осознанно выключен, см.
    docs/research/2026-09-13-ru-handwriting-ocr.md): выбирается
    `BestPathDecoder`, которая работает только через numpy
    (`np.argmax`/`np.transpose`) — без torch.
  - `InferenceTransform` (используется ОБОИМИ ONNX-моделями, это на активном
    пути) использует `torchvision.transforms.Compose` только как контейнер
    вызова трансформаций по очереди — без единой тензорной операции внутри;
    заменяется тривиальным Python-классом ниже без потери эквивалентности.
  - `ToTensor.__call__` (реальный `torch.from_numpy`) вызывается, только если
    `InferenceTransform(..., return_numpy=False)` — у нас везде `True`
    (см. `OCRONNXCPUModel`/`SegmONNXCPUModel.__init__` в апстриме), метод
    не вызывается; оставлен с ленивым импортом внутри на случай, если кто-то
    позже включит return_numpy=False — тогда он просто упадёт понятной
    ошибкой ImportError, а не тихо даст неверный результат.

Экономия: ~200 МБ RSS на импорте torch+torchvision (голое `import torch,
torchvision` в этом образе стоит ~198 МБ, измерено отдельно — см.
docs/research/2026-09-17-readingpipeline-memory.md, попытка #4). Запускается
один раз в Dockerfile между `git clone` и `pip install --no-deps`, до того как
торч/торчвижн вообще перестают ставиться в этом варианте образа
(Dockerfile.notorch).
"""

from pathlib import Path

_NO_TORCH_COMPOSE = '''class _NoTorchCompose:
    """Замена torchvision.transforms.Compose без зависимости от torch/torchvision
    (спайк памяти, попытка #4: единственное использование Compose здесь — вызвать
    трансформации по очереди, без единой тензорной операции)."""

    def __init__(self, transforms):
        self.transforms = transforms

    def __call__(self, x):
        for t in self.transforms:
            x = t(x)
        return x


'''

_TO_TENSOR_OLD = (
    "class ToTensor:\n"
    "    def __call__(self, arr):\n"
    "        arr = torch.from_numpy(arr)\n"
    "        return arr\n"
)
_TO_TENSOR_NEW = (
    "class ToTensor:\n"
    "    def __call__(self, arr):\n"
    "        # Ленивый импорт: реально дёргается только если InferenceTransform\n"
    "        # создан с return_numpy=False — в этом спайке всегда True (ONNX-путь).\n"
    "        import torch  # noqa: PLC0415\n"
    "        arr = torch.from_numpy(arr)\n"
    "        return arr\n"
)


def patch_transforms(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "import torch\n" in text, path
    assert "import torchvision\n" in text, path
    assert "torchvision.transforms.Compose(" in text, path
    assert _TO_TENSOR_OLD in text, path
    text = text.replace("import torch\n", "", 1)
    text = text.replace("import torchvision\n", "", 1)
    text = text.replace("torchvision.transforms.Compose(", "_NoTorchCompose(")
    text = text.replace(_TO_TENSOR_OLD, _TO_TENSOR_NEW)
    text = _NO_TORCH_COMPOSE + text
    path.write_text(text, encoding="utf-8")


def patch_predictor(path: Path, model_import: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert "import torch\n" in text, path
    assert f"{model_import}\n" in text, path
    text = text.replace("import torch\n", "", 1)
    text = text.replace(f"{model_import}\n", "", 1)
    path.write_text(text, encoding="utf-8")


def patch_tokenizer(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "import torch\n" in text, path
    text = text.replace("import torch\n", "", 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    patch_transforms(Path("OCR-model/ocr/transforms.py"))
    patch_transforms(Path("SEGM-model/segm/transforms.py"))
    patch_predictor(Path("OCR-model/ocr/predictor.py"), "from ocr.models import CRNN")
    patch_predictor(Path("SEGM-model/segm/predictor.py"), "from segm.models import LinkResNet")
    patch_tokenizer(Path("OCR-model/ocr/tokenizer.py"))
    print("patched: OCR-model/SEGM-model no longer hard-require torch/torchvision on the ONNX path")


if __name__ == "__main__":
    main()
