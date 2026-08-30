"""Усталость и форма: считаются из истории матчей.

Идея: у нас нет минут на корте, но есть дата, счёт и раунд. Из счёта
восстанавливается объём работы (геймы/сеты), из дат — плотность календаря.

Метрики:
  days_rest          — дней с последнего матча
  matches_7/14/28d   — сколько матчей сыграно в окне
  games_7/14d        — суммарно геймов
  est_minutes_7/14d  — оценка минут на корте
  consecutive_days   — сколько дней подряд игрались матчи (серия к последней дате)
  deciders_14d       — трёхсетовиков за 14 дней
  load_index         — нагрузка с экспоненциальным затуханием по давности
  fatigue_score      — 0..100, где 100 = «на ободах»
  form               — победы в последних N матчах + streak

Все числа — эвристика, не медицинская правда. Калибруйте под свою выборку.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from statistics import mean

# средняя длительность гейма, мин (best-of-3 на челленджерах/ITF)
MIN_PER_GAME = 4.2
# доп. штраф за решающий сет — нервы и физуха стоят дороже геймов
DECIDER_PENALTY_MIN = 12.0
# период полураспада нагрузки, дней
HALF_LIFE_DAYS = 7.0


@dataclass
class FatigueReport:
    as_of: str
    matches_total: int = 0
    last_match_date: str | None = None
    days_rest: int | None = None
    matches_3d: int = 0
    matches_7d: int = 0
    matches_14d: int = 0
    matches_28d: int = 0
    games_7d: int = 0
    games_14d: int = 0
    est_minutes_7d: float = 0.0
    est_minutes_14d: float = 0.0
    consecutive_days: int = 0
    deciders_14d: int = 0
    avg_games_per_match_14d: float | None = None
    load_index: float = 0.0
    fatigue_score: float = 0.0
    fatigue_label: str = "неизвестно"
    wins_last_10: int | None = None
    win_streak: int | None = None
    surfaces_28d: dict | None = None
    surface_switch_14d: bool = False
    notes: list | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _est_minutes(m) -> float:
    games = m.games_played or 0
    minutes = games * MIN_PER_GAME
    if (m.sets_played or 0) >= 3:
        minutes += DECIDER_PENALTY_MIN
    return minutes


def _consecutive_days(dates: list[date]) -> int:
    """Длина серии матчей в идущие подряд дни, считая от самой поздней даты."""
    if not dates:
        return 0
    uniq = sorted(set(dates), reverse=True)
    streak, cursor = 1, uniq[0]
    for d in uniq[1:]:
        if (cursor - d).days == 1:
            streak += 1
            cursor = d
        else:
            break
    return streak


def compute_fatigue(matches: list, as_of: date | None = None) -> FatigueReport:
    """matches — список tennisratio.Match (или любых объектов с теми же полями)."""
    as_of = as_of or date.today()
    dated = sorted(
        [m for m in matches if getattr(m, "date", None)],
        key=lambda m: m.date,
        reverse=True,
    )
    rep = FatigueReport(as_of=as_of.isoformat(), matches_total=len(matches), notes=[])

    if not dated:
        rep.notes.append("Нет матчей с распознанной датой — усталость не рассчитана.")
        return rep

    last = dated[0].date
    rep.last_match_date = last.isoformat()
    rep.days_rest = (as_of - last).days

    def window(days: int) -> list:
        cutoff = as_of - timedelta(days=days)
        return [m for m in dated if m.date >= cutoff]

    w3, w7, w14, w28 = window(3), window(7), window(14), window(28)
    rep.matches_3d, rep.matches_7d = len(w3), len(w7)
    rep.matches_14d, rep.matches_28d = len(w14), len(w28)

    rep.games_7d = sum(m.games_played or 0 for m in w7)
    rep.games_14d = sum(m.games_played or 0 for m in w14)
    rep.est_minutes_7d = round(sum(_est_minutes(m) for m in w7), 1)
    rep.est_minutes_14d = round(sum(_est_minutes(m) for m in w14), 1)
    rep.consecutive_days = _consecutive_days([m.date for m in w14])
    rep.deciders_14d = sum(1 for m in w14 if (m.sets_played or 0) >= 3)
    if w14:
        rep.avg_games_per_match_14d = round(
            mean([m.games_played or 0 for m in w14]), 1
        )

    # покрытия и смена покрытия
    surf = {}
    for m in w28:
        if m.surface:
            surf[m.surface] = surf.get(m.surface, 0) + 1
    rep.surfaces_28d = surf or None
    rep.surface_switch_14d = len({m.surface for m in w14 if m.surface}) > 1

    # форма
    decided = [m for m in dated if m.won is not None]
    if decided:
        rep.wins_last_10 = sum(1 for m in decided[:10] if m.won)
        streak = 0
        for m in decided:
            if m.won:
                streak += 1
            else:
                break
        rep.win_streak = streak

    # индекс нагрузки: минуты с экспоненциальным затуханием
    load = 0.0
    for m in w28:
        age = max(0, (as_of - m.date).days)
        load += _est_minutes(m) * 0.5 ** (age / HALF_LIFE_DAYS)
    rep.load_index = round(load, 1)

    rep.fatigue_score, rep.fatigue_label, extra = _score(rep)
    rep.notes.extend(extra)
    return rep


def _score(r: FatigueReport) -> tuple[float, str, list[str]]:
    """0..100. Отдых сбрасывает счёт, плотный календарь — накручивает."""
    notes: list[str] = []
    score = 0.0

    # базовая нагрузка: ~300 экспоненциально взвешенных минут ≈ потолок
    score += min(55.0, r.load_index / 300.0 * 55.0)

    # матчи подряд без выходного
    if r.consecutive_days >= 3:
        score += 12
        notes.append(f"{r.consecutive_days} дня подряд с матчами")
    elif r.consecutive_days == 2:
        score += 5

    # затяжные трёхсетовики
    if r.deciders_14d >= 3:
        score += 10
        notes.append(f"{r.deciders_14d} трёхсетовика за 14 дней")
    elif r.deciders_14d == 2:
        score += 5

    # объём за неделю
    if r.matches_7d >= 5:
        score += 10
        notes.append(f"{r.matches_7d} матчей за 7 дней")
    elif r.matches_7d == 4:
        score += 5

    if r.surface_switch_14d:
        score += 4
        notes.append("смена покрытия за последние 14 дней")

    # отдых гасит усталость
    if r.days_rest is not None:
        if r.days_rest >= 14:
            score *= 0.25
            notes.append(f"{r.days_rest} дней без матчей — свежий, но возможен недобор ритма")
        elif r.days_rest >= 7:
            score *= 0.5
            notes.append(f"{r.days_rest} дней отдыха")
        elif r.days_rest >= 3:
            score *= 0.8
        elif r.days_rest <= 1:
            score += 6
            notes.append("матч вчера/сегодня — восстановления почти не было")

    score = max(0.0, min(100.0, score))
    if score < 20:
        label = "свежий"
    elif score < 40:
        label = "нормально"
    elif score < 60:
        label = "нагрузка"
    elif score < 80:
        label = "устал"
    else:
        label = "перегружен"
    return round(score, 1), label, notes


def fatigue_edge(a: FatigueReport, b: FatigueReport) -> dict:
    """Насколько один свежее другого. Положительный delta = A свежее."""
    delta = b.fatigue_score - a.fatigue_score
    return {
        "delta_fatigue": round(delta, 1),
        "fresher": "p1" if delta > 0 else ("p2" if delta < 0 else "равны"),
        # очень грубо: 40 пунктов разницы ≈ 20 пунктов Elo
        "elo_equivalent_hint": round(delta * 0.5, 1),
    }
