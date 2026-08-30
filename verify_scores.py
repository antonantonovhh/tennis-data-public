#!/usr/bin/env python3
"""Сверяет счёт каждого матча в bets_db.json с TennisExplorer.

Зачем отдельно от audit: внутренняя проверка «статус ставки против своего же
счёта» такую ошибку не ловит. Если счёт записан развёрнутым — не под тот
порядок игроков, — запись остаётся внутренне непротиворечивой, но говорит
ровно обратное тому, что было на корте. Поймать это можно только сверкой с
внешним источником.

Так уже случалось: бот брал строку TennisExplorer, где игроки идут в другом
порядке, и клал счёт как есть. Разворот в коде появился позже, а записи,
закрытые до него, остались неверными.

    python3 verify_scores.py            # только показать
    python3 verify_scores.py --apply    # исправить (с бэкапом)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from import_bethub import (name_keys, score_to_stats, split_players,  # noqa: E402
                           te_results, te_surname)

DB = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")


def match_dt(m) -> datetime | None:
    g = re.match(r"(\d{1,2})\.(\d{1,2})\.?\s*(\d{1,2}):(\d{2})",
                 str(m.get("date") or ""))
    if not g:
        return None
    now = datetime.now(timezone.utc)
    return datetime(now.year, int(g.group(2)), int(g.group(1)),
                    int(g.group(3)), int(g.group(4)), tzinfo=timezone.utc)


def te_lookup(p1: str, p2: str, dt: datetime, cache: dict):
    """(счёт, нужен ли разворот) с TennisExplorer, или None.

    Разворот нужен, когда TennisExplorer перечисляет игроков в обратном к
    нашему порядке: счёт у него всегда идёт от первой строки.
    """
    k1, k2 = name_keys(p1), name_keys(p2)
    for shift in (0, -1, 1):
        d = dt + timedelta(days=shift)
        key = d.strftime("%Y-%m-%d")
        if key not in cache:
            cache[key] = te_results(d)
        hits = []
        for n1, n2, sc in cache[key]:
            s1, s2 = te_surname(n1), te_surname(n2)
            if s1 in k1 and s2 in k2:
                hits.append((sc, False))
            elif s1 in k2 and s2 in k1:
                hits.append((sc, True))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None            # неоднозначно — не трогаем
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="исправить записи")
    args = ap.parse_args()

    db = json.load(open(DB, encoding="utf-8"))
    cache: dict = {}
    checked = wrong = notfound = 0
    fixes = []

    for m in db["bets"]:
        if not (m.get("score") or "").strip():
            continue
        p1, p2 = split_players(m.get("match") or "")
        dt = match_dt(m)
        if not p1 or not dt:
            continue
        got = te_lookup(p1, p2, dt, cache)
        if not got:
            notfound += 1
            continue
        checked += 1
        st = score_to_stats(got[0], got[1])
        if not st:
            continue
        same = (st["games_p1"] == m.get("games_p1")
                and st["games_p2"] == m.get("games_p2"))
        if same:
            continue
        wrong += 1
        flipped = (st["games_p1"] == m.get("games_p2")
                   and st["games_p2"] == m.get("games_p1"))
        print(f'  {m["match"][:40]:42} {m.get("date","")}')
        print(f'     у нас        : {m.get("score"):24} геймы '
              f'{m.get("games_p1")}-{m.get("games_p2")}')
        print(f'     TennisExplorer: {st["score"]:24} геймы '
              f'{st["games_p1"]}-{st["games_p2"]}'
              f'{"   <- РАЗВЁРНУТ" if flipped else "   <- расходится иначе"}')
        fixes.append((m, st))

    print()
    print(f"сверено: {checked}, расходится: {wrong}, не найдено на TE: {notfound}")
    if not fixes:
        return 0
    if not args.apply:
        print()
        print("это предпросмотр. чтобы исправить: --apply")
        return 0

    shutil.copy2(DB, f'{DB}.bak-{datetime.now():%Y%m%d-%H%M%S}')
    for m, st in fixes:
        m.update({k: v for k, v in st.items() if k != "retired"})
        # статусы ставок пересчитываем по исправленному счёту
        for b in m.get("bets", []):
            if b.get("status") not in ("win", "loss"):
                continue
            stake = float(b.get("stake") or 0)
            pred = b.get("prediction") or ""
            if b.get("type") == "Moneyline":
                if st["sets_p1"] == st["sets_p2"]:
                    continue                       # отказ — не пересчитываем
                won = (st["sets_p1"] > st["sets_p2"]) == (pred == "П1")
            else:
                g = re.match(r"(ТБ|ТМ)\s*([\d.]+)", pred)
                if not g:
                    continue
                total = st["sets_p1"] + st["sets_p2"]
                won = (total > float(g.group(2))) == (g.group(1) == "ТБ")
            b["status"] = "win" if won else "loss"
            b["profit"] = (round(stake * (float(b["odds"]) - 1), 2) if won
                           else -stake)
    tmp = f"{DB}.tmp"
    json.dump(db, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, DB)
    print()
    print(f"исправлено записей: {len(fixes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
