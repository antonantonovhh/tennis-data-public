#!/usr/bin/env python3
"""Занести ожидающую ставку в базу — то же, что кнопка «✅ Ставь!».

Повторяет `save_approved_bets()` + `log_to_csv()` из bot_merged.py, чтобы не
импортировать бота целиком (он на импорте лезет за токенами и поднимает цикл).
Нужен, когда карточка в телеграме уже устарела или её надо занести не так, как
предлагает кнопка: занести, а потом поправить (см. `flip_moneyline.py`).

    python3 approve_awaiting.py <bet_id>            # сухой прогон
    python3 approve_awaiting.py <bet_id> --apply

bet_id — ключ в `awaiting_bets` из bot_state.json.

Службу перед `--apply` остановить: `awaiting_bets` бот держит в памяти и
сохраняет файл целиком, так что правка на живой службе не доживёт до вечера.
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
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("BOT_STATE") or os.path.join(HERE, "bot_state.json")
DB = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")
CSV_FILE = os.environ.get("BETS_CSV") or os.path.join(HERE, "bets_history.csv")
BET_AMOUNT = 1000


def unit_active() -> bool:
    """Жива ли служба. Не systemd (Windows, контейнер) — проверять нечем."""
    try:
        got = subprocess.run(["systemctl", "is-active", "tennis-bot"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return got.stdout.strip() == "active"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bet_id", help="ключ в awaiting_bets (bot_state.json)")
    ap.add_argument("--apply", action="store_true",
                    help="записать (по умолчанию сухой прогон)")
    args = ap.parse_args()

    with open(STATE, encoding="utf-8") as fh:
        state = json.load(fh)
    info = state.get("awaiting_bets", {}).get(args.bet_id)
    if not info:
        have = ", ".join(sorted(state.get("awaiting_bets", {}))) or "пусто"
        sys.exit(f"В awaiting_bets нет {args.bet_id}. Есть: {have}")

    with open(DB, encoding="utf-8") as fh:
        db = json.load(fh)
    # match_id собирается ровно как в bot_merged.py, иначе дедупликация по
    # нажатию кнопки не сработает и матч занесётся вторым экземпляром
    match_id = f"{info['match_name']}_{info['date']}".replace(" ", "_")
    if any(m.get("match_id") == match_id for m in db["bets"]):
        sys.exit(f"Матч уже в базе: {match_id}")

    entry = {
        "match_id": match_id, "date": info["date"],
        "tournament": info["tournament"], "match": info["match_name"],
        "player1": info["p1"], "player2": info["p2"], "added_ts": time.time(),
        "resolved": False, "score": "", "games_p1": 0, "games_p2": 0,
        "sets_p1": 0, "sets_p2": 0, "bets": info["bets"],
    }
    print(f"матч:     {info['match_name']}  ({info['date']})")
    print(f"match_id: {match_id}")
    for b in info["bets"]:
        print(f"  + {b['type']}: {b['prediction']} @ {b['odds']}")

    if unit_active():
        print("\n(служба tennis-bot запущена — правку затрёт ближайший расчёт)")
        if args.apply:
            print("    systemctl stop tennis-bot")
            print(f"    python3 {os.path.basename(__file__)} {args.bet_id} --apply")
            print("    systemctl start tennis-bot")
            return 2
    if not args.apply:
        print("\nСухой прогон. Записать: --apply")
        return 0

    shutil.copy2(DB, f"{DB}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    db["bets"].append(entry)
    with open(DB, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=4)

    if os.path.exists(CSV_FILE):
        shutil.copy2(CSV_FILE, f"{CSV_FILE}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    # BOM и точка с запятой — как пишет сам бот, чтобы Excel не ломался
    with open(CSV_FILE, "a", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        for b in info["bets"]:
            w.writerow([info["tournament"], info["date"], info["match_name"],
                        b["prediction"], f"{BET_AMOUNT}₽", f"{b['odds']:.3f}",
                        "Pin", "В игре", "", "", ""])
    print("\nЗанесено (база + CSV), бэкапы рядом.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
