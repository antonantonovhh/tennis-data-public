"""Разметка как в дампе с сервера: rowspan у времени, кэфов и значков."""
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from tennisratioall.results import parse_result_row, _score_cells, _row_numbers

# верх: время(rowspan=2) + имя + result + 5 score + кэфы + значки(rowspan=2)
top = ('<tr><td class="first time" rowspan="2">16:15</td>'
       '<td class="t-name"><a>Paulson A / Vliegen J</a></td>'
       '<td class="result">2</td>'
       '<td class="score">7</td><td class="score">3</td>'
       '<td class="score">10</td><td class="score"></td>'
       '<td class="score"></td>'
       '<td class="coursew" rowspan="2">1.14</td>'
       '<td class="course" rowspan="2">4.67</td>'
       '<td class="alone-icons" rowspan="2"></td>'
       '<td rowspan="2"><a href="/match-detail/?id=3295577">info</a></td></tr>')
# низ: имя + result + 5 score. Ячеек СЕМЬ против двенадцати сверху.
bot = ('<tr><td class="t-name"><a>Kumstat J / Pecak S.</a></td>'
       '<td class="result">1</td>'
       '<td class="score">64</td><td class="score">6</td>'
       '<td class="score">6</td><td class="score"></td>'
       '<td class="score"></td></tr>')

r1, r2 = BeautifulSoup(f"<table>{top}{bot}</table>", "html.parser").find_all("tr")
print("по позиции (как было):")
print("  верх:", _row_numbers(r1))
print("  низ :", _row_numbers(r2), " <- длина не совпадает, отсюда сдвиг")
print("по классам (как стало):")
print("  верх:", _score_cells(r1))
print("  низ :", _score_cells(r2))
got = parse_result_row(r1, r2, False)
print(f"\nРАЗБОР: {got!r}")
print("было на сервере: '2-64,7-6,3-6'  (итог по сетам встал против сета)")
print("ждём:            '7-64,3-6,10-6'")
assert got == "7-64,3-6,10-6", got
print("\nOK")
