#!/usr/bin/env python3
"""Разовый пересчет уже рассчитанных ставок в bets_db.json.

Правки в bot_merged.py действуют только на матчи, которые закроются после
перезапуска. Все, что бот успел рассчитать раньше, лежит в базе с неверными
статусами: тотал ТБ 2.5 не мог проиграть, а выигранный на тайбрейке сет мог
перевернуть исход матча. Отчеты строятся из базы, поэтому пока ее не поправить,
ROI будет врать даже с исправленным кодом.

По умолчанию ничего не пишет — только показывает, что изменится:

    python3 fix_settlements.py              # сухой прогон
    python3 fix_settlements.py --apply      # записать (с бэкапом)

Бэкап кладется рядом: bets_db.json.bak-<дата>.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime

# Пути берём относительно самого скрипта, а не текущего каталога: запуск вида
# `python3 /opt/tennis_bot/fix_settlements.py` из домашней папки иначе искал бы
# базу в ~, где её нет, и падал с «нет файла базы» при живом файле рядом.
HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")
BOT_FILE = os.environ.get("BOT_FILE") or os.path.join(HERE, "bot_merged.py")


def load_parsers(bot_path: str):
    """Берет разбор счета из самого бота, чтобы логика не разъехалась.

    Импортировать bot_merged.py целиком нельзя — он на импорте лезет за
    токенами и поднимает мониторинг, поэтому выдергиваем только нужный кусок.
    """
    src = open(bot_path, encoding="utf-8").read()
    try:
        start = src.index("def _parse_score_sets")
        end = src.index("def parse_te_last_matches")
    except ValueError:
        sys.exit(f"Не нашел функции разбора счета в {bot_path} — файл не тот или уже изменен.")
    ns: dict = {"re": re}
    exec(src[start:end], ns)  # noqa: S102
    if "parse_match_result" not in ns:
        sys.exit("В боте нет parse_match_result — сначала примените патч разбора счета.")
    return ns["parse_match_result"], ns["_parse_score_sets"]


def settle(bet: dict, s1: int, s2: int,
           is_retired: bool) -> tuple[str, float]:
    """Повторяет логику resolve_match. Возвращает (статус, прибыль).

    Снятие считается по правилам Pinnacle, как в самом боте и в обходчике:
      * ставка на победителя СТОИТ, если доигран хотя бы один полный сет —
        снявшийся объявляется проигравшим независимо от счёта;
      * фора и тотал аннулируются ВСЕГДА;
      * снятие до конца первого сета аннулирует вообще всё.
    """
    pred = bet.get("prediction", "")
    stake = bet.get("stake", 0)
    odds = bet.get("odds", 0)

    if is_retired:
        # Фора и тотал при снятии — всегда возврат (правило Pinnacle).
        # Исход НЕ трогаем: он зависит от присуждённого победителя, а в базе
        # его нет и по счёту не вычислить — сняться может и ведущий. Живой
        # расчёт в боте берёт победителя из колонки result; здесь источника
        # нет, поэтому статус исхода оставляем как записан.
        if bet.get("type") == "Moneyline":
            return bet.get("status", "pending"), bet.get("profit", 0.0)
        return "refund", 0.0

    won = False
    refund = False
    btype = bet.get("type", "")

    if btype == "Moneyline":
        if "П1" in pred and s1 > s2:
            won = True
        elif "П2" in pred and s2 > s1:
            won = True
    elif btype in ("Games Hcap", "Sets Hcap"):
        return bet.get("status", "pending"), bet.get("profit", 0.0)
    elif btype == "Total Sets":
        total = s1 + s2
        m = re.search(r"([\d.]+)", pred)
        line = float(m.group(1)) if m else 2.5
        over = "ТБ" in pred
        if over:
            won, refund = total > line, total == line
        else:
            won, refund = total < line, total == line
    else:
        return bet.get("status", "pending"), bet.get("profit", 0.0)

    if refund:
        return "refund", 0.0
    if won:
        return "win", stake * (odds - 1.0)
    return "loss", -float(stake)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе сухой прогон)")
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--bot", default=BOT_FILE)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        hint = ""
        cwd_db = os.path.join(os.getcwd(), "bets_db.json")
        if os.path.exists(cwd_db):
            hint = f"\nЕсть в текущем каталоге: {cwd_db} — запустите с --db {cwd_db}"
        sys.exit(f"Нет файла базы: {args.db}{hint}")

    if not os.path.exists(args.bot):
        sys.exit(f"Нет файла бота: {args.bot} — укажите его через --bot")
    parse_match_result, parse_score_sets = load_parsers(args.bot)
    db = json.load(open(args.db, encoding="utf-8"))

    changes = []
    delta_profit = 0.0

    for match in db.get("bets", []):
        if not match.get("resolved"):
            continue
        score = match.get("score") or ""
        if not score:
            continue
        s1, s2, g1, g2, sets = parse_match_result(score)
        # Снятие ищем в двух местах и двумя способами.
        #
        # 1) `score` — это причёсанный счёт, из него pretty_score выбрасывает
        #    пометку «ret.»; она остаётся только в `score_raw`.
        # 2) У части матчей пометки нет вообще НИГДЕ: TennisExplorer её не
        #    рисует, а признаком служит колонка итога со счётом 1:0 — она
        #    приходит первым токеном сырой строки. Обычный доигранный матч
        #    даёт там 2-0, 2-1 или 3-x, поэтому 1:0 однозначно означает
        #    «присуждён». Так 27.08.2026 у Garin — Samuel тотал посчитался
        #    выигрышем вместо возврата.
        raw = match.get("score_raw") or ""
        is_retired = "ret" in f"{score} {raw}".lower()
        if not is_retired and raw:
            head = parse_score_sets(raw)
            if head and (head[0][0], head[0][1]) in ((1, 0), (0, 1)):
                is_retired = True

        old_g = (match.get("games_p1"), match.get("games_p2"))
        old_s = (match.get("sets_p1"), match.get("sets_p2"))
        if old_s != (s1, s2) or old_g != (g1, g2):
            changes.append(("СЧЁТ", match.get("match", "?"), score,
                            f"сеты {old_s[0]}-{old_s[1]} -> {s1}-{s2}, "
                            f"геймы {old_g[0]}-{old_g[1]} -> {g1}-{g2}"))
        match["sets_p1"], match["sets_p2"] = s1, s2
        match["games_p1"], match["games_p2"] = g1, g2

        for bet in match.get("bets", []):
            if bet.get("status") in (None, "pending"):
                continue
            new_status, new_profit = settle(bet, s1, s2, is_retired)
            old_status = bet.get("status")
            old_profit = float(bet.get("profit", 0.0))
            if new_status != old_status or abs(new_profit - old_profit) > 0.01:
                changes.append(("СТАВКА", match.get("match", "?"),
                                bet.get("prediction", ""),
                                f"{old_status} {old_profit:+.0f}₽ -> "
                                f"{new_status} {new_profit:+.0f}₽"))
                delta_profit += new_profit - old_profit
            bet["status"], bet["profit"] = new_status, new_profit

    if not changes:
        print("Расхождений нет — база уже согласована с исправленным разбором счета.")
        return 0

    print(f"Найдено расхождений: {len(changes)}\n")
    for kind, name, detail, change in changes:
        print(f"  [{kind:<6}] {name[:34]:<36} {detail[:22]:<24} {change}")
    print(f"\nСуммарная поправка к прибыли: {delta_profit:+.2f}₽")

    if not args.apply:
        print("\nСухой прогон. Чтобы записать: python3 fix_settlements.py --apply")
        return 0

    backup = f"{args.db}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(args.db, backup)
    with open(args.db, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=2)
    print(f"\nЗаписано. Бэкап: {backup}")
    print("Перезапустите бота и вызовите /results или дождитесь отчета.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
