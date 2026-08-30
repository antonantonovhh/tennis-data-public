"""Парсер tennisratio.com — H2H, карточки игроков и история матчей.

ВАЖНО: шапка страницы (имена, возраст, ранг, баланс за 52 недели, текстовое
превью) отдаётся в статическом HTML. А таблицы «<Игрок> Match History»
рисуются на клиенте (DataTables) — в исходном HTML там `0 matches`.

Отсюда три стратегии, по убыванию дешевизны:
  1. static  — вдруг таблица всё-таки заполнена (вёрстка может поменяться);
  2. embedded — ищем JSON с матчами в <script> на странице;
  3. render  — Playwright, ждём заполнения tbody, жмём «All» в селекторе Show.

Режим выбирается автоматически, если не задан явно.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime

from bs4 import BeautifulSoup

from .api import fetch_comparison as api_comparison
from .comparison import is_complete, merge_comparison, parse_comparison
from .http import UA, Fetcher
from .names import slugify

log = logging.getLogger(__name__)

BASE = "https://www.tennisratio.com"


def h2h_url(p1: str, p2: str, tab: str | None = None) -> str:
    url = f"{BASE}/h2h-compare/{slugify(p1)}-vs-{slugify(p2)}.html"
    if tab:
        url += f"?tab={slugify(tab)}-matches"
    return url


# ------------------------------------------------------------------ helpers
def _num(text: str | None) -> float | None:
    if not text:
        return None
    t = text.replace("\xa0", " ").replace("%", "").strip()
    m = re.search(r"-?\d+(?:[.,]\d+)?", t)
    return float(m.group().replace(",", ".")) if m else None


def _int(text: str | None) -> int | None:
    v = _num(text)
    return int(v) if v is not None else None


def _pct(text: str | None) -> float | None:
    """'5/8' -> 62.5, '60.3' -> 60.3.

    Часть колонок сайт отдаёт дробью (брейк-пойнты, геймы), часть — процентом.
    Приводим к одному виду, а исходную строку сохраняем в Match.raw.
    """
    if not text:
        return None
    t = text.strip()
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", t)
    if m:
        won, total = int(m.group(1)), int(m.group(2))
        return round(won / total * 100, 1) if total else None
    return _num(t)


def _parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y",
                "%d/%m/%Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ модели
@dataclass
class PlayerCard:
    name: str
    country: str | None = None
    age: int | None = None
    birthdate: str | None = None
    hand: str | None = None
    rank: int | None = None
    peak_rank: int | None = None
    wins_52w: int | None = None
    losses_52w: int | None = None
    win_pct_52w: float | None = None
    profile_url: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Match:
    date: date | None
    tournament: str | None
    rival: str | None
    round: str | None
    score: str | None
    self_odd: float | None = None
    rival_odd: float | None = None
    rival_rank: int | None = None
    first_serve_in: float | None = None
    first_serve_won: float | None = None
    second_serve_won: float | None = None
    aces: int | None = None
    double_faults: int | None = None
    bp_saved: float | None = None
    service_games_won: float | None = None
    return_first_won: float | None = None
    return_second_won: float | None = None
    bp_converted: float | None = None
    return_games_won: float | None = None
    surface: str | None = None
    # производные
    won: bool | None = None
    sets_played: int | None = None
    games_played: int | None = None
    raw: dict = field(default_factory=dict)   # исходные дроби: {'bp_saved': '5/8'}

    def as_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat() if self.date else None
        return d


@dataclass
class H2H:
    player1: PlayerCard
    player2: PlayerCard
    total_meetings: int = 0
    wins_p1: int = 0
    wins_p2: int = 0
    preview_text: str | None = None
    last_meeting: dict | None = None
    matches_p1: list[Match] = field(default_factory=list)
    matches_p2: list[Match] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)
    api_p1: dict | None = None      # сводка из API: ранг, рука, баланс по ролям
    api_p2: dict | None = None
    source_url: str | None = None

    def as_dict(self) -> dict:
        return {
            "player1": self.player1.as_dict(),
            "player2": self.player2.as_dict(),
            "total_meetings": self.total_meetings,
            "wins_p1": self.wins_p1,
            "wins_p2": self.wins_p2,
            "last_meeting": self.last_meeting,
            "preview_text": self.preview_text,
            "matches_p1": [m.as_dict() for m in self.matches_p1],
            "matches_p2": [m.as_dict() for m in self.matches_p2],
            "comparison": self.comparison,
            "api_p1": self.api_p1,
            "api_p2": self.api_p2,
            "source_url": self.source_url,
        }


# ------------------------------------------------------------------ счёт
_SET_RE = re.compile(r"(\d+)\s*[-–:]\s*(\d+)(?:\s*\(\d+\))?")


def parse_score(score: str | None) -> tuple[int, int, bool | None]:
    """-> (сыграно сетов, сыграно геймов, победа с точки зрения первого счёта).

    Понимает '6-3 6-1', '7-6(5) 4-6 6-2', '6-3 2-1 RET', '6-0 6-0 W/O'.
    """
    if not score:
        return 0, 0, None
    s = score.strip()
    retired = bool(re.search(r"\b(ret|w/?o|def|walkover)\b", s, re.I))
    sets = _SET_RE.findall(s)
    if not sets:
        return 0, 0, None

    games = sum(int(a) + int(b) for a, b in sets)
    won_sets = sum(1 for a, b in sets if int(a) > int(b))
    lost_sets = len(sets) - won_sets

    if retired:
        won = None  # по счёту не определить, кто снялся
    else:
        won = won_sets > lost_sets
    return len(sets), games, won


_SURFACE_HINTS = [
    (re.compile(r"\bclay\b|\bгрунт\b", re.I), "clay"),
    (re.compile(r"\bgrass\b|\bтрава\b", re.I), "grass"),
    (re.compile(r"\bcarpet\b", re.I), "carpet"),
    (re.compile(r"\bhard\b|\bindoor\b|\bхард\b", re.I), "hard"),
]


def guess_surface(text: str | None) -> str | None:
    if not text:
        return None
    for rx, name in _SURFACE_HINTS:
        if rx.search(text):
            return name
    return None


_SLAM_RX = re.compile(
    r"\bgrand slam|\bus open\b|\bwimbledon\b|\broland garros\b"
    r"|\bfrench open\b|\baustralian open\b", re.I)
# «Us Open Qualies», «Qualifying», «Qual.» — в квалификации «Шлема» играют
# до двух побед, как на обычном турнире.
_QUALI_RX = re.compile(r"\bquali", re.I)


def guess_best_of(text: str | None, tour: str = "atp") -> int:
    """Сколько сетов в матче: 5 или 3. Определяется по названию турнира.

    До трёх побед играют только мужчины и только в основной сетке «Большого
    шлема»: у женщин пять сетов не бывает нигде, в квалификации «Шлема» —
    тоже до двух. Название приходит с афиши в виде «Grand Slams Us Open
    (Hard) · Round of 128», квалификация — «Grand Slams Us Open Qualies».

    Формат меняет не подпись под симуляцией, а сами цифры: и вероятность
    победы (в пяти сетах фаворит реализует перевес чаще), и распределение
    сетов, из которого считаются тоталы и форы. Pinnacle на bo5 публикует
    тотал 3.5/4.5 и форы по сетам до ±2.5 — вероятности к ним из трёхсетовой
    симуляции получаются не приближённые, а бессмысленные: «ТМ 3.5» там
    выпадает всегда.
    """
    if not text or (tour or "atp").lower() != "atp":
        return 3
    if _QUALI_RX.search(text):
        return 3
    return 5 if _SLAM_RX.search(text) else 3


# ------------------------------------------------------------------ карточки
_AGE_RE = re.compile(r"Age:\s*(\d+)\s*\((\d{2}-\d{2}-\d{4})\)")
_HAND_RE = re.compile(r"Hand:\s*(Right|Left)", re.I)
_PEAK_RE = re.compile(r"Highest ranked:\s*(\d+)", re.I)
_WL_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*(\d+(?:\.\d+)?)%\s*wins", re.I)


def _parse_cards(soup: BeautifulSoup) -> list[PlayerCard]:
    """Карточки игроков из шапки H2H-страницы."""
    cards: list[PlayerCard] = []
    for a in soup.find_all("a", href=re.compile(r"/players/[^/]+\.html")):
        block = a.find_parent(["div", "section", "article"])
        hops = 0
        while block is not None and hops < 4 and "Age:" not in block.get_text(" ", strip=True):
            block = block.find_parent(["div", "section", "article"])
            hops += 1
        if block is None:
            continue
        text = re.sub(r"\s+", " ", block.get_text(" ", strip=True))

        img = block.find("img", alt=re.compile(r"ATP #|Tennis Player"))
        name = None
        if img and img.get("alt"):
            name = img["alt"].split(" - ")[0].strip()
        if not name:
            name = a.get_text(strip=True).replace("Full Profile", "").replace("→", "").strip()

        flag = block.find("img", src=re.compile(r"/flags/"))
        country = None
        if flag and flag.get("src"):
            country = flag["src"].rsplit("/", 1)[-1].split(".")[0]

        age_m = _AGE_RE.search(text)
        rank_m = re.search(r"#(\d+)\s*Peak", text) or re.search(r"ATP\s*#(\d+)", text)
        wl_m = _WL_RE.search(text)
        peak_m = _PEAK_RE.search(text)
        hand_m = _HAND_RE.search(text)

        card = PlayerCard(
            name=name,
            country=country,
            age=int(age_m.group(1)) if age_m else None,
            birthdate=age_m.group(2) if age_m else None,
            hand=hand_m.group(1).title() if hand_m else None,
            rank=int(rank_m.group(1)) if rank_m else None,
            peak_rank=int(peak_m.group(1)) if peak_m else None,
            wins_52w=int(wl_m.group(1)) if wl_m else None,
            losses_52w=int(wl_m.group(2)) if wl_m else None,
            win_pct_52w=float(wl_m.group(3)) if wl_m else None,
            profile_url=BASE + a["href"] if a["href"].startswith("/") else a["href"],
        )
        if card.name and all(c.name != card.name for c in cards):
            cards.append(card)
    return cards[:2]


_MEET_RE = re.compile(
    r"have met\s+(\d+)\s+times?.*?with\s+(.+?)\s+leading\s+(\d+)\s*-\s*(\d+)", re.I | re.S
)
_LAST_RE = re.compile(
    r"most recent encounter was in\s+(\w+\s+\d{4})\s+at\s+(.+?),\s*where\s+(.+?)\s+prevailed"
    r"(?:\s+on\s+(\w+)\s+court)?\s+([\d\s\-\(\)]+)",
    re.I | re.S,
)


def _parse_preview(soup: BeautifulSoup, cards: list[PlayerCard]) -> dict:
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    out: dict = {"total_meetings": 0, "wins_p1": 0, "wins_p2": 0,
                 "preview_text": None, "last_meeting": None}

    m = _MEET_RE.search(text)
    if not m:
        m2 = re.search(r"have met\s+(\d+)\s+times?", text, re.I)
        if m2:
            out["total_meetings"] = int(m2.group(1))
    else:
        total, leader, a, b = int(m.group(1)), m.group(2).strip(), int(m.group(3)), int(m.group(4))
        out["total_meetings"] = total
        if cards and leader.lower().startswith(cards[0].name.lower()[:6]):
            out["wins_p1"], out["wins_p2"] = a, b
        else:
            out["wins_p1"], out["wins_p2"] = b, a

    lm = _LAST_RE.search(text)
    if lm:
        out["last_meeting"] = {
            "when": lm.group(1),
            "tournament": lm.group(2).strip(),
            "winner": lm.group(3).strip(),
            "surface": (lm.group(4) or "").lower() or None,
            "score": " ".join(lm.group(5).split()),
        }

    pv = re.search(r"Match-up Preview(.{100,4000}?)Read full match preview", text, re.I | re.S)
    if pv:
        out["preview_text"] = pv.group(1).strip()
    return out


# ------------------------------------------------------------------ матчи
# Реальные заголовки таблицы на сайте (снято со страницы):
# DATE | TOURNAMENT | RIVAL | ROUND | RESULT | SELF ODD | RIVAL'S ODD |
# RIVAL'S RANK | 1ST SRV ACCURACY % | 1ST SRV PTS % | 2ND SRV PTS % | ACES |
# DFS | BPS SAVED | SRV GAMES WON | RETURN 1ST SRV PTS % | RETURN 2ND SRV PTS % |
# BPS CONVERTED | RET GAMES WON
_HEADER_MAP = {
    "date": "date",
    "tournament": "tournament",
    "rival s rank": "rival_rank",
    "rival s odd": "rival_odd",
    "rival": "rival",
    "round": "round",
    "result": "score",          # колонка называется RESULT, не SCORE
    "score": "score",
    "self odd": "self_odd",
    "1st srv accuracy": "first_serve_in",
    "1st srv acc": "first_serve_in",
    "1st srv pts": "first_serve_won",
    "2nd srv pts": "second_serve_won",
    "aces": "aces",
    "dfs": "double_faults",
    "bps saved": "bp_saved",
    "srv games won": "service_games_won",
    "return 1st srv pts": "return_first_won",
    "return 2nd srv pts": "return_second_won",
    "ret 1st pts": "return_first_won",
    "ret 2nd pts": "return_second_won",
    "bps conv": "bp_converted",
    "ret games won": "return_games_won",
}

# колонки, приходящие дробью «выиграно/всего»
_FRAC_FIELDS = {"bp_saved", "service_games_won", "bp_converted", "return_games_won"}

_INT_FIELDS = {"aces", "double_faults", "rival_rank"}


def _norm_header(text: str) -> str | None:
    t = re.sub(r"[%\d]*$", "", re.sub(r"\s+", " ", text.strip().lower()))
    t = t.replace("'", " ").replace("’", " ").replace("%", "")
    t = re.sub(r"\s+", " ", t).strip()
    # сначала самые длинные ключи: "rival s rank" не должен схлопнуться в "rival"
    for key in sorted(_HEADER_MAP, key=len, reverse=True):
        if t.startswith(key):
            return _HEADER_MAP[key]
    return None


def _parse_match_table(tbl) -> list[Match]:
    head = tbl.find("thead") or tbl
    header_cells = head.find_all("th") or head.find("tr").find_all(["th", "td"])
    fields = [_norm_header(c.get_text(" ", strip=True)) for c in header_cells]

    body = tbl.find("tbody") or tbl
    matches: list[Match] = []
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        raw: dict[str, str] = {}
        for f, td in zip(fields, tds):
            if f:
                raw[f] = td.get_text(" ", strip=True)
        if not raw.get("date") and not raw.get("score"):
            continue

        m = Match(
            date=_parse_date(raw.get("date", "")),
            tournament=raw.get("tournament") or None,
            rival=raw.get("rival") or None,
            round=raw.get("round") or None,
            score=raw.get("score") or None,
        )
        for f, val in raw.items():
            if f in {"date", "tournament", "rival", "round", "score"}:
                continue
            if f in _INT_FIELDS:
                setattr(m, f, _int(val))
            elif f in _FRAC_FIELDS:
                setattr(m, f, _pct(val))
                m.raw[f] = val          # '5/8' полезнее процента при разборе
            else:
                setattr(m, f, _num(val))

        sets_, games_, won = parse_score(m.score)
        m.sets_played, m.games_played, m.won = sets_, games_, won
        m.surface = guess_surface(m.tournament)
        matches.append(m)
    return matches


def _find_match_tables(soup: BeautifulSoup) -> dict[str, list[Match]]:
    """Возвращает {имя игрока (или 'table_N'): [матчи]} по заголовкам H2."""
    out: dict[str, list[Match]] = {}
    for i, tbl in enumerate(soup.find_all("table")):
        header = " ".join(
            th.get_text(" ", strip=True) for th in (tbl.find("thead") or tbl).find_all(["th", "td"], limit=8)
        ).lower()
        if "rival" not in header or not ("result" in header or "score" in header):
            continue
        rows = _parse_match_table(tbl)
        title = None
        h = tbl.find_previous(["h2", "h3"])
        if h:
            t = h.get_text(" ", strip=True)
            mm = re.match(r"(.+?)\s+Match History", t, re.I)
            title = mm.group(1).strip() if mm else t
        out[title or f"table_{i}"] = rows
    return out


# ------------------------------------------------------------------ рендер
def tab_slugs_from_url(url: str) -> list[str]:
    """.../jan-kumstat-vs-maxim-mrva.html -> ['jan-kumstat-matches', 'maxim-mrva-matches']"""
    m = re.search(r"/h2h-compare/([a-z0-9\-]+?)\.html", url, re.I)
    if not m or "-vs-" not in m.group(1).lower():
        return []
    a, b = m.group(1).lower().split("-vs-", 1)
    return [f"{a}-matches", f"{b}-matches"]


# предикат «таблица истории матчей заполнилась»: именно она, а не любая
# таблица на странице — их тут несколько, и сравнительная заполняется первой
_ROWS_READY_JS = """() => {
  const tables = [...document.querySelectorAll('table')];
  return tables.some(t => {
    const head = ((t.querySelector('thead') || t).innerText || '').toLowerCase();
    if (!head.includes('rival')) return false;
    if (!head.includes('result') && !head.includes('score')) return false;
    const body = t.querySelector('tbody') || t;
    return body.querySelectorAll('tr td').length > 5;
  });
}"""


def _show_all_entries(page) -> None:
    """Переключает селектор «Show N entries» на All, чтобы забрать все страницы."""
    for sel in page.query_selector_all("select"):
        try:
            opts = [o.inner_text().strip() for o in sel.query_selector_all("option")]
            low = [o.lower() for o in opts]
            if "all" in low:
                sel.select_option(label=opts[low.index("all")])
            else:
                # варианта All нет — берём наибольшее число (10/25/50/100)
                nums = [(int(o), o) for o in opts if o.isdigit()]
                if not nums:
                    continue
                sel.select_option(label=max(nums)[1])
            page.wait_for_timeout(1500)
        except Exception:
            continue


def _dump(html: str, target: str) -> None:
    """TP_DUMP_DIR=/tmp/tp — сохранить отрендеренную страницу для разбора.

    Без этого отладка вёрстки превращается в гадание: локально страницу
    не воспроизвести, а на сервере её никто не видит.
    """
    import os

    d = os.environ.get("TP_DUMP_DIR")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        name = re.sub(r"[^a-z0-9]+", "_", target.lower())[-80:] + ".html"
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        log.info("HTML сохранён: %s (%d байт)", path, len(html))
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось сохранить HTML: %s", exc)


def _log_tables(html: str, target: str) -> None:
    """Пишет в лог, какие таблицы нашлись и сколько в них строк."""
    try:
        soup = BeautifulSoup(html, "lxml")
        info = []
        for i, tbl in enumerate(soup.find_all("table")):
            head = " ".join(
                c.get_text(" ", strip=True)
                for c in (tbl.find("thead") or tbl).find_all(["th", "td"], limit=6)
            )
            rows = len((tbl.find("tbody") or tbl).find_all("tr"))
            info.append(f"[{i}] строк={rows} шапка={head[:70]!r}")
        log.info("Таблицы на %s:\n  %s", target, "\n  ".join(info) or "нет таблиц")
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось разобрать таблицы для лога: %s", exc)


_COMPARE_READY_JS = """() => {
  const t = (document.body.innerText || '').toLowerCase();
  return t.includes('key stats') && /\\d\\.\\d/.test(t);
}"""

# бюджеты по умолчанию; переопределяются переменными окружения
DEF_BUDGET_S = float(os.environ.get("TP_RENDER_BUDGET", "110"))
DEF_WAIT_MS = int(os.environ.get("TP_RENDER_WAIT_MS", "15000"))
RENDER_CACHE_TTL = int(os.environ.get("TP_RENDER_TTL", str(30 * 60)))
RENDER_CACHE_DIR = os.environ.get("TP_RENDER_CACHE", ".cache/tennis/render")


def _rc_path(target: str) -> str:
    # покрытие входит в target (…#hard#overall) — иначе отфильтрованная
    # страница и общая делили бы одну запись кэша
    key = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
    return os.path.join(RENDER_CACHE_DIR, f"{key}.html")


def _rc_get(target: str) -> str | None:
    """Кэш уже отрисованных страниц: повторный клик по кнопке должен быть мгновенным."""
    try:
        path = _rc_path(target)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < RENDER_CACHE_TTL:
            with open(path, encoding="utf-8") as fh:
                log.info("рендер из кэша: %s", target)
                return fh.read()
    except Exception:
        pass
    return None


def _rc_put(target: str, html: str) -> None:
    try:
        os.makedirs(RENDER_CACHE_DIR, exist_ok=True)
        with open(_rc_path(target), "w", encoding="utf-8") as fh:
            fh.write(html)
    except Exception as exc:  # noqa: BLE001
        log.debug("кэш рендера не записался: %s", exc)


def _show_all_entries(page) -> None:
    """Переключает «Show N entries» на максимум — но только у нужного селектора.

    Раньше перебирались все select на странице с паузой на каждый; на этой
    странице их много (фильтры периода, уровня, покрытия), и уходили лишние
    секунды на элементы, к таблице отношения не имеющие.
    """
    for sel in page.query_selector_all("select"):
        try:
            opts = [o.inner_text().strip() for o in sel.query_selector_all("option")]
            nums = [(int(o), o) for o in opts if o.isdigit()]
            low = [o.lower() for o in opts]
            if "all" in low:
                sel.select_option(label=opts[low.index("all")])
            elif len(nums) >= 2:          # похоже на 10/25/50/100
                sel.select_option(label=max(nums)[1])
            else:
                continue                  # не тот селектор — не тратим время
            page.wait_for_timeout(900)
            return                        # он на странице один, дальше не ищем
        except Exception:
            continue


SURFACE_BUTTON = {"hard": "HARD", "clay": "CLAY", "grass": "GRASS"}


def _click_surface(page, surface: str | None) -> bool:
    """Жмёт фильтр покрытия на вкладке сравнения.

    Только для блоков сравнения: историю матчей фильтровать нельзя, усталость
    копится от всех матчей подряд, а не от игранных на одном покрытии.
    """
    label = SURFACE_BUTTON.get((surface or "").lower())
    if not label:
        return False
    for sel in (f"text=/^\\s*{label}\\s*$/i", f"button:has-text('{label}')"):
        try:
            page.click(sel, timeout=3000)
            page.wait_for_timeout(1600)
            log.info("фильтр покрытия: %s", label)
            return True
        except Exception:
            continue
    log.warning("не удалось нажать фильтр покрытия %s", label)
    return False


def render_all(
    url: str,
    tabs: list[str] | None = None,
    *,
    want_compare: bool = True,
    headless: bool = True,
    budget_s: float = DEF_BUDGET_S,
    surface: str | None = None,
) -> tuple[list[str], list[str]]:
    """Один браузер, одна вкладка, все страницы за проход.

    Возвращает (html блоков сравнения, html вкладок с матчами).

    Раньше на запрос поднималось два Chromium подряд, а каждое ожидание
    висело до 30 с и ещё столько же в запасном варианте — отсюда минуты.
    Теперь общий бюджет времени: что не успели, то пропускаем, а не ждём.
    """
    started = time.monotonic()

    def left() -> float:
        return budget_s - (time.monotonic() - started)

    compare_html: list[str] = []
    tab_html: list[str] = []
    targets = [f"{url}?tab={t}" for t in (tabs or [])]
    surf_key = f"#{(surface or 'all').lower()}"

    # сперва кэш: если всё есть, браузер не нужен вовсе
    if want_compare:
        for suffix in (f"{surf_key}#overall", f"{surf_key}#serve"):
            got = _rc_get(url + suffix)
            if got:
                compare_html.append(got)
    cached_tabs = [(_rc_get(t), t) for t in targets]
    if all(h for h, _ in cached_tabs) and (not want_compare or compare_html):
        log.info("всё взято из кэша рендера, браузер не запускался")
        return compare_html, [h for h, _ in cached_tabs]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Нужен Playwright: pip install playwright && playwright install chromium"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 2200}, user_agent=UA)
            page.set_default_timeout(DEF_WAIT_MS)

            # ---- вкладка Compare Players ----
            if want_compare and not compare_html and left() > 15:
                t0 = time.monotonic()
                try:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=min(DEF_WAIT_MS, int(left() * 1000)))
                    _click_surface(page, surface)
                    try:
                        page.wait_for_function(
                            _COMPARE_READY_JS,
                            timeout=min(DEF_WAIT_MS, max(3000, int(left() * 1000))),
                        )
                    except Exception:
                        log.warning("Key Stats не дождался, беру как есть")
                    html = page.content()
                    _rc_put(url + surf_key + "#overall", html)
                    _dump(html, url + surf_key + "#overall")
                    compare_html.append(html)

                    if left() > 8:
                        for sel in ("text=/SERVE\\s*&\\s*RETURN/i", "text=/SERVE AND RETURN/i"):
                            try:
                                page.click(sel, timeout=3000)
                                page.wait_for_timeout(1500)
                                html2 = page.content()
                                _rc_put(url + surf_key + "#serve", html2)
                                _dump(html2, url + surf_key + "#serve")
                                compare_html.append(html2)
                                break
                            except Exception:
                                continue
                        else:
                            log.warning("подвкладка Serve & Return не переключилась")
                except Exception as exc:  # noqa: BLE001
                    log.warning("вкладка сравнения не отрисовалась: %s", exc)
                log.info("сравнение: %.1f с", time.monotonic() - t0)

            # ---- вкладки с матчами ----
            for cached, target in cached_tabs:
                if cached:
                    tab_html.append(cached)
                    continue
                if left() < 10:
                    log.warning("бюджет времени исчерпан, пропускаю %s", target)
                    continue
                t0 = time.monotonic()
                try:
                    page.goto(target, wait_until="domcontentloaded",
                              timeout=min(DEF_WAIT_MS, int(left() * 1000)))
                    try:
                        page.wait_for_function(
                            _ROWS_READY_JS,
                            timeout=min(DEF_WAIT_MS, max(3000, int(left() * 1000))),
                        )
                        _show_all_entries(page)
                    except Exception:
                        log.warning("строки матчей не появились: %s", target)
                    html = page.content()
                    _rc_put(target, html)
                    _dump(html, target)
                    _log_tables(html, target)
                    tab_html.append(html)
                except Exception as exc:  # noqa: BLE001
                    log.warning("вкладка %s не отрисовалась: %s", target, exc)
                log.info("вкладка %s: %.1f с", target[-28:], time.monotonic() - t0)
        finally:
            browser.close()

    log.info("рендер всего: %.1f с", time.monotonic() - started)
    return compare_html, tab_html


_JSON_RE = re.compile(
    r"(?:matches|matchData|rows|data)\s*[:=]\s*(\[\s*\{.{200,}?\}\s*\])\s*[,;\n]", re.S
)


def _embedded_matches(html: str) -> dict[str, list[Match]]:
    """Пытается вытащить матчи из JSON внутри <script>."""
    out: dict[str, list[Match]] = {}
    for i, m in enumerate(_JSON_RE.finditer(html)):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        rows: list[Match] = []
        for rec in data:
            if not isinstance(rec, dict):
                continue
            get = lambda *k: next((rec[x] for x in k if x in rec), None)  # noqa: E731
            mt = Match(
                date=_parse_date(str(get("date", "Date", "match_date") or "")),
                tournament=get("tournament", "Tournament"),
                rival=get("rival", "Rival", "opponent"),
                round=get("round", "Round"),
                score=get("score", "Score"),
            )
            mt.sets_played, mt.games_played, mt.won = parse_score(mt.score)
            mt.surface = guess_surface(mt.tournament)
            rows.append(mt)
        if rows:
            out[f"embedded_{i}"] = rows
    return out


# ------------------------------------------------------------------ фасад
def fetch_h2h(
    fetcher: Fetcher,
    p1: str,
    p2: str,
    *,
    mode: str = "auto",          # auto | static | render
    headless: bool = True,
    force: bool = False,
    url: str | None = None,
    want_comparison: bool = True,
    surface: str | None = None,
) -> H2H:
    # url можно передать напрямую — тогда slug не собирается из имён
    # (полезно, когда ссылку прислал пользователь: там уже правильный slug)
    url = url or h2h_url(p1, p2)
    html = fetcher.get(url, force=force)
    soup = BeautifulSoup(html, "lxml")

    cards = _parse_cards(soup)
    if len(cards) < 2:
        cards += [PlayerCard(name=n) for n in (p1, p2)][len(cards):]

    preview = _parse_preview(soup, cards)

    # Блоки сравнения. Порядок по стоимости:
    #   1) JSON API — доли секунды, все 22 показателя, фильтр покрытия параметром
    #   2) статический HTML — бесплатно, но обычно пусто
    #   3) рендер в браузере — десятки секунд, запасной путь
    comparison: dict = {}
    api_p1 = api_p2 = None
    if want_comparison:
        try:
            comparison, api_p1, api_p2 = api_comparison(
                fetcher, cards[0].name or p1, cards[1].name or p2,
                surface=surface, referer=url,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("API сравнения недоступен: %s", exc)

    if not comparison:
        comparison = parse_comparison(html)
        if comparison:
            comparison["_surface"] = "all"

    tables = _find_match_tables(soup)
    total_rows = sum(len(v) for v in tables.values())

    if total_rows == 0 and mode in {"auto", "render"}:
        tables = _embedded_matches(html)
        total_rows = sum(len(v) for v in tables.values())

    need_compare = not is_complete(comparison) and want_comparison
    need_matches = total_rows == 0

    if (need_compare or need_matches) and mode in {"auto", "render"}:
        tabs = (
            tab_slugs_from_url(url)
            or [f"{slugify(cards[0].name)}-matches", f"{slugify(cards[1].name)}-matches"]
        ) if need_matches else []
        try:
            cmp_pages, tab_pages = render_all(
                url, tabs, want_compare=need_compare, headless=headless,
                surface=surface,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Рендер не удался: %s", exc)
            cmp_pages, tab_pages = [], []

        for rendered in cmp_pages:
            comparison = merge_comparison(comparison, parse_comparison(rendered))
        if comparison and cmp_pages:
            # фиксируем, к какому покрытию относятся показатели
            comparison["_surface"] = (surface or "all").lower()

        if tab_pages:
            merged: dict[str, list[Match]] = {}
            for rendered in tab_pages:
                for title, rows in _find_match_tables(BeautifulSoup(rendered, "lxml")).items():
                    # на вкладке игрока А таблица игрока Б пустая — берём длиннейшую
                    if len(rows) > len(merged.get(title, [])):
                        merged[title] = rows
            if merged:
                tables = merged
                total_rows = sum(len(v) for v in tables.values())
            log.info("После рендера: %s", {k: len(v) for k, v in tables.items()})

    if total_rows == 0:
        log.warning("История матчей пуста. Попробуйте mode='render'.")

    def pick(card: PlayerCard, fallback_idx: int) -> list[Match]:
        want = slugify(card.name or "")
        for title, rows in tables.items():
            # заголовок на странице — 'LUCA VAN ASSCHE MATCH HISTORY - ALL SURFACES',
            # сравниваем слагами, чтобы не спотыкаться о регистр и пробелы
            if want and want in slugify(title):
                return rows
        vals = list(tables.values())
        return vals[fallback_idx] if len(vals) > fallback_idx else []

    return H2H(
        player1=cards[0],
        player2=cards[1],
        total_meetings=preview["total_meetings"],
        wins_p1=preview["wins_p1"],
        wins_p2=preview["wins_p2"],
        preview_text=preview["preview_text"],
        last_meeting=preview["last_meeting"],
        matches_p1=pick(cards[0], 0),
        matches_p2=pick(cards[1], 1),
        comparison=comparison,
        api_p1=api_p1,
        api_p2=api_p2,
        source_url=url,
    )
