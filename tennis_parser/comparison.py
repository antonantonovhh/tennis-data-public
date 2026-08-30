"""Блоки сравнения на вкладке Compare Players.

Разбираются три секции:
  KEY STATS COMPARISON / OVERALL STATS   — эйсы, двойные, брейки, доминирование
  KEY STATS COMPARISON / SERVE & RETURN  — подача и приём
  PRESSURE POINTS PERFORMANCE            — очки под давлением

Вёрстку этих блоков я не закладываю в код: значения ищутся по подписи, а не
по позиции в дереве. Сайт может переставить колонки или сменить обёртки —
парсер это переживёт, пока подписи на месте.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# число с необязательным процентом: 62.3%, 1.048, 0.30, -0.5
_NUM_RX = re.compile(r"-?\d+(?:[.,]\d+)?\s*%?")

OVERALL_LABELS = {
    "aces_per_game": "Aces per Game",
    "df_per_game": "Double Faults per Game",
    "bps_created_per_game": "BPs Created per Game",
    "bps_to_defend_per_game": "BPs to Defend per Game",
    "tiebreaks_won_pct": "Tiebreaks Won %",
    "dominance_ratio": "Dominance Ratio",
    "breakpoints_prevail": "Breakpoints Prevail",
    "dominance_efficiency": "Dominance Efficiency",
    "match_efficiency": "Match Efficiency",
}

SERVE_LABELS = {
    "first_serve_accuracy": "1st Serve Accuracy",
    "first_serve_points_won": "1st Serve Points Won",
    "second_serve_points_won": "2nd Serve Points Won",
    "service_games_won": "Service Games Won",
    "break_points_saved": "Break Points Saved",
}

RETURN_LABELS = {
    "return_first_serve_points": "Return 1st Serve Points",
    "return_second_serve_points": "Return 2nd Serve Points",
    "return_games_won": "Return Games Won",
    "break_points_converted": "Break Points Converted",
}

PRESSURE_LABELS = {
    "pressure_won_on_serve": "Pressure Points Won on Serve",
    "pressure_won_on_return": "Pressure Points Won on Return",
    "pts_per_game_on_serve": "Pts/Game on Serve",
    "pts_per_game_on_return": "Pts/Game on Return",
}

# те же подписи в других написаниях — сайт местами ставит пробелы вокруг слэша
PRESSURE_ALIASES = {
    "pts_per_game_on_serve": ["Pts / Game on Serve", "Pts Game on Serve"],
    "pts_per_game_on_return": ["Pts / Game on Return", "Pts Game on Return"],
}

# человекочитаемые названия для вывода
RU = {
    "aces_per_game": "Эйсов/гейм",
    "df_per_game": "Двойных/гейм",
    "bps_created_per_game": "БП создано/гейм",
    "bps_to_defend_per_game": "БП отражать/гейм",
    "tiebreaks_won_pct": "Тайбрейки",
    # Эти четыре — фирменные метрики tennisratio, и на сайте они называются
    # по-английски. Перевод («БП перевес», «Эфф. доминир.») своим сокращением
    # только мешал: сверить с сайтом или загуглить определение по нему нельзя.
    "dominance_ratio": "Dominance Ratio",
    "breakpoints_prevail": "Breakpoints Prevail",
    "dominance_efficiency": "Dominance Efficiency",
    "match_efficiency": "Match Efficiency",
    "first_serve_accuracy": "1-я подача попад.",
    "first_serve_points_won": "1-я подача выигр.",
    "second_serve_points_won": "2-я подача выигр.",
    "service_games_won": "Геймы на подаче",
    "break_points_saved": "БП спасено",
    "return_first_serve_points": "Приём 1-й подачи",
    "return_second_serve_points": "Приём 2-й подачи",
    "return_games_won": "Геймы на приёме",
    "break_points_converted": "БП реализовано",
    "pressure_won_on_serve": "Давление: подача",
    "pressure_won_on_return": "Давление: приём",
    "pts_per_game_on_serve": "Очков/гейм подача",
    "pts_per_game_on_return": "Очков/гейм приём",
}


def _to_float(token: str) -> float | None:
    t = token.replace("%", "").replace(",", ".").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower().rstrip(":")


def build_index(soup: BeautifulSoup) -> list[tuple[str, object]]:
    """(нормализованный текст, элемент) для всех листовых-по-смыслу узлов.

    Ищем по ЭЛЕМЕНТАМ, а не по текстовым узлам: подписи бывают свёрстаны в
    две строки ('PRESSURE POINTS' <br> 'WON ON SERVE') или разбиты на span-ы,
    и тогда цельной строки в дереве попросту нет.

    Индекс строится один раз на страницу — обход дерева ради каждой из 22
    подписей был бы заметно дороже.
    """
    index: list[tuple[str, object]] = []
    for el in soup.find_all(True):
        text = _norm(el.get_text(" ", strip=True))
        if text and len(text) <= 60:
            index.append((text, el))
    return index


def _label_elements(index: list[tuple[str, object]], label: str) -> list:
    """Элементы, чей текст — ровно эта подпись; вложенные дубли отбрасываем."""
    norm = _norm(label)
    hits = [el for text, el in index if text == norm]
    if len(hits) < 2:
        return hits
    # оставляем только самые глубокие: у <div><span>Подпись</span></div>
    # совпадут оба, а нужен внутренний
    deepest = []
    for el in hits:
        if not any(other is not el and other in el.descendants for other in hits):
            deepest.append(el)
    return deepest or hits


def _numbers_near(el_label, label: str, want: int, max_up: int = 5) -> list[float]:
    """Поднимается от подписи вверх, пока в поддереве не найдётся want чисел.

    Останавливаемся на первом подходящем предке: числа этой подписи лежат
    ближе всех, а дальше вверх начнутся чужие.
    """
    el = el_label.parent
    for _ in range(max_up):
        if el is None:
            return []
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        # из текста убираем саму подпись — иначе '1st Serve Accuracy' отдаст 1
        cleaned = re.sub(re.escape(label), " ", text, flags=re.I)
        nums = [_to_float(t) for t in _NUM_RX.findall(cleaned)]
        nums = [n for n in nums if n is not None]
        if len(nums) >= want:
            return nums[:want]
        el = el.parent
    return []


def _pair(index, label: str) -> tuple[float, float] | None:
    """Значение для игрока 1 и игрока 2 из одной строки сравнения."""
    for el in _label_elements(index, label):
        nums = _numbers_near(el, label, want=2)
        if len(nums) == 2:
            return nums[0], nums[1]
    return None


def _per_player(index, label: str) -> tuple[float | None, float | None]:
    """Подпись встречается дважды — по разу на игрока (блок Pressure Points)."""
    vals: list[float] = []
    for el in _label_elements(index, label):
        nums = _numbers_near(el, label, want=1)
        if nums:
            vals.append(nums[0])
    if not vals:
        return None, None
    return vals[0], (vals[1] if len(vals) > 1 else None)


def parse_comparison(html: str) -> dict:
    """Разбирает все три блока. Отсутствующие секции просто не попадают в итог."""
    soup = BeautifulSoup(html, "lxml")
    index = build_index(soup)
    out: dict[str, dict] = {"overall": {}, "serve": {}, "return": {}, "pressure": {}}

    for group, labels in (
        ("overall", OVERALL_LABELS),
        ("serve", SERVE_LABELS),
        ("return", RETURN_LABELS),
    ):
        for key, label in labels.items():
            pair = _pair(index, label)
            if pair:
                out[group][key] = {"p1": pair[0], "p2": pair[1]}

    for key, label in PRESSURE_LABELS.items():
        v1, v2 = _per_player(index, label)
        for alt in PRESSURE_ALIASES.get(key, []):
            if v1 is not None:
                break
            v1, v2 = _per_player(index, alt)
        if v1 is not None:
            out["pressure"][key] = {"p1": v1, "p2": v2}

    found = {k: len(v) for k, v in out.items()}
    log.info("Блоки сравнения: %s", found)
    return {k: v for k, v in out.items() if v}


def surface_of(cmp_data: dict) -> str | None:
    """К какому покрытию относятся показатели: 'hard'/'clay'/'grass'/'all'."""
    return (cmp_data or {}).get("_surface")


def merge_comparison(a: dict, b: dict) -> dict:
    """Сливает результаты нескольких рендеров (вкладки Overall и Serve&Return)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (a or {}).items()}
    for group, items in (b or {}).items():
        if not isinstance(items, dict):
            out.setdefault(group, items)
            continue
        out.setdefault(group, {})
        for key, val in items.items():
            out[group].setdefault(key, val)
    return {k: v for k, v in out.items() if v}


def sections(cmp_data: dict) -> dict:
    """Только содержательные секции, без служебных ключей вроде _surface."""
    return {k: v for k, v in (cmp_data or {}).items()
            if not k.startswith("_") and isinstance(v, dict)}


def is_complete(cmp_data: dict) -> bool:
    """Есть ли обе вкладки Key Stats — если нет, надо кликнуть Serve & Return."""
    return bool(cmp_data.get("overall")) and bool(cmp_data.get("serve"))
