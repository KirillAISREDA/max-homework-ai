"""CLI для ручной проверки интеграции: ping, vision одного фото, офлайн-оценка датасета."""

import argparse
import asyncio
import json
from pathlib import Path

from hwcheck.config import load_settings
from hwcheck.eval.offline import run_offline_eval
from hwcheck.llm import ChatMessage, GigaChatClient
from hwcheck.pipeline.grade import grade
from hwcheck.pipeline.normalize import ImageDecodeError
from hwcheck.pipeline.solver import FileCache, solve_task
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

    solve = sub.add_parser("solve", help="Эталонное решение задания (Solver + самопроверка)")
    solve.add_argument("task", help="Текст задания")
    solve.add_argument("--prompt-version", default="v1")
    solve.add_argument("--no-cache", action="store_true")

    gr = sub.add_parser("grade", help="Проверить решение ученика против эталона")
    gr.add_argument("--task", required=True, help="Текст задания")
    gr.add_argument("--step", action="append", default=[], help="Шаг ученика (повторяемый)")
    gr.add_argument("--answer", default=None, help="Итоговый ответ ученика")
    gr.add_argument("--prompt-version", default="v1")

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
            try:
                rec = await recognize_page(
                    client, args.image.read_bytes(), prompt=prompt, model=settings.vision_model
                )
            except ImageDecodeError as exc:
                raise SystemExit(f"{args.image}: {exc}") from exc
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
        elif args.command == "solve":
            solved, llm_result = await solve_task(
                client,
                args.task,
                model=settings.solver_model,
                prompt_version=args.prompt_version,
                cache=None if args.no_cache else FileCache(Path(".cache/solver")),
            )
            print(solved.model_dump_json(indent=2))
            if llm_result is not None:
                print(
                    f"--- tokens={llm_result.tokens_in}+{llm_result.tokens_out}, "
                    f"latency={llm_result.latency_s:.2f}s"
                )
        elif args.command == "grade":
            solved, _ = await solve_task(
                client,
                args.task,
                model=settings.solver_model,
                prompt_version=args.prompt_version,
                cache=FileCache(Path(".cache/solver")),
            )
            if not solved.ref_ok:
                print("Эталон не прошёл самопроверку — эскалация (review_queue).")
                print(solved.model_dump_json(indent=2))
                return
            grade_result = grade(args.step, args.answer, solved.solution)
            print(grade_result.model_dump_json(indent=2))
            print(f"--- эталон: {solved.solution.answer} {solved.solution.units or ''}".rstrip())
