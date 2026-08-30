#!/usr/bin/env python3
"""Веб-панель со статистикой: http://<ip сервера>:8800/?token=...

Только стандартная библиотека — ни Flask, ни nginx ставить не нужно.
Нагрузка здесь один-два запроса в минуту от одного человека, для этого
http.server более чем достаточен.

Про доступ
----------
Сервер смотрит в интернет, а на странице ваши прогнозы, кэфы и результаты.
Поэтому без токена страница не открывается. Токен берётся из DASH_TOKEN,
а если переменной нет — генерируется при запуске и печатается в лог один раз.

Это не настоящая защита от целенаправленной атаки (нет HTTPS, токен виден
в адресной строке и в логах прокси), но она закрывает главное: случайного
прохожего со сканером портов. Если нужна настоящая — ставьте панель на
127.0.0.1 и ходите через SSH-туннель:

    ssh -L 8800:127.0.0.1:8800 root@<ip>

тогда DASH_HOST=127.0.0.1 и снаружи порт вообще не виден.

    python3 dashboard.py                 # запуск
    DASH_PORT=9000 python3 dashboard.py  # другой порт
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import random
import secrets
import sys
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from webui import load_env as _load_env  # noqa: E402

_load_env(HERE)

from tennisratioall import reports  # noqa: E402
from tennisratioall.journal import (LOG_CSV, LOG_FIELDS, PICK_FIELDS,  # noqa: E402
                                    PICKS_CSV, VALUE_CSV, VALUE_FIELDS,
                                    _read, pf)
from tennisratioall.store import TOUR, Store  # noqa: E402

log = logging.getLogger("dashboard")

HOST = os.environ.get("DASH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8800"))
TOKEN = os.environ.get("DASH_TOKEN") or secrets.token_urlsafe(12)
REFRESH = int(os.environ.get("DASH_REFRESH", "120"))

# Вёрстка, стили и сервер — общие с панелью первого бота, см. webui.py
from webui import (cards, e, fmt_stamp, fmt_when,  # noqa: E402
                   line_chart, links as _links, load_env, money,
                   num, pct, serve, table)


# ------------------------------------------------------- калибровка (вкладка «Метод»)
#
# Логистическая перекалибровка p_cal = sigmoid(a + b * logit(p)). Коэффициенты
# подобраны 29.08.2026 методом Ньютона и с тех пор ЗАМОРОЖЕНЫ: в этом весь
# смысл проверки. Подбирать их на тех же данных, по которым потом судишь о
# прибыли, — то же самое, что угадывать вчерашнюю погоду. Правило объявлено
# заранее, а меряется на матчах, найденных ПОСЛЕ этой даты.
#
# b < 1 означает, что модель слишком уверенная и вероятности надо сжимать к
# середине: она говорит 65%, а выигрывает 53%. Отдельные наборы для разных
# популяций — ценные ставки отбираются по расхождению с рынком, и на них
# переоценка сильнее, чем на обычном прогнозе матча.
# У каждого тура СВОИ коэффициенты. Это не формальность: женская модель
# переоценивает себя заметно сильнее (b = 0.52 против 0.79 на прогнозе П1),
# и мужская калибровка сжимала бы её недостаточно. Смешивать туры нельзя по
# той же причине, по которой у них разведены журналы, — это разные популяции.
CAL_FROZEN = "2026-08-29"
CAL_BY_TOUR = {
    "atp": {
        "p1":    (+0.1700, 0.7939),   # журнал матчей, 210 исходов
        "value": (-0.4206, 0.6106),   # ценные ставки, 493 исхода
        "pick":  (+0.2763, 0.4389),   # ставки на исход, 179 исходов
    },
    "wta": {
        "p1":    (+0.0637, 0.5210),   # журнал матчей, 151 исход
        "value": (-0.0153, 0.7045),   # ценные ставки, 317 исходов
        "pick":  (+0.1490, 0.3654),   # ставки на исход, 150 исходов
    },
}
CAL = CAL_BY_TOUR.get(TOUR, CAL_BY_TOUR["atp"])
# Порог нового правила: берём ставку, только если перевес остаётся
# положительным ПОСЛЕ сжатия вероятности. Ноль, а не подобранное число —
# любой другой порог пришлось бы подгонять по этим же данным.
RULE_MIN_EDGE = 0.0


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _cal(p, kind: str):
    """Калиброванная вероятность. None, если считать не из чего."""
    if p is None:
        return None
    a, b = CAL[kind]
    return _sig(a + b * _logit(p))


def _brier(pairs, idx: int) -> float:
    return sum((x[idx] - x[-1]) ** 2 for x in pairs) / len(pairs)


def _paired_ci(diffs, iters: int = 2000, seed: int = 7):
    """95% интервал для средней разницы. Зерно фиксировано: одни и те же
    данные обязаны давать один и тот же ответ, иначе числами нельзя
    пользоваться (та же логика, что в check_edge.py)."""
    rnd = random.Random(seed)
    n = len(diffs)
    if n < 2:
        return None, None
    got = []
    for _ in range(iters):
        got.append(sum(diffs[rnd.randrange(n)] for _ in range(n)) / n)
    got.sort()
    return got[int(iters * 0.025)], got[int(iters * 0.975)]


def _roi(rows):
    """(ставок, ROI в процентах, прибыль) по journal-строкам."""
    stake = sum(num(r.get("stake"), 0) or 0 for r in rows)
    prof = sum(num(r.get("profit"), 0) or 0 for r in rows)
    return len(rows), (100 * prof / stake if stake else 0.0), prof


def _settled(rows):
    return [r for r in rows if r.get("status") in ("win", "loss", "refund", "push")]


def _rule_takes(row, kind: str) -> bool:
    """Прошла бы ставка новый отбор: перевес после сжатия вероятности."""
    p = _cal(pf(row.get("sim_prob")), kind)
    odds = pf(row.get("odds"))
    if p is None or not odds:
        return False
    return (p * odds - 1) >= RULE_MIN_EDGE


# ------------------------------------------------------------------ страницы
def _by_day(rows, start) -> dict:
    """Прибыль по дням закрытия: {'2026-08-25': 1234.0}.

    День берём по `resolved_at`, а не по дате находки, — так же, как считает
    `reports.collect`: ставка, найденная вчера и сыгранная сегодня, относится
    к сегодняшнему результату. Иначе кривая разошлась бы с числами наверху
    страницы.
    """
    out = {}
    for r in rows:
        if (r.get("status") or "pending") in ("pending", ""):
            continue
        d = reports._as_date(r.get("resolved_at") or "")
        if not d or (start and d < start):
            continue
        key = d.strftime("%Y-%m-%d")
        out[key] = out.get(key, 0.0) + pf(r.get("profit"), 0.0)
    return out


def bankroll_chart(period: str) -> str:
    """Накопленная прибыль по дням: ценные ставки и исходы двумя линиями.

    Складывать их в одну кривую нельзя: это две разные популяции, у них
    свои ROI и своя калибровка — ровно поэтому и журналы разведены. Общего
    итога тут нет намеренно, каждая линия читается сама по себе.
    """
    start = reports._period_start(period)
    value = _by_day(_read(VALUE_CSV, VALUE_FIELDS), start)
    picks = _by_day(_read(PICKS_CSV, PICK_FIELDS), start)
    days = sorted(set(value) | set(picks))
    if len(days) < 2:
        return ""          # за один день кривой не бывает

    def cum(per_day):
        run, out = 0.0, []
        for d in days:
            run += per_day.get(d, 0.0)
            out.append(run)
        return out

    series = []
    if value:
        series.append(("Ценные ставки", "var(--ok)", cum(value)))
    if picks:
        series.append(("Исходы", "var(--acc)", cum(picks)))
    if not series:
        return ""

    labels = [f"{d[8:10]}.{d[5:7]}" for d in days]
    hint = ("Две отдельные кривые, не сумма: ценные ставки и исходы — разные "
            "популяции со своими ROI.")
    tail = " ".join(f"{n}: {v[-1]:+,.0f}".replace(",", " ")
                    for n, _, v in series)
    return (f'<p class="note">Накопленным итогом — {e(tail)}. Смотрите не на '
            'отдельные дни, а на то, ровно ли идёт кривая: прибыль из одного '
            'удачного дня и прибыль из тридцати — это разные вещи.</p>'
            + line_chart(series, labels, hint=hint))


def picks_chart(period: str) -> str:
    """Накопленная прибыль по дням в трёх разрезах «Главного разреза».

    В отличие от кривых на «Сводке», эти три СРАВНИВАЮТ между собой, а не
    читают по отдельности: популяция одна, банк на ставку одинаковый, и
    вопрос страницы — обгоняет ли модель контрольную группу. В таблице
    выше видно только последнюю точку; кривая отвечает на то, чего в ней
    нет, — набирался отрыв ровно или весь пришёл из одного дня.

    Считаем ровно как `reports.collect_picks`, иначе последняя точка
    разойдётся с колонкой «Прибыль» прямо над графиком: возвраты
    пропускаем целиком, а «просто фаворит рынка» — синтетика, своего
    `profit` в журнале у него нет и быть не может.
    """
    start = reports._period_start(period)
    agree, against, base = {}, {}, {}
    for r in _read(PICKS_CSV, PICK_FIELDS):
        status = r.get("status") or "pending"
        # Возврат не выигран и не проигран: он не входит ни в оборот, ни в
        # прибыль ни одной из трёх кривых.
        if status in ("pending", "", "refund", "push"):
            continue
        d = reports._as_date(r.get("resolved_at") or "")
        if not d or (start and d < start):
            continue
        key = d.strftime("%Y-%m-%d")
        won = status == "win"
        agrees = r.get("agree") == "да"
        box = agree if agrees else against
        box[key] = box.get(key, 0.0) + pf(r.get("profit"), 0.0)

        # Что было бы, если ставить просто на фаворита рынка: когда модель
        # согласна — та же ставка, когда спорит — противоположная.
        stake = pf(r.get("stake"), 0.0)
        odds = pf(r.get("odds"), 0.0)
        b_odds, b_won = ((odds, won) if agrees
                         else (reports._other_side_odds(r, odds), not won))
        if b_odds > 1:
            base[key] = base.get(key, 0.0) + (stake * (b_odds - 1) if b_won
                                              else -stake)

    days = sorted(set(agree) | set(against) | set(base))
    if len(days) < 2:
        return ""          # за один день кривой не бывает

    def cum(per_day):
        run, out = 0.0, []
        for d in days:
            run += per_day.get(d, 0.0)
            out.append(run)
        return out

    # Серая для контрольной группы намеренно: это линейка, а не результат,
    # и цветом она соревноваться с моделью не должна.
    series = [(name, colour, cum(per_day)) for name, colour, per_day in (
        ("Спорит с рынком", "var(--ok)", against),
        ("Согласна с рынком", "var(--acc)", agree),
        ("Просто фаворит рынка", "var(--dim)", base)) if per_day]
    if not series:
        return ""

    labels = [f"{d[8:10]}.{d[5:7]}" for d in days]
    hint = ("Одна популяция в трёх разрезах: эти кривые сравнивают друг с "
            "другом, а не читают по отдельности.")
    return ('<h2>Как набиралась разница</h2>'
            '<p class="note">Смотрите не на сами кривые, а на просвет между '
            'зелёной и серой — это и есть вклад модели. Идут вровень — модель '
            'не добавляет ничего, сколько бы она при этом ни зарабатывала. '
            'Отрыв, набранный за один день, отрывом не считается.</p>'
            + line_chart(series, labels, hint=hint))


def view_home(q) -> str:
    period = (q.get("period") or ["all"])[0]
    if period not in reports.PERIODS:
        period = "all"
    data = reports.collect(period)
    acc = reports.model_accuracy(period)
    t = data["total"]
    roi = reports._roi(t)

    links = " · ".join(
        f'<a href="/?period={k}&token={urllib.parse.quote(TOKEN)}" '
        f'style="color:{"#60a5fa" if k == period else "#8c9aa8"};'
        f'text-decoration:none">{v}</a>'
        for k, v in reports.PERIODS.items())

    body = f'<p class="note">Период: {links}</p>'
    body += cards([
        ("Ставок", t["n"]),
        ("Зашло", f'{t["win"]}<span class="dim" style="font-size:14px"> / '
                  f'{t["settled"]} '
                  f'({t["win"] / t["settled"] * 100:.0f}%)</span>'
                  if t["settled"] else "—"),
        ("Прибыль", money(t["profit"])),
        ("ROI", f'<span class="{"ok" if roi > 0 else "bad"}">{roi:+.1f}%</span>'
                if t["settled"] else "—"),
        ("Возвратов", t["push"]),
        ("В ожидании", data["pending"]),
    ])

    body += bankroll_chart(period)

    rows = []
    for market, box in sorted(data["by_market"].items(),
                              key=lambda kv: -kv[1]["profit"]):
        rows.append([e(market), f'<span class=num>{box["settled"]}</span>',
                     f'<span class=num>{box["win"]}</span>',
                     f'<span class=num>{box["loss"]}</span>',
                     f'<span class=num>{money(box["profit"])}</span>',
                     f'<span class=num>{reports._roi(box):+.1f}%</span>'])
    body += "<h2>По рынкам</h2>"
    body += table(["Рынок", "Ставок", "Выигр.", "Проигр.", "Прибыль", "ROI"], rows)

    if acc["n"]:
        hit = acc["hit"] / acc["n"] * 100
        both = acc["both"]
        better = acc["brier_both"] < acc["mkt_brier"] if both else None
        body += "<h2>Точность модели</h2>"
        body += ('<p class="note">Брайер — средний квадрат ошибки вероятности, '
                 'меньше лучше; 0.25 это уровень «всегда 50/50». На первых '
                 'сотнях ставок он говорит о модели больше, чем прибыль: '
                 'одна выигравшая фора перекрывает пять проигрышей, а Брайер '
                 'так не шумит.</p>')
        body += cards([
            ("Матчей с прогнозом", acc["n"]),
            ("Угадан победитель", pct(hit)),
        ])
        # Сравнение только на общей выборке: модель считается по всей афише,
        # а рынок — по матчам, где линия открылась. Смешивать их нельзя.
        body += ('<p class="note">Сравнение с рынком — только на матчах, где '
                 'была и цена: остальные рынок не оценивал, и брать их '
                 'в его пользу или против нечестно.</p>')
        if both:
            body += cards([
                ("Общих матчей", both),
                ("Модель угадала", pct(acc["hit_both"] / both * 100)),
                ("Рынок угадал", pct(acc["mkt_hit"] / both * 100)),
                ("Брайер модели", f"{acc['brier_both']:.3f}"),
                ("Брайер рынка", f"{acc['mkt_brier']:.3f}"),
                ("Кто точнее",
                 '<span class="ok">модель</span>' if better
                 else '<span class="bad">рынок</span>'),
            ])
            if both < 30:
                body += ('<p class="note">Матчей с линией пока '
                         f'{both} — на такой выборке «кто точнее» '
                         'меняется от одного результата.</p>')
        else:
            body += ('<p class="note">Ни одного закрытого матча с ценой '
                     'рынка — сравнивать не с чем.</p>')
        avg_edge = t["edge"] / t["settled"] * 100 if t["settled"] else 0
        body += (f'<p class="note">Средний заявленный перевес '
                 f'<b>{avg_edge:+.1f}%</b> против фактического ROI '
                 f'<b>{roi:+.1f}%</b>. Расхождение между ними и есть мера '
                 f'того, насколько модель себе льстит.</p>')
    return body


def _match_times() -> dict:
    """slug -> время начала матча из журнала.

    Нужно для строк, записанных до появления колонки when: у них время
    матча есть только в matches_log.csv. Карта строится на запрос —
    файл маленький, а вечно тащить миграцию ради этого не хочется.
    """
    return {r["slug"]: (r.get("when") or "")
            for r in _read(LOG_CSV, LOG_FIELDS) if r.get("slug")}


def _waiting(when: str, times: dict, slug: str) -> tuple:
    """(текст времени матча, метка ожидания) для незакрытой ставки.

    Ставка, у которой матч был вчера, а результата нет, — это не «ждём»,
    а «потерялось». Раньше обе выглядели одинаково, и зависшие строки
    приходилось искать глазами по дате находки.
    """
    from tennisratioall.results import parse_when  # noqa: PLC0415

    raw = when or times.get(slug or "", "")
    started = parse_when(raw)
    if not started:
        return raw, ""
    hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
    if hours < 3:
        return raw, ""
    if hours < 24:
        return raw, f'<span class="warn">{hours:.0f} ч</span>'
    return raw, f'<span class="bad">{hours / 24:.0f} дн</span>'


def view_bets(q) -> str:
    rows_raw = _read(VALUE_CSV, VALUE_FIELDS)
    status = (q.get("status") or ["all"])[0]
    if status != "all":
        rows_raw = [r for r in rows_raw if (r.get("status") or "pending") == status]
    rows_raw = _by_match_time(rows_raw)[:300]

    times = _match_times()
    rows = []
    for r in rows_raw:
        st = r.get("status") or "pending"
        icon = {"win": '<span class="ok">выигрыш</span>',
                "loss": '<span class="bad">проигрыш</span>',
                "push": '<span class="dim">возврат</span>',
                "refund": '<span class="dim">возврат</span>'}.get(
                    st, '<span class="warn">ждём</span>')
        when, late = "", ""
        if st in ("pending", ""):
            when, late = _waiting(r.get("when"), times, r.get("slug"))
            if late:
                icon = f'<span class="warn">ждём</span> {late}'
        else:
            when = r.get("when") or times.get(r.get("slug") or "", "")
        line = r.get("line") or ""
        edge = pf(r.get("edge"), 0) * 100
        rows.append([
            e(fmt_stamp(r.get("found_at"))),
            e(fmt_when(when)),
            e(f"{r.get('p1')} — {r.get('p2')}"),
            e(f"{r.get('market')} {r.get('pick')} {line}".strip()),
            f'<span class=num>{e(r.get("odds"))}</span>',
            f'<span class=num>{pf(r.get("sim_prob"), 0) * 100:.0f}%</span>',
            f'<span class="num {"ok" if edge > 0 else "dim"}">{edge:+.1f}%</span>',
            icon,
            f'<span class=num>{money(r.get("profit"))}</span>',
            e(r.get("score") or ""),
        ])
    filt = " · ".join(
        f'<a href="/bets?status={k}&token={urllib.parse.quote(TOKEN)}" '
        f'style="color:{"#60a5fa" if k == status else "#8c9aa8"};'
        f'text-decoration:none">{v}</a>'
        for k, v in [("all", "все"), ("pending", "в ожидании"),
                     ("win", "зашли"), ("loss", "не зашли")])
    return (f'<p class="note">Фильтр: {filt} · показаны последние 300. '
            'Рядом со «ждём» стоит, сколько прошло с начала матча: если там '
            'сутки и больше, результат не нашёлся и его стоит поискать '
            'руками — <code>--diag-results --find ФАМИЛИЯ</code>.</p>'
            + table(["Найдена", "Начало матча", "Матч", "Ставка", "Кэф",
                     "Модель", "Перевес", "Статус", "Прибыль", "Счёт"], rows))


def _calibration(rows) -> str:
    """Блок калибровки: совпадает ли заявленная вероятность с частотой.

    Строится по ВСЕЙ истории, а не по показанным 300 строкам: чем больше
    матчей, тем меньше шума в корзинах.
    """
    pairs = []                       # (прогноз на p1, победил ли p1)
    for r in rows:
        sim, winner = pf(r.get("sim_p1")), r.get("winner")
        if sim is None or winner not in ("p1", "p2"):
            continue
        pairs.append((sim, 1.0 if winner == "p1" else 0.0))
    if not pairs:
        return ('<h2>Калибровка</h2><p class="dim">Пока нет сыгранных матчей '
                'с прогнозом — считать не на чем.</p>')

    brier = sum((p - o) ** 2 for p, o in pairs) / len(pairs)
    acc = sum(1 for p, o in pairs if (p >= 0.5) == (o == 1.0)) / len(pairs)

    body = ['<h2>Калибровка</h2>',
            '<p class="note">Главная проверка модели. Прогнозы разложены по '
            'корзинам, и в каждой сравнивается заявленная вероятность с тем, '
            'что вышло на самом деле. Модель откалибрована, если из матчей с '
            'прогнозом 60% первый игрок выиграл примерно 60 раз из ста. '
            'Отклонение считается в процентных пунктах: плюс — модель '
            'недооценивала первого игрока, минус — переоценивала.</p>']

    body.append(cards([
        ("Матчей с результатом", len(pairs)),
        ("Доля попаданий", f"{acc * 100:.0f}%"),
        ("Ошибка Брайера", f"{brier:.3f}"),
    ]))
    body.append('<p class="note">Ошибка Брайера — средний квадрат промаха '
                'вероятности, от 0 (идеально) до 1. Ориентир: 0.25 — это '
                'уровень «всегда говорить 50 на 50». Всё, что заметно ниже '
                '0.25, значит модель несёт информацию; выше — она хуже '
                'подбрасывания монеты. В отличие от доли попаданий эта мера '
                'наказывает за уверенные ошибки.</p>')

    buckets = []
    for lo in range(0, 100, 10):
        hi = lo + 10
        got = [(p, o) for p, o in pairs
               if lo / 100 <= p < hi / 100 or (hi == 100 and p == 1.0)]
        if not got:
            continue
        n = len(got)
        said = sum(p for p, _ in got) / n * 100
        real = sum(o for _, o in got) / n * 100
        dev = real - said
        if n < 5:
            cls, note = "dim", "мало данных"
        elif abs(dev) <= 5:
            cls, note = "ok", "в норме"
        elif abs(dev) <= 12:
            cls, note = "warn", "заметный сдвиг"
        else:
            cls, note = "bad", "сильный сдвиг"
        buckets.append([
            f"{lo}–{hi}%",
            f'<span class=num>{n}</span>',
            f'<span class=num>{said:.0f}%</span>',
            f'<span class=num>{real:.0f}%</span>',
            f'<span class="num {cls}">{dev:+.0f} п.п.</span>',
            f'<span class="{cls}">{note}</span>',
        ])
    body.append(table(["Прогноз модели", "Матчей", "Обещано", "Вышло",
                       "Отклонение", ""], buckets))
    body.append('<p class="note">Корзины, где матчей меньше пяти, помечены '
                'серым: там отклонение почти наверняка случайность, а не '
                'свойство модели. Осмысленные выводы начинаются с полусотни '
                'матчей в корзине.</p>')

    # --- разрез по «доверию»: работает ли сама эта метка -----------------
    tiers = [("ок", 0.0, 0.18), ("слабое", 0.18, 0.30), ("нет доверия", 0.30, 9.0)]
    trows = []
    for label, lo, hi in tiers:
        got = []
        for r in rows:
            sim, winner, gap = (pf(r.get("sim_p1")), r.get("winner"),
                                pf(r.get("model_gap")))
            if sim is None or gap is None or winner not in ("p1", "p2"):
                continue
            if lo <= gap < hi:
                got.append((sim, 1.0 if winner == "p1" else 0.0))
        if not got:
            continue
        n = len(got)
        a = sum(1 for p, o in got if (p >= 0.5) == (o == 1.0)) / n * 100
        b = sum((p - o) ** 2 for p, o in got) / n
        cls = {"ок": "ok", "слабое": "warn", "нет доверия": "bad"}[label]
        trows.append([f'<span class="{cls}">{label}</span>',
                      f'<span class=num>{n}</span>',
                      f'<span class=num>{a:.0f}%</span>',
                      f'<span class=num>{b:.3f}</span>'])
    if trows:
        body.append('<h2>Проверка метки «Доверие»</h2>')
        body.append('<p class="note">Метка «Доверие» — это НЕ уверенность в '
                    'исходе. Прогноз считается двумя независимыми способами: '
                    'по статистике подачи и приёма и по Elo. Итоговая цифра — '
                    'середина между ними, а метка показывает, насколько сильно '
                    'они разошлись (до 18% — «ок», 18–30% — «слабое», от 30% — '
                    '«нет доверия»). Смысл в том, что середина между «76%» и '
                    '«31%» — это не «умеренный фаворит», а «неизвестно». '
                    'Таблица ниже проверяет, стоит ли за меткой что-то '
                    'реальное: у строки «ок» доля попаданий должна быть выше, '
                    'а ошибка Брайера ниже, чем у «нет доверия». Если разницы '
                    'нет — метка бесполезна и пороги стоит перебрать.</p>')
        body.append(table(["Доверие", "Матчей", "Доля попаданий",
                           "Ошибка Брайера"], trows))
    return "".join(body)


def _model_vs_market(rows) -> str:
    """Кто точнее угадывает победителя — модель или линия букмекера.

    Это прямой ответ на вопрос «насколько точно модель ставит на П1/П2».
    Голая точность модели ничего не значит, пока не с чем сравнить: в
    теннисе фаворит букмекера выигрывает примерно семь раз из десяти, и
    столько же даст «модель», которая просто копирует линию.

    Главное — нижняя строка, матчи с расхождением. Там модель и рынок
    называют РАЗНЫХ победителей, поэтому кто-то один обязательно неправ.
    Только на этой выборке видно, знает ли модель что-то сверх цены.
    """
    both, dis = [], []
    for r in rows:
        sim, mkt, w = (pf(r.get("sim_p1")), pf(r.get("mkt_implied_p1")),
                       r.get("winner"))
        if sim is None or mkt is None or w not in ("p1", "p2"):
            continue
        m_p1, k_p1, won_p1 = sim >= 0.5, mkt >= 0.5, w == "p1"
        both.append((m_p1 == won_p1, k_p1 == won_p1))
        if m_p1 != k_p1:
            dis.append((m_p1 == won_p1, k_p1 == won_p1))
    if not both:
        return ('<h2>Модель против рынка</h2><p class="dim">Нужны сыгранные '
                'матчи, где есть и прогноз модели, и линия. Пока таких нет.</p>')

    def line(name, data, hint):
        n = len(data)
        if not n:
            return [f'{e(name)}<br><span class="dim">{e(hint)}</span>',
                    '<span class="dim">нет данных</span>', "", ""]
        mo = sum(1 for a, _ in data if a) / n * 100
        mk = sum(1 for _, b in data if b) / n * 100
        d = mo - mk
        cls = "dim" if abs(d) < 0.05 else ("ok" if d > 0 else "bad")
        return [f'{e(name)}<br><span class="dim">{e(hint)}</span>',
                f'<span class=num>{n}</span>',
                f'<span class=num>{mo:.0f}%</span>',
                f'<span class=num>{mk:.0f}%</span>',
                f'<span class="num {cls}">{d:+.0f} п.п.</span>']

    rws = [line("Все матчи с линией", both,
                "здесь модель и рынок чаще всего называют одного и того же"),
           line("Только расхождения", dis,
                "модель и рынок назвали РАЗНЫХ победителей — кто-то точно неправ")]

    out = ['<h2>Модель против рынка</h2>',
           '<p class="note">Прямой ответ на вопрос «насколько точно модель '
           'угадывает победителя». Точность сама по себе ничего не говорит, '
           'пока её не с чем сравнить: линия букмекера — это тоже прогноз, '
           'причём очень хороший, и в теннисе фаворит выигрывает примерно '
           'семь раз из десяти. Модель, которая просто копирует линию, '
           'покажет те же 70% и будет совершенно бесполезна.</p>',
           table(["", "Матчей", "Угадала модель", "Угадал рынок", "Разница"],
                 rws)]
    if dis:
        n = len(dis)
        mo = sum(1 for a, _ in dis if a) / n * 100
        if n < 30:
            verdict = (f'<span class="warn">рано судить</span>: расхождений '
                       f'всего {n}, нужно хотя бы тридцать')
        elif mo > 55:
            verdict = ('<span class="ok">модель видит то, чего не видит '
                       'рынок</span>')
        elif mo < 45:
            verdict = ('<span class="bad">когда модель спорит с рынком, она '
                       'чаще неправа — спорить ей не стоит</span>')
        else:
            verdict = ('<span class="warn">в спорах модель и рынок примерно '
                       'равны, своего знания у неё не видно</span>')
        out.append(f'<p class="note">Вывод по расхождениям: {verdict}.</p>')
    out.append('<p class="note">И отдельно про деньги: высокая точность сама '
               'по себе прибыли не даёт. Фаворит по коэффициенту 1.40 заходит '
               'те самые 70% раз, но 0.70 × 1.40 = 0.98 — то есть стабильный '
               'минус 2% с оборота. Чтобы выигрывать, надо угадывать не '
               '«часто», а <b>чаще, чем заложено в цене</b>. Именно поэтому '
               'на вкладке «Исходы» отдельно считаются ставки против рынка: '
               'только они показывают, есть ли у модели собственное знание.</p>')
    return "".join(out)


def _by_match_time(rows, field: str = "when"):
    """Сортировка по времени матча, по убыванию: самые поздние сверху.

    Раньше строки шли в порядке записи в журнал, и даты скакали: 23.08,
    потом 22.08, потом пачка 24.08. Порядок записи — это когда матч попал
    в обход, а смотрят на список по времени начала.

    Строки без разбираемого времени падают в самый низ, чтобы не мешаться:
    иначе они всплывали бы наверх как «нулевая дата».

    Одна функция на все страницы — поле с датой у них разное, отсюда
    параметр field.
    """
    from tennisratioall.results import parse_when  # noqa: PLC0415

    def key(r):
        t = parse_when(r.get(field) or "")
        # (есть_время, отметка) — сортируем по убыванию, поэтому строки без
        # времени помечаем нулём и они оказываются последними
        return (1, t.timestamp()) if t else (0, 0.0)

    return sorted(rows, key=key, reverse=True)


def view_matches(q) -> str:
    allrows = _read(LOG_CSV, LOG_FIELDS)
    raw = _by_match_time(allrows)[:300]
    rows = []
    for r in raw:
        sim = pf(r.get("sim_p1"))
        mkt = pf(r.get("mkt_implied_p1"))
        gap = pf(r.get("model_gap"))
        if gap is None:
            # Пустая ячейка читалась как «всё в порядке», хотя это худший
            # случай: Elo хотя бы у одного игрока нет, второго мнения не
            # существует, и перевес на таких матчах систематически завышен.
            conf = '<span class="bad">без Elo</span>'
        elif gap >= 0.30:
            conf = '<span class="bad">нет доверия</span>'
        elif gap >= 0.18:
            conf = '<span class="warn">слабое</span>'
        else:
            conf = '<span class="ok">ок</span>'
        winner = r.get("winner")
        res = ""
        if winner in ("p1", "p2") and sim is not None:
            said_p1 = sim >= 0.5
            ok = (winner == "p1") == said_p1
            res = ('<span class="ok">угадала</span>' if ok
                   else '<span class="bad">мимо</span>')
        rows.append([
            e(fmt_when(r.get("when")) or fmt_stamp(r.get("logged_at"))),
            e(f"{r.get('p1')} — {r.get('p2')}"),
            e(r.get("surface") or "—"),
            f'<span class=num>{sim * 100:.0f}%</span>' if sim is not None else "—",
            f'<span class=num>{mkt * 100:.0f}%</span>' if mkt is not None else "—",
            conf,
            e(r.get("score") or ""),
            e(f'{r.get("games_p1")}-{r.get("games_p2")}'
              if r.get("games_p1") not in ("", None) else ""),
            res,
        ])
    return (
        '<p class="note">Журнал всех посчитанных матчей, включая те, где '
        'ценной ставки не нашлось. Смысл именно в этом: если судить о модели '
        'только по сделанным ставкам, картина смещена — видны лишь случаи, '
        'где модель сама себе понравилась. Здесь видны все прогнозы, в том '
        'числе неудобные, и на них проверяется калибровка.</p>'
        + _model_vs_market(allrows)
        + _calibration(allrows)
        + '<h2>Матчи</h2>'
        + '<p class="note">'
          '<b>Модель</b> — вероятность победы первого игрока по симуляции '
          'Монте-Карло. <b>Рынок</b> — та же вероятность по линии Pinnacle, '
          'очищенная от маржи букмекера; прочерк значит, что линии не было '
          '(не открылась или уже закрылась). Разница между этими двумя '
          'числами и есть перевес, из которого рождаются ставки на вкладке '
          '«Ставки».<br>'
          '<b>Доверие</b> — насколько согласны две внутренние модели, а не '
          'уверенность в исходе; подробности выше.<br>'
          '<b>Прогноз</b> — «угадала», если Модель ≥ 50% совпала с реальным '
          'победителем. Мера грубая: уверенность не учитывается, прогнозы 51% '
          'и 87% засчитываются одинаково. Для оценки качества смотрите '
          'калибровку и ошибку Брайера выше, а не эту колонку.<br>'
          'Показаны последние 300 матчей, новые сверху.</p>'
        + table(["Когда", "Матч", "Корт", "Модель", "Рынок", "Доверие",
                 "Счёт", "Геймы", "Прогноз"], rows))


def view_queue(q) -> str:
    st = Store()
    c = st.counts()
    body = cards([("Готово", c.get("done", 0)),
                  ("Ждут кэфы", c.get("awaiting_odds", 0)),
                  ("В очереди", c.get("pending", 0)),
                  ("Неудач", c.get("failed", 0))])
    try:
        from tennis_parser import pinnacle_guard as pg
        s = pg.status()
        body += "<h2>Pinnacle</h2>"
        body += cards([
            ("Отступ", f'<span class="bad">{s["cooldown_left"] // 60} мин</span>'
                       if s["cooldown_left"] else '<span class="ok">нет</span>'),
            ("Блокировок подряд", s["block_streak"]),
            ("Матчей в кэше", s["matchups"]),
            ("Возраст кэша", f'{s["cache_age"]} с'
                             if s["cache_age"] is not None else "—"),
        ])
    except Exception:  # noqa: BLE001
        pass

    bad = [(k, v) for k, v in st.entries.items() if v.status == "failed"]
    if bad:
        body += "<h2>Не посчитались</h2>"
        body += table(["Матч", "Попыток", "Ошибка"],
                      [[e(k), f'<span class=num>{v.attempts}</span>',
                        f'<span class="dim">{e(v.error[:90])}</span>']
                       for k, v in bad[:50]])
    return body


def view_picks(q):
    """Ставки на исход — отдельно от value."""
    period = (q.get("period") or ["all"])[0]
    d = reports.collect_picks(period)
    a, ag, ag2 = d["boxes"]["all"], d["boxes"]["agree"], d["boxes"]["against"]
    base = d["baseline"]

    body = ('<p class="note">Здесь измеряется <b>сама модель</b>, а не умение '
            'выбирать ставки. Ставка на фаворита модели делается по '
            '<b>каждому</b> матчу, независимо от того, есть перевес над линией '
            'или нет. Тем эта страница и отличается от вкладки «Ставки»: там '
            'только value — случаи, где модель нашла перевес, их мало и почти '
            'все в форах и тоталах. Здесь модель не может отсидеться, выбирая '
            'удобные матчи, поэтому цифры честнее.</p>')
    body += cards([
        # «Ставок» тут раньше значило «рассчитанных», и рядом со списком из
        # семи строк это читалось как ошибка. Пишем явно, сколько закрыто и
        # сколько всего.
        ("Рассчитано", a["n"]),
        ("Зашло", f'{a["win"]} ({a["win"] / a["settled"] * 100:.0f}%)'
                  if a["settled"] else "—"),
        ("Прибыль", money(a["profit"])),
        ("ROI", f'<span class="{"ok" if reports._roi(a) > 0 else "bad"}">'
                f'{reports._roi(a):+.1f}%</span>' if a["settled"] else "—"),
        ("Возвратов", a["void"]),
        ("Ждут результата", d["pending"]),
    ])
    body += ('<p class="note">Счётчики считают только закрытые ставки — '
             f'в таблице внизу строк больше на те {d["pending"]}, что ещё '
             'ждут результата. Банк на ставку одинаковый, поэтому ROI — это '
             'просто прибыль, делённая на оборот.</p>')

    rows = []
    for box, name, hint in (
            (ag, "Согласна с рынком",
             "модель выбрала того же фаворита, что и букмекер"),
            (ag2, "Спорит с рынком",
             "модель выбрала НЕ фаворита рынка — ради этой строки всё и затеяно"),
            (base, "Просто фаворит рынка",
             "контрольная группа: ставим на фаворита букмекера вообще без модели")):
        # Пустую строку не прячем: «спорит с рынком» — главный ответ страницы,
        # и её отсутствие читается как «блок сломался», а не как «данных нет».
        if not box["settled"]:
            rows.append([f'{e(name)}<br><span class="dim">{e(hint)}</span>',
                         '<span class="dim">нет закрытых</span>',
                         "", "", "", ""])
            continue
        rows.append([f'{e(name)}<br><span class="dim">{e(hint)}</span>',
                     f'<span class=num>{box["settled"]}</span>',
                     f'<span class=num>{box["win"]}</span>',
                     f'<span class=num>'
                     f'{box["win"] / box["settled"] * 100:.0f}%</span>',
                     f'<span class=num>{money(box["profit"])}</span>',
                     f'<span class="num {"ok" if reports._roi(box) > 0 else "bad"}">'
                     f'{reports._roi(box):+.1f}%</span>'])
    body += "<h2>Главный разрез</h2>"
    body += ('<p class="note">Единственный вопрос, на который отвечает эта '
             'страница: <b>добавляет ли модель хоть что-то поверх линии '
             'букмекера</b>. Если она зарабатывает только там, где согласна с '
             'рынком, — она не добавляет ничего: тот же результат даёт нижняя '
             'строка, ставка на фаворита без всякой модели. Ценность '
             'начинается там, где модель спорит и оказывается права.</p>')
    body += table(["", "Ставок", "Зашло", "%", "Прибыль", "ROI"], rows)
    body += picks_chart(period)

    # --- прямой ответ: модель против контрольной группы -------------------
    if a["settled"] and base["settled"]:
        delta = reports._roi(a) - reports._roi(base)
        enough = a["settled"] >= 30
        if not enough:
            verdict = (f'<span class="warn">рано судить</span> — закрытых '
                       f'ставок всего {a["settled"]}, на такой выборке разница '
                       f'в любую сторону это шум')
        elif delta > 2:
            verdict = ('<span class="ok">модель обыгрывает контрольную '
                       'группу</span>')
        elif delta < -2:
            verdict = ('<span class="bad">модель проигрывает простой ставке '
                       'на фаворита</span>')
        else:
            verdict = ('<span class="warn">модель идёт вровень с контрольной '
                       'группой, то есть не добавляет ничего</span>')
        body += "<h2>Итог: модель против рынка</h2>"
        body += cards([
            ("ROI модели", f'{reports._roi(a):+.1f}%'),
            ("ROI без модели", f'{reports._roi(base):+.1f}%'),
            # ровно ноль — это не «плохо», а «никак»: красный тут врёт
            ("Разница", f'<span class="'
                        f'{"dim" if abs(delta) < 0.05 else ("ok" if delta > 0 else "bad")}">'
                        f'{delta:+.1f} п.п.</span>'),
        ])
        body += f'<p class="note">Вывод: {verdict}.</p>'
        if ag["settled"] and not ag2["settled"]:
            body += ('<p class="note">Обратите внимание: все закрытые ставки '
                     'пока только там, где модель <b>согласна</b> с рынком. '
                     'Такие совпадения ничего не доказывают — на них модель и '
                     'контрольная группа по построению дают одинаковый '
                     'результат. Пока не наберётся строка «спорит с рынком», '
                     'судить о пользе модели нельзя вообще никак, каким бы '
                     'красивым ни выглядел ROI выше.</p>')

    raw = _by_match_time(_read(PICKS_CSV, PICK_FIELDS))[:300]
    times = _match_times()
    trows = []
    for r in raw:
        st = r.get("status") or "pending"
        icon = {"win": '<span class="ok">зашла</span>',
                "loss": '<span class="bad">мимо</span>',
                "refund": '<span class="dim">возврат</span>',
                "push": '<span class="dim">возврат</span>'}.get(
                    st, '<span class="warn">ждём</span>')
        # Прочерк читался как «данных нет», хотя это полноценный ответ:
        # модель выбрала того же, что и букмекер. Пишем словом.
        against = ('<span class="warn">против</span>'
                   if r.get("agree") != "да"
                   else '<span class="dim">согласна</span>')
        if st in ("pending", ""):
            when, late = _waiting(r.get("when"), times, r.get("slug"))
            if late:
                icon = f'<span class="warn">ждём</span> {late}'
        else:
            when = r.get("when") or times.get(r.get("slug") or "", "")
        trows.append([
            e(fmt_stamp(r.get("found_at"))),
            e(fmt_when(when)),
            e(f"{r.get('p1')} — {r.get('p2')}"),
            e(r.get("player") or ""),
            f'<span class=num>{pf(r.get("sim_prob"), 0) * 100:.0f}%</span>',
            f'<span class=num>{e(r.get("odds"))}</span>',
            against, icon,
            f'<span class=num>{money(r.get("profit"))}</span>',
            e(r.get("score") or ""),
        ])
    body += "<h2>Все ставки на исход</h2>"
    body += ('<p class="note">'
             '<b>Ставка на</b> — кого выбрала модель. <b>Модель</b> — её '
             'вероятность именно на этого игрока, поэтому число всегда больше '
             '50%.<br>'
             '<b>Рынок</b> — «против», если модель выбрала не фаворита '
             'букмекера, и «согласна», если того же. Именно строки «против» '
             'и составляют главный разрез выше: только на них видно, знает ли '
             'модель что-то сверх линии.<br>'
             '<b>Итог</b> — зашла / мимо / возврат / ждём. Рядом с «ждём» '
             'показано, сколько прошло с начала матча: если там сутки и '
             'больше, значит результат не нашёлся автоматически и его стоит '
             'поискать руками. Возврат — это недоигранный матч (снятие, '
             'отказ); он не входит ни в оборот, ни в процент захода.<br>'
             'Показаны последние 300, новые сверху.</p>')
    body += table(["Найдена", "Начало матча", "Матч", "Ставка на", "Модель",
                   "Кэф", "Рынок", "Итог", "Прибыль", "Счёт"], trows)
    return body


def _forecast_quality() -> str:
    """Кто точнее — модель или цена. Не «угадала победителя», а насколько
    верной была вероятность: это та же проверка, но чувствительнее, и на
    коротком журнале она единственная что-то говорит."""
    pairs = []
    for r in _read(LOG_CSV, LOG_FIELDS):
        p, mkt, w = pf(r.get("sim_p1")), pf(r.get("mkt_implied_p1")), r.get("winner")
        if p is None or mkt is None or w not in ("p1", "p2"):
            continue
        pairs.append((p, _cal(p, "p1"), mkt, 1.0 if w == "p1" else 0.0))
    if len(pairs) < 20:
        return ('<h2>Качество прогноза</h2><p class="dim">Нужны сыгранные '
                'матчи, где есть и прогноз, и линия. Пока их слишком мало.</p>')

    def ll(idx):
        return -sum(x[-1] * math.log(max(x[idx], 1e-9))
                    + (1 - x[-1]) * math.log(max(1 - x[idx], 1e-9))
                    for x in pairs) / len(pairs)

    rows = []
    for name, idx, hint in (
            ("Модель как есть", 0, "то, что показывают карточки"),
            ("Модель после сжатия", 1, f"калибровка заморожена {CAL_FROZEN}"),
            ("Линия Pinnacle", 2, "маржа убрана нормировкой")):
        cls = "ok" if idx == 2 else ""
        rows.append([f'{e(name)}<br><span class="dim">{e(hint)}</span>',
                     f'<span class="num {cls}">{_brier(pairs, idx):.4f}</span>',
                     f'<span class="num {cls}">{ll(idx):.4f}</span>'])

    diffs = [(x[1] - x[-1]) ** 2 - (x[2] - x[-1]) ** 2 for x in pairs]
    obs = sum(diffs) / len(diffs)
    lo, hi = _paired_ci(diffs)

    if lo is not None and lo > 0:
        verdict = ('<span class="bad">модель уступает цене</span> — расхождения '
                   'с линией это её ошибки, а не найденная ценность')
    elif hi is not None and hi < 0:
        verdict = ('<span class="ok">модель точнее цены</span> — есть на чём '
                   'строить отбор')
    else:
        verdict = ('<span class="warn">различить пока нельзя</span>: интервал '
                   'накрывает ноль, данных не хватает')

    return "".join([
        '<h2>Качество прогноза</h2>',
        '<p class="note">Главный вопрос: знает ли модель что-нибудь, чего нет '
        'в цене. Проверяется не долей угаданных победителей, а точностью самой '
        'вероятности. Ошибка Брайера — средний квадрат промаха, меньше значит '
        'лучше; logloss наказывает за уверенные ошибки сильнее. Линия '
        'букмекера — это тоже прогноз, и очень хороший: обыграть надо именно '
        'её, а не подбрасывание монетки.</p>',
        table(["Прогноз", "Ошибка Брайера", "Logloss"], rows),
        f'<p class="note">Разница (модель после сжатия минус рынок): '
        f'<b>{obs:+.4f}</b>, 95%: '
        f'[{lo:+.4f} … {hi:+.4f}] по {len(pairs)} матчам. Положительная '
        f'разница означает, что модель ХУЖЕ. Вывод: {verdict}.</p>',
        '<p class="note">Почему смотрим сюда, а не на ROI: качество прогноза '
        'сходится в разы быстрее прибыли. На деньгах интервал шириной в '
        'тридцать пунктов, здесь — уже видно направление.</p>',
    ])


def _blend_weight() -> str:
    """Сколько веса данные готовы дать модели поверх цены."""
    pairs = []
    for r in _read(LOG_CSV, LOG_FIELDS):
        p, mkt, w = pf(r.get("sim_p1")), pf(r.get("mkt_implied_p1")), r.get("winner")
        if p is None or mkt is None or w not in ("p1", "p2"):
            continue
        pairs.append((_cal(p, "p1"), mkt, 1.0 if w == "p1" else 0.0))
    if len(pairs) < 20:
        return ""
    best_w, best_b = 0.0, None
    for i in range(21):
        w = i / 20
        b = sum((w * m + (1 - w) * k - y) ** 2 for m, k, y in pairs) / len(pairs)
        if best_b is None or b < best_b:
            best_w, best_b = w, b
    mkt_only = sum((k - y) ** 2 for _, k, y in pairs) / len(pairs)
    gain = mkt_only - best_b
    return "".join([
        '<h2>Сколько модель добавляет к цене</h2>',
        '<p class="note">Смешиваем прогноз модели с ценой в разной пропорции и '
        'смотрим, какая смесь точнее всего. Если лучший вес модели близок к '
        'нулю — значит поверх линии она не добавляет ничего, и любой отбор по '
        'её расхождениям с рынком будет ловить шум.</p>',
        cards([
            ("Лучший вес модели", f"{best_w * 100:.0f}%"),
            ("Вес цены", f"{(1 - best_w) * 100:.0f}%"),
            ("Выигрыш к цене", f"{gain:+.4f}"),
        ]),
        '<p class="note">«Выигрыш к цене» — насколько лучшая смесь точнее '
        'голой линии по ошибке Брайера. Значение около нуля значит, что модель '
        'не добавляет ничего измеримого.</p>',
    ])


def _calibration_after() -> str:
    """Что обещала калиброванная вероятность и что вышло."""
    pairs = []
    for r in _read(LOG_CSV, LOG_FIELDS):
        p, w = pf(r.get("sim_p1")), r.get("winner")
        if p is None or w not in ("p1", "p2"):
            continue
        pairs.append((p, _cal(p, "p1"), 1.0 if w == "p1" else 0.0))
    if not pairs:
        return ""
    rows = []
    for lo, hi in ((0, .3), (.3, .4), (.4, .5), (.5, .6), (.6, .7), (.7, .8), (.8, 1.01)):
        b = [x for x in pairs if lo <= x[0] < hi]
        if len(b) < 5:
            continue
        raw = sum(x[0] for x in b) / len(b) * 100
        cal = sum(x[1] for x in b) / len(b) * 100
        act = sum(x[2] for x in b) / len(b) * 100
        d = act - cal
        cls = "ok" if abs(d) <= 5 else ("warn" if abs(d) <= 12 else "bad")
        rows.append([f"{lo * 100:.0f}–{hi * 100:.0f}%",
                     f'<span class=num>{len(b)}</span>',
                     f'<span class=num>{raw:.0f}%</span>',
                     f'<span class=num>{cal:.0f}%</span>',
                     f'<span class=num>{act:.0f}%</span>',
                     f'<span class="num {cls}">{d:+.0f} п.п.</span>'])
    return "".join([
        '<h2>Калибровка после сжатия</h2>',
        '<p class="note">Прогнозы разложены по корзинам исходной вероятности. '
        '«Модель» — что она заявляла, «после сжатия» — что осталось после '
        'замороженной калибровки, «вышло» — фактическая доля побед П1. '
        'Отклонение считается от сжатой вероятности: если сжатие сделано '
        'верно, последний столбец должен колебаться вокруг нуля.</p>',
        table(["Прогноз модели", "Матчей", "Модель", "После сжатия", "Вышло",
               "Отклонение"], rows),
    ])


def _edge_inversion() -> str:
    """ROI по заявленному перевесу: растёт он или падает."""
    rows_all = _settled(_read(VALUE_CSV, VALUE_FIELDS))
    out = []
    for lo, hi, name in ((0, .05, "0–5%"), (.05, .10, "5–10%"),
                         (.10, .15, "10–15%"), (.15, .25, "15–25%"),
                         (.25, 99, "больше 25%")):
        b = [r for r in rows_all if lo <= (pf(r.get("edge")) or 0) < hi]
        if not b:
            continue
        n, roi, prof = _roi(b)
        promised = sum(pf(r.get("edge")) or 0 for r in b) / n * 100
        cls = "ok" if roi > 0 else "bad"
        out.append([name, f'<span class=num>{n}</span>',
                    f'<span class=num>{promised:.1f}%</span>',
                    f'<span class="num {cls}">{roi:+.1f}%</span>',
                    money(prof)])
    if not out:
        return ""
    return "".join([
        '<h2>Обещанный перевес против настоящего</h2>',
        '<p class="note">Если бы перевес считался верно, столбцы «обещано» и '
        '«вышло» росли бы вместе. Обратная картина — признак отбора по ошибке: '
        'ставка берётся там, где модель сильнее всего спорит с линией, а значит '
        'отбор собирает не ценность, а собственные промахи модели. Это и есть '
        'причина, по которой потолок перевеса стоит держать низким.</p>',
        table(["Заявленный перевес", "Ставок", "Обещано", "Вышло (ROI)",
               "Прибыль"], out),
    ])


CLV_CSV = (os.environ.get("TRA_CLV_CSV")
           or os.path.join(os.path.dirname(VALUE_CSV),
                           os.path.basename(VALUE_CSV).replace("value_bets", "clv")))
CLV_FIELDS = ["key", "stream", "slug", "p1", "p2", "when", "market", "pick",
              "line", "odds_open", "odds_close", "odds_close_other",
              "taken_at", "closed_at"]


def _clv_rows():
    """Ставки со снятой закрывающей ценой -> (поток, рынок, EV по закрытию).

    EV считается по цене БЕЗ маржи: закрывающие котировки обеих сторон дают
    вероятность p = (1/наша) / (1/наша + 1/чужая), и EV = p * наш кэф - 1.
    Без снятия маржи любая ставка выглядела бы убыточной просто потому, что
    комиссия букмекера сидит в цене, — измерение было бы смещено всегда в
    одну сторону. Если чужой стороны нет, строку пропускаем: чинить нечем.
    """
    if not os.path.exists(CLV_CSV):
        return []
    out = []
    for r in _read(CLV_CSV, CLV_FIELDS):
        op, cl, ot = (pf(r.get("odds_open")), pf(r.get("odds_close")),
                      pf(r.get("odds_close_other")))
        if not (op and cl and ot):
            continue
        p = (1 / cl) / (1 / cl + 1 / ot)
        out.append({
            "stream": r.get("stream") or "?",
            "market": r.get("market") or "?",
            "pick": r.get("pick") or "",
            "line": (r.get("line") or "").replace(",", "."),
            "p1": r.get("p1") or "", "p2": r.get("p2") or "",
            "when": r.get("when") or "",
            "open": op, "close": cl,
            "ev": p * op - 1,          # ожидаемая доходность по закрытию
            "move": op / cl - 1,       # насколько наша цена выше закрывающей
            "closed_at": r.get("closed_at") or "",
        })
    return out


def _clv_detail(rows, limit: int = 200) -> str:
    """Построчно: по какой цене поставлено и какой оказалась закрывающая."""
    rows = sorted(rows, key=lambda r: r["closed_at"], reverse=True)[:limit]
    body = []
    # Пометка потока обязательна: один и тот же исход попадает и в ценные
    # ставки, и в журнал исходов — без неё две строки выглядят как дубль,
    # хотя это разные популяции со своим отбором.
    STREAM = {"value": "ценная", "pick": "исход"}
    for r in rows:
        sel = " ".join(x for x in (r["market"], r["pick"], r["line"]) if x)
        tag = STREAM.get(r["stream"])
        if tag:
            sel = f'{e(sel)} <span class="dim">· {tag}</span>'
        else:
            sel = e(sel)
        cls = "ok" if r["ev"] > 0 else "bad"
        # Стрелка показывает движение цены, а не выгоду: вверх — наша цена
        # была выше закрывающей, то есть мы успели взять лучше.
        mark = ("=" if abs(r["move"]) < 0.005
                else ('<span class="ok">↑</span>' if r["move"] > 0
                      else '<span class="bad">↓</span>'))
        body.append([
            f'{e(r["p1"])} — {e(r["p2"])}<br>'
            f'<span class="dim">{e(fmt_when(r["when"]))}</span>',
            sel,
            f'<span class=num>{r["open"]:.3f}</span>',
            f'<span class=num>{r["close"]:.3f}</span> {mark}',
            f'<span class="num {cls}">{r["ev"] * 100:+.1f}%</span>',
        ])
    tail = (f'<p class="note">Показаны последние {limit}.</p>'
            if len(rows) == limit else "")
    return ("<h3>Ставка за ставкой</h3>"
            + table(["Матч", "Ставка", "Взяли", "Закрытие", "CLV"], body)
            + tail)


def _clv_block() -> str:
    """CLV: взяли ли мы цену лучше закрывающей.

    Главный измеритель на коротком журнале. Сигнал даёт каждая ставка сразу,
    не дожидаясь исхода матча, поэтому шума в нём в разы меньше, чем в ROI.
    """
    rows = _clv_rows()
    head = ['<h2>CLV — цена против закрывающей</h2>',
            '<p class="note">Самая быстрая проверка перевеса. Сравнивается '
            'цена, по которой ставка записана, с последней ценой Pinnacle '
            'перед стартом матча — маржа при этом убирается по обеим сторонам. '
            'Логика простая: закрывающая линия у Pinnacle — лучший из '
            'публично доступных прогнозов, и если наши ставки систематически '
            'берут цену лучше неё, значит модель знает что-то сверх рынка. '
            'Если нет — никакой фильтр по перевесу этого не изменит. Важно, '
            'что ждать результата матча не нужно: сигнал даёт каждая ставка '
            'сразу, поэтому выборка набирается в разы быстрее, чем для ROI.</p>']
    if not rows:
        return "".join(head + [
            '<p class="dim">Пока ничего не снято. Закрывающая линия берётся '
            'по таймеру <code>clv-collect.timer</code> за 20 минут до начала '
            'матча — первые строки появятся, когда подойдёт время ближайших '
            'матчей из журнала.</p>'])

    def block(name, data):
        if not data:
            return None
        ev = [x["ev"] for x in data]
        avg = sum(ev) / len(ev)
        pos = sum(1 for v in ev if v > 0) / len(ev) * 100
        lo, hi = _paired_ci(ev)
        cls = "ok" if avg > 0 else "bad"
        ci = (f"[{lo * 100:+.1f}% … {hi * 100:+.1f}%]"
              if lo is not None else "—")
        return [e(name), f'<span class=num>{len(data)}</span>',
                f'<span class="num {cls}">{avg * 100:+.2f}%</span>',
                f'<span class=num>{pos:.0f}%</span>',
                f'<span class="num dim">{ci}</span>']

    body = [block("Ценные ставки", [x for x in rows if x["stream"] == "value"]),
            block("Исходы", [x for x in rows if x["stream"] == "pick"]),
            block("Вместе", rows)]
    body = [b for b in body if b]

    ev = [x["ev"] for x in rows]
    avg = sum(ev) / len(ev)
    lo, hi = _paired_ci(ev)
    if len(rows) < 100:
        verdict = ('<span class="warn">рано судить</span>: снято '
                   f'{len(rows)} ставок, для вывода нужно хотя бы сотня-другая')
    elif lo is not None and lo > 0:
        verdict = ('<span class="ok">цена систематически лучше закрывающей</span> '
                   '— перевес есть, и это самый сильный аргумент из доступных')
    elif hi is not None and hi < 0:
        verdict = ('<span class="bad">цена систематически хуже закрывающей</span> '
                   '— перевеса нет, и на этой модели он не появится')
    else:
        verdict = ('<span class="warn">различить нельзя</span>: интервал '
                   'накрывает ноль')

    return "".join(head + [
        table(["Поток", "Ставок", "Средний CLV", "Доля с плюсом", "95% интервал"],
              body),
        f'<p class="note">Вывод: {verdict}.</p>',
        '<p class="note">«Средний CLV» — это ожидаемая доходность ставки, '
        'посчитанная по закрывающей цене без маржи. Плюс означает, что мы '
        'взяли цену выше справедливой на момент закрытия. Ноль или минус '
        'означает, что мы просто платили комиссию. Обратите внимание: CLV — '
        'измерительный прибор, а не источник денег. Он не делает модель '
        'лучше, он быстро говорит, есть ли смысл продолжать.</p>',
        _clv_detail(rows),
    ])


def _new_rule() -> str:
    """Параллельный журнал: что взяло бы новое правило. Старый поток не
    трогается — правило это фильтр поверх него, поэтому обе ставки живут на
    одних и тех же матчах и сравнимы честно."""
    blocks = ['<h2>Новое правило — параллельный журнал</h2>',
              '<p class="note">Правило: берём ставку, только если перевес '
              'остаётся положительным <b>после сжатия вероятности</b> '
              f'замороженной калибровкой. Ничего не публикуется и не меняется '
              f'в текущем отборе — это бумажный журнал поверх того же потока. '
              f'Всё, что найдено с {CAL_FROZEN}, идёт в зачёт «вперёд»: только '
              f'эти строки — честная проверка, остальное правило видело при '
              f'настройке.</p>']
    for title, path, fields, kind in (
            ("Ценные ставки", VALUE_CSV, VALUE_FIELDS, "value"),
            ("Исходы", PICKS_CSV, PICK_FIELDS, "pick")):
        rows = _settled(_read(path, fields))
        fwd = [r for r in rows if (r.get("found_at") or "") >= CAL_FROZEN]
        table_rows = []
        for label, data, tag in (("вся история", rows, "подгонка"),
                                 ("вперёд", fwd, "проверка")):
            if not data:
                table_rows.append([f'{e(label)} <span class="dim">({tag})</span>',
                                   '<span class="dim">нет данных</span>', "", "", ""])
                continue
            n0, roi0, _ = _roi(data)
            sel = [r for r in data if _rule_takes(r, kind)]
            n1, roi1, prof1 = _roi(sel)
            c0 = "ok" if roi0 > 0 else "bad"
            c1 = "ok" if roi1 > 0 else "bad"
            table_rows.append([
                f'{e(label)} <span class="dim">({tag})</span>',
                f'<span class=num>{n0}</span>',
                f'<span class="num {c0}">{roi0:+.1f}%</span>',
                f'<span class=num>{n1}</span>',
                f'<span class="num {c1}">{roi1:+.1f}%</span>' if n1 else
                '<span class="dim">ни одной</span>'])
        blocks.append(f"<h3>{e(title)}</h3>")
        blocks.append(table(["Период", "Старый поток", "ROI старого",
                             "Взяло правило", "ROI правила"], table_rows))
    blocks.append('<p class="note">Пока строк «вперёд» мало, читать их нельзя: '
                  'разница в пару ставок переворачивает ROI. Смотреть сюда '
                  'имеет смысл, когда наберётся хотя бы несколько сотен.</p>')
    return "".join(blocks)


def view_method(q) -> str:
    """Вкладка «Метод»: по чему судим о модели и что проверяем дальше."""
    # Предупреждение остаётся на случай тура, для которого своих коэффициентов
    # ещё не подобрали: тогда берутся мужские, и об этом надо сказать прямо.
    # У ATP и WTA свои, подобранные каждый на своих журналах.
    warn = ""
    if TOUR not in CAL_BY_TOUR:
        warn = ('<p class="note"><span class="bad">Осторожно:</span> '
                f'для тура {e(TOUR.upper())} своих коэффициентов калибровки '
                'нет, взяты мужские. Числа ниже показательны только как '
                'заготовка — пересчитайте на своих данных.</p>')
    return "".join([
        '<h2>Порядок работы</h2>',
        warn,
        '<p class="note">На коротком журнале ROI бесполезен: интервал шире '
        'самого перевеса, и любой разрез, выбранный по результату, окажется '
        '«прибыльным» случайно. Поэтому судим не по деньгам, а по качеству '
        'вероятностей — оно сходится в разы быстрее. Порядок такой: '
        '<b>1)</b> смотрим CLV — берём ли мы цену лучше закрывающей, это '
        'самый быстрый измеритель, ему не нужен исход матча; '
        '<b>2)</b> сравниваем прогноз с ценой по ошибке Брайера; '
        '<b>3)</b> смотрим, какой вес данные готовы дать модели поверх цены; '
        '<b>4)</b> держим правило отбора замороженным и меряем его только '
        'вперёд. Старые журналы при этом продолжают собираться как прежде — '
        'ни один поток не выключен.</p>',
        f'<p class="note">Калибровка заморожена <b>{CAL_FROZEN}</b> и '
        f'подобрана на журналах <b>{e(TOUR.upper())}</b>: сжатие прогноза на '
        f'П1 — коэффициент <b>{CAL["p1"][1]:.2f}</b> (меньше единицы значит '
        '«модель слишком уверенная»). У каждого тура он свой, и это не '
        'формальность — смешивать их нельзя по той же причине, по которой '
        'разведены журналы. Пересчитывать на новых данных можно, но тогда '
        'отсчёт «вперёд» начинается заново, иначе правило снова будет '
        'подогнано под то, на чём его проверяют.</p>',
        _clv_block(),
        _forecast_quality(),
        _blend_weight(),
        _calibration_after(),
        _edge_inversion(),
        _new_rule(),
    ])


ROUTES = {"/": (view_home, "home", "Сводка"),
          "/picks": (view_picks, "picks", "Исходы"),
          "/bets": (view_bets, "bets", "Ставки"),
          "/matches": (view_matches, "matches", "Матчи"),
          "/method": (view_method, "method", "Метод"),
          "/queue": (view_queue, "queue", "Очередь")}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Тур в названии: панели ATP и WTA открываются одновременно в соседних
    # вкладках, и с одинаковым заголовком их не различить ни в шапке, ни в
    # заголовке вкладки. Название уходит и в <title>, поэтому вкладки тоже
    # становятся разными.
    serve(title=f"tennisratio{TOUR.upper()}", subtitle="поиск ценности",
          routes=ROUTES, token=TOKEN, host=HOST, port=PORT, refresh=REFRESH)
    if not os.environ.get("DASH_TOKEN"):
        print(f"Токен на этот запуск: {TOKEN} — задайте DASH_TOKEN в .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
