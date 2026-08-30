#!/usr/bin/env python3
"""Сет с тайбрейком не должен выбрасывать матч из расчёта.

История: ставка на Rocha — Harris (7-6(10) в третьем сете) неделю висела
«в игре», хотя матч давно сыгран и на TennisExplorer находился по именам.

Причина — один символ разметки. Сет с тайбрейком записан как
`<td class="score">6<sup>10</sup></td>`, то есть текстом это «610». Фильтр
`\\d{1,2}` такую ячейку отбрасывал, списки геймов по двум строкам матча
получались разной длины ([2,2,6,7] против [1,6,2]), проверка на равенство
длин не проходила — и пара не попадала в найденные матчи ВООБЩЕ. Снаружи
это выглядит не как ошибка разбора, а как «бот не видит результат».

Разметка ниже — настоящая, снята со страницы результатов за 26.08.2026.

    python3 test_te_tiebreak.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# bot_merged на импорте требует токен: подставляем пустышку, сеть не трогаем.
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("CHAT_ID", "0")

from bs4 import BeautifulSoup  # noqa: E402

import bot_merged as B  # noqa: E402

# Реальные строки матча Rocha — Harris. Первая ячейка result — сеты, дальше
# геймы по сетам; третий сет у Rocha с тайбрейком: 6<sup>10</sup>.
ROWS = """
<table class="result">
<tr class="one bott">
  <td class="first time" rowspan="2">23:20</td>
  <td class="t-name"><a href="/player/harris-9f62b/">Harris L.</a></td>
  <td class="result">2</td>
  <td class="score">2</td><td class="score">6</td><td class="score">7</td>
  <td class="score"> </td><td class="score"> </td>
  <td class="coursew" rowspan="2">2.15</td>
  <td class="course" rowspan="2">1.67</td>
</tr>
<tr class="one">
  <td class="t-name"><a href="/player/rocha-0492a/">Rocha H.</a> (15)</td>
  <td class="result">1</td>
  <td class="score">6</td><td class="score">2</td>
  <td class="score">6<sup>10</sup></td>
  <td class="score"> </td><td class="score"> </td>
</tr>
</table>
"""

# Матч без тайбрейка — чтобы правка не сломала обычный случай.
PLAIN = """
<table class="result">
<tr class="one bott">
  <td class="first time" rowspan="2">12:00</td>
  <td class="t-name">Player A</td>
  <td class="result">2</td>
  <td class="score">6</td><td class="score">6</td>
  <td class="score"> </td>
</tr>
<tr class="one">
  <td class="t-name">Player B</td>
  <td class="result">0</td>
  <td class="score">3</td><td class="score">4</td>
  <td class="score"> </td>
</tr>
</table>
"""

ok = fails = 0


def check(name, got, want):
    global ok, fails
    if got == want:
        ok += 1
        print(f"  OK   {name}")
    else:
        fails += 1
        print(f"  FAIL {name}\n       получили {got!r}\n       ожидали  {want!r}")


def rows_of(html):
    soup = BeautifulSoup(html, "html.parser")
    return soup.find_all("tr")


def main() -> int:
    print("Тайбрейк в строке результатов TennisExplorer\n")

    r1, r2 = rows_of(ROWS)
    g1, g2 = B._te_row_scores(r1), B._te_row_scores(r2)

    # Первое число — сеты из td.result, дальше геймы. Тайбрейк приезжает
    # склеенным (610 = 6 геймов и 10 очков на тайбрейке) — именно так его
    # ждёт _parse_score_sets ниже по течению.
    check("строка с тайбрейком разобрана целиком", g2, [1, 6, 2, 610])
    check("строка соперника разобрана", g1, [2, 2, 6, 7])
    check("длины совпадают — матч не будет отброшен", len(g1) == len(g2), True)

    # Ровно та склейка, которую делает check_tennis_explorer_results.
    score = ",".join(f"{a}-{b}" for a, b in zip(g1, g2))
    check("склеенный счёт", score, "2-1,2-6,6-2,7-610")

    # И полный путь до разбора: итог по сетам отбрасывается, тайбрейк
    # раскрывается. Счёт от лица Harris, выигравшего 2-6, 6-2, 7-6(10).
    s1, s2, _, _, sets = B.parse_match_result(score)
    check("сеты после разбора", (s1, s2), (2, 1))
    check("третий сет — тайбрейк 7-6(10)", sets[-1], (7, 6, "10"))

    # Обычный матч не должен пострадать от расширения фильтра.
    p1, p2 = rows_of(PLAIN)
    check("матч без тайбрейка", (B._te_row_scores(p1), B._te_row_scores(p2)),
          ([2, 6, 6], [0, 3, 4]))

    print(f"\nпройдено {ok}, провалено {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
