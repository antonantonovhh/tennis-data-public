"""Парсер теннисной статистики: TennisRatio (H2H, матчи) + TennisAbstract (Elo, yElo)."""

__version__ = "0.1.0"

from .fatigue import compute_fatigue, fatigue_edge  # noqa: F401
from .http import Fetcher  # noqa: F401
from .simulation import build_simulation, format_simulation_telegram  # noqa: F401
from .tennisabstract import load_ratings, elo_win_probability, blended_elo  # noqa: F401
from .tennisratio import fetch_h2h, h2h_url, parse_score  # noqa: F401
