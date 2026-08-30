"""Прямой доступ к JSON API tennisratio.com.

Найдено в HAR-логе страницы сравнения:

    GET /api/player/{PlayerId}/compare-filtered/?surface=clay&range=52w&level=all
    GET /api/player/{PlayerId}/stats-filtered/?surface=clay&range=52w&level=all

Ни кук, ни CSRF-токена, ни X-Requested-With — достаточно обычного GET
с Referer. Это отменяет рендер вкладки сравнения: те же цифры приходят
за доли секунды вместо десятков секунд работы Chromium.

PlayerId — имя без пробелов и диакритики: 'Jan Kumstat' -> 'JanKumstat',
то есть ровно тот же формат, что у TennisAbstract в player.cgi?p=.

Матчей в этих ответах нет — их по-прежнему собирает рендер вкладок.
"""

from __future__ import annotations

import logging

from .http import Fetcher
from .names import camel_key

log = logging.getLogger(__name__)

API = "https://www.tennisratio.com/api/player/{pid}/{endpoint}/"

# сопоставление полей API с ключами блоков сравнения
FIELD_MAP = {
    "overall": {
        "aces_per_game": "aces_avg",
        "df_per_game": "doublefaults_avg",
        "bps_created_per_game": "breakpoints_created_per_game",
        "bps_to_defend_per_game": "breakpoints_todefend_per_game",
        "tiebreaks_won_pct": "tiebreak_won_perc",
        "dominance_ratio": "dominance_ratio",
        "breakpoints_prevail": "breakpoints_prevail_ratio",
        "dominance_efficiency": "dominance_eff_ratio",
        "match_efficiency": "match_eff_ratio",
    },
    "serve": {
        "first_serve_accuracy": "first_serve_accuracy",
        "first_serve_points_won": "first_serve_points",
        "second_serve_points_won": "second_serve_points",
        "service_games_won": "service_games_won_ratio",
        "break_points_saved": "breakpoints_saved_ratio",
    },
    "return": {
        "return_first_serve_points": "return_1st_serve_points",
        "return_second_serve_points": "return_2nd_serve_points",
        "return_games_won": "return_games_won_ratio",
        "break_points_converted": "breakpoints_converted_ratio",
    },
    "pressure": {
        "pressure_won_on_serve": "serve_pressure_avg",
        "pressure_won_on_return": "return_pressure_avg",
        "pts_per_game_on_serve": "serve_points_per_game",
        "pts_per_game_on_return": "return_points_per_game",
    },
}

# записи по роли в котировках букмекера — в вёрстке этого не было вовсе
ODDS_BRACKETS = [
    ("surety", "Явный фаворит"),
    ("favorite", "Фаворит"),
    ("slightfav", "Небольшой фаворит"),
    ("slightunder", "Небольшой андердог"),
    ("under", "Андердог"),
    ("underdog", "Явный андердог"),
]

# Диапазоны кэфов для каждой роли, десятичные, полуинтервал [низ, верх).
#
# Значения с самого сайта, блок ODDS PERFORMANCE COMPARISON. Ключевая деталь:
# фаворитские роли заданы СВОИМ кэфом, а андердожьи — кэфом СОПЕРНИКА. Это не
# одно и то же: "андердог" у tennisratio значит "соперник шёл по 1.20-1.50",
# а не "я шёл по 1.20-1.50". Своего кэфа в андердожьих корзинах нет вовсе,
# поэтому пересчитывать их в свои цены нельзя — маржа неизвестна.
#
# Проверка на Тиафо (37W-20L): 4-0, 17-3, 8-4, 6-7, 2-4, 0-2 суммируются
# ровно в 37-20, то есть корзины исчерпывающие и не пересекаются.
ODDS_RANGES = {
    #                basis    низ    верх
    "surety":      ("self",   None,  1.20),   # STRONG FAVOURITE
    "favorite":    ("self",   1.20,  1.50),   # CLEAR FAVOURITE
    "slightfav":   ("self",   1.50,  None),   # SLIGHT FAVOURITE
    "slightunder": ("rival",  1.50,  None),   # NOT A FAVOURITE
    "under":       ("rival",  1.20,  1.50),   # NOT A FAVOURITE
    "underdog":    ("rival",  None,  1.20),   # PRE-MATCH UNDERDOG
}

BASIS_RU = {"self": "свой", "rival": "соп."}


def _range_of(prefix: str, stats: dict) -> tuple[str, float | None, float | None]:
    """Пороги из ответа API, если они там есть; иначе из константы выше."""
    lo = stats.get(f"{prefix}_odds_min", stats.get(f"{prefix}_min_odds"))
    hi = stats.get(f"{prefix}_odds_max", stats.get(f"{prefix}_max_odds"))
    basis, dlo, dhi = ODDS_RANGES.get(prefix, ("self", None, None))
    if lo is not None or hi is not None:
        return basis, lo, hi
    return basis, dlo, dhi


def format_odds_range(basis, lo, hi) -> str:
    """('self', None, 1.2) -> 'свой <1.20'; ('rival', 1.2, 1.5) -> 'соп. 1.20-1.50'."""
    if lo is None and hi is None:
        return ""
    pre = BASIS_RU.get(basis, "")
    if lo is None:
        body = f"<{hi:.2f}"
    elif hi is None:
        body = f">{lo:.2f}"
    else:
        body = f"{lo:.2f}-{hi:.2f}"
    return f"{pre} {body}".strip()

SURFACE_PARAM = {"hard": "hard", "clay": "clay", "grass": "grass"}


def player_id(name_or_slug: str) -> str:
    """'Jan Kumstat' / 'jan-kumstat' -> 'JanKumstat'."""
    return camel_key(name_or_slug.replace("-", " "))


def fetch_player(
    fetcher: Fetcher,
    pid: str,
    *,
    surface: str | None = None,
    date_range: str = "52w",
    level: str = "all",
    referer: str | None = None,
    endpoint: str = "compare-filtered",
) -> dict | None:
    """Один игрок. None — если API не ответил (тогда работает старый путь)."""
    url = API.format(pid=pid, endpoint=endpoint)
    params = {
        "surface": SURFACE_PARAM.get((surface or "").lower(), "all"),
        "range": date_range,
        "level": level,
    }
    headers = {"Referer": referer} if referer else None
    try:
        data = fetcher.get_json(url, params=params, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.warning("API %s не ответил: %s", pid, exc)
        return None
    # у compare-filtered имя в 'name', у stats-filtered — в 'player_name'
    if not isinstance(data, dict) or not (data.get("name") or data.get("player_name")):
        log.warning("API %s вернул неожиданное тело", pid)
        return None
    return data


def _stats_of(data: dict) -> dict:
    """У compare-filtered показатели лежат и в ovr, и в корне; у stats-filtered — в stats."""
    for key in ("ovr", "stats"):
        block = data.get(key)
        if isinstance(block, dict) and block:
            return block
    return data


def to_comparison(d1: dict, d2: dict, surface: str | None) -> dict:
    """Приводит два ответа API к тому же виду, что даёт разбор вёрстки."""
    s1, s2 = _stats_of(d1), _stats_of(d2)
    out: dict = {}
    for group, mapping in FIELD_MAP.items():
        block = {}
        for key, api_field in mapping.items():
            v1, v2 = s1.get(api_field), s2.get(api_field)
            if v1 is None and v2 is None:
                continue
            block[key] = {
                "p1": round(v1, 3) if isinstance(v1, (int, float)) else None,
                "p2": round(v2, 3) if isinstance(v2, (int, float)) else None,
            }
        if block:
            out[group] = block
    if out:
        out["_surface"] = (surface or "all").lower()
        out["_source"] = "api"
    return out


def player_summary(data: dict) -> dict:
    """Поля, которых в вёрстке не было: баланс по ролям в котировках и т.п."""
    s = _stats_of(data)
    odds = []
    for prefix, label in ODDS_BRACKETS:
        played = s.get(f"{prefix}_played") or 0
        won = s.get(f"{prefix}_won") or 0
        if played:
            basis, lo, hi = _range_of(prefix, s)
            odds.append({"label": label, "played": played, "won": won,
                         "pct": round(won / played * 100, 1),
                         "odds_range": format_odds_range(basis, lo, hi)})
    return {
        "id": data.get("id") or data.get("player_id"),
        "name": data.get("name") or data.get("player_name"),
        "rank": data.get("rank"),
        "peak_rank": data.get("peak_rank"),
        "country": data.get("country"),
        "hand": data.get("hand"),
        "matches_played": s.get("matches_played"),
        "matches_won": s.get("matches_won"),
        "win_percentage": s.get("win_percentage"),
        "odds_record": odds,
    }


def fetch_comparison(
    fetcher: Fetcher,
    p1: str,
    p2: str,
    *,
    surface: str | None = None,
    date_range: str = "52w",
    level: str = "all",
    referer: str | None = None,
) -> tuple[dict, dict | None, dict | None]:
    """(блоки сравнения, сводка по игроку 1, сводка по игроку 2).

    Пустой первый элемент означает, что API не сработал и нужен рендер.
    """
    d1 = fetch_player(fetcher, player_id(p1), surface=surface,
                      date_range=date_range, level=level, referer=referer)
    d2 = fetch_player(fetcher, player_id(p2), surface=surface,
                      date_range=date_range, level=level, referer=referer)
    if not d1 or not d2:
        return {}, (player_summary(d1) if d1 else None), (player_summary(d2) if d2 else None)

    cmp_data = to_comparison(d1, d2, surface)
    log.info("API: сравнение получено (%s vs %s, покрытие %s)",
             d1.get("name") or d1.get("player_name"),
             d2.get("name") or d2.get("player_name"), surface or "all")
    return cmp_data, player_summary(d1), player_summary(d2)
