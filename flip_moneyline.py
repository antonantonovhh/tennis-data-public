#!/usr/bin/env python3
"""Развернуть ставку Moneyline на другого игрока в bets_db.json.

Бот всегда ставит на фаворита: в `calculate_potential_bets` (bot_merged.py)
стоит `is_fav_p1 = val1 <= val2`, и в ставку идёт игрок с МЕНЬШИМ кэфом.
Кнопка «✅ Ставь!» заносит ровно то, что перечислено в карточке, выбрать
андердога из телеграма нельзя. Этот скрипт — разовая правка уже занесённой
ставки: П1 <-> П2 с подстановкой цены другой стороны.

Цена берётся из самой записи (`odds_p1`/`odds_p2` — бот кладёт обе именно
для таких проверок), заново ходить в Pinnacle не нужно. Если их в записи
нет (старые ставки или линия пришла односторонней) — цену задать вручную
через `--odds`.

    systemctl stop tennis-bot
    python3 flip_moneyline.py Rocha            # сухой прогон
    python3 flip_moneyline.py Rocha --apply
    systemctl start tennis-bot

Останавливать службу обязательно: `resolve_match` пишет bets_db.json
целиком из памяти и следом пересобирает bets_history.csv, так что правка
на живой службе может не дожить до ближайшего расчёта.

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

# Пути от самого скрипта, а не от текущего каталога: запуск вида
# `python3 /opt/tennis_bot/flip_moneyline.py` из домашней папки иначе искал бы
# базу в ~, где её нет.
HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")
CSV_FILE = os.environ.get("BETS_CSV") or os.path.join(HERE, "bets_history.csv")
UNIT = "tennis-bot"

OTHER = {"П1": "П2", "П2": "П1"}


def bot_running() -> bool:
    """Жива ли служба. Не systemd (Windows, контейнер) — проверять нечем."""
    try:
        got = subprocess.run(["systemctl", "is-active", UNIT],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return got.stdout.strip() == "active"


def find_match(db: dict, needle: str) -> list:
    """Матчи, где подстрока встречается в названии, id или именах игроков."""
    n = needle.casefold()
    out = []
    for m in db.get("bets", []):
        hay = " ".join(str(m.get(k, "")) for k in
                       ("match", "match_id", "player1", "player2"))
        if n in hay.casefold():
            out.append(m)
    return out


def side_of(bet: dict) -> str:
    """«П1»/«П2» из поля prediction. Пусто, если формат не тот."""
    pred = bet.get("prediction", "")
    for side in OTHER:
        if side in pred:
            return side
    return ""


def player_name(match: dict, side: str) -> str:
    return match.get("player1" if side == "П1" else "player2", "?")


def patch_csv(path: str, match_name: str, old_pred: str,
              new_pred: str, new_odds: float, apply: bool) -> int:
    """Правит строку в bets_history.csv. Возвращает число изменённых строк.

    Строку всё равно перепишет `regenerate_csv_from_db()` при ближайшем
    расчёте любого матча, но до тех пор в журнале висел бы старый прогноз.
    """
    if not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    if not rows:
        return 0
    head = rows[0]
    try:
        c_event = head.index("Событие")
        c_pred = head.index("Прогноз")
        c_odds = head.index("Коэф.")
    except ValueError:
        print(f"  ! в {os.path.basename(path)} нет нужных колонок, пропускаю")
        return 0

    hit = 0
    for r in rows[1:]:
        if len(r) <= max(c_event, c_pred, c_odds):
            continue
        if r[c_event] == match_name and r[c_pred] == old_pred:
            r[c_pred] = new_pred
            r[c_odds] = f"{new_odds:.3f}"
            hit += 1
    if hit and apply:
        shutil.copy2(path, f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        # BOM и точка с запятой — как пишет сам бот, чтобы Excel не ломался
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh, delimiter=";").writerows(rows)
    return hit


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("match", help="часть названия матча или фамилия игрока")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (по умолчанию сухой прогон)")
    ap.add_argument("--odds", type=float,
                    help="цена новой стороны, если её нет в записи")
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--csv", default=CSV_FILE)
    ap.add_argument("--force", action="store_true",
                    help="править при живой службе и уже рассчитанные ставки")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"Нет файла базы: {args.db}")
    with open(args.db, encoding="utf-8") as fh:
        db = json.load(fh)

    found = find_match(db, args.match)
    if not found:
        sys.exit(f"По «{args.match}» в базе ничего не нашлось.")
    if len(found) > 1:
        print(f"По «{args.match}» подходит {len(found)} матчей — уточните запрос:")
        for m in found:
            print(f"  • {m.get('match', '?')}  ({m.get('date', '')})")
        return 2
    match = found[0]

    ml = [b for b in match.get("bets", []) if b.get("type") == "Moneyline"]
    if not ml:
        sys.exit(f"У матча «{match.get('match', '?')}» нет ставки Moneyline.")
    if len(ml) > 1:
        sys.exit(f"У матча «{match.get('match', '?')}» {len(ml)} ставок Moneyline — "
                 "разбирайтесь руками, автоматом не угадаю нужную.")
    bet = ml[0]

    old = side_of(bet)
    if not old:
        sys.exit(f"Не понял сторону ставки: prediction = {bet.get('prediction')!r}. "
                 "Ожидалось «П1» или «П2».")
    new = OTHER[old]

    if bet.get("status") != "pending" and not args.force:
        sys.exit(f"Ставка уже рассчитана (status={bet.get('status')!r}, "
                 f"profit={bet.get('profit')}). Разворот задним числом сломает ROI: "
                 "сначала пересчитайте её (recheck_results.py / fix_settlements.py) "
                 "либо повторите с --force, если понимаете, что делаете.")

    odds = args.odds or bet.get("odds_p2" if new == "П2" else "odds_p1")
    if not odds:
        sys.exit(f"В записи нет цены для {new} (odds_p1/odds_p2 отсутствуют). "
                 "Задайте её вручную: --odds <кэф>")
    odds = float(odds)

    old_pred = bet.get("prediction", "")
    new_pred = old_pred.replace(old, new)

    print(f"матч:   {match.get('match', '?')}  ({match.get('date', '')})")
    print(f"было:   {old_pred} — {player_name(match, old)}, кэф {bet.get('odds')}")
    print(f"станет: {new_pred} — {player_name(match, new)}, кэф {odds:.3f}")

    csv_hits = patch_csv(args.csv, match.get("match", ""), old_pred,
                         new_pred, odds, apply=False)
    print(f"строк в {os.path.basename(args.csv)} к правке: {csv_hits}")

    if bot_running():
        print(f"\n(служба {UNIT} запущена — правку затрёт ближайший расчёт матча)")
        if args.apply and not args.force:
            me = os.path.basename(__file__)
            print(f"    systemctl stop {UNIT}")
            print(f"    python3 {me} {args.match} --apply")
            print(f"    systemctl start {UNIT}")
            print("Либо повторите с --force, если понимаете, что делаете.")
            return 2

    if not args.apply:
        print("\nСухой прогон. Записать: --apply")
        return 0

    bet["prediction"] = new_pred
    bet["odds"] = odds

    bak = f"{args.db}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(args.db, bak)
    with open(args.db, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=4)
    patch_csv(args.csv, match.get("match", ""), old_pred, new_pred, odds, apply=True)
    print(f"\nЗаписано. Бэкап базы: {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
