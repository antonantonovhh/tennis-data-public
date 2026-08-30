"""Парсер рейтингов TennisAbstract: общий Elo + покрытия (hElo/cElo/gElo) и yElo.

Обе страницы — статический HTML с одной большой таблицей, JS не нужен.

Колонки atp_elo_ratings.html:
    Elo Rank | Player | Age | Elo | | hElo Rank | hElo | cElo Rank | cElo |
    gElo Rank | gElo | | Peak Elo | Peak Month | | ATP Rank | Log diff

Колонки atp_season_yelo_ratings.html:
    Rank | Player | Wins | Losses | yElo

hElo = hard, cElo = clay, gElo = grass.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict, field
from datetime import date

from bs4 import BeautifulSoup

from .http import Fetcher
from .names import normalize, match_name

log = logging.getLogger(__name__)

ELO_URL = {
    "atp": "https://tennisabstract.com/reports/atp_elo_ratings.html",
    "wta": "https://tennisabstract.com/reports/wta_elo_ratings.html",
}
YELO_URL = {
    "atp": "https://tennisabstract.com/reports/atp_season_yelo_ratings.html",
    "wta": "https://tennisabstract.com/reports/wta_season_yelo_ratings.html",
}

SURFACE_ALIAS = {
    "hard": "hElo", "h": "hElo", "хард": "hElo",
    "clay": "cElo", "c": "cElo", "грунт": "cElo",
    "grass": "gElo", "g": "gElo", "трава": "gElo",
}


def _num(text: str | None) -> float | None:
    if not text:
        return None
    t = text.replace("\xa0", " ").strip().replace(",", "")
    if not t or t in {"-", "--"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def _int(text: str | None) -> int | None:
    v = _num(text)
    return int(v) if v is not None else None


@dataclass
class EloRow:
    player: str
    elo_rank: int | None = None
    elo: float | None = None
    age: float | None = None
    helo_rank: int | None = None
    helo: float | None = None
    celo_rank: int | None = None
    celo: float | None = None
    gelo_rank: int | None = None
    gelo: float | None = None
    peak_elo: float | None = None
    peak_month: str | None = None
    atp_rank: int | None = None
    log_diff: float | None = None
    # yElo подмешивается сюда же
    yelo: float | None = None
    yelo_rank: int | None = None
    yelo_wins: int | None = None
    yelo_losses: int | None = None
    updated: str | None = None

    def surface_elo(self, surface: str) -> float | None:
        col = SURFACE_ALIAS.get(surface.strip().lower())
        return {"hElo": self.helo, "cElo": self.celo, "gElo": self.gelo}.get(col)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class EloTable:
    rows: dict[str, EloRow] = field(default_factory=dict)  # normalize(name) -> EloRow
    updated: str | None = None

    def get(self, name: str) -> EloRow | None:
        return match_name(name, self.rows)  # type: ignore[return-value]

    def __len__(self) -> int:
        return len(self.rows)


def _pick_table(soup: BeautifulSoup, must_have: str):
    """Находит нужную таблицу по заголовку — устойчивее, чем хардкод id."""
    best = None
    for tbl in soup.find_all("table"):
        head = " ".join(th.get_text(" ", strip=True) for th in tbl.find_all(["th", "td"], limit=25))
        if must_have.lower() in head.lower() and "player" in head.lower():
            # берём самую длинную подходящую
            if best is None or len(tbl.find_all("tr")) > len(best.find_all("tr")):
                best = tbl
    return best


def _extract_updated(soup: BeautifulSoup) -> str | None:
    m = re.search(r"Last update:\s*(\d{4}-\d{2}-\d{2})", soup.get_text(" ", strip=True))
    return m.group(1) if m else None


ELO_MIN, ELO_MAX = 500.0, 3500.0   # разумный диапазон рейтинга


def _header_labels(tbl) -> list[str]:
    head_row = tbl.find("tr")
    cells = head_row.find_all(["th", "td"])
    return [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).lower() for c in cells]


def _index_map(labels: list[str]) -> dict[str, int]:
    idx: dict[str, int] = {}
    for i, lab in enumerate(labels):
        if lab:
            idx.setdefault(lab, i)
    return idx


def _candidate_maps(labels: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """Две раскладки колонок.

    В шапке есть пустые колонки-разделители, а в строках тела их может как
    быть, так и не быть — и они стоят В СЕРЕДИНЕ строки. Поэтому единый сдвиг
    не спасает: нужно выбирать раскладку целиком.

    full       — ячейки тела 1:1 с ячейками шапки (разделители есть везде)
    compressed — тело без разделителей, поэтому индексы считаем только по
                 непустым заголовкам
    """
    full = _index_map(labels)
    compressed = _index_map([lab for lab in labels if lab])
    return full, compressed


def _plausibility_yelo(col) -> int:
    """Критерий правдоподобия для таблицы yElo — колонки там свои."""
    score = 0
    v = _num(col("yelo"))
    if v is not None and ELO_MIN <= v <= ELO_MAX:
        score += 3
    elif v is not None:
        score -= 3
    rank = _num(col("rank"))
    if rank is not None and 1 <= rank <= 5000 and float(rank).is_integer():
        score += 1
    for key in ("wins", "losses"):
        n = _num(col(key))
        if n is not None and 0 <= n <= 250 and float(n).is_integer():
            score += 1
        elif n is not None:
            score -= 1
    return score


def _plausibility(col) -> int:
    """Сколько полей строки выглядят осмысленно при данной раскладке.

    Подбор вместо угадывания: у шапки и тела на этой странице разное число
    ячеек, причём разделители стоят в середине, поэтому ни прямое
    сопоставление, ни один общий сдвиг не работают на всех строках.
    Зато правильная раскладка легко узнаётся по значениям: рейтинги лежат
    в диапазоне 500–3500, возраст 10–60, пик — это дата вида YYYY-MM.
    """
    score = 0
    for key in ("elo", "helo", "celo", "gelo", "peak elo"):
        v = _num(col(key))
        if v is not None and ELO_MIN <= v <= ELO_MAX:
            score += 2
        elif v is not None:
            score -= 2          # число есть, но это явно не рейтинг
    for key in ("elo rank", "helo rank", "celo rank", "gelo rank"):
        v = _num(col(key))
        if v is not None and 1 <= v <= 5000 and float(v).is_integer():
            score += 1
    age = _num(col("age"))
    if age is not None and 10 <= age <= 60:
        score += 1
    pm = col("peak month") or ""
    if re.fullmatch(r"\d{4}-\d{2}", pm.strip()):
        score += 2
    elif pm.strip():
        score -= 1
    return score


def _make_reader(tds, labels: list[str], kind: str = "elo"):
    """Возвращает col(*names) с раскладкой, подобранной по этой строке."""
    scorer = _plausibility_yelo if kind == "yelo" else _plausibility
    variants = []
    for base in _candidate_maps(labels):
        for shift in range(-4, 5):
            variants.append({k: v + shift for k, v in base.items()})

    def make(idx):
        def col(*names):
            for n in names:
                i = idx.get(n)
                if i is not None and 0 <= i < len(tds):
                    return tds[i].get_text(" ", strip=True)
            return None
        return col

    best, best_score = None, None
    for idx in variants:
        col = make(idx)
        score = scorer(col)
        if best_score is None or score > best_score:
            best, best_score = col, score
    return best


def parse_elo(html: str) -> EloTable:
    soup = BeautifulSoup(html, "lxml")
    tbl = _pick_table(soup, "elo")
    if tbl is None:
        raise ValueError("Таблица Elo не найдена — вероятно, изменилась вёрстка страницы")

    labels = _header_labels(tbl)
    updated = _extract_updated(soup)
    table = EloTable(updated=updated)
    skipped = 0

    for tr in tbl.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        col = _make_reader(tds, labels, kind="elo")
        link = tr.find("a", href=re.compile(r"player\.cgi"))
        player = (link.get_text(strip=True) if link else col("player")) or ""
        if not player:
            continue

        elo = _num(col("elo"))
        if elo is not None and not (ELO_MIN <= elo <= ELO_MAX):
            elo = None
            skipped += 1

        row = EloRow(
            player=player,
            elo_rank=_int(col("elo rank", "rank")),
            age=_num(col("age")),
            elo=elo,
            helo_rank=_int(col("helo rank")),
            helo=_num(col("helo")),
            celo_rank=_int(col("celo rank")),
            celo=_num(col("celo")),
            gelo_rank=_int(col("gelo rank")),
            gelo=_num(col("gelo")),
            peak_elo=_num(col("peak elo")),
            peak_month=(col("peak month") or None),
            atp_rank=_int(col("atp rank", "wta rank")),
            log_diff=_num(col("log diff")),
            updated=updated,
        )
        table.rows[normalize(player)] = row

    if skipped:
        log.warning("У %d игроков Elo вне диапазона — колонки не распознаны", skipped)
    log.info("Elo: разобрано %d игроков (обновление %s)", len(table), updated)
    return table


def parse_yelo(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "lxml")
    tbl = _pick_table(soup, "yelo")
    if tbl is None:
        raise ValueError("Таблица yElo не найдена")

    labels = _header_labels(tbl)
    updated = _extract_updated(soup)
    out: dict[str, dict] = {}

    for tr in tbl.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        col = _make_reader(tds, labels, kind="yelo")
        link = tr.find("a", href=re.compile(r"player\.cgi"))
        player = (link.get_text(strip=True) if link else col("player")) or ""
        if not player:
            continue
        yv = _num(col("yelo"))
        out[normalize(player)] = {
            "player": player,
            "yelo_rank": _int(col("rank")),
            "yelo": yv if (yv is None or ELO_MIN <= yv <= ELO_MAX) else None,
            "yelo_wins": _int(col("wins")),
            "yelo_losses": _int(col("losses")),
            "updated": updated,
        }
    log.info("yElo: разобрано %d игроков", len(out))
    return out


def load_ratings(fetcher: Fetcher, tour: str = "atp", *, force: bool = False) -> EloTable:
    """Скачивает обе таблицы и склеивает yElo в EloTable."""
    table = parse_elo(fetcher.get(ELO_URL[tour], force=force))
    yelo = parse_yelo(fetcher.get(YELO_URL[tour], force=force))

    for key, row in table.rows.items():
        y = yelo.get(key)
        if y is None:
            hit = match_name(row.player, yelo)  # type: ignore[arg-type]
            y = hit if isinstance(hit, dict) else None
        if y:
            row.yelo = y["yelo"]
            row.yelo_rank = y["yelo_rank"]
            row.yelo_wins = y["yelo_wins"]
            row.yelo_losses = y["yelo_losses"]
    return table


# ------------------------------------------------------------------ прогноз
def elo_win_probability(elo_a: float, elo_b: float, best_of: int = 3) -> float:
    """Вероятность победы A. Классическая логистическая формула Elo.

    Для bo5 фаворит выигрывает чаще — грубая поправка через сжатие разницы.
    """
    diff = elo_a - elo_b
    if best_of == 5:
        diff *= 1.25
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def blended_elo(row: EloRow, surface: str | None, weight_surface: float = 0.5) -> float | None:
    """Смесь общего Elo и покрытия.

    TennisAbstract уже отдаёт hElo/cElo/gElo как смесь, поэтому по умолчанию
    50/50 — просто способ смягчить шум на маленькой выборке по покрытию.
    """
    if surface is None:
        return row.elo
    s = row.surface_elo(surface)
    if s is None:
        return row.elo
    if row.elo is None:
        return s
    w = max(0.0, min(1.0, weight_surface))
    return row.elo * (1 - w) + s * w
