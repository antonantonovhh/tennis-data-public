"""Монте-Карло симуляция матча поверх собранного отчёта.

Работает с тем, что уже вернул build_report(): блоки сравнения (подача/приём),
Elo-прогноз и усталость. Никаких новых сетевых запросов — чистый счёт.

Почему четыре модели, а не одна
-------------------------------
Стата подачи/приёма и Elo регулярно расходятся, и расходятся сильно: игрок
может иметь отличные проценты на приёме, набранные против слабой сетки, при
Elo на 400 пунктов ниже соперника. Одна усреднённая цифра прячет это
расхождение, поэтому считаются обе крайности и два бленда между ними.

  1. stats  — только показатели подачи/приёма (52 недели, нужное покрытие)
  2. elo    — SPW подогнан так, чтобы вероятность матча совпала с Elo-прогнозом
  3. blend  — 50/50 + штраф за усталость
  4. work   — рабочая: TP_SIM_ELO_WEIGHT (по умолчанию 0.7) на Elo + усталость

Механика
--------
Гейм считается аналитически (замкнутая формула), тайбрейк — динамическим
программированием, Монте-Карло крутится только на уровне геймов и сетов.
Это на два порядка быстрее поточечной симуляции при том же распределении:
10 000 матчей на четыре модели укладываются в доли секунды, что важно, раз
всё это висит на кнопке в телеграме.

Все числа — модель, а не пророчество. Штрафы за усталость — эвристика,
калибруйте под свою выборку.
"""

from __future__ import annotations

import html as _html
import logging
import os
import random
from functools import lru_cache

log = logging.getLogger(__name__)

# средний выигрыш очка на своей подаче — база для коррекции «серв против приёма»
TOUR_AVG_SPW = {"hard": 0.635, "clay": 0.610, "grass": 0.655, None: 0.625}

# штраф уставшему по сетам, п.п. SPW, при разнице усталости 60 пунктов
FATIGUE_PENALTY_BY_SET = (0.010, 0.018, 0.030, 0.040, 0.050)
# доля штрафа, которая возвращается свежему как бонус на его подаче
FATIGUE_BONUS_SHARE = 0.35
# разница усталости, на которую откалиброваны штрафы выше
FATIGUE_REFERENCE_DELTA = 60.0
# потолок множителя: разница 100 пунктов не должна давать -5 п.п. в первом сете
FATIGUE_MAX_SCALE = 1.5

DEFAULT_RUNS = 10_000
DEFAULT_ELO_WEIGHT = 0.7

# Названия короткие намеренно: таблицы читают с телефона, а всё шире
# ~32 символов там переносится и разъезжается. Пометка * = учтена усталость.
# Ширина всех моноширинных блоков. Telegraph на телефоне показывает <pre>
# заметно более крупным шрифтом, чем чат Telegram: 31 символ там уже
# переносится и таблица разъезжается. 24 — измеренный предел.
TABLE_W = 24

MODEL_TITLES = {
    "stats": "Стата",
    "elo": "Elo",
    "blend": "Бленд *",
    "work": "Рабочая *",
}


# ------------------------------------------------------------------ механика
def game_win_prob(p: float) -> float:
    """Вероятность выиграть свой гейм при вероятности очка p.

    Сумма исходов до деюса плюс деюс:
        40-0   p^4
        40-15  4 p^4 q
        40-30  10 p^4 q^2
        деюс   20 p^3 q^3 * p^2/(p^2 + q^2)
    Проверка: p=0.5 даёт ровно 0.5.
    """
    p = min(max(p, 1e-6), 1 - 1e-6)
    q = 1.0 - p
    straight = p ** 4 * (1 + 4 * q + 10 * q * q)
    deuce = 20 * p ** 3 * q ** 3 * (p * p / (p * p + q * q))
    return straight + deuce


def _tb_prob(pa: float, pb: float, target: int = 7) -> float:
    """Вероятность, что тайбрейк выиграет игрок A, подающий первое очко.

    Динамика по состояниям (a, b): кто подаёт, однозначно определяется числом
    сыгранных очков — первое очко подаёт A, дальше по два.
    """
    memo: dict[tuple[int, int], float] = {}

    def server_is_a(points: int) -> bool:
        # очко 0 — A; очки 1,2 — B; 3,4 — A; 5,6 — B ...
        return ((points + 1) // 2) % 2 == 0

    def rec(a: int, b: int) -> float:
        if a >= target and a - b >= 2:
            return 1.0
        if b >= target and b - a >= 2:
            return 0.0
        if a + b > 80:  # практически недостижимо, страховка от рекурсии
            return 0.5
        key = (a, b)
        if key in memo:
            return memo[key]
        p_srv = pa if server_is_a(a + b) else pb
        # вероятность, что очко берёт A
        p_a = p_srv if server_is_a(a + b) else 1 - p_srv
        val = p_a * rec(a + 1, b) + (1 - p_a) * rec(a, b + 1)
        memo[key] = val
        return val

    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 5000))
    try:
        return rec(0, 0)
    finally:
        sys.setrecursionlimit(old)


@lru_cache(maxsize=4096)
def _tb_prob_cached(pa_r: int, pb_r: int, target: int) -> float:
    return _tb_prob(pa_r / 10000.0, pb_r / 10000.0, target)


def tiebreak_win_prob(pa: float, pb: float, target: int = 7) -> float:
    """Кэширующая обёртка: pa/pb округляются до 0.01 п.п."""
    return _tb_prob_cached(int(round(pa * 10000)), int(round(pb * 10000)), target)


def _play_set(hold_a: float, hold_b: float, tb_a_first: float, tb_b_first: float,
              a_serves: bool, rnd) -> tuple[int, int, int]:
    """Один сет на уровне геймов. Возвращает (победил A, геймы A, геймы B).

    На 6-6 первым в тайбрейке подаёт тот, чья очередь была подавать 13-й гейм,
    поэтому нужны обе вероятности — сценарии несимметричны.
    """
    a = b = 0
    while True:
        if a == 6 and b == 6:
            p_tb = tb_a_first if a_serves else tb_b_first
            return (1, 7, 6) if rnd.random() < p_tb else (0, 6, 7)
        hold = rnd.random() < (hold_a if a_serves else hold_b)
        if a_serves:
            a += 1 if hold else 0
            b += 0 if hold else 1
        else:
            b += 1 if hold else 0
            a += 0 if hold else 1
        a_serves = not a_serves
        if a >= 6 and a - b >= 2:
            return 1, a, b
        if b >= 6 and b - a >= 2:
            return 0, a, b


def tiebreak_win_prob_receiving(pa: float, pb: float, target: int = 7) -> float:
    """Вероятность победы A в тайбрейке, когда первое очко подаёт B.

    Численно совпадает с tiebreak_win_prob(pa, pb): порядок 1-2-2-2 выровнен,
    после каждого чётного числа очков оба подали поровну, поэтому право первой
    подачи преимущества не даёт. Это не дубль по ошибке — функция оставлена
    отдельно, потому что для нестандартных тайбрейков (до 10) равенство уже
    не гарантировано, а вызывающий код не должен об этом думать.
    """
    return 1 - tiebreak_win_prob(pb, pa, target)


# ------------------------------------------------------------------ вход
def _pct(cmp_block: dict, key: str, who: str):
    node = (cmp_block or {}).get(key)
    if not node:
        return None
    val = node.get(who)
    return None if val is None else val / 100.0


def extract_inputs(report: dict) -> dict:
    """Достаёт из отчёта всё, что нужно счётчику. Отсутствующее — None."""
    h = report.get("h2h") or {}
    cmp_data = h.get("comparison") or {}
    serve = cmp_data.get("serve") or {}
    ret = cmp_data.get("return") or {}
    fc = report.get("elo_forecast") or {}
    fat = report.get("fatigue") or {}
    f1 = fat.get("p1") or {}
    f2 = fat.get("p2") or {}

    def side(who):
        return {
            "first_in": _pct(serve, "first_serve_accuracy", who),
            "w1": _pct(serve, "first_serve_points_won", who),
            "w2": _pct(serve, "second_serve_points_won", who),
            "r1": _pct(ret, "return_first_serve_points", who),
            "r2": _pct(ret, "return_second_serve_points", who),
            "hold": _pct(serve, "service_games_won", who),
        }

    return {
        "p1_name": (h.get("player1") or {}).get("name") or "Игрок 1",
        "p2_name": (h.get("player2") or {}).get("name") or "Игрок 2",
        "p1": side("p1"),
        "p2": side("p2"),
        "surface": fc.get("surface") or (cmp_data.get("_surface") if cmp_data.get("_surface") != "all" else None),
        "elo_p1_prob": fc.get("p1_win_prob"),
        "best_of": fc.get("best_of") or 3,
        "fatigue_p1": f1.get("fatigue_score"),
        "fatigue_p2": f2.get("fatigue_score"),
        "cmp_surface": cmp_data.get("_surface"),
    }


def _raw_spw(side: dict):
    if side["first_in"] is None or side["w1"] is None or side["w2"] is None:
        return None
    return side["first_in"] * side["w1"] + (1 - side["first_in"]) * side["w2"]


def _rpw_vs(ret_side: dict, srv_side: dict):
    """Приёмный процент против конкретной раскладки подачи соперника."""
    if ret_side["r1"] is None or ret_side["r2"] is None or srv_side["first_in"] is None:
        return None
    return srv_side["first_in"] * ret_side["r1"] + (1 - srv_side["first_in"]) * ret_side["r2"]


def stats_spw(inp: dict) -> tuple[float, float] | None:
    """SPW обоих игроков с поправкой на качество приёма соперника.

    Классическая коррекция Klaassen–Magnus: свой процент на подаче плюс
    настолько, насколько приём соперника слабее среднего по туру.
    """
    s1, s2 = _raw_spw(inp["p1"]), _raw_spw(inp["p2"])
    if s1 is None or s2 is None:
        return None
    r1 = _rpw_vs(inp["p1"], inp["p2"])  # как p1 принимает подачу p2
    r2 = _rpw_vs(inp["p2"], inp["p1"])
    avg = TOUR_AVG_SPW.get(inp.get("surface"), TOUR_AVG_SPW[None])
    if r1 is None or r2 is None:
        return s1, s2
    return s1 + ((1 - avg) - r2), s2 + ((1 - avg) - r1)


# ------------------------------------------------------------------ прогон
def _match_prob(spw1: float, spw2: float, best_of: int, runs: int, seed: int) -> float:
    """Быстрая оценка вероятности матча — нужна для подгонки под Elo."""
    res = run_matches([spw1] * 5, [spw2] * 5, best_of=best_of, runs=runs, seed=seed)
    return res["p1_win"]


def solve_spw_for_prob(target_p1: float, base: float, best_of: int,
                       seed: int = 4242, runs: int = 4000) -> tuple[float, float]:
    """Подбирает симметричную дельту SPW, дающую нужную вероятность матча.

    База (полусумма SPW) сохраняется — меняется только разрыв, иначе вместе с
    вероятностью уехали бы тоталы: матч из одних брейков и матч из одних
    холдов дают разное число геймов при той же вероятности победы.
    """
    target_p1 = min(max(target_p1, 0.001), 0.999)
    lo, hi = -0.25, 0.25   # дельта в пользу p1
    for _ in range(24):
        mid = (lo + hi) / 2
        p = _match_prob(base + mid, base - mid, best_of, runs, seed)
        if p < target_p1:
            lo = mid
        else:
            hi = mid
    d = (lo + hi) / 2
    return base + d, base - d


def run_matches(spw1_by_set, spw2_by_set, *, best_of: int = 3,
                runs: int = DEFAULT_RUNS, seed: int | None = None) -> dict:
    """Монте-Карло. spw*_by_set — список SPW по номеру сета (хватит длины best_of)."""
    rnd = random.Random(seed)
    max_sets = best_of
    need = best_of // 2 + 1

    holds1 = [game_win_prob(p) for p in spw1_by_set[:max_sets]]
    holds2 = [game_win_prob(p) for p in spw2_by_set[:max_sets]]
    tb_a = [tiebreak_win_prob(spw1_by_set[i], spw2_by_set[i]) for i in range(max_sets)]
    tb_b = [tiebreak_win_prob_receiving(spw1_by_set[i], spw2_by_set[i]) for i in range(max_sets)]

    wins = 0
    score_counts: dict[str, int] = {}
    games_total = [0] * runs
    # геймы по игрокам: без них нельзя оценить фору по геймам, а именно там
    # у Pinnacle обычно самая ходовая линия
    games_diff = [0] * runs
    set_wins_by_index = [0] * max_sets
    sets_played_hist: dict[int, int] = {}
    p1_sets_hist: dict[int, int] = {}
    first_set_then_win = [0, 0]  # выиграл 1-й сет и матч, выиграл 1-й сет

    for i in range(runs):
        s1 = s2 = 0
        total = 0
        g1 = g2 = 0
        won_first = None
        for idx in range(max_sets):
            a_serves = rnd.random() < 0.5
            w, ga, gb = _play_set(holds1[idx], holds2[idx],
                                  tb_a[idx], tb_b[idx], a_serves, rnd)
            total += ga + gb
            g1 += ga
            g2 += gb
            if w:
                s1 += 1
                set_wins_by_index[idx] += 1
            else:
                s2 += 1
            if idx == 0:
                won_first = bool(w)
            if s1 == need or s2 == need:
                break
        wins += 1 if s1 == need else 0
        key = f"{s1}-{s2}"
        score_counts[key] = score_counts.get(key, 0) + 1
        games_total[i] = total
        games_diff[i] = g1 - g2
        sets_played_hist[s1 + s2] = sets_played_hist.get(s1 + s2, 0) + 1
        p1_sets_hist[s1] = p1_sets_hist.get(s1, 0) + 1
        if won_first:
            first_set_then_win[1] += 1
            if s1 == need:
                first_set_then_win[0] += 1

    diff_sorted = sorted(games_diff)
    games_total.sort()

    def pct(x):
        return x / runs

    def quant(q):
        return games_total[min(int(q * runs), runs - 1)]

    mean_games = sum(games_total) / runs
    return {
        "runs": runs,
        "best_of": best_of,
        "p1_win": pct(wins),
        "p2_win": 1 - pct(wins),
        "scores": {k: pct(v) for k, v in sorted(score_counts.items(), reverse=True)},
        "sets_played": {k: pct(v) for k, v in sorted(sets_played_hist.items())},
        "p1_sets": {k: pct(v) for k, v in sorted(p1_sets_hist.items())},
        "games_mean": mean_games,
        "games_median": games_total[runs // 2],
        "games_p05": quant(0.05),
        "games_p95": quant(0.95),
        "set_win_by_index": [pct(x) for x in set_wins_by_index],
        "p1_first_set": pct(first_set_then_win[1]),
        "p1_win_given_first_set": (first_set_then_win[0] / first_set_then_win[1]
                                   if first_set_then_win[1] else None),
        "_games": games_total,
        "_games_diff": diff_sorted,
        "games_diff_mean": sum(games_diff) / runs,
        "spw1": spw1_by_set[0],
        "spw2": spw2_by_set[0],
    }


def totals(res: dict, lines=None, span: int = 2) -> list[tuple[float, float, float]]:
    """[(линия, доля «больше», доля «меньше»)].

    Без явных линий строится окно вокруг медианы: у разгрома и у затяжного
    матча ходовые линии разные, фиксированный набор 19.5–23.5 половину времени
    показывал бы 0% и 100%.
    """
    import bisect
    g = res["_games"]
    n = len(g)
    if lines is None:
        centre = res["games_median"]
        lines = [centre - span + k + 0.5 for k in range(span * 2 + 1)]
    out = []
    for line in lines:
        under = bisect.bisect_left(g, line) / n
        out.append((line, 1 - under, under))
    return out


def snapshot(sim: dict) -> dict:
    """Компактный слепок для повторной оценки ценности без пересчёта.

    Списки на 10k чисел в состояние не кладём: гистограммы разницы и суммы
    геймов занимают пару сотен байт и дают ровно те же вероятности, потому что
    значения целые.
    """
    m = sim["headline"]
    diff_hist: dict[str, int] = {}
    for d in m["_games_diff"]:
        diff_hist[str(d)] = diff_hist.get(str(d), 0) + 1
    tot_hist: dict[str, int] = {}
    for g in m["_games"]:
        tot_hist[str(g)] = tot_hist.get(str(g), 0) + 1
    return {
        "runs": sim["runs"], "best_of": sim["best_of"],
        "p1_win": m["p1_win"], "p2_win": m["p2_win"],
        "scores": m["scores"], "sets_played": m["sets_played"],
        "diff_hist": diff_hist, "games_hist": tot_hist,
    }


def from_snapshot(snap: dict) -> dict:
    """Разворачивает слепок обратно в вид, понятный prob_* функциям."""
    diffs = []
    for k, n in snap.get("diff_hist", {}).items():
        diffs.extend([int(k)] * n)
    games = []
    for k, n in snap.get("games_hist", {}).items():
        games.extend([int(k)] * n)
    head = {
        "p1_win": snap["p1_win"], "p2_win": snap["p2_win"],
        "scores": {k: float(v) for k, v in snap["scores"].items()},
        "sets_played": {int(k): float(v) for k, v in snap["sets_played"].items()},
        "_games_diff": sorted(diffs), "_games": sorted(games),
        "best_of": snap["best_of"],
    }
    return {"runs": snap["runs"], "best_of": snap["best_of"],
            "headline": head, "models": {}}


def prob_games_handicap(res: dict, line: float, for_p1: bool = True) -> float:
    """Вероятность пройти фору по геймам.

    Линия задаётся как у букмекера: П1 -3.5 значит, что первый должен выиграть
    с перевесом больше 3.5 геймов. Целые линии дают возврат, поэтому считаем
    строгое неравенство и отдельно долю ровного попадания вызывающему.
    """
    import bisect
    diffs = res["_games_diff"]
    n = len(diffs)
    if for_p1:
        # нужно diff + line > 0  ->  diff > -line
        return 1 - bisect.bisect_right(diffs, -line) / n
    # для второго: -diff + line > 0  ->  diff < line
    return bisect.bisect_left(diffs, line) / n


def prob_total_games(res: dict, line: float, over: bool = True) -> float:
    import bisect
    g = res["_games"]
    under = bisect.bisect_left(g, line) / len(g)
    return (1 - under) if over else under


def prob_sets_handicap(res: dict, line: float, for_p1: bool = True) -> float:
    """Фора по сетам. В bo3 осмысленна только ±1.5."""
    need = res["best_of"] // 2 + 1
    total = 0.0
    for key, val in res["scores"].items():
        a, b = (int(x) for x in key.split("-"))
        margin = (a - b) if for_p1 else (b - a)
        if margin + line > 0:
            total += val
    return total


def prob_total_sets(res: dict, line: float, over: bool = True) -> float:
    p = sum(v for k, v in res["sets_played"].items() if k > line)
    return p if over else 1 - p


def _set_total_lines(best_of: int) -> list[float]:
    """Осмысленные линии тотала сетов для формата.

    В bo3 линия одна — 2.5, то есть «дойдёт ли до решающего». В bo5 их две:
    3.5 и 4.5. Линию 2.5 в bo5 не показываем: она заходит почти всегда и
    ставить по ней нечего.
    """
    return [2.5] if best_of == 3 else [3.5, 4.5]


def _fatigue_curve(delta: float, max_sets: int) -> tuple[list[float], list[float]]:
    """Штрафы уставшему и бонусы свежему по сетам, в долях SPW.

    delta > 0 — второй игрок устал сильнее.
    """
    scale = min(abs(delta) / FATIGUE_REFERENCE_DELTA, FATIGUE_MAX_SCALE)
    pen = [FATIGUE_PENALTY_BY_SET[min(i, len(FATIGUE_PENALTY_BY_SET) - 1)] * scale
           for i in range(max_sets)]
    bonus = [p * FATIGUE_BONUS_SHARE for p in pen]
    return pen, bonus


# ------------------------------------------------------------------ сборка
def build_simulation(report: dict, *, runs: int = DEFAULT_RUNS,
                     elo_weight: float | None = None,
                     seed: int | None = 20260101) -> dict | None:
    """Считает все доступные модели. None — если считать не из чего."""
    inp = extract_inputs(report)
    best_of = inp["best_of"]
    max_sets = best_of
    if elo_weight is None:
        try:
            elo_weight = float(os.environ.get("TP_SIM_ELO_WEIGHT", DEFAULT_ELO_WEIGHT))
        except ValueError:
            elo_weight = DEFAULT_ELO_WEIGHT
    elo_weight = min(max(elo_weight, 0.0), 1.0)

    pair_stats = stats_spw(inp)
    elo_prob = inp["elo_p1_prob"]
    if pair_stats is None and elo_prob is None:
        return None

    out = {
        "p1_name": inp["p1_name"],
        "p2_name": inp["p2_name"],
        "surface": inp["surface"],
        "best_of": best_of,
        "runs": runs,
        "elo_weight": elo_weight,
        "models": {},
        "order": [],
        "inputs": {},
        "notes": [],
    }

    # ---- база SPW: из статы, иначе средняя по покрытию
    avg = TOUR_AVG_SPW.get(inp["surface"], TOUR_AVG_SPW[None])
    if pair_stats:
        m1_1, m1_2 = pair_stats
        base = (m1_1 + m1_2) / 2
        out["inputs"] = {
            "raw_spw1": _raw_spw(inp["p1"]),
            "raw_spw2": _raw_spw(inp["p2"]),
            "rpw1": _rpw_vs(inp["p1"], inp["p2"]),
            "rpw2": _rpw_vs(inp["p2"], inp["p1"]),
            "adj_spw1": m1_1,
            "adj_spw2": m1_2,
            "tour_avg": avg,
        }
    else:
        m1_1 = m1_2 = None
        base = avg
        out["notes"].append("Показателей подачи/приёма нет — считаю только по Elo.")

    # ---- усталость
    fp1, fp2 = inp["fatigue_p1"], inp["fatigue_p2"]
    delta = (fp2 - fp1) if (fp1 is not None and fp2 is not None) else 0.0
    out["fatigue_delta"] = delta
    pen, bonus = _fatigue_curve(delta, max_sets)
    tired_is_p2 = delta > 0

    def with_fatigue(s1: float, s2: float) -> tuple[list[float], list[float]]:
        a, b = [], []
        for i in range(max_sets):
            if abs(delta) < 1e-9:
                a.append(s1)
                b.append(s2)
            elif tired_is_p2:
                a.append(s1 + bonus[i])
                b.append(s2 - pen[i])
            else:
                a.append(s1 - pen[i])
                b.append(s2 + bonus[i])
        return a, b

    def add(tag: str, s1: float, s2: float, fatigue: bool, seed_off: int):
        a, b = with_fatigue(s1, s2) if fatigue else ([s1] * max_sets, [s2] * max_sets)
        res = run_matches(a, b, best_of=best_of, runs=runs,
                          seed=None if seed is None else seed + seed_off)
        res["spw_by_set"] = (a, b)
        # Не «усталость вообще есть», а «поправка ощутима». При разнице
        # в десятые доли пункта штраф округляется в ноль, и помечать такую
        # модель как учитывающую усталость — обещать то, чего не произошло.
        biggest = max((abs(x - s1) for x in a), default=0.0)
        res["fatigue_applied"] = bool(fatigue and biggest >= 0.001)
        res["fatigue_shift"] = round(biggest, 4)
        out["models"][tag] = res
        out["order"].append(tag)

    if m1_1 is not None:
        add("stats", m1_1, m1_2, False, 1)

    m2_1 = m2_2 = None
    if elo_prob is not None:
        m2_1, m2_2 = solve_spw_for_prob(elo_prob, base, best_of,
                                        seed=(seed or 0) + 777)
        add("elo", m2_1, m2_2, False, 2)

    if m1_1 is not None and m2_1 is not None:
        add("blend", (m1_1 + m2_1) / 2, (m1_2 + m2_2) / 2, True, 3)
        add("work", elo_weight * m2_1 + (1 - elo_weight) * m1_1,
            elo_weight * m2_2 + (1 - elo_weight) * m1_2, True, 4)
        gap = abs(out["models"]["stats"]["p1_win"] - out["models"]["elo"]["p1_win"])
        out["divergence"] = gap
    elif m1_1 is not None:
        add("work", m1_1, m1_2, True, 4)
    else:
        add("work", m2_1, m2_2, True, 4)

    out["headline"] = out["models"]["work"]
    return out


# ------------------------------------------------------------------ вывод
def _e(x) -> str:
    return _html.escape(str(x if x is not None else "—"))


def _short(name: str, width: int = 13) -> str:
    parts = name.split()
    if len(parts) > 1:
        name = f"{parts[0][0]}. {' '.join(parts[1:])}"
    return name if len(name) <= width else name[: width - 1] + "…"


def _odds(p: float) -> str:
    return f"{1/p:.2f}" if p > 1e-6 else "—"


SURFACE_RU = {"hard": "хард", "clay": "грунт", "grass": "трава"}


def format_simulation_telegram(sim: dict) -> str:
    """HTML-блоки для Telegram. Возвращает готовый текст, режется вызывающим."""
    n1, n2 = sim["p1_name"], sim["p2_name"]
    best_of = sim["best_of"]
    parts: list[str] = []

    surf = SURFACE_RU.get(sim["surface"], sim["surface"]) if sim["surface"] else "все покрытия"
    runs_str = f"{sim['runs']:,}".replace(",", " ")
    parts.append(f"🎲 <b>Симуляция матча</b> — {runs_str} прогонов, bo{best_of}, {_e(surf)}")

    # --- сводка по моделям
    rows = []
    any_fatigue = False
    for tag in sim["order"]:
        m = sim["models"][tag]
        title = MODEL_TITLES[tag].format(w=sim["elo_weight"] * 100)
        if m.get("fatigue_applied"):
            any_fatigue = True
        else:
            title = title.replace(" *", "")
        rows.append((title, f"{m['p1_win']:.0%}", f"{m['p2_win']:.0%}"))
    # Имена вынесены строкой над блоком: внутри таблицы они съедали
    # половину ширины и всё равно обрезались до «L. Pou…»
    lw = max(len(r[0]) for r in rows)
    cw = max(5, (TABLE_W - lw) // 2)
    lines = ["-" * (lw + cw * 2)]
    for a, b, c in rows:
        lines.append(f"{a:<{lw}}{b:>{cw}}{c:>{cw}}")
    # Прежняя подпись «* с усталостью · Рабочая = 70% Elo» читалась так,
    # будто рабочая модель — это просто 70% Elo, а усталость к ней сбоку.
    w = sim["elo_weight"] * 100
    legend = (f"Бленд = 50/50, Рабочая = {w:.0f}% Elo + {100 - w:.0f}% статы"
              + (", обе с поправкой на усталость" if any_fatigue
                 else " (усталость не влияет: игроки равны)"))
    parts.append(f"<b>Вероятность победы</b>\n"
                 f"<i>{_e(_short(n1, 16))} / {_e(_short(n2, 16))}</i>\n"
                 f"<pre>{_e(chr(10).join(lines))}</pre>\n"
                 f"<i>{_e(legend)}</i>")

    if sim.get("divergence") is not None and sim["divergence"] > 0.20:
        parts.append(
            f"⚠️ <i>Стата и Elo расходятся на {sim['divergence']:.0%}. "
            "Обычно это значит, что проценты набраны против разной по силе сетки — "
            "смотрите на рабочую модель, а не на крайности.</i>"
        )

    # --- детали рабочей модели
    m = sim["headline"]
    a_set, b_set = m["spw_by_set"]
    parts.append(
        f"<b>Рабочая модель</b>\n"
        f"SPW: {_e(_short(n1))} {a_set[0]:.1%} · {_e(_short(n2))} {b_set[0]:.1%}\n"
        f"Победа: <b>{_e(_short(n1))} {m['p1_win']:.1%}</b> (кэф {_odds(m['p1_win'])}) · "
        f"<b>{_e(_short(n2))} {m['p2_win']:.1%}</b> (кэф {_odds(m['p2_win'])})"
    )

    # счёт по сетам
    sc_rows = []
    for key, val in sorted(m["scores"].items(), key=lambda kv: -kv[1]):
        s1, s2 = (int(x) for x in key.split("-"))
        # счёт всегда от лица победителя: строка «J. Kumstat 0-2» читалась
        # как поражение Кумстата, хотя это его победа
        who, hi, lo = (n1, s1, s2) if s1 > s2 else (n2, s2, s1)
        sc_rows.append((f"{_short(who, 9)} {hi}-{lo}", f"{val:.0%}", _odds(val)))
    lw = max(len(r[0]) for r in sc_rows)
    lw = min(lw, 14)
    cw = max(5, (TABLE_W - lw) // 2)
    lines = [f"{'':<{lw}}{'вер.':>{cw}}{'кэф':>{cw}}", "-" * (lw + cw * 2)]
    for a, b, c in sc_rows:
        lines.append(f"{a[:lw]:<{lw}}{b:>{cw}}{c:>{cw}}")
    parts.append(f"<b>Точный счёт по сетам</b>\n<pre>{_e(chr(10).join(lines))}</pre>")

    # --- тотал сетов: именно на него ставит бот, поэтому идёт первым
    set_lines = _set_total_lines(best_of)
    if set_lines:
        rows = []
        for line in set_lines:
            over = sum(v for k, v in m["sets_played"].items() if k > line)
            rows.append((line, over, 1 - over))
        lines = ["-" * TABLE_W]
        for line, over, under in rows:
            lines.append(f"{line:<6}ТБ {over:>3.0%} {_odds(over):>5}")
            lines.append(f"{'':<6}ТМ {under:>3.0%} {_odds(under):>5}")
        parts.append(f"<b>Тотал сетов</b>\n<pre>{_e(chr(10).join(lines))}</pre>\n"
                     "<i>Кэф справедливый, без маржи: ставить есть смысл, только "
                     "если у букмекера цена выше.</i>")

    # --- тотал геймов
    tot = totals(m)
    lines = ["-" * TABLE_W]
    for line, over, under in tot:
        lines.append(f"{line:<6}ТБ {over:>3.0%} {_odds(over):>5}")
        lines.append(f"{'':<6}ТМ {under:>3.0%} {_odds(under):>5}")
    lines.append("")
    lines.append(f"сред. {m['games_mean']:.1f}  мед. {m['games_median']}")
    lines.append(f"5-95%: {m['games_p05']}-{m['games_p95']}")
    parts.append(f"<b>Тотал геймов</b>\n<pre>{_e(chr(10).join(lines))}</pre>")

    need = best_of // 2 + 1
    p1_no_sets = m["p1_sets"].get(0, 0.0)
    p1_sweep = m["scores"].get(f"{need}-0", 0.0)
    a1, a2 = _short(n1, 9), _short(n2, 9)

    rows = [
        (f"{a1} +{need - 0.5:g}", 1 - p1_no_sets),
        (f"{a2} +{need - 0.5:g}", 1 - p1_sweep),
        (f"{a1} {need}-0", p1_sweep),
        (f"{a2} {need}-0", p1_no_sets),
        ("Решающий сет", m["sets_played"].get(best_of, 0.0)),
    ]
    lw = min(max(len(r[0]) for r in rows), 14)
    cw = max(5, (TABLE_W - lw) // 2)
    lines = [f"{'':<{lw}}{'вер.':>{cw}}{'кэф':>{cw}}", "-" * (lw + cw * 2)]
    for label, prob in rows:
        lines.append(f"{label[:lw]:<{lw}}{prob:>{cw}.0%}{_odds(prob):>{cw}}")
    if m["p1_win_given_first_set"] is not None:
        lines.append("")
        lines.append(f"1-й сет: {m['p1_first_set']:.0%}")
        lines.append(f"→ матч: {m['p1_win_given_first_set']:.0%}")
    parts.append(f"<b>Форы и сеты</b>\n<pre>{_e(chr(10).join(lines))}</pre>")

    if m["fatigue_applied"]:
        d = sim["fatigue_delta"]
        tired = n2 if d > 0 else n1
        drift = [f"{i+1}-й {(a_set[i] if d < 0 else b_set[i]):.1%}" for i in range(len(a_set))]
        parts.append(
            f"<i>Учтена усталость: {_e(tired)} устал сильнее на {abs(d):.1f} п. "
            f"SPW по сетам — {_e(', '.join(drift))}.</i>"
        )

    inp = sim.get("inputs") or {}
    if inp.get("raw_spw1") is not None:
        lines = [
            "-" * TABLE_W,
            f"{'SPW сырой':<14}{inp['raw_spw1']:>5.0%}{inp['raw_spw2']:>5.0%}",
        ]
        if inp.get("rpw1") is not None:
            lines.append(f"{'RPW vs сопер.':<14}{inp['rpw1']:>5.0%}{inp['rpw2']:>5.0%}")
            lines.append(f"{'SPW с попр.':<14}{inp['adj_spw1']:>5.0%}{inp['adj_spw2']:>5.0%}")
        # одна и та же величина для обоих: пустая вторая колонка читалась
        # как «у второго игрока данных нет»
        lines.append(f"{'Средний SPW':<14}{inp['tour_avg']:>5.0%}"
                     f"{inp['tour_avg']:>5.0%}")
        parts.append(f"<b>Как посчитано</b>\n"
                     f"<i>{_e(_short(n1, 16))} / {_e(_short(n2, 16))}</i>\n"
                     f"<pre>{_e(chr(10).join(lines))}</pre>")

    for note in sim.get("notes", []):
        parts.append(f"<i>{_e(note)}</i>")

    parts.append("<i>Модель, а не прогноз: штрафы за усталость — эвристика, "
                 "проценты — из 52-недельной выборки.</i>")
    return "\n\n".join(parts)


def format_simulation_console(sim: dict) -> str:
    """То же самое без HTML — для CLI."""
    import re
    text = format_simulation_telegram(sim)
    text = re.sub(r"</?(b|i|pre)>", "", text)
    return _html.unescape(text)
