#!/usr/bin/env python3
"""Перепроверить уже закрытые матчи: не перевёрнут ли счёт.

TennisExplorer перечисляет игроков в своём порядке — часто победителем
вперёд. Бот это вычислял, но не применял, и матчи, где порядок отличался
от нашего, закрывались с зеркальным счётом: победитель записывался
проигравшим, а все ставки по матчу считались наоборот.

Скрипт заново скачивает результаты и сверяет с базой.

    ./venv/bin/python3 recheck_results.py           # показать расхождения
    ./venv/bin/python3 recheck_results.py --apply   # исправить, с бэкапом
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from webui import load_env  # noqa: E402

load_env(HERE)

DB = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=14,
                    help="за сколько дней тянуть результаты")
    args = ap.parse_args()

    from tennisratioall.results import (fetch_results, match_result,  # noqa: PLC0415
                                        outcome_from_score)

    db = json.load(open(DB, encoding="utf-8"))
    resolved = [m for m in db.get("bets", []) if m.get("resolved")]
    if not resolved:
        print("В базе нет закрытых матчей.")
        return 0

    print(f"Закрытых матчей: {len(resolved)}. Тяну результаты за {args.days} дн…")
    found = fetch_results(days_back=args.days)
    print(f"Найдено на TennisExplorer: {len(found)}\n")
    if not found:
        print("Результаты не скачались — повторите позже.")
        return 1

    changed = []
    for m in resolved:
        hit = match_result(m.get("player1", ""), m.get("player2", ""), found)
        if not hit:
            continue
        score, flipped = hit
        out = outcome_from_score(score, flipped)
        if not out["winner"]:
            continue
        was = (m.get("sets_p1"), m.get("sets_p2"))
        now = (out["sets_p1"], out["sets_p2"])
        if was == now:
            continue
        changed.append((m, out, was, now))

    # Заодно приводим счёт к читаемому виду у всех записей, даже там, где
    # порядок игроков верный: в старых строках лежит сырой формат
    # TennisExplorer со склеенным тайбрейком.
    import re as _re
    src = open(os.path.join(HERE, "bot_merged.py"), encoding="utf-8").read()
    ns = {"re": _re}
    exec(src[src.index("def _parse_score_sets"):  # noqa: S102
             src.index("def parse_te_last_matches")], ns)
    pretty = ns["pretty_score"]
    tidied = 0
    for m in resolved:
        raw = m.get("score") or ""
        nice = pretty(raw, True)
        if nice and nice != raw:
            tidied += 1
            if args.apply:
                m.setdefault("score_raw", raw)
                m["score"] = nice
    if tidied:
        print(f"Счёт приведён к читаемому виду: {tidied} записей"
              f"{'' if args.apply else ' (будет при --apply)'}\n")

    if not changed:
        print("Порядок игроков везде верный.")
        if tidied and args.apply:
            bak = f"{DB}.bak-{datetime.now():%Y%m%d-%H%M%S}"
            shutil.copy2(DB, bak)
            json.dump(db, open(DB, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            print(f"Записано. Бэкап: {bak}")
        return 0

    print(f"Матчей с перевёрнутым счётом: {len(changed)}\n")
    delta = 0.0
    for m, out, was, now in changed:
        print(f"  {m.get('match')}")
        print(f"    было {was[0]}-{was[1]}, на самом деле {now[0]}-{now[1]}"
              f"   ({out['score']})")
        for b in m.get("bets", []):
            old_st = b.get("status")
            old_pr = float(b.get("profit") or 0)
            pred = b.get("prediction", "")
            s1, s2 = out["sets_p1"], out["sets_p2"]
            # Логика та же, что в resolve_match. Форы не трогаем: там линия
            # хранится в тексте прогноза и разбирается иначе, а угадывать
            # за расчётчик здесь не стоит.
            if b.get("type") == "Moneyline":
                win = (s1 > s2) if "П1" in pred else (s2 > s1)
            elif b.get("type") == "Total Sets":
                import re
                found_ln = re.search(r"([\d.]+)", pred)
                ln = float(found_ln.group(1)) if found_ln else 2.5
                tot = s1 + s2
                win = (tot > ln) if "ТБ" in pred else (tot < ln)
            else:
                continue
            new_st = "win" if win else "loss"
            new_pr = (float(b.get("stake", 0)) * (float(b.get("odds", 1)) - 1)
                      if win else -float(b.get("stake", 0)))
            if new_st != old_st:
                print(f"      {pred}: {old_st} {old_pr:+.0f} -> "
                      f"{new_st} {new_pr:+.0f}")
                delta += new_pr - old_pr
            if args.apply:
                b["status"], b["profit"] = new_st, round(new_pr, 2)
        if args.apply:
            m.update(score=out["score"], sets_p1=out["sets_p1"],
                     sets_p2=out["sets_p2"], games_p1=out["games_p1"],
                     games_p2=out["games_p2"])

    print(f"\nПоправка к прибыли: {delta:+.2f}")
    if not args.apply:
        print("Сухой прогон. Записать: --apply")
        return 0

    bak = f"{DB}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(DB, bak)
    json.dump(db, open(DB, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Записано. Бэкап: {bak}")
    print("Дальше: ./venv/bin/python3 rebuild_history.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
