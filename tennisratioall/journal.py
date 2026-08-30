"""Два CSV: найденные value-ставки и журнал всех матчей.

Зачем два файла, а не один
--------------------------
`value_bets.csv` — только то, на что модель нашла перевес: узкий список, с ним
работают руками. `matches_log.csv` — вообще всё, что посчитано, включая матчи
без ценности и без ставки. Второй нужен именно для поиска закономерностей:
если писать только ставки, выборка окажется отобранной по результату модели,
и любая проверка калибровки на ней будет врать.

Строки дописываются на этапе прогноза и дополняются после матча. Файлы
небольшие (сотни строк в день), поэтому обновление — это перезапись целиком
через временный файл: проще и не оставляет битых строк при обрыве.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))

# Суффикс тура: у ATP его нет (имена файлов исторические), у WTA — «_wta».
# Смешивать туры в одном журнале нельзя: ROI и калибровка считались бы
# сразу по двум разным популяциям. Определение живёт в store.py, здесь
# только используется — see store.TOUR.
from .store import SUFFIX as _SUF  # noqa: E402

VALUE_CSV = (os.environ.get("TRA_VALUE_CSV")
             or os.path.join(HERE, f"value_bets{_SUF}.csv"))
PICKS_CSV = (os.environ.get("TRA_PICKS_CSV")
             or os.path.join(HERE, f"picks{_SUF}.csv"))
LOG_CSV = (os.environ.get("TRA_LOG_CSV")
           or os.path.join(HERE, f"matches_log{_SUF}.csv"))

_lock = threading.Lock()

VALUE_FIELDS = [
    "bet_id", "found_at", "when", "slug", "p1", "p2", "tournament",
    "surface", "best_of",
    "market", "pick", "line", "odds", "sim_prob", "fair_odds", "edge", "kelly",
    "stake", "status", "profit",
    "score", "sets_p1", "sets_p2", "games_p1", "games_p2", "games_total",
    "games_diff", "resolved_at",
]

# Ставки на исход — отдельно от value. Смысл разный: value-ставка делается,
# когда модель нашла перевес, и таких мало, к тому же почти все в форах и
# тоталах. Ставка на исход делается ВСЕГДА, на того, кого модель считает
# фаворитом, независимо от перевеса. Это чистая мера предсказательной силы:
# по ней видно, права модель или нет, без примеси того, как она выбирает
# рынки.
#
# Колонка agree — согласна ли модель с рынком. По ней и проверяется главное:
# добавляет ли она что-нибудь к простому «ставь на фаворита букмекера».
PICK_FIELDS = [
    "slug", "found_at", "when", "p1", "p2", "tournament", "surface", "best_of",
    "side", "player", "sim_prob", "odds", "odds_p1", "odds_p2",
    "fair_odds", "edge",
    "market_side", "market_prob", "agree", "model_gap",
    "stake", "status", "profit",
    "score", "sets_p1", "sets_p2", "games_p1", "games_p2", "games_diff",
    "winner", "resolved_at",
]

LOG_FIELDS = [
    "slug", "logged_at", "p1", "p2", "tournament", "when", "surface", "best_of",
    # модель
    "sim_p1", "sim_p2", "fair_p1", "fair_p2", "sim_decider", "sim_games_median",
    "sim_games_diff", "elo_p1", "model_gap", "fatigue_delta", "fresher",
    # рынок
    "mkt_p1", "mkt_p2", "mkt_margin", "mkt_implied_p1",
    "mkt_total_sets", "mkt_h_sets", "mkt_h_games", "odds_at",
    # расхождение
    "edge_p1", "edge_p2", "best_edge", "best_market", "value_found",
    # результат
    "score", "sets_p1", "sets_p2", "games_p1", "games_p2", "games_total",
    "games_diff", "winner", "resolved_at",
]


# Поля, которые Excel в русской локали норовит превратить в дату: 2.5 -> 2 мая,
# 1.787 -> фев.23. Пишем их с запятой как разделителем — так Excel читает их
# числами, а не датами, и заодно это привычный формат для ; в качестве
# разделителя колонок.
DECIMAL_FIELDS = {
    "line", "odds", "odds_p1", "odds_p2", "market_prob",
    "sim_prob", "fair_odds", "edge", "kelly", "stake",
    "profit", "sim_p1", "sim_p2", "fair_p1", "fair_p2", "sim_decider",
    "sim_games_diff", "elo_p1", "model_gap", "fatigue_delta", "mkt_p1",
    "mkt_p2", "mkt_margin", "mkt_implied_p1", "edge_p1", "edge_p2",
    "best_edge",
}


def pf(v, default=None):
    """Число из ячейки CSV. Понимает и точку, и запятую."""
    if v in (None, ""):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return default


def _out(field: str, value):
    """Значение для записи: десятичная запятая там, где иначе выйдет дата."""
    if field not in DECIMAL_FIELDS or value in (None, ""):
        return value
    if isinstance(value, (int, float)):
        return str(value).replace(".", ",")
    txt = str(value)
    return txt.replace(".", ",") if pf(txt) is not None else txt


def _read(path: str, fields: list[str]) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        # utf-8-sig снимает BOM, если он есть, и не мешает, если его нет
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh, delimiter=";"))
    except Exception as exc:  # noqa: BLE001
        log.error("%s не читается: %s", path, exc)
        return []


def _write(path: str, fields: list[str], rows: list[dict]) -> None:
    tmp = f"{path}.tmp"
    try:
        # BOM обязателен: без него Excel открывает файл как cp1251,
        # и «П1» превращается в «Pц1»
        with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, delimiter=";",
                               extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: _out(k, v) for k, v in r.items()})
        os.replace(tmp, path)
    except OSError as exc:
        log.error("%s не записан: %s", path, exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ value
def add_value_bets(rec: dict, bets: list[dict], stake: float) -> list[str]:
    """Дописывает найденные ставки. Возвращает их bet_id."""
    if not bets:
        return []
    with _lock:
        rows = _read(VALUE_CSV, VALUE_FIELDS)
        have = {r["bet_id"] for r in rows}
        ids = []
        for b in bets:
            bet_id = f"{rec['slug']}|{b['market']}|{b['pick']}|{b.get('line')}"
            if bet_id in have:
                continue  # тот же матч на следующем круге — не дублируем
            ids.append(bet_id)
            rows.append({
                "bet_id": bet_id, "found_at": _now(), "slug": rec["slug"],
                # Время начала матча, а не только время находки: без него
                # по строке нельзя понять, когда искать результат, и
                # зависшую ставку не отличить от свежей.
                "when": rec.get("when", ""),
                "p1": rec["p1"], "p2": rec["p2"],
                "tournament": rec.get("tournament", ""),
                "surface": rec.get("surface", ""), "best_of": rec.get("best_of", ""),
                "market": b["market"], "pick": b["pick"],
                "line": "" if b.get("line") is None else b["line"],
                "odds": b["odds"], "sim_prob": b["sim_prob"],
                "fair_odds": b["fair_odds"], "edge": b["edge"], "kelly": b["kelly"],
                "stake": stake, "status": "pending", "profit": 0,
            })
        if ids:
            _write(VALUE_CSV, VALUE_FIELDS, rows)
        return ids


def pending_value_bets() -> list[dict]:
    with _lock:
        return [r for r in _read(VALUE_CSV, VALUE_FIELDS)
                if r.get("status") in ("pending", "")]


def resolve_value_bets(slug: str, outcome: dict, settle_fn) -> list[dict]:
    """Закрывает все ставки матча. settle_fn(bet_row) -> (status, profit)."""
    with _lock:
        rows = _read(VALUE_CSV, VALUE_FIELDS)
        touched = []
        for r in rows:
            if r.get("slug") != slug or r.get("status") not in ("pending", ""):
                continue
            status, prof = settle_fn(r)
            r.update(status=status, profit=round(prof, 2),
                     resolved_at=_now(), **outcome)
            touched.append(r)
        if touched:
            _write(VALUE_CSV, VALUE_FIELDS, rows)
        return touched


# ------------------------------------------------------------------ журнал
def log_match(rec: dict, odds: dict | None, bets: list[dict]) -> None:
    """Строка на матч: модель, рынок, расхождение. Результат допишется позже."""
    from .value import market_margin, _num  # noqa: PLC0415

    sim_p1, sim_p2 = rec.get("sim_p1"), rec.get("sim_p2")
    o1 = _num((odds or {}).get("p1"))
    o2 = _num((odds or {}).get("p2"))
    margin = market_margin(o1, o2)
    implied = (1 / o1) / (1 / o1 + 1 / o2) if (o1 and o2) else None
    best = bets[0] if bets else None

    row = {
        "slug": rec["slug"], "logged_at": _now(),
        "p1": rec["p1"], "p2": rec["p2"],
        "tournament": rec.get("tournament", ""), "when": rec.get("when", ""),
        "surface": rec.get("surface", ""), "best_of": rec.get("best_of", ""),
        "sim_p1": sim_p1, "sim_p2": sim_p2,
        "fair_p1": round(1 / sim_p1, 3) if sim_p1 else "",
        "fair_p2": round(1 / sim_p2, 3) if sim_p2 else "",
        "sim_decider": rec.get("decider", ""),
        "sim_games_median": rec.get("games_median", ""),
        "sim_games_diff": rec.get("games_diff_mean", ""),
        "elo_p1": rec.get("elo_p1", ""), "model_gap": rec.get("model_gap", ""),
        "fatigue_delta": rec.get("fatigue_delta", ""),
        "fresher": rec.get("fresher", ""),
        "mkt_p1": o1 or "", "mkt_p2": o2 or "",
        "mkt_margin": round(margin, 4) if margin is not None else "",
        "mkt_implied_p1": round(implied, 4) if implied is not None else "",
        "mkt_total_sets": (odds or {}).get("total_sets", ""),
        "mkt_h_sets": (odds or {}).get("h_sets", ""),
        "mkt_h_games": (odds or {}).get("h_games", ""),
        "odds_at": _now() if odds else "",
        "edge_p1": round(sim_p1 * o1 - 1, 4) if (sim_p1 and o1) else "",
        "edge_p2": round(sim_p2 * o2 - 1, 4) if (sim_p2 and o2) else "",
        "best_edge": best["edge"] if best else "",
        "best_market": f"{best['market']} {best['pick']}" if best else "",
        "value_found": len(bets),
    }
    if not rec.get("slug"):
        log.error("log_match без slug (%s vs %s) — не пишу: такую строку "
                  "потом не с чем сопоставить", rec.get("p1"), rec.get("p2"))
        return
    with _lock:
        rows = _read(LOG_CSV, LOG_FIELDS)
        # матч мог пройти круг без линии, а потом с ней — обновляем ту же строку
        for r in rows:
            if r.get("slug") and r.get("slug") == rec["slug"]:
                r.update(row)
                break
        else:
            rows.append(row)
        _write(LOG_CSV, LOG_FIELDS, rows)


def log_result(slug: str, outcome: dict) -> bool:
    """Записывает результат матча по его slug.

    Пустой slug отвергаем: сравнение `r["slug"] == ""` истинно для КАЖДОЙ
    строки с пустым полем, и один результат размазывался по всему журналу —
    в панели все матчи показывали один и тот же счёт.
    """
    if not slug:
        log.error("log_result вызван с пустым slug — пропускаю, "
                  "иначе результат уйдёт во все строки сразу")
        return False
    with _lock:
        rows = _read(LOG_CSV, LOG_FIELDS)
        hit = 0
        for r in rows:
            if r.get("slug") and r.get("slug") == slug:
                r.update(outcome, resolved_at=_now())
                hit += 1
        if hit > 1:
            log.warning("slug %s встречается в журнале %d раз — "
                        "результат записан во все", slug, hit)
        if hit:
            _write(LOG_CSV, LOG_FIELDS, rows)
        return bool(hit)


def unresolved_slugs() -> list[dict]:
    """Матчи в журнале без результата — их и ищем на TennisExplorer.

    Строки без slug отбрасываем: сопоставить результат с ними всё равно
    нечем, а попытка это сделать портит соседние записи.
    """
    out, broken = [], 0
    for r in _read(LOG_CSV, LOG_FIELDS):
        if r.get("resolved_at"):
            continue
        if not r.get("slug"):
            broken += 1
            continue
        out.append(r)
    if broken:
        log.warning("в журнале %d строк без slug — пропускаю их", broken)
    return out


# ------------------------------------------------------------------ исходы
def add_pick(rec: dict, odds: dict, stake: float) -> dict | None:
    """Записывает ставку на исход по мнению модели. Возвращает строку или None.

    Одна строка на матч: повторный вызов на следующем круге ничего не меняет,
    иначе один матч попадал бы в статистику несколько раз.
    """
    from .value import _num, market_margin  # noqa: PLC0415

    if not rec.get("slug"):
        return None
    sim1, sim2 = rec.get("sim_p1"), rec.get("sim_p2")
    if sim1 is None or sim2 is None:
        return None
    o1, o2 = _num((odds or {}).get("p1")), _num((odds or {}).get("p2"))
    if not (o1 and o2):
        return None   # без цены прибыль не посчитать

    side = "П1" if sim1 >= sim2 else "П2"
    prob = max(sim1, sim2)
    price = o1 if side == "П1" else o2
    inv1, inv2 = 1 / o1, 1 / o2
    mkt1 = inv1 / (inv1 + inv2)
    market_side = "П1" if mkt1 >= 0.5 else "П2"

    row = {
        "slug": rec["slug"], "found_at": _now(),
        "when": rec.get("when", ""),
        "p1": rec["p1"], "p2": rec["p2"],
        "tournament": rec.get("tournament", ""),
        "surface": rec.get("surface", ""), "best_of": rec.get("best_of", ""),
        "side": side,
        "player": rec["p1"] if side == "П1" else rec["p2"],
        "sim_prob": round(prob, 4), "odds": price,
        # Обе цены, а не только своя. Без второй нельзя честно посчитать,
        # сколько дала бы ставка на фаворита рынка, когда модель с ним
        # спорит: восстановленная из вероятностей цена идёт без маржи.
        "odds_p1": o1, "odds_p2": o2,
        "fair_odds": round(1 / prob, 3) if prob else "",
        "edge": round(prob * price - 1, 4),
        "market_side": market_side,
        "market_prob": round(mkt1 if market_side == "П1" else 1 - mkt1, 4),
        "agree": "да" if side == market_side else "нет",
        "model_gap": rec.get("model_gap", ""),
        "stake": stake, "status": "pending", "profit": 0,
    }
    with _lock:
        rows = _read(PICKS_CSV, PICK_FIELDS)
        if any(r.get("slug") == rec["slug"] for r in rows):
            return None
        rows.append(row)
        _write(PICKS_CSV, PICK_FIELDS, rows)
    return row


def resolve_pick(slug: str, outcome: dict) -> dict | None:
    """Закрывает ставку на исход по результату матча.

    Исход — это ставка на победителя, а её Pinnacle при снятии НЕ
    возвращает: если доигран хотя бы один полный сет, снявшийся считается
    проигравшим. Поэтому решает не флаг void, а наличие победителя:
    `outcome_from_score` ставит его у снятия только тогда, когда матч
    присуждён и условие про сет выполнено. Нет победителя — возврат.
    """
    if not slug:
        return None
    with _lock:
        rows = _read(PICKS_CSV, PICK_FIELDS)
        hit = None
        for r in rows:
            if r.get("slug") != slug or r.get("status") not in ("pending", ""):
                continue
            stake = pf(r.get("stake"), 0.0)
            odds = pf(r.get("odds"), 0.0)
            if outcome.get("winner") not in ("p1", "p2"):
                status, prof = "refund", 0.0
            else:
                won = outcome["winner"] == ("p1" if r.get("side") == "П1" else "p2")
                status = "win" if won else "loss"
                prof = stake * (odds - 1) if won else -stake
            r.update(outcome, status=status, profit=round(prof, 2),
                     resolved_at=_now())
            hit = r
        if hit:
            _write(PICKS_CSV, PICK_FIELDS, rows)
        return hit


def picks(period_start=None) -> list[dict]:
    rows = _read(PICKS_CSV, PICK_FIELDS)
    if period_start is None:
        return rows
    out = []
    for r in rows:
        stamp = r.get("resolved_at") or r.get("found_at") or ""
        try:
            from datetime import datetime as _dt
            if _dt.fromisoformat(stamp).date() >= period_start:
                out.append(r)
        except ValueError:
            continue
    return out


RESULT_FIELDS = ("score", "sets_p1", "sets_p2", "games_p1", "games_p2",
                 "games_total", "games_diff", "winner", "resolved_at")


def clear_result(slug: str) -> bool:
    """Стирает результат матча в журнале, оставляя прогноз и цены.

    Нужно для пересборки: строка с неверным результатом должна снова стать
    незакрытой, иначе обход её пропустит как готовую.
    """
    if not slug:
        return False
    with _lock:
        rows = _read(LOG_CSV, LOG_FIELDS)
        hit = False
        for r in rows:
            if r.get("slug") != slug:
                continue
            for k in RESULT_FIELDS:
                r[k] = ""
            hit = True
        if hit:
            _write(LOG_CSV, LOG_FIELDS, rows)
        return hit


def reopen_bets(slug: str) -> int:
    """Возвращает все ставки матча в ожидание. Сколько строк тронуто."""
    if not slug:
        return 0
    total = 0
    with _lock:
        for path, fields in ((VALUE_CSV, VALUE_FIELDS), (PICKS_CSV, PICK_FIELDS)):
            rows = _read(path, fields)
            n = 0
            for r in rows:
                if r.get("slug") != slug or r.get("status") in ("pending", ""):
                    continue
                r.update(status="pending", profit=0)
                for k in RESULT_FIELDS:
                    if k in fields:
                        r[k] = ""
                n += 1
            if n:
                _write(path, fields, rows)
            total += n
    return total


def stranded_pending() -> list[tuple[str, dict]]:
    """[(slug, исход)] — матчи с результатом в журнале и висящими ставками.

    Закрытие ходит по журналу и ищет результаты на TennisExplorer. Строку,
    у которой результат УЖЕ записан, оно пропускает как готовую — а ставки
    по ней могли остаться незакрытыми: ставка добавилась после того, как
    матч закрылся, или закрытие оборвалось на середине. Такая ставка не
    закроется никогда: искать её результат некому, он уже лежит рядом.

    Исход восстанавливается из самой строки журнала, без похода в сеть.
    """
    log_rows = {r["slug"]: r for r in _read(LOG_CSV, LOG_FIELDS)
                if r.get("slug") and r.get("resolved_at")}
    if not log_rows:
        return []
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for path, fields in ((VALUE_CSV, VALUE_FIELDS), (PICKS_CSV, PICK_FIELDS)):
        for r in _read(path, fields):
            slug = r.get("slug")
            if (not slug or slug in seen
                    or r.get("status") not in ("pending", "")):
                continue
            row = log_rows.get(slug)
            if not row:
                continue          # это сирота, ею занимается orphan_pending
            seen.add(slug)
            def _i(key):
                return int(pf(row.get(key), 0) or 0)
            score = row.get("score") or ""
            winner = row.get("winner") or ""
            outcome = {
                "score": score,
                "sets_p1": _i("sets_p1"), "sets_p2": _i("sets_p2"),
                "games_p1": _i("games_p1"), "games_p2": _i("games_p2"),
                "games_total": _i("games_total"),
                "games_diff": _i("games_diff"),
                "winner": winner if winner in ("p1", "p2") else "",
                "void": not winner,
            }
            out.append((slug, outcome))
    if out:
        log.info("ставок с уже известным результатом: %d матчей", len(out))
    return out


def orphan_pending() -> list[dict]:
    """Матчи, по которым есть незакрытые ставки, но нет строки в журнале.

    Так получается, если строку журнала удалили (например --fix-log убрал
    записи без slug), а ставки остались. Закрытие результатов ходит по
    журналу, поэтому такие ставки висели бы в ожидании вечно.

    Возвращаем их в том же виде, что и строки журнала: slug, имена, время —
    больше для поиска результата ничего не нужно.
    """
    known = {r.get("slug") for r in _read(LOG_CSV, LOG_FIELDS) if r.get("slug")}
    out: dict[str, dict] = {}
    for path, fields in ((VALUE_CSV, VALUE_FIELDS), (PICKS_CSV, PICK_FIELDS)):
        for r in _read(path, fields):
            slug = r.get("slug")
            if not slug or slug in known or slug in out:
                continue
            if r.get("status") not in ("pending", ""):
                continue
            out[slug] = {"slug": slug, "p1": r.get("p1"), "p2": r.get("p2"),
                         "when": r.get("when") or r.get("found_at") or "",
                         "_orphan": True}
    if out:
        log.info("ставок без строки в журнале: %d матчей", len(out))
    return list(out.values())
