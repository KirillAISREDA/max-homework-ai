"""Команды стенда: `hwcheck bench run` (прогон конфигурации) и `hwcheck bench report` (отчёт)."""

import argparse
from pathlib import Path

from hwcheck.bench.client import BenchClient
from hwcheck.bench.golden import index_photos, load_cases, missing_photos
from hwcheck.bench.runner import (
    BenchConfig,
    load_run,
    pair_disagreement,
    render_report,
    run_bench,
    summarize,
)
from hwcheck.config import Settings
from hwcheck.llm.gigachat_client import GigaChatClient


def add_bench_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    bench = sub.add_parser("bench", help="Стенд сравнения моделей на эталонной разметке (bench/)")
    commands = bench.add_subparsers(dest="bench_command", required=True)

    run = commands.add_parser("run", help="Прогнать конфигурацию моделей по эталонным кейсам")
    run.add_argument("--config", type=Path, required=True, help="bench/configs/<имя>.json")
    run.add_argument("--golden", type=Path, default=Path("bench/golden"))
    run.add_argument(
        "--photos", type=Path, action="append", help="Каталог с фото (по умолчанию data)"
    )
    run.add_argument("--cases", default=None, help="id кейсов через запятую")
    run.add_argument("--max-calls", type=int, default=300, help="Лимит свежих вызовов модели")
    run.add_argument("--cache", type=Path, default=Path(".cache/bench/llm"))
    run.add_argument("--out", type=Path, default=None, help="JSONL прогона")

    report = commands.add_parser("report", help="Отчёт по сохранённым прогонам")
    report.add_argument("runs", type=Path, nargs="+", help=".cache/bench/runs/<имя>.jsonl")
    report.add_argument("--golden", type=Path, default=Path("bench/golden"))
    report.add_argument(
        "--pair",
        nargs=2,
        action="append",
        metavar=("A", "B"),
        help="Сравнить расшифровки двух конфигураций (имена из config)",
    )
    report.add_argument("--out", type=Path, default=None, help="bench/reports/<файл>.md")


async def run_command(args: argparse.Namespace, settings: Settings) -> None:
    config = BenchConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
    cases = load_cases(args.golden)
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.id in wanted]
    index = index_photos(args.photos or [Path("data")])
    missing = missing_photos(cases, index)
    if missing:
        raise SystemExit(f"Нет фото для кейсов: {missing}")
    out = args.out or Path(".cache/bench/runs") / f"{config.name}.jsonl"
    async with GigaChatClient(settings) as llm:
        client = BenchClient(llm, args.cache, max_calls=args.max_calls)
        runs = await run_bench(client, cases, index, config, out)
    print(render_report([(config, summarize(cases, runs))]))
    print(f"Прогон: {out}")


def report_command(args: argparse.Namespace) -> None:
    cases = load_cases(args.golden)
    loaded = [load_run(path) for path in args.runs]
    results = [(config, summarize(cases, runs)) for config, runs in loaded]
    by_name = {config.name: runs for config, runs in loaded}
    pairs = [
        (a, b, pair_disagreement(cases, by_name[a], by_name[b]))
        for a, b in (args.pair or [])
        if a in by_name and b in by_name
    ]
    text = render_report(results, pairs)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"Отчёт: {args.out}")
    else:
        print(text)
