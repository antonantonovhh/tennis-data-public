#!/usr/bin/env python3
"""Сырая разметка строки TennisExplorer по фамилии — для отладки разбора.

Зачем
-----
`--diag-results --find` показывает только те матчи, которые разбор ПРИНЯЛ.
Матч, отброшенный как недоигранный, туда не попадёт — и выглядит это ровно
как «матча на сайте нет», хотя он там есть. А если счёт разобрался криво,
по итоговой строке не понять, в какой колонке произошёл сдвиг.

Этот скрипт печатает всё: каждую ячейку строки, её класс и текст, решение
разбора и причину. Ничего не меняет, только читает.

    python3 dump_te_row.py Kumstat
    python3 dump_te_row.py Musetti Jianu --days 3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="+", help="фамилии для поиска")
    ap.add_argument("--days", type=int, default=4)
    args = ap.parse_args()

    import requests
    from bs4 import BeautifulSoup

    from bot_merged import HEADERS, get_msk_time
    from tennisratioall.results import (UNFINISHED_RE, _is_player_row,
                                        _match_id, _row_numbers, pair_rows,
                                        parse_result_row)

    wanted = [n.lower() for n in args.names]
    now = get_msk_time()
    hits = 0

    for i in range(args.days):
        d = now - timedelta(days=i)
        url = (f"https://www.tennisexplorer.com/results/"
               f"?type=all&year={d.year}&month={d.month:02d}&day={d.day:02d}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
        except Exception as exc:  # noqa: BLE001
            print(f"{d.date()}: не скачалось — {exc}")
            continue
        if resp.status_code != 200:
            print(f"{d.date()}: HTTP {resp.status_code}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")

        for table in soup.find_all("table", class_=re.compile(r"\bresult\b")):
            rows = table.find_all("tr")
            # Сначала — как таблица разбилась на строки игроков и шапки.
            # Именно здесь жил сдвиг на строку, из-за которого проигравший
            # одного матча склеивался с победителем следующего.
            if any(w in tr.get_text(" ", strip=True).lower()
                   for tr in rows for w in wanted):
                print("-" * 70)
                print("разбивка таблицы:")
                for j, tr in enumerate(rows):
                    kind = "игрок " if _is_player_row(tr) else "ШАПКА "
                    cls = " ".join(tr.get("class", []) or []) or "—"
                    txt = tr.get_text(" ", strip=True)[:48]
                    print(f"  {j:>3} {kind} [tr class={cls:<12}] "
                          f"id матча={_match_id(tr)}  {txt}")

            for r1, r2 in pair_rows(rows):
                n1 = r1.find("td", class_=re.compile(r"\bt-name\b"))
                n2 = r2.find("td", class_=re.compile(r"\bt-name\b"))
                if not (n1 and n2):
                    continue
                name1 = n1.get_text(" ", strip=True)
                name2 = n2.get_text(" ", strip=True)
                if not any(w in name1.lower() or w in name2.lower()
                           for w in wanted):
                    continue

                hits += 1
                marker = bool(UNFINISHED_RE.search(r1.get_text(" ", strip=True))
                              or UNFINISHED_RE.search(r2.get_text(" ", strip=True)))
                print("=" * 70)
                print(f"{d.date()}   {name1}  —  {name2}")
                print(f"пометка недоигранного: {marker}")
                for tag, row in (("верх", r1), ("низ ", r2)):
                    print(f"  {tag}:")
                    for td in row.find_all("td"):
                        cls = " ".join(td.get("class", []) or []) or "—"
                        txt = td.get_text(strip=True).replace("\u00a0", "·")
                        raw = td.decode_contents().strip()[:60]
                        print(f"    [{cls:<14}] текст={txt!r:<10} html={raw!r}")
                    print(f"    -> числа по колонкам: {_row_numbers(row)}")
                got = parse_result_row(r1, r2, marker)
                print(f"  РАЗБОР: {got!r}"
                      + ("   (матч пропущен: не доигран или счёт не сошёлся)"
                         if got is None else ""))

    if not hits:
        print(f"\nНи одной строки с {', '.join(args.names)} "
              f"за последние {args.days} дн. на TennisExplorer нет.")
        print("Значит матч действительно не игрался — либо игрок писался "
              "там иначе.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
