# -*- coding: utf-8 -*-
"""Снятие и присуждённый матч в расчёте БОТА (bot_merged.py).

Правила Pinnacle те же, что у обходчика (см. test_retired_rules.py), но код
другой, и здесь он ломался иначе.

27.08.2026, Garin — Samuel: Гарин снялся при 0:5 в третьем. В разметке
TennisExplorer пометки «ret.» НЕ БЫЛО вовсе — единственный признак это
колонка result со счётом 1:0. Итог 1:0 уехал в список сетов четвёртым
«сетом», снятие не распозналось, и ТБ 2.5 засчитался выигрышем (+1350)
вместо возврата.

Вторая ловушка того же случая: flip_score() собирает счёт из разобранных
сетов заново и теряла пометку «ret.». У матча с обратным порядком игроков
снятие переставало определяться даже когда пометка в источнике была.

    python3 test_bot_retired.py
"""
import os, sys
os.environ.setdefault("TELEGRAM_TOKEN", "x"); os.environ.setdefault("CHAT_ID", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot_merged import (_te_awarded, _completed_sets, flip_score,
                        parse_match_result, resolve_match)

fails = []
def check(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  ПРОВАЛ ") + f"{name}: {got!r}")
    if not ok:
        fails.append(f"{name}: {got!r} != {want!r}")

print("распознавание присуждённого матча")
check("result 1:0 -> p1", _te_awarded(1, 0), "p1")
check("result 0:1 -> p2", _te_awarded(0, 1), "p2")
check("обычный 2:0 -> не присуждён", _te_awarded(2, 0), "")
check("обычный 2:1 -> не присуждён", _te_awarded(2, 1), "")

print("\nперенос пометки при перевороте")
check("flip сохраняет ret.", "ret" in flip_score("1-0,4-6,6-4,5-0 ret.").lower(), True)
check("flip без пометки её не выдумывает", "ret" in flip_score("2-0,6-4,6-3").lower(), False)

print("\nдоигранные сеты")
check("6-4,4-6,0-5 -> два полных", _completed_sets(parse_match_result("6-4,4-6,0-5 ret.")[4]), 2)
check("0-5 один -> ноль полных", _completed_sets(parse_match_result("0-5 ret.")[4]), 0)

def run(score, awarded, bets):
    m = {"match_id": "t", "match": "A - B", "player1": "A", "player2": "B",
         "bets": [dict(b) for b in bets], "date": "27.08. 15:00"}
    import bot_merged
    bot_merged.send_notification = lambda *a, **k: None
    resolve_match(m, score, awarded)
    return [(b["type"], b["status"], b["profit"]) for b in m["bets"]]

ML = {"type": "Moneyline", "prediction": "П2", "odds": 1.472, "stake": 1000}
TS = {"type": "Total Sets", "prediction": "ТБ 2.5 (сеты)", "odds": 2.35, "stake": 1000}

print("\nGarin — Samuel: снятие при 0:5, присуждён П2")
got = run("6-4,4-6,0-5 ret.", "p2", [ML, TS])
check("исход П2 — выигрыш (доигран сет)", got[0][:2], ("Moneyline", "win"))
check("тотал — возврат", got[1][:2], ("Total Sets", "refund"))
check("прибыль тотала 0", got[1][2], 0)

print("\nснятие в первом сете — аннулируется всё")
got = run("0-5 ret.", "p2", [ML, TS])
check("исход — возврат", got[0][1], "refund")
check("тотал — возврат", got[1][1], "refund")

print("\nставка на снявшегося — проигрыш")
got = run("6-4,4-6,0-5 ret.", "p1", [ML])
check("П2 при присуждении P1 — проигрыш", got[0][1], "loss")

print("\nдоигранный матч считается как раньше")
got = run("6-4,4-6,6-3", "", [ML, TS])
check("исход П2 — проигрыш", got[0][1], "loss")
check("ТБ 2.5 при трёх сетах — выигрыш", got[1][1], "win")

print()
print("ПРОВАЛЕНО: " + "; ".join(fails) if fails else "всё сошлось")
sys.exit(1 if fails else 0)
