# -*- coding: utf-8 -*-
"""Дата матча с афиши tennisratio: обе вёрстки и ловушка «August Holmgren».

24.08.2026 в телеграм и в веб-панель уехала дата «August Holmgren 15:00».
Причин было две, и обе воспроизводятся ниже:

1. Афиша вёрстается двумя способами. В компактном списке строка матча — это
   сама ссылка `a.compact-row`, поэтому `find_parent(['div', ...])` находил
   `div.matches-compact` — блок со ВСЕМИ матчами сразу. Время бралось от
   первого матча в блоке и раздавалось всем остальным.
2. Даты в этом блоке нет, и код уходил вверх по дереву искать заголовок со
   словом-месяцем. Первым подходящим оказывалось имя игрока August Holmgren:
   «august» — это месяц.

Плюс структурные данные страницы (ItemList → itemListElement) не разбирались
вовсе, так что точный startDate до бота не доходил.

    python3 test_match_dates.py
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("CHAT_ID", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bs4 import BeautifulSoup  # noqa: E402

import bot_merged  # noqa: E402

LD = {
    "@context": "https://schema.org",
    "@type": "ItemList",
    "itemListElement": [
        {"@type": "SportsEvent",
         "name": "Kei Nishikori vs Sebastian Ofner - Us Open Qualies (Round of 32)",
         "startDate": "2026-08-25T15:00:00Z"},
        # Порядок игроков в structured data обратный ссылке — так на сайте
        {"@type": "SportsEvent",
         "name": "Federico Coria vs Clement Chidekh - Us Open Qualies (Round of 32)",
         "startDate": "2026-08-25T18:30:00Z"},
        {"@type": "SportsEvent",
         "name": "August Holmgren vs Jack Pinnington Jones - Roehampton 2 Challenger (Quarterfinal)",
         "startDate": "2026-08-25T09:00:00Z"},
    ],
}

PAGE = """
<html><body>
<h2>Challengers Roehampton 2 Challenger</h2>
<div class="match-card" data-surface="Hard">
  <div class="match-details">
    <div class="match-time">
      <span class="match-date-display" data-utc="2026-08-25T09:00:00Z">&#128197; 25.08.</span>
      <span class="match-time-display" data-utc="2026-08-25T09:00:00Z">&#9200; 09:00</span>
      <span>Quarterfinal</span>
    </div>
    <div class="players">
      <span class="compact-name">August Holmgren</span>
      <div class="vs-divider">VS</div>
      <span class="compact-name">Jack Pinnington Jones</span>
    </div>
  </div>
  <a class="compare-btn"
     href="https://www.tennisratio.com/h2h-compare/august-holmgren-vs-jack-pinnington-jones.html">Match preview</a>
</div>

<h2>Grand Slams Us Open Qualies</h2>
<div class="matches-compact">
  <a class="compact-row" data-surface="Hard"
     href="https://www.tennisratio.com/h2h-compare/kei-nishikori-vs-sebastian-ofner.html">
    <span class="compact-time" data-utc="2026-08-25T15:00:00Z">15:00</span>
    <span class="compact-name">Kei Nishikori</span>
    <span class="compact-vs">VS</span>
    <span class="compact-name">Sebastian Ofner</span>
  </a>
  <a class="compact-row" data-surface="Hard"
     href="https://www.tennisratio.com/h2h-compare/clement-chidekh-vs-federico-coria.html">
    <span class="compact-time" data-utc="2026-08-25T18:30:00Z">18:30</span>
    <span class="compact-name">Clement Chidekh</span>
    <span class="compact-vs">VS</span>
    <span class="compact-name">Federico Coria</span>
  </a>
</div>
<script type="application/ld+json">__LD__</script>
</body></html>
""".replace("__LD__", json.dumps(LD))


class _Resp:
    status_code = 200
    text = PAGE


def _no_network(url, *a, **kw):
    if "tennisratio.com" in url:
        return _Resp()
    raise AssertionError(f"тест полез в сеть: {url}")


def main() -> int:
    soup = BeautifulSoup(PAGE, "html.parser")
    links = {a["href"].rsplit("/", 1)[-1].replace(".html", ""): a
             for a in soup.find_all("a") if "-vs-" in a.get("href", "")}
    fails = []

    def check(what, got, want):
        ok = got == want
        print(f"  {'OK  ' if ok else 'FAIL'} {what}: {got!r}")
        if not ok:
            fails.append(f"{what}: ожидалось {want!r}, получено {got!r}")

    print("вёрстка: крупная карточка (div.match-card)")
    card = links["august-holmgren-vs-jack-pinnington-jones"]
    check("дата", bot_merged.get_date_for_match(card), "25.08. 09:00")
    check("раунд", bot_merged.get_round_for_match(card), "Quarterfinal")

    print("вёрстка: компактный список (a.compact-row)")
    # Второй строке раньше доставалось время первой — 15:00 вместо 18:30
    check("дата 1-й строки",
          bot_merged.get_date_for_match(links["kei-nishikori-vs-sebastian-ofner"]),
          "25.08. 15:00")
    check("дата 2-й строки",
          bot_merged.get_date_for_match(links["clement-chidekh-vs-federico-coria"]),
          "25.08. 18:30")
    check("время 2-й строки",
          bot_merged.get_time_for_match(links["clement-chidekh-vs-federico-coria"]),
          "18:30")

    print("эвристика по заголовкам: имя игрока — не дата")
    check("«August Holmgren 15:00»",
          bot_merged._looks_like_date_text("August Holmgren 15:00"), False)
    check("«August Holmgren»",
          bot_merged._looks_like_date_text("August Holmgren"), False)
    check("«Monday, 25 August»",
          bot_merged._looks_like_date_text("Monday, 25 August"), True)

    print("structured data: ItemList → itemListElement")
    events = list(bot_merged._iter_ld_events(LD))
    check("найдено SportsEvent", len(events), 3)
    check("slug по имени события (порядок игроков обратный)",
          "clement-chidekh-vs-federico-coria"
          in bot_merged._slug_candidates("Federico Coria vs Clement Chidekh - Us Open Qualies (Round of 32)"),
          True)
    check("раунд из имени события",
          bot_merged._round_from_event_name(
              "Kei Nishikori vs Sebastian Ofner - Us Open Qualies (Round of 32)"),
          "Round of 32")

    print("parse_matches целиком")
    bot_merged.requests.get = _no_network
    got = bot_merged.parse_matches({}, {}, "atp")
    check("матчей на афише", len(got), 3)
    for slug, want in (("kei-nishikori-vs-sebastian-ofner", "25.08. 15:00"),
                       ("clement-chidekh-vs-federico-coria", "25.08. 18:30"),
                       ("august-holmgren-vs-jack-pinnington-jones", "25.08. 09:00")):
        check(f"date[{slug}]", got.get(slug, {}).get("date"), want)

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
