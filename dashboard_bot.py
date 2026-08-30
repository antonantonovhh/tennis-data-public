#!/usr/bin/env python3
"""Панель для бота tennisratio: http://<ip>:8801/?token=...

Данные берутся из bets_db.json — базы одобренных вами ставок. Это другая
история, чем у tennisratioall: там модель сама ищет ценность, здесь вы
нажимаете «Ставь!» и бот ведёт учёт.

    python3 dashboard_bot.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from webui import (bar, cards, e, fmt_stamp, fmt_when, line_chart,  # noqa: E402
                   links, load_env, money, num, pct, serve, table)


def nice_score(raw: str) -> str:
    """Счёт для показа: '2-1,66-7,6-3,6-3' -> '6-7, 6-3, 6-3'.

    В базе он лежит сырым, как отдал TennisExplorer: с итоговым токеном по
    сетам впереди и склеенным тайбрейком. Разбор берём из бота, чтобы логика
    была одна на всех.
    """
    if not raw:
        return ""
    try:
        import re as _re
        src = open(os.path.join(HERE, "bot_merged.py"), encoding="utf-8").read()
        ns = {"re": _re}
        exec(src[src.index("def _parse_score_sets"):  # noqa: S102
                 src.index("def parse_te_last_matches")], ns)
        return ns["pretty_score"](raw)
    except Exception:  # noqa: BLE001
        return raw

load_env(HERE)

DB = os.environ.get("BETS_DB") or os.path.join(HERE, "bets_db.json")
HOST = os.environ.get("DASH_HOST", "0.0.0.0")
PORT = int(os.environ.get("BOT_DASH_PORT", "8801"))
TOKEN = os.environ.get("DASH_TOKEN") or secrets.token_urlsafe(12)
REFRESH = int(os.environ.get("DASH_REFRESH", "120"))

PERIODS = [("day", "сегодня"), ("week", "неделя"), ("month", "месяц"),
           ("all", "всё время")]

log = logging.getLogger("dash-bot")


# ------------------------------------------------------------------ данные
def load():
    try:
        with open(DB, encoding="utf-8") as fh:
            return json.load(fh).get("bets", [])
    except Exception as exc:  # noqa: BLE001
        log.error("%s не читается: %s", DB, exc)
        return []


def _match_dt(m):
    """Дата матча. В базе она строкой вида '21.08. 14:00' или ISO — берём
    что получится, иначе падаем на время добавления."""
    raw = str(m.get("date") or "")
    got = re.search(r"(\d{2})\.(\d{2})", raw)
    if got:
        day, month = int(got.group(1)), int(got.group(2))
        year = datetime.now(timezone.utc).year
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            pass
    ts = m.get("added_ts")
    if ts:
        try:
            return datetime.fromtimestamp(float(ts), timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return None


def found_at(ts):
    """Когда ставка попала в базу: «08-23 09:18» в зоне показа.

    Формат и зона те же, что в панели tennisratioall, чтобы таблицы
    читались одинаково. Здесь в bets_db.json лежит unix-timestamp,
    в том журнале — ISO-строка; fmt_stamp принимает и то и другое.
    """
    out = fmt_stamp(ts)
    return e(out) if out else '<span class="dim">—</span>'


def match_name(m, b=None) -> str:
    """Название матча, с пометкой источника для импортированных.

    В базе рядом лежат две породы записей: свои, которые бот завёл сам по
    нажатию «Ставь!», и перенесённые из архива bet-hub. Отличать их важно —
    у импортированных нет наших кэфов по обеим сторонам, маржи и, у части,
    счёта, так что «пусто» в этих колонках у них норма, а у своих — сбой.

    Смотрим сначала на саму ставку, потом на матч: один матч бывает смешанным.
    Каналы BIGTENBETS и BIGTENBETS2 ставят на одни матчи разное (исход и
    тотал), и тотал из архива вполне может лежать в матче, который завёл бот.
    """
    name = e(m.get("match") or "")
    src = (b or {}).get("source") or m.get("source") or ""
    if src == "bet-hub":
        name += (' <span class="dim" style="font-size:12px;'
                 'white-space:nowrap">· bet-hub</span>')
    return name


def by_match_time(matches):
    """Матчи по времени начала, по убыванию: поздние сверху.

    Раньше сортировали по added_ts — когда ставка попала в базу. Это не то
    же самое, что время матча: находка вчерашнего вечера могла оказаться
    выше сегодняшнего утреннего матча, и список читался вперемешку.

    parse_when берём из обходчика: он уже умеет формат «22.08. 11:00»
    вместе со временем и правильно обрабатывает переход через год.
    Матчи без разбираемой даты падают вниз, к ним в запас — added_ts.
    """
    from tennisratioall.results import parse_when  # noqa: PLC0415

    def key(m):
        t = parse_when(str(m.get("date") or ""))
        if t:
            return (1, t.timestamp())
        return (0, float(m.get("added_ts") or 0))

    return sorted(matches, key=key, reverse=True)


def period_start(period):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    if period == "day":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    return None


def collect(period):
    start = period_start(period)
    by_type = defaultdict(lambda: {"n": 0, "win": 0, "loss": 0, "push": 0,
                                   "stake": 0.0, "profit": 0.0})
    total = {"n": 0, "win": 0, "loss": 0, "push": 0, "stake": 0.0,
             "profit": 0.0}
    pending = 0
    for m in load():
        dt = _match_dt(m)
        if start and dt and dt < start:
            continue
        for b in m.get("bets", []):
            st = b.get("status") or "pending"
            if st in ("pending", ""):
                pending += 1
                continue
            box = by_type[b.get("type") or "?"]
            for t in (box, total):
                t["n"] += 1
                t["stake"] += num(b.get("stake"), 0.0)
                t["profit"] += num(b.get("profit"), 0.0)
                if st == "win":
                    t["win"] += 1
                elif st == "loss":
                    t["loss"] += 1
                else:
                    t["push"] += 1
    return {"by_type": dict(by_type), "total": total, "pending": pending}


def roi(box):
    return (box["profit"] / box["stake"] * 100) if box["stake"] else 0.0


# ------------------------------------------------------------------ страницы
def view_home(q):
    period = (q.get("period") or ["all"])[0]
    data = collect(period)
    t = data["total"]
    body = (f'<p class="note">Период: '
            f'{links("/", PERIODS, period, TOKEN)}</p>')
    body += cards([
        ("Ставок", t["n"]),
        ("Зашло", (f'{t["win"]}<span class="dim" style="font-size:14px"> / '
                   f'{t["n"]} ({t["win"] / t["n"] * 100:.0f}%)</span>'
                   f'{bar(t["win"], t["n"])}') if t["n"] else "—"),
        ("Оборот", f'{t["stake"]:,.0f}'.replace(",", " ")),
        ("Прибыль", money(t["profit"])),
        ("ROI", f'<span class="{"ok" if roi(t) > 0 else "bad"}">'
                f'{roi(t):+.1f}%</span>' if t["n"] else "—"),
        ("Ждут результата", data["pending"]),
    ])

    body += bankroll_chart(period)

    rows = []
    for name, box in sorted(data["by_type"].items(),
                            key=lambda kv: -kv[1]["profit"]):
        hit = box["win"] / box["n"] * 100 if box["n"] else 0
        rows.append([e(name), f'<span class=num>{box["n"]}</span>',
                     f'<span class=num>{box["win"]}</span>',
                     f'<span class=num>{box["loss"]}</span>',
                     f'<span class=num>{hit:.0f}%</span>',
                     f'<span class=num>{money(box["profit"])}</span>',
                     f'<span class="num {"ok" if roi(box) > 0 else "bad"}">'
                     f'{roi(box):+.1f}%</span>'])
    body += "<h2>По типам ставок</h2>"
    body += table(["Тип", "Ставок", "Выигр.", "Проигр.", "Заходит",
                   "Прибыль", "ROI"], rows)

    if t["n"] < 30:
        body += ('<p class="note">Ставок пока мало: на такой выборке ROI — '
                 'это в основном шум. Одна выигравшая ставка по 2.5 двигает '
                 'его на десятки процентов. Осмысленные выводы начинаются '
                 'сотни с полутора.</p>')
    return body


def view_bets(q):
    status = (q.get("status") or ["all"])[0]
    rows = []
    for m in by_match_time(load()):
        for b in m.get("bets", []):
            st = b.get("status") or "pending"
            if status != "all" and st != status:
                continue
            icon = {"win": '<span class="ok">выигрыш</span>',
                    "loss": '<span class="bad">проигрыш</span>',
                    "refund": '<span class="dim">возврат</span>',
                    "push": '<span class="dim">возврат</span>'}.get(
                        st, '<span class="warn">в игре</span>')
            games = ""
            if m.get("resolved"):
                g1, g2 = m.get("games_p1", 0), m.get("games_p2", 0)
                games = f"{g1}-{g2} ({g1 + g2}, {g1 - g2:+d})"
            rows.append([
                found_at(m.get("added_ts")),
                e(fmt_when(m.get("date"))),
                match_name(m, b),
                e(b.get("prediction") or ""),
                f'<span class=num>{e(b.get("odds"))}</span>',
                icon,
                f'<span class=num>{money(b.get("profit"))}</span>',
                e(nice_score(m.get("score") or "")),
                f'<span class="dim">{games}</span>',
            ])
            if len(rows) >= 400:
                break
    filt = links("/bets", [("all", "все"), ("pending", "в игре"),
                           ("win", "зашли"), ("loss", "не зашли")],
                 status, TOKEN, param="status")
    return (f'<p class="note">Фильтр: {filt} · последние 400</p>'
            + table(["Найдена", "Начало матча", "Матч", "Прогноз", "Кэф",
                     "Статус", "Прибыль", "Счёт", "Геймы"], rows))


def view_live(q):
    """Матчи, по которым ставки сделаны, а результата ещё нет."""
    rows = []
    total_risk = 0.0
    for m in by_match_time(load()):
        if m.get("resolved"):
            continue
        live = [b for b in m.get("bets", [])
                if (b.get("status") or "pending") in ("pending", "")]
        if not live:
            continue
        picks = ", ".join(f'{b.get("prediction")} @ {b.get("odds")}'
                          for b in live)
        # сумму считаем здесь и складываем сразу: вытаскивать её обратно
        # из готового HTML — верный способ однажды сломать вёрсткой
        risk = sum(num(b.get("stake"), 0.0) for b in live)
        total_risk += risk
        rows.append([
            e(fmt_when(m.get("date"))),
            e(m.get("tournament") or ""),
            match_name(m),
            e(picks),
            f'<span class=num>{risk:,.0f}</span>'.replace(",", " "),
        ])
    body = cards([
        ("Матчей в игре", len(rows)),
        ("Ставок", sum(len(r[3].split(", ")) for r in rows)),
        ("Под риском", f'{total_risk:,.0f}'.replace(",", " ")),
    ])
    return body + table(["Дата", "Турнир", "Матч", "Ставки", "Сумма"], rows)


def _flat_profit(bet, unit: float) -> float:
    """Сколько дала бы ставка, если бы на все шла одинаковая сумма.

    Считаем от котировки, а не масштабируем готовый `profit`: у возвратов и
    половинных расчётов он не пропорционален сумме, и множитель наврал бы.
    """
    status = bet.get("status")
    if status == "win":
        return unit * ((num(bet.get("odds"), 0.0) or 0.0) - 1)
    if status == "loss":
        return -unit
    return 0.0        # возврат, отмена и всё прочее — в ноль


def _profit_by_day(start=None):
    """Разбор ставок по дням матча: (по дням, [(день, ставка)], суммы ставок).

    Один сбор на две страницы: таблицу «По дням» и кривую на «Сводке».
    `flat` дозаполняется вызывающим — единица флэта известна только после
    того, как просмотрены все ставки.
    """
    per_day = defaultdict(lambda: {"n": 0, "win": 0, "profit": 0.0,
                                   "stake": 0.0, "flat": 0.0})
    settled = []                       # (день, ставка) — для второго прохода
    stakes = Counter()
    for m in load():
        dt = _match_dt(m)
        if start and dt and dt < start:
            continue
        key = dt.strftime("%Y-%m-%d") if dt else "?"
        for b in m.get("bets", []):
            if (b.get("status") or "pending") in ("pending", ""):
                continue
            d = per_day[key]
            d["n"] += 1
            d["win"] += 1 if b.get("status") == "win" else 0
            d["profit"] += num(b.get("profit"), 0.0)
            d["stake"] += num(b.get("stake"), 0.0)
            settled.append((key, b))
            stakes[num(b.get("stake"), 0.0)] += 1
    return per_day, settled, stakes


def view_days(q):
    """Прибыль по дням таблицей. Кривая роста банка живёт на «Сводке»."""
    per_day, _settled, _stakes = _profit_by_day()

    rows = []
    for day in sorted(per_day, reverse=True)[:60]:
        d = per_day[day]
        rows.append([e(day), f'<span class=num>{d["n"]}</span>',
                     f'<span class=num>{d["win"]}</span>',
                     f'<span class=num>{money(d["profit"])}</span>',
                     f'<span class=num>{(d["profit"] / d["stake"] * 100) if d["stake"] else 0:+.0f}%</span>'])

    return table(["День", "Ставок", "Зашло", "Прибыль", "ROI"], rows)


def bankroll_chart(period) -> str:
    """Накопленная прибыль по дням — кривая роста банка.

    Живёт на «Сводке»: это первое, что видно при заходе на панель, и главный
    ответ на вопрос «как идут дела». Вкладка «По дням» осталась таблицей —
    там разбор по дням, а не общая картина.

    Период тот же, что у карточек над графиком, иначе цифры наверху и кривая
    под ними говорили бы о разных отрезках времени.
    """
    per_day, settled, stakes = _profit_by_day(period_start(period))
    days = [d for d in sorted(per_day) if d != "?"]
    if len(days) < 2:
        return ""          # за один день кривой не бывает

    unit = stakes.most_common(1)[0][0] if stakes else 0.0
    for key, b in settled:
        per_day[key]["flat"] += _flat_profit(b, unit)

    labels, cum, cum_flat = [], [], []
    run = run_flat = 0.0
    for day in days:
        run += per_day[day]["profit"]
        run_flat += per_day[day]["flat"]
        labels.append(f"{day[8:10]}.{day[5:7]}")     # 2026-08-23 -> 23.08
        cum.append(run)
        cum_flat.append(run_flat)

    running = sum(d["profit"] for d in per_day.values())
    series = [("Прибыль", "var(--ok)", cum)]
    # Пробел как разделитель разрядов ставим точечно: общий replace по всей
    # строке заодно выкашивал запятые самого предложения.
    unit_txt = f"{unit:,.0f}".replace(",", " ")
    # Когда суммы у всех ставок равны, флэт совпадает с фактом — рисовать
    # вторую линию поверх первой незачем, честнее сказать это словами.
    if all(abs(a - b) < 1 for a, b in zip(cum, cum_flat)):
        hint = (f"Все ставки одинаковые ({unit_txt}), поэтому флэт совпал бы "
                "с фактом — вторая линия появится, когда суммы начнут "
                "различаться.")
    else:
        series.append(("Прибыль флэтом", "var(--acc)", cum_flat))
        hint = f"Флэт — если бы на каждую ставку шло по {unit_txt}."

    return (f'<p class="note">Накопленным итогом: {money(running)}. '
            'Смотрите не на отдельные дни, а на то, ровно ли идёт кривая: '
            'прибыль из одного удачного дня и прибыль из тридцати — это '
            'разные вещи.</p>'
            + line_chart(series, labels, hint=hint))


CLV_CSV = (os.environ.get("BOT_CLV_CSV")
           or os.path.join(os.path.dirname(DB), "clv_bot.csv"))


def _clv_rows():
    """Ставки со снятой закрывающей ценой.

    Файл пишет collect_clv.py --source bot по таймеру. Базу ставок он не
    трогает: связь по ключу, как и у обходчика.
    """
    if not os.path.exists(CLV_CSV):
        return []
    import csv  # noqa: PLC0415
    out = []
    with open(CLV_CSV, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            op, cl, ot = (num(r.get("odds_open")), num(r.get("odds_close")),
                          num(r.get("odds_close_other")))
            if not (op and cl and ot):
                continue
            # Маржа снимается по обеим сторонам: без этого любая ставка
            # выглядит убыточной просто потому, что комиссия сидит в цене.
            p = (1 / cl) / (1 / cl + 1 / ot)
            out.append({
                "market": r.get("market") or "?",
                "pick": r.get("pick") or "",
                "line": (r.get("line") or "").replace(",", "."),
                "p1": r.get("p1") or "", "p2": r.get("p2") or "",
                "when": r.get("when") or "",
                "open": op, "close": cl,
                "ev": p * op - 1,
                "move": op / cl - 1,
                "closed_at": r.get("closed_at") or "",
            })
    return out


def _clv_detail(rows, limit: int = 200) -> str:
    """Построчно: по какой цене поставлено и какой оказалась закрывающая."""
    rows = sorted(rows, key=lambda r: r["closed_at"], reverse=True)[:limit]
    body = []
    for r in rows:
        sel = " ".join(x for x in (r["market"], r["pick"], r["line"]) if x)
        cls = "ok" if r["ev"] > 0 else "bad"
        # Стрелка про движение цены, а не про выгоду: вверх — наша цена была
        # выше закрывающей, то есть мы успели взять лучше.
        mark = ("=" if abs(r["move"]) < 0.005
                else ('<span class="ok">↑</span>' if r["move"] > 0
                      else '<span class="bad">↓</span>'))
        body.append([
            f'{e(r["p1"])} — {e(r["p2"])}<br>'
            f'<span class="dim">{e(fmt_when(r["when"]))}</span>',
            e(sel),
            f'<span class=num>{r["open"]:.3f}</span>',
            f'<span class=num>{r["close"]:.3f}</span> {mark}',
            f'<span class="num {cls}">{r["ev"] * 100:+.1f}%</span>',
        ])
    return ("<h3>Ставка за ставкой</h3>"
            + table(["Матч", "Ставка", "Взяли", "Закрытие", "CLV"], body))


def view_clv(q):
    """CLV: брали ли мы цену лучше закрывающей."""
    rows = _clv_rows()
    head = ['<p class="note">Закрывающая линия — последняя цена Pinnacle '
            'перед стартом матча, маржа из неё убрана по обеим сторонам. '
            'Смысл прост: если наши ставки систематически берут цену лучше '
            'неё, у отбора есть перевес; если нет — его нет. Ценно тем, что '
            'ждать результата матча не нужно: сигнал даёт каждая ставка '
            'сразу, поэтому выборка набирается в разы быстрее, чем для ROI. '
            'Это измерительный прибор, а не источник денег.</p>']
    if not rows:
        return "".join(head + [
            '<p class="dim">Пока ничего не снято. Линия берётся по таймеру '
            '<code>clv-collect-bot.timer</code> за 20 минут до начала матча — '
            'первые строки появятся, когда подойдёт время ближайшей ставки.</p>'])

    def line(name, data):
        if not data:
            return None
        ev = [x["ev"] for x in data]
        avg = sum(ev) / len(ev)
        pos = sum(1 for v in ev if v > 0) / len(ev) * 100
        cls = "ok" if avg > 0 else "bad"
        return [e(name), f'<span class=num>{len(data)}</span>',
                f'<span class="num {cls}">{avg * 100:+.2f}%</span>',
                f'<span class=num>{pos:.0f}%</span>']

    body = []
    for market in sorted({r["market"] for r in rows}):
        got = line(market, [x for x in rows if x["market"] == market])
        if got:
            body.append(got)
    body.append(line("Вместе", rows))

    ev = [x["ev"] for x in rows]
    avg = sum(ev) / len(ev)
    if len(rows) < 50:
        verdict = ('<span class="warn">рано судить</span>: снято '
                   f'{len(rows)} ставок')
    elif avg > 0:
        verdict = '<span class="ok">цена в среднем лучше закрывающей</span>'
    else:
        verdict = '<span class="bad">цена в среднем хуже закрывающей</span>'

    return "".join(head + [
        table(["Рынок", "Ставок", "Средний CLV", "Доля с плюсом"], body),
        f'<p class="note">Вывод: {verdict}. «Средний CLV» — ожидаемая '
        'доходность ставки по закрывающей цене без маржи: плюс означает, что '
        'мы взяли цену выше справедливой на момент закрытия, ноль или минус — '
        'что просто платили комиссию.</p>',
        _clv_detail(rows),
    ])


ROUTES = {
    "/": (view_home, "home", "Сводка"),
    "/bets": (view_bets, "bets", "Ставки"),
    "/live": (view_live, "live", "В игре"),
    "/days": (view_days, "days", "По дням"),
    "/clv": (view_clv, "clv", "Закрытие"),
}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not os.path.exists(DB):
        print(f"Нет базы {DB} — панель покажет пустые таблицы.")
    serve(title="tennisratio", subtitle="ставки по кнопке", routes=ROUTES,
          token=TOKEN, host=HOST, port=PORT, refresh=REFRESH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
