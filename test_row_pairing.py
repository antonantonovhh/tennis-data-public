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
