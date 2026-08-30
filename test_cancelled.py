# -*- coding: utf-8 -*-
"""Отменённый матч закрывается возвратом, а не висит «ждём» вечно.

23.08.2026 матч Samsonova — Preston в Филадельфии отменили. TennisExplorer
отменённые матчи не публикует вообще: строки нет ни за один день, поиск
результата не находит ничего, и ставка остаётся в ожидании навсегда — в
панели она показывала «ждём 19 ч» и продолжала бы считать часы.

Отличить «отменён» от «результат ещё не выложили» можно только по времени,
поэтому через TRA_ABANDON_HOURS после начала матч считается несостоявшимся и
ставки уходят в возврат — так же поступает букмекер.

    python3 test_cancelled.py
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("TRA_TOUR", "atp")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tennisratioall import journal, results as R, scanner as sc  # noqa: E402


def _when(hours_ago: float) -> str:
    """Строка вида «23.08. 21:00» на столько-то часов назад (UTC)."""
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime("%d.%m. %H:%M")


def main() -> int:
    fails = []

    def check(what, got, want):
        ok = got == want
        print(f"  {'OK  ' if ok else 'FAIL'} {what}: {got!r}")
        if not ok:
            fails.append(f"{what}: ожидалось {want!r}, получено {got!r}")

    print(f"порог: {R.ABANDON_HOURS:.0f} ч")
    check("свежий матч ещё ждём", R.abandoned(_when(2)), False)
    check("час до порога — ещё ждём",
          R.abandoned(_when(R.ABANDON_HOURS - 1)), False)
    check("час после порога — несостоявшийся",
          R.abandoned(_when(R.ABANDON_HOURS + 1)), True)
    # Без разобранного времени закрывать нельзя: лучше висящая строка,
    # чем возврат по сыгранному матчу
    check("время не разобралось — не трогаем", R.abandoned("непонятно"), False)
    check("пустое время — не трогаем", R.abandoned(""), False)

    print("исход несостоявшегося матча")
    out = R.cancelled_outcome()
    check("void", out["void"], True)
    check("победителя нет", out["winner"], "")
    check("счёт", out["score"], R.CANCELLED_SCORE)
    check("геймы обнулены", (out["games_p1"], out["games_p2"]), (0, 0))

    print("проход по незакрытым матчам")
    logged, picks_closed, bets_closed, said = [], [], [], []
    journal.log_result = lambda slug, o: logged.append((slug, o["score"]))
    journal.resolve_pick = lambda slug, o: picks_closed.append(slug) or {
        "slug": slug, "status": "refund", "profit": 0}
    journal.resolve_value_bets = lambda slug, o, fn: (
        bets_closed.append(slug) or [{"slug": slug}])

    scan = sc.Scanner.__new__(sc.Scanner)          # без сети
    scan.say = lambda text: said.append(text)

    unmatched = [
        {"slug": "fresh-vs-match", "p1": "A", "p2": "B", "when": _when(3)},
        {"slug": "liudmila-samsonova-vs-taylah-preston",
         "p1": "Liudmila Samsonova", "p2": "Taylah Preston",
         "when": _when(R.ABANDON_HOURS + 5)},
        {"slug": "no-time-vs-match", "p1": "C", "p2": "D", "when": ""},
    ]
    closed = scan._close_abandoned(unmatched)

    check("закрыт ровно один", closed, 1)
    check("закрыт нужный", [s for s, _ in logged],
          ["liudmila-samsonova-vs-taylah-preston"])
    check("исход закрыт", picks_closed,
          ["liudmila-samsonova-vs-taylah-preston"])
    check("ценные ставки закрыты", bets_closed,
          ["liudmila-samsonova-vs-taylah-preston"])
    check("в чат ушло одно сообщение", len(said), 1)
    check("в тексте сказано, что матч не состоялся",
          "не состоялся" in (said[0] if said else ""), True)
    # У несостоявшегося матча нет счёта — нули в карточке только путали бы
    check("в карточке нет строки со счётом",
          "по сетам" in (said[0] if said else ""), False)

    print("матч без строки в журнале (_orphan) не пишется в журнал")
    logged.clear()
    scan._close_abandoned([
        {"slug": "orphan-vs-bet", "p1": "E", "p2": "F",
         "when": _when(R.ABANDON_HOURS + 5), "_orphan": True}])
    check("в журнал не писали", logged, [])

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
