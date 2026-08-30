"""CLI: собирает H2H + Elo/покрытия + yElo + усталость в один отчёт.

Примеры:
    python -m tennis_parser.cli h2h "Jan Kumstat" "Maxim Mrva" --surface clay
    python -m tennis_parser.cli h2h "Jan Kumstat" "Maxim Mrva" --mode render --out out/
    python -m tennis_parser.cli elo "Jan Kumstat"
    python -m tennis_parser.cli dump-elo --csv elo.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .http import Fetcher
from .report import build_report, format_console, json_safe
from .simulation import build_simulation, format_simulation_console
from .tennisabstract import load_ratings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _write_json(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"→ {path}", file=sys.stderr)


def _write_matches_csv(matches: list, path: Path) -> None:
    if not matches:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [m.as_dict() for m in matches]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"→ {path}", file=sys.stderr)


# ------------------------------------------------------------------ команды
def cmd_h2h(args) -> int:
    fetcher = Fetcher(cache_dir=args.cache, ttl_seconds=args.ttl)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    report = build_report(
        fetcher, args.p1, args.p2,
        surface=args.surface,
        surface_weight=args.surface_weight,
        best_of=args.best_of,
        tour=args.tour,
        mode=args.mode,
        headless=not args.headed,
        force=args.force,
        as_of=as_of,
    )

    if args.out:
        out = Path(args.out)
        stem = f"{args.p1}_vs_{args.p2}".replace(" ", "_").lower()
        _write_json(json_safe(report), out / f"{stem}.json")
        _write_matches_csv(report["_matches"]["p1"], out / f"{stem}_p1_matches.csv")
        _write_matches_csv(report["_matches"]["p2"], out / f"{stem}_p2_matches.csv")
    else:
        print(json.dumps(json_safe(report), ensure_ascii=False, indent=2, default=str))

    if args.summary:
        print(format_console(report))

    if args.simulate:
        sim = build_simulation(report, runs=args.runs, elo_weight=args.elo_weight)
        if sim is None:
            print("Симуляцию не считал: нет ни показателей сравнения, ни Elo.",
                  file=sys.stderr)
        else:
            print(format_simulation_console(sim))
    return 0


def cmd_elo(args) -> int:
    fetcher = Fetcher(cache_dir=args.cache, ttl_seconds=args.ttl)
    ratings = load_ratings(fetcher, tour=args.tour, force=args.force)
    for name in args.players:
        row = ratings.get(name)
        if row is None:
            print(f"{name}: не найден в таблице Elo", file=sys.stderr)
            continue
        print(json.dumps(row.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_dump_elo(args) -> int:
    fetcher = Fetcher(cache_dir=args.cache, ttl_seconds=args.ttl)
    ratings = load_ratings(fetcher, tour=args.tour, force=args.force)
    rows = [r.as_dict() for r in ratings.rows.values()]
    path = Path(args.csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"→ {path} ({len(rows)} игроков)", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ парсер
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tennis_parser", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--cache", default=".cache/tennis", help="папка кэша ('' — выключить)")
    p.add_argument("--ttl", type=int, default=6 * 3600, help="время жизни кэша, сек")
    p.add_argument("--force", action="store_true", help="игнорировать кэш")
    p.add_argument("--tour", choices=["atp", "wta"], default="atp")

    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("h2h", help="полный отчёт по паре игроков")
    h.add_argument("p1")
    h.add_argument("p2")
    h.add_argument("--surface", choices=["hard", "clay", "grass"], default=None)
    h.add_argument("--surface-weight", type=float, default=0.5,
                   help="вес покрытия при смешивании с общим Elo (0..1)")
    h.add_argument("--best-of", type=int, choices=[3, 5], default=3)
    h.add_argument("--mode", choices=["auto", "static", "render"], default="auto")
    h.add_argument("--headed", action="store_true", help="показать окно браузера")
    h.add_argument("--as-of", help="дата расчёта усталости, YYYY-MM-DD")
    h.add_argument("--out", help="папка для JSON/CSV; без неё — вывод в stdout")
    h.add_argument("--summary", action="store_true", help="таблица сравнения в консоль")
    h.add_argument("--simulate", action="store_true",
                   help="Монте-Карло симуляция матча из собранных данных")
    h.add_argument("--runs", type=int, default=10000, help="прогонов симуляции")
    h.add_argument("--elo-weight", type=float, default=None,
                   help="вес Elo в рабочей модели, 0..1 (по умолчанию 0.7)")
    h.set_defaults(func=cmd_h2h)

    e = sub.add_parser("elo", help="Elo/yElo одного или нескольких игроков")
    e.add_argument("players", nargs="+")
    e.set_defaults(func=cmd_elo)

    d = sub.add_parser("dump-elo", help="выгрузить всю таблицу Elo+yElo в CSV")
    d.add_argument("--csv", default="elo_ratings.csv")
    d.set_defaults(func=cmd_dump_elo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    if args.cache == "":
        args.cache = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
