"""Отчёты за день, неделю и месяц по value-ставкам.

Строятся из `value_bets.csv` — того же файла, куда пишется каждая найденная
ставка. Считать нечего, пока матчи не сыграны: незакрытые ставки в сводку
не попадают, но показываются отдельной строкой, чтобы было видно, сколько
ещё в воздухе.

Кроме привычных «оборот / прибыль / ROI» здесь есть блок точности модели: как
часто её фаворит действительно выигрывал. Он важнее ROI на первых сотнях
ставок — прибыль на такой выборке почти целиком шум, а доля угаданных
фаворитов сходится быстрее и раньше скажет, врёт модель или нет.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .journal import (LOG_CSV, LOG_FIELDS, PICK_FIELDS, PICKS_CSV, VALUE_CSV,
                      VALUE_FIELDS, _read, pf)

log = logging.getLogger(__name__)

PERIODS = {
    "day": "за сегодня",
    "week": "за неделю",
    "month": "за месяц",
    "all": "за всё время",
}


def _period_start(period: str, today: date | None = None) -> date | None:
    today = today or datetime.now(timezone.utc).date()
    if period == "day":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    return None


def _as_date(iso: str) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        return None


def _f(v, default=0.0) -> float:
    """Число из ячейки CSV — с учётом десятичной запятой."""
    got = pf(v, None)
    return default if got is None else got


def _box() -> dict:
    # settled — ставки, у которых был риск: без возвратов. Именно по ним
    # считаются и процент захода, и ROI.
    return {"n": 0, "settled": 0, "win": 0, "loss": 0, "push": 0,
            "stake": 0.0, "profit": 0.0, "edge": 0.0}


def collect(period: str) -> dict:
    """Сводка по ставкам периода: по рынкам и целиком.

    Возвраты (снятие, push по тоталу) считаются отдельно и НЕ входят ни в
    оборот, ни в знаменатель процента захода. Ставка, деньги по которой
    вернулись, не выиграна и не проиграна; когда она сидела в обороте, ROI
    занижался, а «зашло» показывало меньше, чем есть на самом деле.
    """
    start = _period_start(period)
    rows = _read(VALUE_CSV, VALUE_FIELDS)

    by_market: dict[str, dict] = defaultdict(_box)
    pending = 0
    total = _box()

    for r in rows:
        # период считаем по дате закрытия: ставка, найденная вчера и сыгранная
        # сегодня, относится к сегодняшнему результату
        d = _as_date(r.get("resolved_at") or "")
        status = r.get("status") or "pending"
        if status in ("pending", ""):
            # Незакрытые считаем все, а не только найденные внутри периода:
            # у такой ставки ещё нет даты результата, значит её не к чему
            # отнести, и «в ожидании» за сегодня расходилось с тем же полем
            # на вкладке исходов.
            pending += 1
            continue
        if start and (not d or d < start):
            continue

        m = by_market[r.get("market") or "?"]
        for box in (m, total):
            box["n"] += 1
            box["profit"] += _f(r.get("profit"))
            if status == "win":
                box["win"] += 1
            elif status == "loss":
                box["loss"] += 1
            else:
                box["push"] += 1
                continue
            box["settled"] += 1
            box["stake"] += _f(r.get("stake"))
            box["edge"] += _f(r.get("edge"))

    return {"period": period, "start": start, "by_market": dict(by_market),
            "total": total, "pending": pending}


def model_accuracy(period: str) -> dict:
    """Насколько часто фаворит модели выигрывал — и что говорил рынок.

    Модель и рынок сравниваются ТОЛЬКО на общей выборке — матчах, где есть
    и прогноз, и цена. Раньше Брайер модели считался по всем матчам, а
    Брайер рынка по тем немногим, где линия успела открыться, и строка
    «точнее модель» сравнивала триста матчей с тремя. При большой разнице
    в числе матчей это не сравнение, а лотерея.

    Поля: n/hit/brier — модель на всех матчах; both/hit_both/brier_both
    против mkt_hit/mkt_brier — те же матчи, что и у рынка.
    """
    start = _period_start(period)
    rows = _read(LOG_CSV, LOG_FIELDS)
    out = {"n": 0, "hit": 0, "brier": 0.0,
           "both": 0, "hit_both": 0, "brier_both": 0.0,
           "mkt_hit": 0, "mkt_brier": 0.0}
    for r in rows:
        d = _as_date(r.get("resolved_at") or "")
        if not d or (start and d < start):
            continue
        winner = r.get("winner")
        if winner not in ("p1", "p2"):
            continue
        from .results import UNFINISHED_RE  # noqa: PLC0415
        if UNFINISHED_RE.search(r.get("score") or ""):
            # Снятие и неявка в точность не идут: победителя там определил
            # не теннис, а травма, и «модель угадала» ничего не значит.
            continue
        sim = _f(r.get("sim_p1"), -1)
        if not 0 <= sim <= 1:
            continue
        actual = 1.0 if winner == "p1" else 0.0
        out["n"] += 1
        out["hit"] += int((sim >= 0.5) == (actual == 1.0))
        # Брайер: средний квадрат ошибки вероятности. Ниже — лучше;
        # 0.25 это уровень «всегда говорить 50/50»
        out["brier"] += (sim - actual) ** 2

        mkt = _f(r.get("mkt_implied_p1"), -1)
        if not 0 <= mkt <= 1:
            continue
        out["both"] += 1
        out["hit_both"] += int((sim >= 0.5) == (actual == 1.0))
        out["brier_both"] += (sim - actual) ** 2
        out["mkt_hit"] += int((mkt >= 0.5) == (actual == 1.0))
        out["mkt_brier"] += (mkt - actual) ** 2
    if out["n"]:
        out["brier"] /= out["n"]
    if out["both"]:
        out["brier_both"] /= out["both"]
        out["mkt_brier"] /= out["both"]
    # прежнее имя поля — чтобы не сломать сторонних читателей
    out["mkt_n"] = out["both"]
    return out


def collect_picks(period: str) -> dict:
    """Ставки на исход: всего, и отдельно там, где модель спорит с рынком.

    Разрез «согласна / против рынка» — главный. Если модель зарабатывает
    только там, где согласна с букмекером, она не добавляет ничего: тот же
    результат даст ставка на фаворита без всякой модели. Ценность начинается
    там, где она спорит и оказывается права.
    """
    start = _period_start(period)
    boxes = {k: {"n": 0, "settled": 0, "win": 0, "void": 0,
                 "stake": 0.0, "profit": 0.0, "edge": 0.0}
             for k in ("all", "agree", "against")}
    pending = 0
    baseline = {"n": 0, "settled": 0, "win": 0, "void": 0,
                "stake": 0.0, "profit": 0.0}

    for r in _read(PICKS_CSV, PICK_FIELDS):
        status = r.get("status") or "pending"
        if status in ("pending", ""):
            pending += 1
            continue
        d = _as_date(r.get("resolved_at") or "")
        if start and (not d or d < start):
            continue
        stake = _f(r.get("stake"), 0.0)
        odds = _f(r.get("odds"), 0.0)
        won = status == "win"
        # Недоигранный матч — возврат: он не выигран и не проигран, поэтому
        # не входит ни в оборот, ни в знаменатель процента захода.
        void = status in ("refund", "push")
        for key in ("all", "agree" if r.get("agree") == "да" else "against"):
            b = boxes[key]
            b["n"] += 1
            if void:
                b["void"] += 1
                continue
            b["settled"] += 1
            b["win"] += int(won)
            b["stake"] += stake
            b["profit"] += _f(r.get("profit"), 0.0)
            b["edge"] += _f(r.get("edge"), 0.0)
        if void:
            baseline["n"] += 1
            baseline["void"] += 1
            continue

        # Что было бы, если ставить просто на фаворита рынка. Когда модель
        # согласна — та же ставка; когда спорит — противоположная.
        if r.get("agree") == "да":
            b_odds, b_won = odds, won
        else:
            b_odds, b_won = _other_side_odds(r, odds), not won
        if b_odds > 1:
            baseline["n"] += 1
            baseline["settled"] += 1
            baseline["win"] += int(b_won)
            baseline["stake"] += stake
            baseline["profit"] += stake * (b_odds - 1) if b_won else -stake

    return {"boxes": boxes, "pending": pending, "baseline": baseline}


def _other_side_odds(row: dict, our_odds: float) -> float:
    """Реальная цена противоположной стороны.

    Сначала берём сохранённый кэф. Для старых строк, где записана только
    своя цена, восстанавливаем чужую С ТОЙ ЖЕ МАРЖОЙ:

        p_наша_норм = 1 - market_prob,  1/odds = p_наша_норм·(1+маржа)
        => чужой кэф = odds · (1 - market_prob) / market_prob

    Раньше здесь стояло 1/market_prob — справедливая цена без маржи. Она
    всегда выше настоящей, и «просто фаворит рынка» получал кэфы, которых
    в линии не бывает: его ROI завышался на всю маржу Pinnacle, а модель
    на этом фоне выглядела хуже, чем есть.
    """
    side = row.get("side")
    stored = _f(row.get("odds_p2" if side == "П1" else "odds_p1"), 0.0)
    if stored > 1:
        return stored
    mkt_prob = _f(row.get("market_prob"), 0.0)
    if not (0 < mkt_prob < 1) or our_odds <= 1:
        return 0.0
    return our_odds * (1 - mkt_prob) / mkt_prob


def format_picks(period: str) -> str:
    d = collect_picks(period)
    a = d["boxes"]["all"]
    if not a["settled"]:
        return ""
    lines = ["", "<b>Ставки на исход</b>",
             "<i>На фаворита модели, независимо от перевеса — чистая мера "
             "того, права она или нет.</i>", "<pre>"]
    lw = 13  # «фаворит рынка» — 13 знаков, при 12 колонки съезжали
    lines.append(f"{'':<{lw}}{'ст.':>4}{'зашло':>8}{'ROI':>7}")
    lines.append("-" * (lw + 19))
    for key, name in (("all", "Всего"), ("agree", "с рынком"),
                      ("against", "против")):
        b = d["boxes"][key]
        if not b["settled"]:
            continue
        lines.append(f"{name:<{lw}}{b['settled']:>4}{b['win']:>4} "
                     f"{b['win'] / b['settled'] * 100:>3.0f}%{_roi(b):>6.0f}%")
    base = d["baseline"]
    if base["settled"]:
        lines.append("-" * (lw + 19))
        lines.append(f"{'фаворит рынка':<{lw}}{base['settled']:>4}"
                     f"{base['win']:>4} "
                     f"{base['win'] / base['settled'] * 100:>3.0f}%"
                     f"{_roi(base):>6.0f}%")
    lines.append("</pre>")

    if d["pending"]:
        lines.append(f"В ожидании: {d['pending']}")

    if a["void"]:
        lines.append(f"Возвратов (матч не доигран): {a['void']}")

    against = d["boxes"]["against"]
    if against["settled"] >= 10:
        verdict = ("модель находит то, чего не видит рынок"
                   if _roi(against) > 0 else
                   "спорить с рынком пока в минус")
        lines.append(f"<i>Там, где модель спорит с рынком: "
                     f"{against['settled']} ставок, ROI {_roi(against):+.0f}% — "
                     f"{verdict}. Это главная цифра: совпадения с рынком "
                     f"ничего не доказывают, их даёт и ставка на фаворита "
                     f"без всякой модели.</i>")
    elif against["settled"]:
        lines.append(f"<i>Против рынка пока только {against['settled']} ставок — "
                     "рано о чём-то судить.</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ вывод
def _roi(box: dict) -> float:
    return (box["profit"] / box["stake"] * 100) if box["stake"] else 0.0


def format_report(period: str) -> str:
    data = collect(period)
    acc = model_accuracy(period)
    t = data["total"]
    title = PERIODS.get(period, period)

    if not t["n"]:
        tail = (f"\n<i>в ожидании результата: {data['pending']}</i>"
                if data["pending"] else "")
        return f"📊 <b>Отчёт {title}</b>\nРассчитанных ставок нет.{tail}"

    lines = [f"📊 <b>Отчёт {title}</b>", ""]

    rows = []
    for market, box in sorted(data["by_market"].items(),
                              key=lambda kv: -kv[1]["profit"]):
        rows.append((market[:12], box))
    rows.append(("ИТОГО", t))

    # ширина как в симуляции: Telegraph на телефоне переносит всё длиннее ~24
    lw = min(max(len(r[0]) for r in rows), 11)
    lines.append("<pre>")
    lines.append(f"{'':<{lw}}{'ст.':>5}{'зашло':>8}")
    lines.append("-" * (lw + 13))
    for name, box in rows:
        # знаменатель — сыгранные, без возвратов: возврат не проигрыш
        settled = box.get("settled", box["n"])
        pct = box["win"] / settled * 100 if settled else 0
        lines.append(f"{name[:lw]:<{lw}}{settled:>5}{box['win']:>4} {pct:>3.0f}%")
    lines.append("")
    lines.append(f"{'':<{lw}}{'прибыль':>7}{'ROI':>6}")
    lines.append("-" * (lw + 13))
    for name, box in rows:
        # ROI бывает -100%, это пять знаков со скобкой процента — при
        # ширине 4 колонки слипались в «-1000-100%»
        lines.append(f"{name[:lw]:<{lw}}{box['profit']:>+7.0f}{_roi(box):>5.0f}%")
    lines.append("</pre>")

    lines.append(f"Оборот: {t['stake']:,.0f} ₽".replace(",", " "))
    if t["push"]:
        lines.append(f"Возвратов: {t['push']}")
    if data["pending"]:
        lines.append(f"В ожидании результата: {data['pending']}")

    if acc["n"]:
        lines.append("")
        lines.append(f"<b>Точность модели</b> ({acc['n']} матчей)")
        lines.append(f"Угадан победитель: {acc['hit']}/{acc['n']} "
                     f"({acc['hit'] / acc['n']:.0%})")
        if acc["both"]:
            # Всё, что ниже, — только по матчам, где была и цена рынка:
            # сравнивать модель на всей афише с рынком на её обрывке нельзя
            lines.append(f"<i>Сравнение с рынком — на общих {acc['both']} "
                         f"матчах, где линия была:</i>")
            lines.append(f"Модель {acc['hit_both']}/{acc['both']} "
                         f"({acc['hit_both'] / acc['both']:.0%}) · "
                         f"рынок {acc['mkt_hit']}/{acc['both']} "
                         f"({acc['mkt_hit'] / acc['both']:.0%})")
            better = ("модель" if acc["brier_both"] < acc["mkt_brier"]
                      else "рынок")
            lines.append(f"Брайер: модель {acc['brier_both']:.3f} · "
                         f"рынок {acc['mkt_brier']:.3f} → точнее <b>{better}</b>")
            if acc["both"] < 30:
                lines.append("<i>Матчей с линией мало — вывод пока ни о чём.</i>")
            lines.append("<i>Брайер — средний квадрат ошибки вероятности, "
                         "меньше лучше. 0.25 = уровень «всегда 50/50».</i>")

    picks_block = format_picks(period)
    if picks_block:
        lines.append(picks_block)

    avg_edge = t["edge"] / t["settled"] if t["settled"] else 0
    lines.append("")
    lines.append(f"<i>Средний заявленный перевес: {avg_edge:+.1%}. "
                 f"Фактический ROI: {_roi(t):+.0f}%. "
                 "Расхождение между ними и есть мера того, насколько модель "
                 "себе льстит.</i>")
    return "\n".join(lines)


def export_csv(period: str, path: str | None = None) -> str | None:
    """Выгрузка ставок периода в CSV со сводкой снизу, как в старом боте."""
    start = _period_start(period)
    rows = [r for r in _read(VALUE_CSV, VALUE_FIELDS)
            if (r.get("status") not in ("pending", ""))
            and (not start or ((_as_date(r.get("resolved_at") or "") or date.min) >= start))]
    if not rows:
        return None

    path = path or os.path.join(
        os.path.dirname(VALUE_CSV),
        f"report_{period}_{datetime.now():%Y%m%d}.csv")
    head = ["Турнир", "Событие", "Прогноз", "Кэф", "Модель %", "Перевес",
            "Ставка", "Прибыль", "Счёт", "Сеты", "Сумма геймов",
            "Разница геймов", "Статус"]
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            # Числа пишем с запятой: иначе Excel в русской локали читает
            # 1.787 как дату «фев.23», а 2.5 как «02.май»
            def dec(v):
                return str(v).replace(".", ",")

            w.writerow(head)
            for r in rows:
                line = "" if r.get("line") in ("", None) else f" {r['line']}"
                w.writerow([
                    r.get("tournament", ""), f"{r.get('p1')} - {r.get('p2')}",
                    f"{r.get('market')} {r.get('pick')}{line}",
                    dec(r.get("odds")), f"{_f(r.get('sim_prob')) * 100:.1f}%".replace(".", ","),
                    f"{_f(r.get('edge')) * 100:+.1f}%".replace(".", ","),
                    dec(r.get("stake")), dec(r.get("profit")), r.get("score"),
                    f"{r.get('sets_p1')}-{r.get('sets_p2')}",
                    r.get("games_total"), r.get("games_diff"),
                    r.get("status"),
                ])
            data = collect(period)
            w.writerow([])
            w.writerow([f"СТАТИСТИКА {PERIODS.get(period, period).upper()}"])
            w.writerow(["Тип ставки", "Ставок", "Выигрышей", "Проигрышей",
                        "Оборот", "Прибыль", "ROI"])
            for market, box in sorted(data["by_market"].items()):
                w.writerow([market, box["settled"], box["win"], box["loss"],
                            dec(f"{box['stake']:.0f}"),
                            dec(f"{box['profit']:+.2f}"),
                            dec(f"{_roi(box):.2f}") + "%"])
            t = data["total"]
            w.writerow(["ИТОГО", t["settled"], t["win"], t["loss"],
                        dec(f"{t['stake']:.0f}"), dec(f"{t['profit']:+.2f}"),
                        dec(f"{_roi(t):.2f}") + "%"])
    except OSError as exc:
        log.error("выгрузка не записана: %s", exc)
        return None
    return path
