"""Сравнение кэфов симуляции с линией Pinnacle.

Как считается ценность
----------------------
Перевес (edge) = p_модели × кэф − 1. Положительный означает, что по нашей
оценке ставка окупается. Ноль — безубыток, и это НЕ повод ставить: наша
оценка сама по себе шумная.

Отдельно считается margin — маржа букмекера по паре исходов. Она нужна как
проверка вменяемости: у Pinnacle на теннис обычно 2-4%, и если по нашим
расчётам вышло 20% или отрицательное значение, значит цены собраны из разных
рынков и сравнивать их с моделью нельзя.

Чего эта штука не делает
------------------------
Она не знает, права ли модель. Штрафы за усталость и вес Elo взяты
эвристически и на данных не калибровались, так что «перевес 12%» означает
«модель считает, что перевес 12%», а не «перевес есть». Для того и пишется
журнал всех исходов: через сотню-другую матчей станет видно, окупаются ли
найденные value-ставки на самом деле.
"""

from __future__ import annotations

import logging
import os
import re

from tennis_parser.simulation import (prob_games_handicap, prob_sets_handicap,
                                      prob_total_games, prob_total_sets)

log = logging.getLogger(__name__)


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# минимальный перевес, чтобы считать ставку ценной
MIN_EDGE = _f("TRA_MIN_EDGE", 0.05)
# Потолок перевеса. Всё выше — не находка, а поломка.
#
# Pinnacle держит на теннисе одну из самых острых линий в мире. Перевес в
# 20% над ней означал бы, что мы видим то, чего не видит рынок с миллионными
# оборотами; перевес в 70% не означает вообще ничего, кроме ошибки в модели.
# Замер 23.08.2026 по 114 ставкам: медиана +17%, максимум +196% («Games Hcap
# П2 4.5» при цене 3.62 — модель дала 82% там, где рынок даёт 28%).
#
# Порог намеренно щедрый: настоящие перевесы в теннисе исчисляются единицами
# процентов, и 25% уже далеко за пределами правдоподобного. Смысл не в тонкой
# настройке, а в том, чтобы любая будущая поломка модели не выливалась в
# «находки» на плюс двести процентов.
MAX_EDGE = _f("TRA_MAX_EDGE", 0.25)
# Сколько ставок брать с одного матча. Девять ставок по Yevseyev — Basic
# выражали одну и ту же мысль («второй сильно недооценён»): если она неверна,
# проигрывают все девять разом. Келли считает долю банка как для независимых
# событий, а они здесь почти полностью связаны, поэтому суммарная экспозиция
# на один матч выходила девятикратной. Берём только лучшие по перевесу.
MAX_BETS_PER_MATCH = int(_f("TRA_MAX_BETS_PER_MATCH", 3))
# кэфы вне этого коридора не рассматриваем: на очень низких перевес съедается
# погрешностью модели, на очень высоких выборка симуляции слишком тонкая
MIN_ODDS = _f("TRA_MIN_ODDS", 1.30)
MAX_ODDS = _f("TRA_MAX_ODDS", 6.00)
# доля банка по Келли, урезанная: полный Келли на некалиброванной модели
# разоряет быстрее, чем обогащает
KELLY_FRACTION = _f("TRA_KELLY_FRACTION", 0.25)
# допустимая маржа пары исходов
MARGIN_MAX = _f("TRA_MARGIN_MAX", 0.12)


# ------------------------------------------------------------------ разбор
_PAIR = re.compile(r"(П1|П2|ТБ|ТМ)\s*([+-]?\d+(?:\.\d+)?)\s*\(([\d.]+)\)")


def parse_line(text: str) -> list[tuple[str, float, float]]:
    """'П1 -1.5 (1.85) | П2 +1.5 (1.95)' -> [('П1', -1.5, 1.85), ...]"""
    if not text or text == "-":
        return []
    return [(m.group(1), float(m.group(2)), float(m.group(3)))
            for m in _PAIR.finditer(text)]


def _num(v):
    """Кэф из чего угодно: числа, строки с точкой или с запятой.

    Запятая обязательна: в CSV числа пишутся с ней ради Excel, и без этой
    поддержки любое чтение кэфов из журнала молча возвращало None.
    """
    if v in (None, "", "-"):
        return None
    try:
        f = float(str(v).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 else None


def market_margin(o1: float | None, o2: float | None) -> float | None:
    if not o1 or not o2:
        return None
    return 1 / o1 + 1 / o2 - 1


# ------------------------------------------------------------------ поиск
def find_value(sim: dict, odds: dict, *, min_edge: float = None) -> list[dict]:
    """Все ставки с перевесом выше порога.

    Смотрим четыре рынка: исход, тотал сетов, фора по сетам, фора по геймам.
    Тотала геймов у Pinnacle в нашем разборе нет — он туда не попадает.
    """
    if not sim or not odds:
        return []
    min_edge = MIN_EDGE if min_edge is None else min_edge
    m = sim["headline"]
    out: list[dict] = []

    def add(market: str, pick: str, line, p: float, price: float, margin=None):
        if not (MIN_ODDS <= price <= MAX_ODDS):
            return
        if margin is not None and not (-0.005 <= margin <= MARGIN_MAX):
            log.info("пропускаю %s %s: маржа %.1f%% вне нормы",
                     market, pick, margin * 100)
            return
        edge = p * price - 1
        if edge < min_edge:
            return
        if MAX_EDGE and edge > MAX_EDGE:
            # Не тихий пропуск: такой перевес — сигнал, что сломана модель,
            # и это стоит видеть в логе, а не молча недосчитываться ставок.
            log.warning("пропускаю %s %s %s: перевес %.0f%% выше потолка %.0f%% "
                        "— это ошибка модели, а не ценность (p=%.0f%%, кэф %.2f)",
                        market, pick, "" if line is None else f"{line:+g}",
                        edge * 100, MAX_EDGE * 100, p * 100, price)
            return
        # Келли: доля банка = (p*k - 1) / (k - 1), урезанная коэффициентом
        kelly = max(0.0, (p * price - 1) / (price - 1)) * KELLY_FRACTION
        out.append({
            "market": market, "pick": pick, "line": line,
            "odds": round(price, 3), "sim_prob": round(p, 4),
            "fair_odds": round(1 / p, 3) if p > 0 else None,
            "edge": round(edge, 4), "kelly": round(kelly, 4),
        })

    # --- исход
    o1, o2 = _num(odds.get("p1")), _num(odds.get("p2"))
    margin = market_margin(o1, o2)
    if o1:
        add("Moneyline", "П1", None, m["p1_win"], o1, margin)
    if o2:
        add("Moneyline", "П2", None, m["p2_win"], o2, margin)

    # --- тотал сетов
    tb = {p: (ln, pr) for p, ln, pr in parse_line(odds.get("total_sets", ""))}
    for pick, over in (("ТБ", True), ("ТМ", False)):
        if pick in tb:
            ln, pr = tb[pick]
            other = tb.get("ТМ" if over else "ТБ")
            mg = market_margin(pr, other[1]) if other and other[0] == ln else None
            add("Total Sets", pick, ln, prob_total_sets(m, ln, over), pr, mg)

    # --- фора по сетам
    for pick, ln, pr in parse_line(odds.get("h_sets", "")):
        if pick not in ("П1", "П2"):
            continue
        p = prob_sets_handicap(m, ln, for_p1=(pick == "П1"))
        add("Sets Hcap", pick, ln, p, pr)

    # --- фора по геймам
    for pick, ln, pr in parse_line(odds.get("h_games", "")):
        if pick not in ("П1", "П2"):
            continue
        p = prob_games_handicap(m, ln, for_p1=(pick == "П1"))
        add("Games Hcap", pick, ln, p, pr)

    out.sort(key=lambda b: -b["edge"])
    if MAX_BETS_PER_MATCH and len(out) > MAX_BETS_PER_MATCH:
        log.info("матч дал %d ставок — оставляю %d лучших по перевесу "
                 "(они выражают одну и ту же мысль и проигрывают вместе)",
                 len(out), MAX_BETS_PER_MATCH)
        out = out[:MAX_BETS_PER_MATCH]
    return out


def describe(bet: dict) -> str:
    """'Total Sets ТБ 2.5' — как это показать человеку."""
    line = "" if bet.get("line") is None else f" {bet['line']:g}"
    if bet["market"] in ("Sets Hcap", "Games Hcap") and bet.get("line") is not None:
        line = f" {bet['line']:+g}"
    return f"{bet['market']} {bet['pick']}{line}"


# ------------------------------------------------------------------ расчёт
def settle(bet: dict, sets_p1: int, sets_p2: int,
           games_p1: int, games_p2: int, retired: bool = False,
           winner: str = "") -> str:
    """Итог ставки по счёту: win / loss / push / refund.

    Снятие считается по правилам Pinnacle, а они разные по рынкам:
      * ставка на победителя СТОИТ, если доигран хотя бы один полный сет —
        снявшийся объявляется проигравшим независимо от счёта;
      * форы и тоталы (и по геймам, и по сетам) аннулируются ВСЕГДА.
    Условие про сет проверено раньше: `winner` приходит пустым, если снятие
    случилось до конца первого сета.
    """
    if retired:
        if bet["market"] == "Moneyline" and winner in ("p1", "p2"):
            won = (winner == "p1") == (bet["pick"] == "П1")
            return "win" if won else "loss"
        return "refund"
    market, pick, line = bet["market"], bet["pick"], bet.get("line")

    if market == "Moneyline":
        won = (sets_p1 > sets_p2) if pick == "П1" else (sets_p2 > sets_p1)
        return "win" if won else "loss"

    if market == "Total Sets":
        total = sets_p1 + sets_p2
        if line is None:
            return "push"
        if total == line:
            return "push"
        over = total > line
        return "win" if (over == (pick == "ТБ")) else "loss"

    if market in ("Sets Hcap", "Games Hcap"):
        if line is None:
            return "push"
        a, b = ((sets_p1, sets_p2) if market == "Sets Hcap"
                else (games_p1, games_p2))
        margin = (a - b) if pick == "П1" else (b - a)
        adj = margin + line
        if abs(adj) < 1e-9:
            return "push"
        return "win" if adj > 0 else "loss"

    return "push"


def profit(bet: dict, status: str, stake: float) -> float:
    if status == "win":
        return stake * (bet["odds"] - 1)
    if status == "loss":
        return -stake
    return 0.0
