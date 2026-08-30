import sys, re
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from tennisratioall.results import parse_result_row, UNFINISHED_RE

def rows(html):
    s = BeautifulSoup(f"<table>{html}</table>", "html.parser")
    return s.find_all("tr")

def row(time, name, result, scores, odds=("1.30", "3.40"), extra=""):
    tds = [f'<td class="first time">{time}</td>',
           f'<td class="t-name"><a>{name}</a>{extra}</td>',
           f'<td class="result">{result}</td>']
    tds += [f'<td class="score">{x}</td>' for x in scores]
    tds += [f'<td class="coursew">{odds[0]}</td>',
            f'<td class="course">{odds[1]}</td>',
            '<td class="icons"><a>i</a></td>']
    return "<tr>" + "".join(tds) + "</tr>"

def check(name, html, expect, finished_day=True):
    r1, r2 = rows(html)
    marker = bool(UNFINISHED_RE.search(r1.get_text(" ", strip=True))
                  or UNFINISHED_RE.search(r2.get_text(" ", strip=True)))
    got = parse_result_row(r1, r2, marker, finished_day=finished_day)
    ok = "OK " if got == expect else "FAIL"
    print(f"{ok} {name:<38} -> {got!r}   (ждали {expect!r})")

# 1. обычный доигранный матч 2-0
check("2-0, пустые хвостовые сеты",
      row("10:00","Alcaraz C.","2",["6","6","","",""]) +
      row("","Sinner J.","0",["4","3","","",""]),
      "6-4,6-3")

# 2. три сета с тайбрейком (у проигравшего склеено 64)
check("три сета, тайбрейк 7-6(4)",
      row("11:00","A B","2",["7","3","6","",""]) +
      row("","C D","1",["64","6","4","",""]),
      "7-64,3-6,6-4")

# 3. тайбрейк 10+ у проигравшего: 610
check("тайбрейк 6(10)-7 (три цифры)",
      row("11:00","A B","2",["610","6","6","",""]) +
      row("","C D","1",["7","3","4","",""]),
      "610-7,6-3,6-4")

# 4. ЖИВОЙ матч: итог 1-0, а сетов сыграно полтора
# Сегодняшняя страница: 1:0 при 6-4, 2-1 может быть и живым матчем,
# и снятием в пользу лидера. Отличить нельзя — не закрываем.
check("живой матч сегодня — не закрывать",
      row("14:00","A B","1",["6","2","","",""]) +
      row("","C D","0",["4","1","","",""]),
      None, finished_day=False)

# Вчерашняя страница: живых матчей там нет, значит это снятие.
check("тот же счёт вчера — снятие",
      row("14:00","A B","1",["6","2","","",""]) +
      row("","C D","0",["4","1","","",""]),
      "6-4,2-1 ret.")

# 5. афиша без счёта вообще
check("матч ещё не начался",
      row("18:00","A B","",["","","","",""]) +
      row("","C D","",["","","","",""]),
      None)

# 6. снятие с пометкой
check("снятие (ret.)",
      row("12:00","A B","1",["6","2","","",""]) +
      row("","C D","0",["3","1","","",""], extra=" <span>ret.</span>"),
      "6-3,2-1 ret.")

# 7. неявка
check("неявка (w.o.)",
      row("12:00","A B","1",["","","","",""]) +
      row("","C D","0",["","","","",""], extra=" <span>w.o.</span>"),
      "w.o.")

# 8. фамилия Defosse не должна читаться как def.
check("фамилия с 'def' внутри",
      row("10:00","Defosse M.","2",["6","6","","",""]) +
      row("","Other P.","0",["4","3","","",""]),
      "6-4,6-3")

# 9. у одного игрока ячейка сета пустая — сдвига быть не должно
check("рваная строка: пустая ячейка в середине",
      row("10:00","A B","2",["6","","6","",""]) +
      row("","C D","0",["4","","3","",""]),
      "6-4,6-3")

# 10. матч 3-1 (bo5)
check("пять сетов, 3-1",
      row("10:00","A B","3",["6","4","6","7",""]) +
      row("","C D","1",["4","6","3","64",""]),
      "6-4,4-6,6-3,7-64")

print()
# --- случаи с колонкой посева, из-за которой счёт съезжал
def seeded(time, name, seed, result, scores, extra=""):
    tds = [f'<td class="first time">{time}</td>',
           f'<td class="t-name"><a>{name}</a>{extra}</td>',
           f'<td class="seed">{seed}</td>',
           f'<td class="result">{result}</td>']
    tds += [f'<td class="score">{x}</td>' for x in scores]
    tds += ['<td class="coursew">1.30</td>', '<td class="course">3.40</td>']
    return "<tr>" + "".join(tds) + "</tr>"

check("сеяный сверху, несеяный снизу",
      seeded("12:00","Cretu C.","(7)","0",["1","2","",""]) +
      seeded("","Jianu F.","","2",["6","6","",""]),
      "1-6,2-6")

check("оба сеяные",
      seeded("12:00","Auger Aliassime F.","(2)","0",["63","4","",""]) +
      seeded("","Musetti L.","(10)","2",["7","6","",""]),
      "63-7,4-6")

check("посев только снизу",
      seeded("12:00","Molleker R.","","2",["7","6","",""]) +
      seeded("","Kumstat J.","(4)","0",["64","4","",""]),
      "7-64,6-4")

# --- разъехавшиеся колонки должны отбрасываться, а не записываться
def bogus(name, result, scores):
    tds = [f'<td class="t-name"><a>{name}</a></td>',
           f'<td class="result">{result}</td>']
    tds += [f'<td class="score">{x}</td>' for x in scores]
    return "<tr>" + "".join(tds) + "</tr>"

check("невозможный счёт 7-4 — не записывать",
      bogus("A B","2",["7","7"]) + bogus("C D","0",["6","4"]),
      None)
check("невозможный счёт 1-2 — не записывать",
      bogus("A B","0",["1","2"]) + bogus("C D","2",["2","6"]),
      None)
check("решающий тайбрейк 10-6 — записать",
      bogus("A B","2",["7","3","10"]) + bogus("C D","1",["64","6","6"]),
      "7-64,3-6,10-6")
import sys, re
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from tennisratioall.results import pair_rows, parse_result_row, UNFINISHED_RE

def prow(name, result, scores, seed=""):
    tds = ['<td class="first time">16:15</td>',
           f'<td class="t-name"><a>{name}</a>{seed}</td>',
           f'<td class="result">{result}</td>']
    tds += [f'<td class="score">{x}</td>' for x in scores]
    tds += ['<td class="coursew">1.30</td>', '<td class="course">3.40</td>']
    return "<tr class='one bott'>" + "".join(tds) + "</tr>"

def header(title):
    return (f'<tr class="head flags"><td class="t-name" colspan="9">'
            f'<a href="/tournament/x/">{title}</a></td></tr>')

# Реальные матчи из скриншотов: Прага, челленджер
html = "<table class='result'>" + header("Prague, CZ") + \
    prow("Kumstat J.", "2", ["6","6","",""]) + prow("Jianu F.", "0", ["1","4","",""]) + \
    prow("Tseng C.",   "2", ["6","6","",""]) + prow("Gombos N.","0", ["4","2","",""]) + \
    "</table>"

rows = BeautifulSoup(html, "html.parser").find_all("tr")
print(f"строк в таблице: {len(rows)}")

print("\n--- СТАРЫЙ обход (парами подряд, шапку не отличал) ---")
idx = 0
while idx < len(rows) - 1:
    r1, r2 = rows[idx], rows[idx+1]
    n1 = r1.find("td", class_=re.compile(r"\bt-name\b"))
    n2 = r2.find("td", class_=re.compile(r"\bt-name\b"))
    if not (n1 and n2):
        idx += 1; continue
    sc = parse_result_row(r1, r2, False)
    print(f"  {n1.get_text(' ',strip=True):<12} — {n2.get_text(' ',strip=True):<12} {sc}")
    idx += 2

print("\n--- НОВЫЙ обход (шапка отброшена, пары по игрокам) ---")
for r1, r2 in pair_rows(rows):
    n1 = r1.find("td", class_=re.compile(r"\bt-name\b"))
    n2 = r2.find("td", class_=re.compile(r"\bt-name\b"))
    sc = parse_result_row(r1, r2, False)
    print(f"  {n1.get_text(' ',strip=True):<12} — {n2.get_text(' ',strip=True):<12} {sc}")

print("\nждём: Kumstat — Jianu 6-1,6-4  и  Tseng — Gombos 6-4,6-2")

# та же таблица, но со ссылками на страницу матча
def prow_id(name, result, scores, mid):
    tds = ['<td class="first time">16:15</td>',
           f'<td class="t-name"><a href="/match-detail/?id={mid}">{name}</a></td>',
           f'<td class="result">{result}</td>']
    tds += [f'<td class="score">{x}</td>' for x in scores]
    return "<tr>" + "".join(tds) + "</tr>"

html2 = "<table class='result'>" + header("Cincinnati") + \
    prow_id("Tiafoe F.", "2", ["7","7"], "111") + prow_id("Musetti L.","0",["62","5"], "111") + \
    prow_id("Daniel T.", "2", ["6","6"], "222") + prow_id("Fearnley J.","0",["4","1"], "222")
rows2 = BeautifulSoup(html2 + "</table>", "html.parser").find_all("tr")
print("\n--- группировка по ссылке на матч ---")
for r1, r2 in pair_rows(rows2):
    n1 = r1.find("td", class_=re.compile(r"\bt-name\b"))
    n2 = r2.find("td", class_=re.compile(r"\bt-name\b"))
    print(f"  {n1.get_text(' ',strip=True):<12} — {n2.get_text(' ',strip=True):<12} "
          f"{parse_result_row(r1, r2, False)}")
print("ждём: Tiafoe — Musetti 7-62,7-5  и  Daniel — Fearnley 6-4,6-1")
