# -*- coding: utf-8 -*-
"""Повторный круг за линией не должен тащить старую дату из слепка.

Матч, у которого линия ещё не открылась, получает статус `awaiting_odds`:
разбор страницы и симуляция сохраняются в `Entry.rec`/`Entry.sim`, и на
следующих кругах `_retry_odds` спрашивает только котировки, ничего не
пересчитывая. Слепок при этом хранил и `when` — а он приходит с афиши.

24.08.2026 это дало живучий баг. После правки разбора карточки афиша стала
отдавать верные даты, журналы починили скриптом — но матчи в `awaiting_odds`
каждые десять минут заново переписывали в журналы «August Holmgren 15:00»
из своего старого слепка, и кривые даты возвращались сами собой.

    python3 test_retry_when.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("TRA_TOUR", "atp")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tennisratioall import journal, scanner as sc  # noqa: E402
from tennisratioall.store import Entry, MatchRef  # noqa: E402

СТАРОЕ = {"slug": "kei-nishikori-vs-sebastian-ofner",
          "p1": "Kei Nishikori", "p2": "Sebastian Ofner",
          "when": "August Holmgren 15:00",          # мусор из старого слепка
          "tournament": "Us Open Qualies",
          "url": "https://old.example/h2h.html",
          "sim_p1": 0.54, "model_gap": 0.1}


class _Store:
    def upsert(self, e, **kw):
        pass


def main() -> int:
    ref = MatchRef(slug=СТАРОЕ["slug"], p1=СТАРОЕ["p1"], p2=СТАРОЕ["p2"],
                   url="https://www.tennisratio.com/h2h-compare/x.html",
                   tournament="Grand Slams Us Open Qualies (Hard)",
                   when="25.08. 15:00")                    # свежее с афиши
    entry = Entry(slug=ref.slug, status="awaiting_odds",
                  sim={"runs": 1}, rec=dict(СТАРОЕ), summary={})

    written = {}

    scan = sc.Scanner.__new__(sc.Scanner)      # без __init__: сеть не нужна
    scan.store = _Store()
    scan._attach_odds = lambda r, e, rec: ("found", {"p1": 1.9}, [])
    scan._record_pick = lambda rec, odds: None
    scan._announce_value = lambda r, rec, bets: None

    sys.modules["tennis_parser.simulation"].from_snapshot = lambda s: None
    journal.log_match = lambda rec, odds, bets: written.update(rec)

    import threading
    scan._retry_odds(ref, entry, threading.Lock(), [])

    fails = []

    def check(what, got, want):
        ok = got == want
        print(f"  {'OK  ' if ok else 'FAIL'} {what}: {got!r}")
        if not ok:
            fails.append(f"{what}: ожидалось {want!r}, получено {got!r}")

    print("повторный круг за линией берёт свежие поля с афиши")
    check("when", written.get("when"), "25.08. 15:00")
    check("tournament", written.get("tournament"),
          "Grand Slams Us Open Qualies (Hard)")
    check("url", written.get("url"),
          "https://www.tennisratio.com/h2h-compare/x.html")
    print("...а посчитанное из слепка остаётся нетронутым")
    check("sim_p1", written.get("sim_p1"), 0.54)
    check("model_gap", written.get("model_gap"), 0.1)

    # Пустая афиша (матч исчез) не должна затирать то, что уже знаем
    written.clear()
    entry2 = Entry(slug=ref.slug, status="awaiting_odds",
                   sim={"runs": 1}, rec=dict(СТАРОЕ, when="25.08. 15:00"),
                   summary={})
    empty = MatchRef(slug=ref.slug, p1=ref.p1, p2=ref.p2, url="",
                     tournament="", when="")
    scan._retry_odds(empty, entry2, threading.Lock(), [])
    print("матч пропал с афиши — старое значение сохраняется, а не пустеет")
    check("when", written.get("when"), "25.08. 15:00")

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
