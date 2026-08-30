"""Снятие: Hassan — Giustino, Sion 21.08, итог 1:0 при счёте 3-6, 3-2."""
import sys, types, re as _re
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
src = open("bot_merged.py", encoding="utf-8").read()
stub = types.ModuleType("bot_merged")
exec(compile(src[src.index("def _parse_score_sets"):src.index("def parse_te_last_matches")],
             "bot_merged", "exec"), stub.__dict__)
stub.__dict__["re"] = _re; stub.remove_accents = lambda s: s
sys.modules["bot_merged"] = stub
from tennisratioall.results import parse_result_row, outcome_from_score

def prow(name, result, scores, time=""):
    tds = ([f'<td class="first time" rowspan="2">{time}</td>'] if time else []) + [
        f'<td class="t-name"><a>{name}</a></td>',
        f'<td class="result">{result}</td>']
    tds += [f'<td class="score">{x}</td>' for x in scores]
    return "<tr>" + "".join(tds) + "</tr>"

def check(name, html, expect, finished_day=True):
    r1, r2 = BeautifulSoup(f"<table>{html}</table>", "html.parser").find_all("tr")
    got = parse_result_row(r1, r2, False, finished_day=finished_day)
    print(("OK  " if got == expect else "FAIL"), f"{name:<40} -> {got!r}")
    return got

# снятие: пометки ret. в таблице НЕТ, есть только итог 1:0
sc = check("снятие, итог 1:0 (Hassan — Giustino)",
           prow("Hassan B.", "1", ["3", "3", "", ""], "16:30") +
           prow("Giustino L.", "0", ["6", "2", "", ""]),
           "3-6,3-2 ret.")

# неявка: итог 1:0 и вообще никакого счёта
check("неявка, итог 1:0 без счёта",
      prow("A B", "1", ["", "", "", ""], "12:00") + prow("C D", "0", ["", "", "", ""]),
      "w.o.")

# живой матч, ведёт 1:0 по сетам — закрывать всё ещё нельзя
check("живой матч 6-4, 2-1",
      prow("A B", "1", ["6", "2", "", ""], "14:00") +
      prow("C D", "0", ["4", "1", "", ""]),
      None, finished_day=False)

# тот же живой счёт, но страница вчерашняя -> матч закончен снятием
check("вчерашний 6-4, 2-1 = снятие",
      prow("A B", "1", ["6", "2", "", ""], "14:00") +
      prow("C D", "0", ["4", "1", "", ""]),
      "6-4,2-1 ret.")

# снятие распознаётся даже на сегодняшней странице: присуждено тому,
# кто проигрывает по доигранным сетам -> живым быть не может
check("сегодняшнее снятие 3-6, 3-2",
      prow("Hassan B.", "1", ["3", "3", "", ""], "16:30") +
      prow("Giustino L.", "0", ["6", "2", "", ""]),
      "3-6,3-2 ret.", finished_day=False)

# нормальный доигранный — без изменений
check("обычный 2-0",
      prow("A B", "2", ["6", "6", "", ""], "10:00") +
      prow("C D", "0", ["4", "3", "", ""]),
      "6-4,6-3")

print()
out = outcome_from_score(sc)
print("исход снятия:", {k: out[k] for k in
                        ("score", "sets_p1", "sets_p2", "games_p1",
                         "games_p2", "winner", "void")})
print("-> победитель пуст, void=True: ставки уйдут в возврат,")
print("   как их и считает Pinnacle при недоигранном матче.")
