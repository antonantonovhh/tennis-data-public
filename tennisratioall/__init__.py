"""tennisratioall — обход всей афиши tennisratio со статистикой и симуляцией."""

__version__ = "0.1.0"

from .scanner import Scanner, alerts_for, discover, process  # noqa: F401
from .store import MatchRef, Store  # noqa: F401
