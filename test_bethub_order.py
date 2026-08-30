# -*- coding: utf-8 -*-
"""Очередь публикации на bet-hub: ближайшее событие — первым.

Квота провайдера позволяет одну публикацию в минуту, поэтому порядок очереди
решает всё. Раньше она шла по убыванию перевеса, и матч, стартующий через
десять минут, мог простоять дольше собственного начала. Теперь порядок — по
времени начала, а уже начавшиеся отсеиваются, чтобы не занимать голову
очереди и не жечь квоту впустую.

    python3 test_bethub_order.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("TRA_TOUR", "wta")
os.environ.setdefault("BETHUB_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bethub_publish as P  # noqa: E402


def when(minutes_from_now: float) -> str:
    """Строка «25.08. 16:00» (UTC) со сдвигом от текущего момента."""
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return t.strftime("%d.%m. %H:%M")


def main() -> int:
    fails = []

    def check(what, got, want):
        ok = got == want
        print(f"  {'OK  ' if ok else 'FAIL'} {what}: {got!r}")
        if not ok:
            fails.append(f"{what}: ожидалось {want!r}, получено {got!r}")

    print("отсев начавшихся матчей")
    check("начавшийся час назад", P.already_started({"when": when(-60)}), True)
    check("стартует через 30 мин", P.already_started({"when": when(30)}), False)
    # Неразобранное время не повод выбрасывать прогноз
    check("время не разобралось", P.already_started({"when": "непонятно"}), False)
    check("времени нет вовсе", P.already_started({"when": ""}), False)

    print("порядок очереди")
    bets = [
        {"slug": "d", "when": when(600), "edge": "0,90"},   # поздний, но жирный
        {"slug": "b", "when": when(45), "edge": "0,05"},
        {"slug": "a", "when": when(12), "edge": "0,01"},    # ближайший, слабый
        {"slug": "e", "when": "", "edge": "0,99"},          # без времени
        {"slug": "c", "when": when(45), "edge": "0,50"},    # ничья по времени
    ]

    def order(r):
        t = P.starts_at(r)
        return (0, t.timestamp(), -P.pf(r.get("edge"))) if t \
            else (1, 0.0, -P.pf(r.get("edge")))

    got = [r["slug"] for r in sorted(bets, key=order)]
    # a (12 мин) -> c и b (оба 45 мин, c с бо́льшим перевесом) -> d -> e
    check("ближайший первым, без времени — в конце", got,
          ["a", "c", "b", "d", "e"])
    check("слабый ближайший обгоняет жирный поздний",
          got.index("a") < got.index("d"), True)
    check("при равном времени вперёд больший перевес",
          got.index("c") < got.index("b"), True)

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
