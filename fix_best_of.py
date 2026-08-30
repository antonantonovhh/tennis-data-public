#!/usr/bin/env python3
"""Убрать из журналов обходчика прогнозы, посчитанные не в том формате матча.

До появления `guess_best_of()` симуляция считала ВСЁ как bo3, включая
основную сетку мужского «Большого шлема», где играют до трёх побед. Против
пятисетовой линии Pinnacle (тотал 3.5/4.5, форы по сетам до ±2.5) такие
вероятности не приближённые, а бессмысленные: «ТМ 3.5» в трёхсетовой модели
выпадает всегда. Перекос односторонний — bo3 недооценивает фаворита, поэтому
почти вся «ценность» вылезает на андердоге.

Скрипт находит НЕЗАКРЫТЫЕ строки, у которых в журнале записан `best_of=3`, а
турнир на самом деле пятисетовый, удаляет их и сбрасывает эти матчи в
состоянии обходчика, чтобы он разобрал их заново уже правильно.

Почему удаляет, а не помечает возвратом: `journal.add_value_bets` пропускает
ставку, если такой `bet_id` уже есть в файле, а `add_pick` — если уже есть
строка с таким `slug`. Помеченная строка осталась бы на месте и просто не
пустила бы на своё место пересчитанную.

    systemctl stop tennisratioall
    python3 fix_best_of.py                  # сухой прогон
    python3 fix_best_of.py --apply
    systemctl start tennisratioall

Бэкапы кладутся рядом: <имя>.bak-<дата>.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _pick_tour_early() -> str:
    """--tour надо разобрать ДО импорта пакета.

    Пути к журналам и состоянию вычисляются на импорте store.py по TRA_TOUR,
    а argparse отрабатывает позже — тот же приём, что в tennisratioall_run.py.
    Без него прогон с --tour wta молча открыл бы мужские файлы.
    """
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--tour" and i + 1 < len(argv):
            tour = argv[i + 1].lower()
            break
        if a.startswith("--tour="):
            tour = a.split("=", 1)[1].lower()
            break
    else:
        tour = (os.environ.get("TRA_TOUR") or "atp").lower()
    os.environ["TRA_TOUR"] = tour
    return tour


TOUR = _pick_tour_early()

from tennis_parser.tennisratio import guess_best_of  # noqa: E402
from tennisratioall.journal import PICKS_CSV, VALUE_CSV  # noqa: E402
from tennisratioall.store import STATE_FILE  # noqa: E402


def crawler_running() -> str:
    """Имя запущенного юнита обходчика или пусто.

    Состояние он держит в памяти и сохраняет файл целиком, так что правку на
    живой службе затрёт ближайший круг.
    """
    unit = "tennisratioall" if TOUR == "atp" else f"tennisratioall-{TOUR}"
    try:
        got = subprocess.run(["systemctl", "is-active", unit],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""      # не systemd (Windows, контейнер) — проверять нечем
    return unit if got.stdout.strip() == "active" else ""


def wrong_format(row: dict) -> bool:
    """Строка посчитана как bo3, а турнир на самом деле пятисетовый."""
    if (row.get("status") or "").strip() != "pending":
        return False          # закрытую ставку задним числом не трогаем
    if (row.get("best_of") or "").strip() != "3":
        return False
    return guess_best_of(row.get("tournament") or "", TOUR) == 5


def clean_csv(path: str, apply: bool) -> tuple[int, int, list]:
    """Убирает из журнала строки не того формата. -> (было, удалено, примеры)"""
    if not os.path.exists(path):
        return 0, 0, []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        fields = reader.fieldnames or []
        rows = list(reader)
    keep = [r for r in rows if not wrong_format(r)]
    drop = [r for r in rows if wrong_format(r)]
    if drop and apply:
        shutil.copy2(path, f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
            w.writeheader()
            w.writerows(keep)
    return len(rows), len(drop), drop


def reset_state(slugs: set, apply: bool) -> int:
    """Возвращает матчи в очередь, чтобы обходчик разобрал их заново."""
    if not slugs or not os.path.exists(STATE_FILE):
        return 0
    with open(STATE_FILE, encoding="utf-8") as fh:
        state = json.load(fh)
    entries = state.get("entries", {})
    hit = 0
    for slug in slugs:
        e = entries.get(slug)
        if not isinstance(e, dict):
            continue
        hit += 1
        if apply:
            e["status"] = "pending"
            e["attempts"] = 0
            e["error"] = ""
            # announced не трогаем: карточка по матчу уже уходила, и сбрасывать
            # её значило бы прислать в чат три десятка повторов разом
    if hit and apply:
        shutil.copy2(STATE_FILE, f"{STATE_FILE}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    return hit


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (по умолчанию сухой прогон)")
    ap.add_argument("--tour", default=TOUR, choices=["atp", "wta"],
                    help="какой тур чинить (учитывается до импорта, см. код)")
    ap.add_argument("--force", action="store_true",
                    help="править, даже если служба обходчика запущена")
    args = ap.parse_args()

    print(f"тур: {TOUR}")
    unit = crawler_running()
    if unit and args.apply and not args.force:
        tail = f" --tour {TOUR}" if TOUR != "atp" else ""
        me = os.path.basename(__file__)
        print(f"\nСлужба {unit} запущена — она держит состояние в памяти и "
              "сохраняет файл целиком.")
        print(f"    systemctl stop {unit}")
        print(f"    python3 {me} --apply{tail}")
        print(f"    systemctl start {unit}")
        print("Либо повторите с --force, если понимаете, что делаете.")
        return 2
    if unit:
        print(f"(служба {unit} запущена — для --apply её надо остановить)")

    slugs, total_drop = set(), 0
    for path in (VALUE_CSV, PICKS_CSV):
        was, dropped, rows = clean_csv(path, apply=args.apply)
        total_drop += dropped
        for r in rows:
            if r.get("slug"):
                slugs.add(r["slug"])
        name = os.path.basename(path)
        print(f"{name}: строк {was}, к удалению {dropped}")
        for r in rows[:3]:
            what = r.get("market") or r.get("side") or "исход"
            print(f"   • {r.get('p1')} — {r.get('p2')}: {what} {r.get('pick', '')}")
        if dropped > 3:
            print(f"   … и ещё {dropped - 3}")

    hit = reset_state(slugs, apply=args.apply)
    print(f"матчей вернётся в очередь на пересчёт: {hit} (из {len(slugs)})")

    if not args.apply:
        print("\nСухой прогон. Записать: --apply")
        return 0
    print(f"\nГотово: удалено строк {total_drop}, сброшено матчей {hit}. "
          "Бэкапы рядом с файлами.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
