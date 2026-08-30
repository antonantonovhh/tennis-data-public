#!/usr/bin/env python3
"""Снять закрывающую линию Pinnacle по уже записанным ставкам (CLV).

Зачем
-----
ROI на коротком журнале бесполезен: интервал шире самого перевеса, и любой
разрез, выбранный по результату, оказывается «прибыльным» случайно (см.
вкладку «Метод»). CLV — движение цены от нашей до закрывающей — решает эту
проблему: сигнал даёт КАЖДАЯ ставка сразу, не дожидаясь исхода матча, и шума
в нём в разы меньше. Если ставки систематически берут цену лучше
закрывающей, перевес есть; если нет — никакой фильтр этого не изменит.

Это измерительный прибор, а не источник прибыли: он не делает модель лучше,
он быстро и честно говорит, есть ли смысл продолжать.

Как работает
------------
Раз в несколько минут по таймеру: находит ставки, у которых матч начинается
в ближайшие минуты, один раз ходит за линией на матч (не на ставку — квота
Pinnacle общая и дорогая) и дописывает строку в отдельный файл `clv.csv`.

**Существующие журналы не трогаются вообще.** Ни одной колонки в
`value_bets.csv` и `picks.csv` не добавляется, обходчик не изменён: связь
идёт по ключу ставки. Это то же решение, что и с правилом отбора на вкладке
«Метод» — новое считается поверх старого потока, а не вместо него.

    python3 collect_clv.py            # сухой прогон: кого бы снял
    python3 collect_clv.py --apply    # снять и записать
    python3 collect_clv.py --window 30 --apply

Окно `--window` — за сколько минут до начала матча считать линию
закрывающей. По умолчанию 20: с таймером в 10 минут каждый матч гарантированно
попадает в окно хотя бы раз. Матч, начавшийся больше `--grace` минут назад
(по умолчанию 5), пропускается: после старта Pinnacle показывает уже live-цену,
и записать её как закрывающую значило бы испортить измерение.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _pick_tour_early() -> str:
    """--tour до импорта пакета: пути к журналам считаются на импорте store.py."""
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

from tennisratioall.journal import (PICK_FIELDS, PICKS_CSV,  # noqa: E402
                                    VALUE_CSV, VALUE_FIELDS, _read)
from tennisratioall.results import parse_when  # noqa: E402
from tennisratioall.value import parse_line  # noqa: E402

log = logging.getLogger("clv")

CLV_CSV = (os.environ.get("TRA_CLV_CSV")
           or os.path.join(os.path.dirname(VALUE_CSV),
                           os.path.basename(VALUE_CSV).replace("value_bets", "clv")))

# Ставки бота — отдельная популяция со своим отбором (нажатая кнопка, а не
# найденное расхождение), поэтому и файл отдельный. Смешивать их с журналами
# обходчика нельзя по той же причине, по которой у тех разведены туры.
BOT_DB = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")
BOT_CLV_CSV = (os.environ.get("BOT_CLV_CSV")
               or os.path.join(os.path.dirname(BOT_DB), "clv_bot.csv"))

FIELDS = ["key", "stream", "slug", "p1", "p2", "when", "market", "pick", "line",
          "odds_open", "odds_close", "odds_close_other", "taken_at", "closed_at"]

# Откуда брать нужную сторону в ответе get_pinnacle_odds: рынок -> ключ строки.
MARKET_KEY = {"Total Sets": "total_sets", "Sets Hcap": "h_sets",
              "Games Hcap": "h_games"}


def bet_key(row: dict, stream: str) -> str:
    """Ключ ставки. У ценных он уже есть (bet_id), у исходов — по слагу."""
    if row.get("_key"):
        return row["_key"]          # ставки бота приносят ключ с собой
    if stream == "value":
        return f"value|{row.get('bet_id') or ''}"
    return f"pick|{row.get('slug') or ''}"


def bot_rows() -> list:
    """Ставки бота из bets_db.json в том же виде, что строки журналов.

    Структура там другая — матч с вложенным списком ставок, — но дальше по
    коду нужен один и тот же набор полей, поэтому приводим здесь, а не
    разводим два пути съёма линии.
    """
    try:
        with open(BOT_DB, encoding="utf-8") as fh:
            db = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("%s не читается: %s", BOT_DB, exc)
        return []
    out = []
    for m in db.get("bets", []):
        if m.get("resolved"):
            continue
        for b in m.get("bets", []):
            if b.get("status") != "pending":
                continue
            kind, pred = b.get("type"), (b.get("prediction") or "")
            if kind == "Moneyline":
                market, pick, line = "Moneyline", pred.strip(), None
                if pick not in ("П1", "П2"):
                    continue
            elif kind == "Total Sets":
                # «ТБ 2.5 (сеты)» -> сторона и линия. Разбираем, а не сверяем
                # со строкой целиком: линия зависит от формата матча (2.5 в
                # трёх сетах, 3.5 в пяти).
                got = re.search(r"(ТБ|ТМ)\s*([\d.,]+)", pred)
                if not got:
                    continue
                market, pick = "Total Sets", got.group(1)
                line = float(got.group(2).replace(",", "."))
            else:
                continue
            out.append({
                "slug": m.get("match_id"), "p1": m.get("player1"),
                "p2": m.get("player2"), "when": m.get("date"),
                "market": market, "pick": pick, "line": line,
                "odds": b.get("odds"), "status": "pending",
                "found_at": m.get("added_ts") or "",
                "_key": f"bot|{m.get('match_id')}|{kind}|{pred}",
            })
    return out


def load_done(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return {r.get("key") for r in csv.DictReader(fh, delimiter=";")}


def append(path: str, rows: list) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";",
                           extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def due(rows: list, stream: str, done: set, window: int, grace: int) -> list:
    """Ставки, у которых матч вот-вот начнётся, а цена закрытия не снята."""
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        if r.get("status") != "pending":
            continue          # рассчитанную снимать поздно и незачем
        if bet_key(r, stream) in done:
            continue
        start = parse_when(r.get("when"))
        if not start:
            continue
        if start - timedelta(minutes=window) <= now <= start + timedelta(minutes=grace):
            out.append(r)
    return out


def price_for(odds: dict, market: str, pick: str, line) -> tuple:
    """(цена нашей стороны, цена противоположной) из ответа Pinnacle."""
    if market == "Moneyline":
        a, b = odds.get("p1"), odds.get("p2")
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            return None, None
        return (a, b) if pick == "П1" else (b, a)

    key = MARKET_KEY.get(market)
    if not key:
        return None, None
    pairs = parse_line(odds.get(key, "") or "")
    if not pairs:
        return None, None
    try:
        want = float(str(line).replace(",", "."))
    except (TypeError, ValueError):
        return None, None
    # Линия сравнивается ТОЧНО, со знаком. Сравнение по модулю склеивало
    # П1 -1.5 с П1 +1.5 — это разные ставки с разной ценой, и в закрытие
    # приезжала чужая (1.461 против 3.650 на живых данных).
    # Противоположная сторона нужна, чтобы потом убрать маржу: без неё CLV
    # считается по цене с зашитой комиссией и систематически занижен.
    # У тотала это та же линия (ТБ 3.5 / ТМ 3.5), у форы — зеркальная
    # (П1 -1.5 против П2 +1.5).
    other = {"ТБ": "ТМ", "ТМ": "ТБ", "П1": "П2", "П2": "П1"}.get(pick)
    other_line = want if market == "Total Sets" else -want
    mine = opp = None
    for p, ln, pr in pairs:
        if p == pick and abs(ln - want) < 1e-6:
            mine = pr
        elif p == other and abs(ln - other_line) < 1e-6:
            opp = pr
    return mine, opp


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию сухой прогон)")
    ap.add_argument("--tour", default=TOUR, choices=["atp", "wta"])
    ap.add_argument("--window", type=int, default=20,
                    help="за сколько минут до начала снимать линию")
    ap.add_argument("--grace", type=int, default=5,
                    help="сколько минут после начала ещё допустимо")
    ap.add_argument("--source", default="crawler", choices=["crawler", "bot"],
                    help="чьи ставки снимать: журналы обходчика или bets_db.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out_csv = BOT_CLV_CSV if args.source == "bot" else CLV_CSV
    done = load_done(out_csv)
    if args.source == "bot":
        todo = [(r, "bot") for r in
                due(bot_rows(), "bot", done, args.window, args.grace)]
    else:
        todo = (
            [(r, "value") for r in due(_read(VALUE_CSV, VALUE_FIELDS), "value", done, args.window, args.grace)]
            + [(r, "pick") for r in due(_read(PICKS_CSV, PICK_FIELDS), "pick", done, args.window, args.grace)]
        )
    if not todo:
        log.info("в окне никого: снимать нечего")
        return 0

    by_match = {}
    for r, stream in todo:
        by_match.setdefault(r.get("slug"), []).append((r, stream))
    log.info("матчей в окне: %d, ставок: %d", len(by_match), len(todo))

    # bot_merged импортируем здесь, а не наверху: он тянет токены и тяжёлые
    # зависимости, а при сухом прогоне без матчей они не нужны вовсе.
    import bot_merged  # noqa: PLC0415

    out = []
    for slug, items in by_match.items():
        first = items[0][0]
        p1, p2 = first.get("p1"), first.get("p2")
        odds = bot_merged.get_pinnacle_odds(p1, p2, is_manual=False)
        if not odds or odds.get("error"):
            log.warning("линия не пришла: %s — %s", p1, p2)
            continue
        for r, stream in items:
            market = r.get("market") or "Moneyline"     # в picks.csv рынка нет
            pick = r.get("pick") or r.get("side")
            close, other = price_for(odds, market, pick, r.get("line"))
            if not close:
                log.warning("не нашёл сторону в закрывающей линии: %s %s %s",
                            slug, market, pick)
                continue
            out.append({
                "key": bet_key(r, stream), "stream": stream, "slug": slug,
                "p1": p1, "p2": p2, "when": r.get("when"),
                "market": market, "pick": pick, "line": r.get("line", ""),
                "odds_open": r.get("odds"), "odds_close": f"{close:.3f}",
                "odds_close_other": f"{other:.3f}" if other else "",
                "taken_at": r.get("found_at", ""),
                "closed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            log.info("%s %s %s: было %s, закрытие %.3f",
                     slug, market, pick, r.get("odds"), close)

    if not out:
        log.info("нечего записывать")
        return 0
    if not args.apply:
        log.info("сухой прогон: записал бы %d строк в %s", len(out), out_csv)
        return 0
    append(out_csv, out)
    log.info("записано строк: %d -> %s", len(out), out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
