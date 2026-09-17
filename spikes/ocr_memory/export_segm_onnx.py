"""Переэкспорт сегментации LinkResNet из .ckpt в ONNX с другим входным размером.

Нужен только для попытки #3 из спайка памяти: SEGM-model/scripts/torch2onnx.py
(апстрим) экспортирует ONNX с `dynamic_axes` только по batch_size — высота и
ширина входа зашиты в граф на момент экспорта (896x896 у весов
ai-forever/ReadingPipeline-notebooks). Патч конфига препроцессинга (resize)
без переэкспорта графа не работает (см. run.py:make_segm_config_override и
README.md) — если нужен вход 640x640/768x768, граф надо переэкспортировать
из .ckpt заново с этим размером, как здесь.

Throwaway-скрипт, не для прода. Запуск (не через ENTRYPOINT, вручную):
    docker run --rm --entrypoint python \
      -v $PWD/segm_model.ckpt:/tmp/segm_model.ckpt:ro \
      -v $PWD/out:/out \
      spike-ocr-memory /app/export_segm_onnx.py --size 640 \
      --ckpt /tmp/segm_model.ckpt --out /out/segm_model_640.onnx
"""

import argparse

import torch
from segm.predictor import SegmTorchModel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", required=True, help="Путь к segm_model.ckpt")
    parser.add_argument(
        "--config",
        default="/app/weights/segm/segm_config.json",
        help="segm_config.json из весов (нужен только для числа классов/alphabet)",
    )
    parser.add_argument("--size", type=int, required=True, help="Новый H=W входа, напр. 640")
    parser.add_argument("--out", required=True, help="Путь для сохранения .onnx")
    args = parser.parse_args()

    segm_torch_model = SegmTorchModel(
        model_path=args.ckpt,
        config_path=args.config,
        device="cpu",
    )

    example_forward_input = torch.rand(1, 3, args.size, args.size)

    torch.onnx.export(
        segm_torch_model.model,
        example_forward_input,
        args.out,
        opset_version=12,
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {0: "batch_size"}, "output": {0: "batch_size"}},
    )
    print(f"exported {args.out} at input size {args.size}x{args.size}")


if __name__ == "__main__":
    main()
