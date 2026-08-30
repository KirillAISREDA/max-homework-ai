"""CLI для ручной проверки интеграции: ping, vision одного фото, офлайн-оценка датасета."""

import argparse
import asyncio
import json
from pathlib import Path

from hwcheck.config import load_settings
from hwcheck.eval.offline import run_offline_eval
from hwcheck.llm import ChatMessage, GigaChatClient
from hwcheck.pipeline.vision import recognize_page
from hwcheck.prompts import load_prompt


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hwcheck")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Проверка доступа к GigaChat (короткий запрос к Lite)")

    vision = sub.add_parser("vision", help="Распознать одно фото домашней работы")
    vision.add_argument("image", type=Path)
    vision.add_argument("--prompt-version", default="v1")

    ev = sub.add_parser("eval", help="Офлайн-прогон Vision по датасету фото")
    ev.add_argument("dataset", type=Path, help="Папка с .jpg/.png")
    ev.add_argument("--out", type=Path, default=Path("data/eval/vision_results.jsonl"))
    ev.add_argument("--prompt-version", default="v1")
    ev.add_argument("--limit", type=int, default=None)

    args = parser.parse_args(argv)
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    if not settings.gigachat_credentials:
        raise SystemExit("Не задан GIGACHAT_CREDENTIALS (см. .env.example)")

    async with GigaChatClient(settings) as client:
        if args.command == "ping":
            result = await client.chat(
                [ChatMessage(role="user", content="Ответь одним словом: работает?")],
                model=settings.lite_model,
            )
            print(f"{result.model}: {result.content!r}")
            print(f"tokens={result.tokens_in}+{result.tokens_out}, latency={result.latency_s:.2f}s")
        elif args.command == "vision":
            prompt = load_prompt("vision", args.prompt_version)
            rec = await recognize_page(
                client,
                args.image.read_bytes(),
                prompt=prompt,
                model=settings.vision_model,
                filename=args.image.name,
            )
            if rec.page is not None:
                print(rec.page.model_dump_json(indent=2))
            else:
                print(f"Не удалось распознать. Последний ответ модели:\n{rec.raw}")
            print(
                f"\n--- orientation={rec.orientation}°, attempts={rec.attempts}, "
                f"tokens={rec.tokens_in}+{rec.tokens_out}, latency={rec.latency_s:.2f}s"
            )
        elif args.command == "eval":
            prompt = load_prompt("vision", args.prompt_version)
            results = await run_offline_eval(
                client,
                args.dataset,
                args.out,
                model=settings.vision_model,
                prompt=prompt,
                prompt_version=args.prompt_version,
                limit=args.limit,
            )
            print(f"\nПодробности: {args.out} ({len(results)} записей)")
            print(json.dumps({"done": len(results)}, ensure_ascii=False))
