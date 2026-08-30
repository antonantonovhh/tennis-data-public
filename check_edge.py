#!/usr/bin/env python3
"""Есть ли перевес на самом деле: ROI с доверительным интервалом.

Панели показывают ROI одним числом, и на коротком журнале это вводит в
заблуждение: «+23%» на 33 ставках и «+23%» на 3300 — разные утверждения, а
выглядят одинаково. Скрипт считает к каждому ROI 95%-й интервал и прямо
пишет, отличается он от нуля или нет.

    python3 /opt/tennis_bot/check_edge.py                 # ATP
    python3 /opt/tennis_bot/check_edge.py --tour wta      # WTA по суффиксу

Отдельный экземпляр WTA держит журналы в своём каталоге и задаёт пути через
TRA_VALUE_CSV/TRA_PICKS_CSV, поэтому там нужен его же конфиг:

    set -a; . /opt/tennis_bot/.env; . /opt/tennis_bot_wta/.env; set +a
    PYTHONPATH=/opt/tennis_bot python3 /opt/tennis_bot/check_edge.py

Почему бутстрап кластерный
--------------------------
Ресэмплим МАТЧИ, а не ставки. На один матч приходится до нескольких ставок
(тотал сетов и пачка геймовых гандикапов), и исходы у них общие: затянувшийся
матч заносит их разом. Обычный интервал «по n ставкам» считал бы такие ставки
независимыми и вышел бы уже настоящего в полтора-два раза — то есть врал бы
ровно в сторону «перевес есть».

Зерно фиксировано: на одних и тех же данных ответ обязан повторяться, иначе
числами нельзя пользоваться.

Чего скрипт НЕ делает
---------------------
Не исправляет отбор по результату. Если прогнать его по десятку разрезов и
взять те, где интервал ушёл в плюс, — получится ровно та ошибка, ради которой
он написан: при широких интервалах часть разрезов оказывается «значимой»
случайно. Интервал говорит про одну корзину, выбранную заранее, а не про
лучшую из просмотренных.
"""

from __future__ import annotations

import argparse
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _pick_tour_early() -> None:
    """Ставит TRA_TOUR из --tour ДО импорта пакета.

    Пути к журналам вычисляются на импорте store.py, а argparse отрабатывает
    позже — тот же приём, что в tennisratioall_run.py. Без него --tour wta
    молча открыл бы мужские файлы.
    """
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        val = None
        if a.startswith("--tour="):
            val = a.split("=", 1)[1]
        elif a == "--tour" and i + 1 < len(argv):
            val = argv[i + 1]
        if val:
            os.environ["TRA_TOUR"] = val.strip().lower()
            return


_pick_tour_early()

from tennisratioall import reports                              # noqa: E402
from tennisratioall.journal import (PICK_FIELDS, PICKS_CSV,     # noqa: E402
                                    VALUE_CSV, VALUE_FIELDS,
                                    _read, pf)
from tennisratioall.store import TOUR                           # noqa: E402

# Рынки перечислены явно и в этом порядке, чтобы вывод не прыгал между
# запусками вслед за содержимым журнала.
MARKETS = ("Games Hcap", "Sets Hcap", "Total Sets", "Moneyline")

# Возврат не выигран и не проигран: он не входит ни в оборот, ни в прибыль.
# Тот же фильтр, что в reports.collect — иначе цифры разойдутся с панелью.
SKIP = ("pending", "", "refund", "push")


def settled(rows, start):
    out = []
    for r in rows:
        if (r.get("status") or "pending") in SKIP:
            continue
        d = reports._as_date(r.get("resolved_at") or "")
        if not d or (start and d < start):
            continue
        out.append(r)
    return out


def roi_of(recs):
    stake = sum(pf(r.get("stake"), 0.0) for r in recs)
    profit = sum(pf(r.get("profit"), 0.0) for r in recs)
    return (profit / stake * 100 if stake else 0.0), stake, profit


def interval(recs, iters: int, seed: int):
    """95% интервал ROI кластерным бутстрапом. None — матчей слишком мало."""
    by = {}
    for r in recs:
        by.setdefault(r.get("slug") or "?", []).append(r)
    keys = list(by)
    if len(keys) < 5:
        return None
    rnd, out = random.Random(seed), []
    for _ in range(iters):
        stake = profit = 0.0
        for k in rnd.choices(keys, k=len(keys)):
            for r in by[k]:
                stake += pf(r.get("stake"), 0.0)
                profit += pf(r.get("profit"), 0.0)
        if stake:
            out.append(profit / stake * 100)
    if not out:
        return None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))], len(keys)


def line(tag: str, recs, iters: int, seed: int) -> None:
    if not recs:
        print(f"  {tag:<24} —")
        return
    roi = roi_of(recs)[0]
    got = interval(recs, iters, seed)
    if not got:
        print(f"  {tag:<24} {len(recs):>4} ст.             "
              f"ROI {roi:+6.1f}%   мало матчей для интервала")
        return
    lo, hi, matches = got
    if lo > 0:
        verdict = "плюс"
    elif hi < 0:
        verdict = "минус"
    else:
        verdict = "НЕ ОТЛИЧИМ ОТ НУЛЯ"
    print(f"  {tag:<24} {len(recs):>4} ст. / {matches:>4} матч."
          f"   ROI {roi:+6.1f}%   95%: [{lo:+6.1f}% … {hi:+6.1f}%]"
          f"   {verdict}")


def horizon(recs, iters: int, seed: int) -> None:
    """Сколько матчей нужно, чтобы интервал стал приемлемо узким.

    Полуширина падает как 1/sqrt(числа матчей), отсюда и пересчёт. Это грубая
    оценка «при том же качестве данных», а не обещание: если модель по дороге
    изменится, отсчёт начнётся заново.
    """
    got = interval(recs, iters, seed)
    if not got:
        return
    lo, hi, matches = got
    half = (hi - lo) / 2
    if half <= 0:
        return
    print(f"    сейчас {matches} матчей, полуширина ±{half:.1f} п.п.")
    for target in (10.0, 5.0):
        if half <= target:
            print(f"    ±{target:.0f} п.п. — уже достигнуто")
            continue
        need = matches * (half / target) ** 2
        got_txt = f"{need:,.0f}".replace(",", " ")
        print(f"    ±{target:.0f} п.п. — около {got_txt} матчей "
              f"(×{need / matches:.1f} к нынешнему)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ROI с доверительным интервалом по журналам обходчика")
    ap.add_argument("--tour", default=os.environ.get("TRA_TOUR", "atp"),
                    help="atp|wta (разбирается до импортов, см. код)")
    ap.add_argument("--period", default="all",
                    choices=("all", "day", "week", "month"),
                    help="как на панелях: по дате расчёта")
    ap.add_argument("--iters", type=int, default=4000,
                    help="итераций бутстрапа (по умолчанию 4000)")
    ap.add_argument("--seed", type=int, default=7,
                    help="зерно; менять только чтобы проверить устойчивость")
    a = ap.parse_args()

    start = reports._period_start(a.period)
    value = settled(_read(VALUE_CSV, VALUE_FIELDS), start)
    picks = settled(_read(PICKS_CSV, PICK_FIELDS), start)

    print(f"Тур: {TOUR.upper()}   период: {a.period}   "
          f"бутстрап: {a.iters} итераций, зерно {a.seed}")
    for tag, path, rows in (("ценные", VALUE_CSV, value),
                            ("исходы", PICKS_CSV, picks)):
        note = ""
        # Пустой журнал почти всегда означает не «нет ставок», а «открыли не
        # тот файл»: у отдельного экземпляра WTA пути заданы через TRA_*_CSV,
        # и без его .env --tour wta уходит на файлы с суффиксом рядом с кодом.
        # Молчать тут нельзя — прочерки в таблице читаются как результат.
        if not os.path.exists(path):
            note = "  ← ФАЙЛА НЕТ, тот ли это экземпляр?"
        elif not rows:
            note = "  ← пусто: нет рассчитанных ставок или не тот файл"
        print(f"  {tag}:  {path}{note}")

    print("\nЦЕННЫЕ СТАВКИ — по рынкам")
    for m in MARKETS:
        line(m, [r for r in value if r.get("market") == m], a.iters, a.seed)
    line("ВСЕ вместе", value, a.iters, a.seed)
    horizon(value, a.iters, a.seed)

    print("\nИСХОДЫ — главный разрез")
    line("Согласна с рынком", [r for r in picks if r.get("agree") == "да"],
         a.iters, a.seed)
    line("Спорит с рынком", [r for r in picks if r.get("agree") != "да"],
         a.iters, a.seed)
    line("ВСЕ вместе", picks, a.iters, a.seed)
    horizon(picks, a.iters, a.seed)

    print("\nКак это читать")
    print("  «НЕ ОТЛИЧИМ ОТ НУЛЯ» — перевеса не видно; это не значит, что его")
    print("  нет, только что данных пока не хватает, чтобы его увидеть.")
    print("  Отбирать корзины по этому выводу нельзя: при широких интервалах")
    print("  часть из них окажется «значимой» случайно. Решайте, что ставите,")
    print("  ДО того как смотрите сюда, — иначе меряете собственный отбор.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
