# -*- coding: utf-8 -*-
"""Расчёт снятий по правилам Pinnacle — они разные для разных рынков.

Раньше бот отправлял в возврат ВСЁ, что не доиграно. У Pinnacle иначе:

  * ставка на победителя (Moneyline) СТОИТ, если доигран хотя бы один
    полный сет: снявшийся объявляется проигравшим независимо от счёта;
  * фора и тотал — и по геймам, и по сетам — аннулируются ВСЕГДА;
  * снятие до конца первого сета аннулирует вообще всё.

Победителя снятого матча по счёту не вычислить — сняться может и ведущий.
Его называет TennisExplorer: в колонке итога у присуждённого матча стоит
1:0, единица у прошедшего дальше. Пример из жизни — Kwon — Lajovic 25.08.2026,
US Open qualification: 6-4, 5-7, 1-3 и снятие Квона, выигравшего первый сет.

    python3 test_retired_rules.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("TRA_TOUR", "atp")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tennisratioall import results as R  # noqa: E402
from tennisratioall.value import settle  # noqa: E402


class _Cell:
    def __init__(self, cls, text):
        self._cls, self._text = cls, text

    def get(self, key, default=None):
        return self._cls if key == "class" else default

    def get_text(self, *a, **kw):
        return self._text


class _Row:
    """Минимальная подделка строки таблицы: только ячейки result и score."""

    def __init__(self, result, scores):
        self._tds = [_Cell(["result"], str(result))]
        self._tds += [_Cell(["score"], str(x)) for x in scores]

    def find_all(self, _name):
        return self._tds


def main() -> int:
    fails = []

    def check(what, got, want):
        ok = got == want
        print(f"  {'OK  ' if ok else 'FAIL'} {what}: {got!r}")
        if not ok:
            fails.append(f"{what}: ожидалось {want!r}, получено {got!r}")

    print("кому присуждён недоигранный матч")
    # Kwon — Lajovic: у Лаёвича 1, у Квона 0, хотя Квон выиграл первый сет
    check("1:0 — победил первый",
          R.awarded_winner(_Row(1, [4, 7, 3]), _Row(0, [6, 5, 1])), "p1")
    check("0:1 — победил второй",
          R.awarded_winner(_Row(0, [6, 5, 1]), _Row(1, [4, 7, 3])), "p2")
    # Доигранный матч: в колонке итога 2:1, это не присуждение
    check("2:1 — обычный матч, не присуждение",
          R.awarded_winner(_Row(2, [6, 4, 6]), _Row(1, [4, 6, 3])), "")

    print("сколько сетов доиграно")
    check("6-4, 5-7, 1-3 ret.", R.completed_sets("4-6,7-5,3-1 ret."), 2)
    check("6-4, 3-0 ret. — один сет", R.completed_sets("6-4,3-0 ret."), 1)
    check("5-0 ret. — ни одного", R.completed_sets("5-0 ret."), 0)
    check("неявка", R.completed_sets("w.o."), 0)
    check("тайбрейк считается", R.completed_sets("7-64,3-6,10-6"), 3)
    check("6-5 сетом не считается", R.completed_sets("6-5 ret."), 0)

    print("исход матча: победитель ставится только при сыгранном сете")
    o = R.outcome_from_score("4-6,7-5,3-1 ret.", False, "p1")
    check("сыграно 2 сета — победитель есть", o["winner"], "p1")
    check("флаг void всё равно стоит (форы и тоталы в возврат)", o["void"], True)

    early = R.outcome_from_score("5-0 ret.", False, "p1")
    check("снятие в первом сете — победителя нет", early["winner"], "")
    check("и это по-прежнему void", early["void"], True)

    flip = R.outcome_from_score("4-6,7-5,3-1 ret.", True, "p1")
    check("перевёрнутый порядок переворачивает и победителя",
          flip["winner"], "p2")

    unknown = R.outcome_from_score("4-6,7-5,3-1 ret.", False, "")
    check("TennisExplorer не назвал победителя — возврат",
          unknown["winner"], "")

    print("расчёт ставок при снятии")
    ml1 = {"market": "Moneyline", "pick": "П1", "line": None}
    ml2 = {"market": "Moneyline", "pick": "П2", "line": None}
    check("Moneyline на победителя — выигрыш",
          settle(ml1, 2, 1, 20, 18, retired=True, winner="p1"), "win")
    check("Moneyline на снявшегося — проигрыш",
          settle(ml2, 2, 1, 20, 18, retired=True, winner="p1"), "loss")
    check("Moneyline без победителя — возврат",
          settle(ml1, 0, 0, 5, 0, retired=True, winner=""), "refund")

    for market, pick, line in (("Games Hcap", "П2", -1.5),
                               ("Sets Hcap", "П1", 1.5),
                               ("Total Sets", "ТБ", 2.5)):
        bet = {"market": market, "pick": pick, "line": line}
        check(f"{market} при снятии — всегда возврат",
              settle(bet, 2, 1, 20, 18, retired=True, winner="p1"), "refund")

    print("неявка (w.o.) — матча не было, возврат всего")
    # Регрессия 27.08.2026. Scanner._settle_row определял снятие поиском
    # подстроки «ret» в счёте. У неявки счёт «w.o.», подстроки нет — флаг не
    # выставлялся, и форы считались по счёту 0:0, где проходит ЛЮБАЯ плюсовая
    # линия. Так «выиграли» Sets Hcap П2 +1.5 и Games Hcap П2 +4.5 на
    # Ann Li — Maria Timofeeva. Теперь признак берётся из outcome["void"].
    from tennisratioall.scanner import Scanner  # noqa: PLC0415

    wo = R.outcome_from_score("w.o.")
    check("w.o. помечен недоигранным", wo["void"], True)
    check("w.o. — сеты 0:0", (wo["sets_p1"], wo["sets_p2"]), (0, 0))
    check("w.o. — победителя нет", wo["winner"], "")
    check("подстроки «ret» в счёте нет (та самая ловушка)",
          "ret" in wo["score"].lower(), False)

    for market, pick, line in (("Sets Hcap", "П2", 1.5),
                               ("Games Hcap", "П2", 4.5),
                               ("Total Sets", "ТБ", 2.5)):
        row = {"market": market, "pick": pick, "line": str(line),
               "odds": "1,694", "stake": "1000"}
        status, prof = Scanner._settle_row(row, wo)
        check(f"{market} {pick} {line} при неявке — возврат", status, "refund")
        check(f"{market} {pick} {line} при неявке — прибыль 0", prof, 0.0)

    ml_row = {"market": "Moneyline", "pick": "П2", "line": "",
              "odds": "1,694", "stake": "1000"}
    check("Moneyline при неявке — возврат",
          Scanner._settle_row(ml_row, wo)[0], "refund")

    # Снятие с доигранным сетом по-прежнему считается: исход стоит, фора нет
    ret = dict(R.outcome_from_score("6-4, 3-0 ret.", ret_winner="p1"))
    check("снятие: исход стоит", Scanner._settle_row(
        {"market": "Moneyline", "pick": "П1", "line": "",
         "odds": "1,5", "stake": "1000"}, ret)[0], "win")
    check("снятие: фора в возврат", Scanner._settle_row(
        {"market": "Games Hcap", "pick": "П1", "line": "-1,5",
         "odds": "1,5", "stake": "1000"}, ret)[0], "refund")

    print("доигранный матч считается как раньше")
    check("Moneyline по сетам", settle(ml1, 2, 0, 12, 6), "win")
    check("фора по геймам", settle({"market": "Games Hcap", "pick": "П1",
                                    "line": -1.5}, 2, 0, 12, 6), "win")

    print()
    if fails:
        print(f"ПРОВАЛЕНО: {len(fails)}")
        for f in fails:
            print("   ", f)
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
