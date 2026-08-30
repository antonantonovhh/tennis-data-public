#!/usr/bin/env python3
"""Проверка привязки кэфов по истории ставок в bets_db.json.

Зачем это отдельно от бота
--------------------------
Проигравшая ставка выглядит одинаково при правильной и при перепутанной
привязке: пишется минус на размер ставки, коэффициент в расчете не участвует.
Поймать перепутанную привязку одной ставкой нельзя в принципе — только
накоплением.

Зацепка в том, что бот всегда ставит на фаворита рынка. Если кэфы привязаны
верно, такие ставки должны заходить примерно с той частотой, которую
подразумевает цена. Если стороны перепутаны, бот на самом деле ставит на
аутсайдера по цене фаворита, и частота попаданий провалится намного ниже
подразумеваемой.

Что считается
-------------
1. Калибровка по корзинам кэфа: сколько зашло против того, сколько должно.
2. Сравнение двух гипотез по правдоподобию — «привязка верна» против
   «привязка перевернута». Отношение логарифмов правдоподобия говорит, какая
   версия лучше объясняет наблюдения, и насколько уверенно.
3. Биномиальная проверка: насколько маловероятен такой результат, если
   привязка верна.

Ограничение, о котором стоит помнить: на десятке ставок ни одна из этих цифр
ничего не доказывает. Разделу «вердикт» можно верить примерно от 30 матчей,
и он говорит о систематическом сдвиге, а не про конкретную ставку.

    python3 audit_odds.py
    python3 audit_odds.py --db /path/bets_db.json --min-sample 40
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict

# Пути берём относительно самого скрипта, а не текущего каталога: запуск вида
# `python3 /opt/tennis_bot/fix_settlements.py` из домашней папки иначе искал бы
# базу в ~, где её нет, и падал с «нет файла базы» при живом файле рядом.
HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")

BUCKETS = [(1.0, 1.3), (1.3, 1.6), (1.6, 2.0), (2.0, 3.0), (3.0, 99.0)]


def implied_from_bet(bet: dict) -> float | None:
    """Вероятность стороны, на которую поставлено, очищенная от маржи.

    Свежие записи содержат обе цены — тогда маржа снимается честно. У старых
    есть только цена своей стороны: там просто 1/кэф, чуть завышенная оценка.
    """
    o1, o2 = bet.get("odds_p1"), bet.get("odds_p2")
    if o1 and o2:
        inv1, inv2 = 1 / o1, 1 / o2
        p1 = inv1 / (inv1 + inv2)
        return p1 if "П1" in bet.get("prediction", "") else 1 - p1
    odds = bet.get("odds")
    return 1 / odds if odds else None


def collect(db: dict) -> list[dict]:
    out = []
    for match in db.get("bets", []):
        if not match.get("resolved"):
            continue
        s1, s2 = match.get("sets_p1"), match.get("sets_p2")
        if s1 is None or s2 is None or s1 == s2:
            continue  # незакрытый или отказ — исхода нет
        for bet in match.get("bets", []):
            if bet.get("type") != "Moneyline" or bet.get("status") not in ("win", "loss"):
                continue
            p = implied_from_bet(bet)
            if not p or not 0 < p < 1:
                continue
            out.append({
                "match": match.get("match", "?"),
                "pred": bet.get("prediction", ""),
                "odds": bet.get("odds"),
                "implied": p,
                "won": bet.get("status") == "win",
                "has_both": bool(bet.get("odds_p1") and bet.get("odds_p2")),
            })
    return out


def binom_tail(k: int, n: int, p: float) -> float:
    """P(попаданий <= k) при n испытаниях с вероятностью p."""
    total = 0.0
    for i in range(k + 1):
        total += math.comb(n, i) * p ** i * (1 - p) ** (n - i)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--min-sample", type=int, default=30,
                    help="с какого объема выносить вердикт")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        hint = ""
        cwd_db = os.path.join(os.getcwd(), "bets_db.json")
        if os.path.exists(cwd_db):
            hint = f"\nЕсть в текущем каталоге: {cwd_db} — запустите с --db {cwd_db}"
        sys.exit(f"Нет файла базы: {args.db}{hint}")
    rows = collect(json.load(open(args.db, encoding="utf-8")))

    if not rows:
        print("Рассчитанных ставок на исход в базе нет — проверять нечего.")
        return 0

    n = len(rows)
    wins = sum(1 for r in rows if r["won"])
    exp = sum(r["implied"] for r in rows)
    both = sum(1 for r in rows if r["has_both"])

    print(f"Ставок на исход: {n} (с обеими ценами: {both})")
    print(f"Зашло: {wins}   Ожидалось по цене: {exp:.1f}   "
          f"Отклонение: {wins - exp:+.1f}\n")

    print("Калибровка по корзинам кэфа")
    print(f"{'кэф':<12}{'ставок':>8}{'зашло':>8}{'факт':>9}{'по цене':>10}{'разница':>10}")
    print("-" * 57)
    groups = defaultdict(list)
    for r in rows:
        for lo, hi in BUCKETS:
            if lo <= (r["odds"] or 0) < hi:
                groups[(lo, hi)].append(r)
                break
    for lo, hi in BUCKETS:
        g = groups.get((lo, hi))
        if not g:
            continue
        k = sum(1 for r in g if r["won"])
        e = sum(r["implied"] for r in g) / len(g)
        f = k / len(g)
        label = f"{lo:g}-{hi:g}" if hi < 90 else f"{lo:g}+"
        print(f"{label:<12}{len(g):>8}{k:>8}{f:>8.0%}{e:>10.0%}{f - e:>+10.0%}")

    # --- две гипотезы
    ll_ok = sum(math.log(r["implied"] if r["won"] else 1 - r["implied"]) for r in rows)
    ll_swap = sum(math.log((1 - r["implied"]) if r["won"] else r["implied"]) for r in rows)
    diff = ll_ok - ll_swap

    print("\nЧто лучше объясняет наблюдения")
    print(f"  привязка верна:      логправдоподобие {ll_ok:8.2f}")
    print(f"  привязка перевернута:                 {ll_swap:8.2f}")
    print(f"  разница: {diff:+.2f} "
          f"({'в пользу верной' if diff > 0 else 'в пользу перевернутой'})")

    p_val = binom_tail(wins, n, exp / n)
    print(f"\nЕсли привязка верна, получить {wins} или меньше попаданий из {n}: "
          f"{p_val:.1%}")

    print("\nВердикт:", end=" ")
    if n < args.min_sample:
        print(f"выборки мало ({n} < {args.min_sample}) — цифры выше "
              "смотрите как ориентир, доказательством они пока не являются.")
    elif diff > 4.6 and p_val > 0.05:
        print("привязка выглядит верной. Наблюдения объясняются ценой "
              "заметно лучше, чем перевернутой версией.")
    elif diff < -4.6:
        print("СИГНАЛ: перевернутая привязка объясняет историю лучше верной. "
              "Проверьте маппинг home/away в get_pinnacle_odds на живом матче.")
    elif p_val < 0.02:
        print(f"фавориты заходят реже, чем должны ({wins} против {exp:.1f}). "
              "Это может быть перепутанная привязка, а может — плохой отбор "
              "матчей. Различить поможет только сверка кэфов с сайтом.")
    else:
        print("явных признаков перепутанной привязки нет, но и уверенно "
              "подтвердить ее верность на этой выборке нельзя.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
