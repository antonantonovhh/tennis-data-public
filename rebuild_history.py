#!/usr/bin/env python3
"""Пересобрать bets_history.csv из базы.

Правки в коде действуют на новые строки; уже записанные остаются как были.
После обновления разбора счёта (убран итоговый токен по сетам и склеенный
тайбрейк) старые строки надо переписать — этим и занимается скрипт.

    ./venv/bin/python3 rebuild_history.py            # показать, что изменится
    ./venv/bin/python3 rebuild_history.py --apply    # переписать, с бэкапом
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

for line in (open(os.path.join(HERE, ".env"), encoding="utf-8")
             if os.path.exists(os.path.join(HERE, ".env")) else []):
    line = line.strip().removeprefix("export ")
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def load_pretty():
    """Берём разбор счёта из бота, не дублируя логику."""
    src = open(os.path.join(HERE, "bot_merged.py"), encoding="utf-8").read()
    try:
        start = src.index("def _parse_score_sets")
        end = src.index("def parse_te_last_matches")
    except ValueError:
        sys.exit("Не нашёл функции разбора счёта — файл не тот или не обновлён.")
    ns: dict = {"re": re}
    exec(src[start:end], ns)  # noqa: S102
    if "pretty_score" not in ns:
        sys.exit("В боте нет pretty_score — сначала обновите bot_merged.py.")
    return ns["pretty_score"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csv", default=os.path.join(HERE, "bets_history.csv"))
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        sys.exit(f"Нет файла: {args.csv}")

    pretty = load_pretty()
    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    if not rows:
        sys.exit("Файл пуст.")

    head = rows[0]
    try:
        col = head.index("Счёт")
    except ValueError:
        sys.exit(f"Нет колонки «Счёт». Есть: {head}")

    changed = []
    for r in rows[1:]:
        if len(r) <= col or not r[col] or r[col] == "В игре":
            continue
        new = pretty(r[col])
        if new != r[col]:
            changed.append((r[col], new))
            r[col] = new

    if not changed:
        print("Все строки уже в порядке.")
        return 0

    print(f"Строк к исправлению: {len(changed)}\n")
    seen = set()
    for old, new in changed:
        if old in seen:
            continue
        seen.add(old)
        print(f"  {old:<26} ->  {new}")

    if not args.apply:
        print("\nСухой прогон. Записать: --apply")
        return 0

    bak = f"{args.csv}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(args.csv, bak)
    # BOM и точка с запятой — чтобы Excel открывал без плясок с кодировкой
    with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh, delimiter=";").writerows(rows)
    print(f"\nЗаписано. Бэкап: {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
