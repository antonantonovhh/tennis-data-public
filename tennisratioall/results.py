"""Линия Pinnacle и результаты матчей.

Линия открывается не сразу: у челленджеров бывает за сутки, а иногда за час до
начала. Поэтому матч без линии не выбрасывается, а помечается ожидающим и
переспрашивается на каждом круге, пока не появится или пока не истечёт срок.

Результаты берём с TennisExplorer той же машинерией, что и основной бот. Она
там зашита внутрь функции, работающей с его базой ставок, поэтому здесь
вызываем её напрямую и сопоставляем сами.
"""

from __future__ import annotations

import logging
import os
import re
import traceback
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# сколько часов держать матч в ожидании линии, прежде чем махнуть рукой
WAIT_HOURS = float(os.environ.get("TRA_ODDS_WAIT_HOURS", "36"))

# Через сколько часов после начала матч, которого нет в результатах, считаем
# несостоявшимся. Отменённые матчи TennisExplorer не публикует вовсе — строки
# нет ни за один день, — поэтому отличить «отменён» от «результат ещё не
# выложили» можно только по времени. Порог с запасом: обычные результаты
# появляются за часы, а поспешный возврат выкидывает матч из статистики
# модели. Ставки при этом ведут себя как у букмекера при отмене — возврат.
ABANDON_HOURS = float(os.environ.get("TRA_ABANDON_HOURS", "48"))

# Текст в поле счёта у такого матча. По нему же его узнают панель и карточка
# в чате, поэтому он один на всех и лежит рядом с порогом.
CANCELLED_SCORE = "не состоялся"


class OddsResult:
    """Три исхода запроса: линия есть, линии ещё нет, ошибка."""

    FOUND = "found"
    NOT_OPEN = "not_open"
    ERROR = "error"

    def __init__(self, state: str, odds: dict | None = None, note: str = ""):
        self.state, self.odds, self.note = state, odds, note

    def __repr__(self):
        return f"<OddsResult {self.state} {self.note}>"


def fetch_odds(p1: str, p2: str) -> OddsResult:
    """Линия по матчу. Различает «нет в линии», «отступ» и «сломалось»."""
    from tennis_parser import pinnacle_guard as pg  # noqa: PLC0415

    left = pg.cooldown_left()
    if left > 0:
        # Пока идёт отступ после блокировки, к API не ходим вовсе. Матч
        # остаётся ждать линии: это ровно та ситуация, ради которой ожидание
        # и заводилось.
        return OddsResult(OddsResult.NOT_OPEN,
                          note=f"отступ Pinnacle ещё {left / 60:.0f} мин")

    try:
        from bot_merged import get_pinnacle_odds  # noqa: PLC0415
    except SystemExit as exc:
        return OddsResult(OddsResult.ERROR, note=f"bot_merged: {exc}")
    except Exception as exc:  # noqa: BLE001
        return OddsResult(OddsResult.ERROR, note=str(exc)[:120])

    try:
        odds = get_pinnacle_odds(p1, p2, is_manual=True)
    except Exception as exc:  # noqa: BLE001
        log.error("Pinnacle упал на %s — %s:\n%s", p1, p2, traceback.format_exc())
        return OddsResult(OddsResult.ERROR, note=f"{type(exc).__name__}: {exc}"[:120])

    if not odds:
        return OddsResult(OddsResult.NOT_OPEN, note="матча нет в линии")
    if odds.get("error"):
        # «матч не найден», «линия закрыта» и блокировка — это ожидание, а не
        # поломка: различаем, чтобы не тратить попытки на то, что пройдёт само
        text = str(odds["error"])
        waiting = ("не найден" in text or "линия закрыта" in text
                   or "коэффициенты отсутствуют" in text
                   or "блокировка" in text or "отступ" in text
                   or odds.get("cooldown"))
        return OddsResult(OddsResult.NOT_OPEN if waiting else OddsResult.ERROR,
                          note=text[:160])
    if all(odds.get(k) in (None, "-") for k in ("p1", "p2", "total_sets",
                                                "h_sets", "h_games")):
        return OddsResult(OddsResult.NOT_OPEN, note="все рынки пустые")
    return OddsResult(OddsResult.FOUND, odds=odds)


def parse_when(raw: str):
    """Время матча из того, как его пишет tennisratio: '22.08. 15:00'.

    fromisoformat такой формат не берёт, а в моём коде неразобранная дата
    означала «матч уже сыгран» — из-за чего вся завтрашняя афиша выглядела
    как потерянные результаты.

    Год в строке отсутствует. Берём текущий, но если получилось больше чем
    на неделю в будущем, значит матч из прошлого декабря — сдвигаем назад.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        got = datetime.fromisoformat(raw)
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.?\s*(\d{1,2}):(\d{2})", raw)
    if not m:
        m2 = re.match(r"(\d{1,2})\.(\d{1,2})\.?$", raw)
        if not m2:
            return None
        day, month, hh, mm = int(m2.group(1)), int(m2.group(2)), 0, 0
    else:
        day, month, hh, mm = (int(m.group(1)), int(m.group(2)),
                              int(m.group(3)), int(m.group(4)))
    now = datetime.now(timezone.utc)
    for year in (now.year, now.year - 1):
        try:
            got = datetime(year, month, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if got - now < timedelta(days=7):
            return got
    return None


def wait_expired(first_seen_iso: str, match_when: str = "") -> bool:
    """Истёк ли срок ожидания линии.

    Во время отступа после блокировки срок не течёт: иначе за шесть часов
    бана вся афиша молча протухла бы, ни разу не спросив котировки.

    Если известно время начала матча, ожидание прекращается через час после
    него: линии там уже не будет, а держать матч в очереди тридцать шесть
    часов бессмысленно.
    """
    from tennis_parser import pinnacle_guard as pg  # noqa: PLC0415
    if pg.cooldown_left() > 0:
        return False
    started = parse_when(match_when)
    if started and datetime.now(timezone.utc) - started > timedelta(hours=1):
        return True
    try:
        seen = datetime.fromisoformat(first_seen_iso)
    except (TypeError, ValueError):
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - seen > timedelta(hours=WAIT_HOURS)


def abandoned(match_when: str) -> bool:
    """Пора ли считать матч несостоявшимся.

    Без разобранного времени начала судить не о чем — такие строки остаются
    висеть, это лучше, чем закрыть возвратом сыгранный матч.
    """
    started = parse_when(match_when)
    if not started:
        return False
    return (datetime.now(timezone.utc) - started
            > timedelta(hours=ABANDON_HOURS))


def cancelled_outcome() -> dict:
    """Исход матча, которого не было: возврат, без победителя и без счёта."""
    return {"score": CANCELLED_SCORE,
            "sets_p1": 0, "sets_p2": 0, "games_p1": 0, "games_p2": 0,
            "games_total": 0, "games_diff": 0, "winner": "", "void": True}


# ------------------------------------------------------------------ результаты
def _norm(name: str) -> str:
    try:
        from bot_merged import remove_accents  # noqa: PLC0415
        name = remove_accents(name)
    except Exception:  # noqa: BLE001
        pass
    return re.sub(r"[^a-z]", "", name.lower())


def _last_name(full: str) -> str:
    parts = [p for p in re.split(r"[\s.]+", full.strip()) if len(p) > 1]
    return _norm(parts[-1]) if parts else _norm(full)


# Пометки, которыми TennisExplorer обозначает недоигранный матч: снятие,
# неявка, дисквалификация, отмена. Ищутся отдельными словами, чтобы «def»
# внутри фамилии Defosse не превращало матч в отказ.
UNFINISHED_RE = re.compile(
    r"\b(?:ret(?:ired)?|w\.?o\.?|walkover|def|abd|abandoned|canc(?:elled)?)\.?\b",
    re.I)

# Колонки, которые к счёту отношения не имеют. «first» стоит на ячейке со
# временем, «course»/«coursew» — на кэфах букмекера.
_SKIP_TD = ("t-name", "first", "time", "flag", "coupon", "course", "icons")

# Разрешить закрывать матчи, у которых итог по сетам не сошёлся с самими
# сетами. По умолчанию нельзя: именно так в журнал попадали живые матчи.
STRICT = os.environ.get("TRA_RESULTS_STRICT", "1") not in ("0", "no", "false")


def _cell_num(td):
    """Число из ячейки, или None. Понимает слитный тайбрейк «610» = 6-6(10)."""
    t = td.get_text(strip=True).replace("\u00a0", "")
    if re.fullmatch(r"\d{1,2}", t):
        return int(t)
    if re.fullmatch(r"\d{3}", t) and int(t[0]) <= 7 and int(t[1:]) >= 8:
        return int(t)
    return None


def _score_cells(row):
    """(итог по сетам, [геймы по сетам]) из ячеек с классами result и score.

    Разбор идёт ПО КЛАССАМ, а не по номеру ячейки. У TennisExplorer ячейка
    времени и колонки с кэфами объединены на две строки через rowspan:
    у верхнего игрока ячеек двенадцать, у нижнего семь. Выравнивание по
    позиции на этом разъезжается — итог по сетам верхнего вставал против
    первого сета нижнего, и 7-6(4), 3-6, 10-6 читалось как 2-6(4), 7-6, 3-6.

    Возвращает (None, []) если таких классов в разметке нет — тогда
    вызывающий переходит на разбор по позиции.
    """
    result, scores, seen = None, [], False
    for td in row.find_all("td"):
        cls = " ".join(td.get("class", []) or [])
        if "result" in cls:
            seen = True
            result = _cell_num(td)
        elif "score" in cls:
            seen = True
            scores.append(_cell_num(td))
    return (result, scores) if seen else (None, [])


def _row_numbers(row) -> list:
    """Запасной разбор — по позиции ячейки, когда классов в разметке нет.

    Значение возвращается на КАЖДУЮ ячейку: число или None. Пропускать
    непохожие ячейки нельзя — у сеяного в ячейке посева стоит «(7)», у
    несеяного она пустая, и счёт съезжал на колонку.
    """
    vals: list = []
    for td in row.find_all("td"):
        cls = " ".join(td.get("class", []) or [])
        vals.append(None if any(k in cls for k in _SKIP_TD) else _cell_num(td))
    return vals


def _complete_set(a: int, b: int) -> bool:
    """Доигран ли сет: 6-4, 7-5, 7-6, 10-8. 3-2 и 2-1 — нет."""
    if a > 15:
        a = int(str(a)[0])
    if b > 15:
        b = int(str(b)[0])
    hi, lo = max(a, b), min(a, b)
    return hi >= 10 or (hi == 7 and lo in (5, 6)) or (hi == 6 and hi - lo >= 2)


def _awarded(head) -> bool:
    """Похож ли итог по сетам на присуждение, а не на сыгранный матч.

    1:0 или 0:1 — так TennisExplorer отмечает матч, доведённый до конца
    снятием или неявкой. Настоящий матч так закончиться не может: там
    всегда 2:0, 2:1 или 3:x.
    """
    if head is None or None in head:
        return False
    a, b = head
    return sum((a, b)) == 1 and max(a, b) == 1


def awarded_winner(r1, r2) -> str:
    """Кому присуждён недоигранный матч: 'p1', 'p2' или '' если непонятно.

    У снятия TennisExplorer ставит в колонку итога 1:0 — это НЕ «один сет
    против нуля», а «матч присуждён»: единица у того, кто прошёл дальше,
    ноль у снявшегося. Так, Kwon — Lajovic 25.08 при счёте 6-4, 5-7, 1-3
    стоит как 0:1, хотя Квон выиграл сет.

    Без этой колонки победителя снятого матча взять неоткуда: по счёту его
    не вычислить, сняться может и ведущий.
    """
    res1, _ = _score_cells(r1)
    res2, _ = _score_cells(r2)
    if res1 is None or res2 is None or not _awarded((res1, res2)):
        return ""
    return "p1" if res1 == 1 else "p2"


def completed_sets(score: str) -> int:
    """Сколько сетов доиграно до конца.

    От этого зависит расчёт снятия у Pinnacle: ставка на победителя стоит,
    только если сыгран хотя бы один полный сет, иначе аннулируется всё.
    """
    body = UNFINISHED_RE.sub("", str(score or ""))
    body = re.sub(r"\((\d+)\)", r"\1", body)      # 7-6(4) -> 7-64
    n = 0
    for chunk in body.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d{1,2})-(\d{1,3})", chunk)
        if m and _complete_set(int(m.group(1)), int(m.group(2))):
            n += 1
    return n


def _plausible(sets: list) -> bool:
    """Похожи ли пары на настоящие сеты.

    Сет заканчивается на шести геймах, семи при 7-5 и 7-6, или на десяти
    в решающем тайбрейке. Пара вроде 1-2 или 7-4 сетом быть не может —
    это признак того, что колонки всё-таки разъехались. Лучше пропустить
    матч и разобраться, чем записать в журнал выдуманный счёт.
    """
    for a, b in sets:
        if a > 15:
            a = int(str(a)[0])
        if b > 15:
            b = int(str(b)[0])
        hi, lo = max(a, b), min(a, b)
        if hi < 6:
            return False
        if hi == 7 and lo not in (5, 6):
            return False
        if 8 <= hi <= 9:
            return False
    return True


def parse_result_row(r1, r2, marker: bool, finished_day: bool = True):
    """Пара строк таблицы -> счёт, или None, если матч закрывать рано.

    Возвращает строку вида '6-4,3-6,7-64'. У недоигранного матча к ней
    дописывается ' ret.' — по этой пометке ставки уходят в возврат, а не
    закрываются по недоигранному счёту.

    Главное, что здесь появилось, — отказ закрывать матч, который ещё идёт.
    Страница результатов TennisExplorer показывает и живые матчи, и по ним
    в журнал уходил счёт на момент обхода: победитель определялся по двум
    сыгранным геймам, ставка закрывалась, и переписать её потом было нечем.
    Признак готовности — колонка с итогом по сетам: у доигранного матча она
    сходится с самими сетами и в ней есть двойка (или тройка для пяти сетов).
    """
    res1, sc1 = _score_cells(r1)
    res2, sc2 = _score_cells(r2)
    if sc1 or sc2:
        # Основной путь: ячейки взяты по классам result и score, поэтому
        # rowspan у времени и кэфов ничего не сдвигает.
        width = max(len(sc1), len(sc2))
        sc1 = sc1 + [None] * (width - len(sc1))
        sc2 = sc2 + [None] * (width - len(sc2))
        pairs = [(a, b) for a, b in zip(sc1, sc2)
                 if not (a is None and b is None)]
        if res1 is not None and res2 is not None:
            pairs.insert(0, (res1, res2))
    else:
        g1, g2 = _row_numbers(r1), _row_numbers(r2)
        if not (g1 and g2):
            return None
        if len(g1) != len(g2):
            # Разметка без классов и строки разной длины: сопоставить
            # колонки нечем. Гадать нельзя — придумаем счёт.
            log.warning("строки матча разной длины (%d и %d) и без классов "
                        "score — пропускаю", len(g1), len(g2))
            return None
        pairs = [(a, b) for a, b in zip(g1, g2)
                 if not (a is None and b is None)]
    if not pairs:
        return None

    def played(seq):
        return [(a, b) for a, b in seq if a is not None and b is not None]

    def games(a: int, b: int) -> tuple:
        """Геймы сета без склеенного тайбрейка: (7, 64) -> (7, 6).

        Считать победителя сета по сырым числам нельзя: 64 больше 7, и
        выигранный на тайбрейке сет уезжал сопернику. Из-за этого итог
        по сетам не сходился с колонкой TennisExplorer, и матч с любым
        тайбрейком выглядел недоигранным — то есть не закрывался вовсе.
        """
        if b > 15:
            b = int(str(b)[0])
        if a > 15:
            a = int(str(a)[0])
        return a, b

    def won(seq):
        pairs = [games(a, b) for a, b in seq]
        return (sum(1 for a, b in pairs if a > b),
                sum(1 for a, b in pairs if b > a))

    head = pairs[0] if pairs else None
    # Первым числом TennisExplorer ставит итог по сетам: «2 | 6 7» против
    # «0 | 4 5». Без обеих цифр и без ограничения на тройку сюда попал бы
    # настоящий сет, доигранный до 3-2 при отказе.
    looks_summary = (len(pairs) >= 2 and head is not None
                     and None not in head and max(head) <= 3 and sum(head) > 0)

    if looks_summary:
        rest = played(pairs[1:])
        w1, w2 = won(rest)
        if (w1, w2) == head:
            sets = rest
            done = max(head) >= 2       # 2-0, 2-1, 3-x — матч сыгран
            unfinished = marker or not done
            if unfinished and not marker and not rest:
                return None
        elif marker:
            sets, unfinished = rest, True
        elif _awarded(head):
            # Снятие. TennisExplorer ставит в колонку итога 1:0 — и это НЕ
            # «один сет против нуля», а «матч присуждён»: Hassan — Giustino
            # 21.08 стоит как 1:0 при счёте 3-6, 3-2, потому что Джустино
            # снялся по ходу второго сета, ВЕДЯ по сетам.
            #
            # Пометки «ret.» в строке таблицы нет, она есть только на
            # странице матча. Раньше такой матч не сходился с итогом и
            # молча пропускался как «идёт прямо сейчас» — то есть висел в
            # ожидании вечно. Так и потерялся Hassan — Giustino.
            #
            # Живой матч выглядит так же: 1:0 при счёте 6-4, 2-1. Отличаем
            # по доигранным сетам. Если присудили тому, кто по ним
            # проигрывает, матч точно закончен — живым он быть не может.
            # Если присудили лидеру, отличить нельзя, и тогда решает день:
            # вчерашние матчи закончены все, сегодняшние — не факт.
            done_sets = [(a, b) for a, b in rest if _complete_set(a, b)]
            cw1, cw2 = won(done_sets)
            contradicts = (cw2 > cw1) if head[0] == 1 else (cw1 > cw2)
            if not (contradicts or finished_day):
                return None
            sets, unfinished = rest, True
        else:
            # Итог не сходится, пометки нет, на присуждение не похоже —
            # счёт неполный, то есть матч идёт прямо сейчас. Пропускаем:
            # доберём на следующем круге.
            return None
    else:
        sets = played(pairs)
        w1, w2 = won(sets)
        if marker or (len(pairs) == 1 and _awarded(pairs[0])):
            # Второе условие — неявка: счёта нет вовсе, в строке остался
            # только итог 1:0. Пометки «w.o.» в таблице может не быть.
            unfinished = True
            # Записывать этот итог как сет 1-0 нельзя: в журнал уйдёт
            # гейм, которого не было.
            if len(sets) == 1 and max(sets[0]) <= 3:
                sets = []
        elif STRICT:
            # Колонки с итогом не нашлось — вёрстка сайта изменилась.
            # Закрываем только очевидно доигранное.
            if max(w1, w2) < 2 or len(sets) > 5:
                return None
            unfinished = False
        else:
            unfinished = False

    if not sets:
        # Неявка: строка есть, счёта нет вовсе. Возвращаем одну пометку —
        # ставки уйдут в возврат, а не будут висеть в ожидании вечно.
        return "w.o." if unfinished else None
    if not unfinished and not _plausible(sets):
        # Сеты не похожи на сеты: колонки разъехались. Молча записывать
        # такой счёт нельзя — он попадёт в расчёт форы и тотала геймов.
        log.warning("счёт не похож на настоящий, матч пропущен: %s", sets)
        return None
    score = ",".join(f"{a}-{b}" for a, b in sets)
    return f"{score} ret." if unfinished else score


def _is_player_row(tr) -> bool:
    """Строка таблицы — это игрок, а не шапка турнира?

    У TennisExplorer шапка турнира — такая же <tr>, и в ней тоже есть
    ячейка t-name (название турнира ссылкой). Слепой обход парами «строка
    и следующая» съедал шапку вместе с первым игроком, и дальше ВСЯ
    таблица шла со сдвигом на строку: проигравший одного матча склеивался
    с победителем следующего. Счёт при этом выглядел правдоподобно —
    геймы проигравшего против геймов победителя, — поэтому ошибка молчала.
    """
    name = tr.find("td", class_=re.compile(r"\bt-name\b"))
    if not name or name.get("colspan"):
        return False
    cls = " ".join(tr.get("class", []) or [])
    if "head" in cls:
        return False
    return len(tr.find_all("td")) >= 3


def _match_id(tr):
    """Идентификатор матча из ссылки на его страницу, если он там есть."""
    for a in tr.find_all("a", href=True):
        m = re.search(r"match-detail.*?id=(\d+)", a["href"])
        if m:
            return m.group(1)
    return None


def pair_rows(rows) -> list:
    """Строки таблицы -> пары строк одного матча.

    Сначала пробуем сгруппировать по ссылке на страницу матча: это прямой
    признак принадлежности, сдвиг при нём невозможен. Если ссылок нет,
    берём подряд идущие строки игроков, отбросив шапки турниров.
    """
    players = [tr for tr in rows if _is_player_row(tr)]
    by_id: dict = {}
    for tr in players:
        mid = _match_id(tr)
        if mid is None:
            by_id = {}
            break
        by_id.setdefault(mid, []).append(tr)
    if by_id:
        pairs = [tuple(v) for v in by_id.values() if len(v) == 2]
        if pairs:
            return pairs
    return [(players[i], players[i + 1]) for i in range(0, len(players) - 1, 2)]


def fetch_results(days_back: int = 4) -> list[tuple[str, str, str, str]]:
    """[(игрок1, игрок2, '6-3,4-6,6-4', победитель)] с TennisExplorer.

    Четвёртый элемент заполняется только у недоигранных матчей: 'p1'/'p2' —
    кому присуждён матч, '' — если непонятно. Он нужен для расчёта снятий по
    правилам Pinnacle (ставка на победителя стоит, форы и тоталы — возврат).
    Кортеж расширен с конца, поэтому обращение по индексам 0..2 не менялось.
    """
    try:
        import requests  # noqa: PLC0415
        from bs4 import BeautifulSoup  # noqa: PLC0415
        from bot_merged import HEADERS, get_msk_time  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.error("не подгрузились зависимости для результатов: %s", exc)
        return []

    found: list[tuple[str, str, str]] = []
    now = get_msk_time()
    for i in range(days_back):
        d = now - timedelta(days=i)
        url = (f"https://www.tennisexplorer.com/results/"
               f"?type=all&year={d.year}&month={d.month:02d}&day={d.day:02d}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                log.warning("TennisExplorer %s: HTTP %s", d.date(), resp.status_code)
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            day = skipped = unfinished = 0
            for table in soup.find_all("table", class_=re.compile(r"\bresult\b")):
                for r1, r2 in pair_rows(table.find_all("tr")):
                    n1 = r1.find("td", class_=re.compile(r"\bt-name\b"))
                    n2 = r2.find("td", class_=re.compile(r"\bt-name\b"))
                    if not (n1 and n2):
                        continue
                    marker = bool(
                        UNFINISHED_RE.search(r1.get_text(" ", strip=True))
                        or UNFINISHED_RE.search(r2.get_text(" ", strip=True)))
                    # i == 0 — сегодняшняя страница, там матчи могут идти
                    score = parse_result_row(r1, r2, marker, finished_day=i > 0)
                    if score is None:
                        skipped += 1
                    else:
                        won_by = ""
                        if UNFINISHED_RE.search(score):
                            unfinished += 1
                            won_by = awarded_winner(r1, r2)
                        found.append((n1.get_text(" ", strip=True),
                                      n2.get_text(" ", strip=True), score,
                                      won_by))
                        day += 1
            # Цифра «пропущено» — то, что не готово: живые матчи и афиша.
            # Если она большая, а готовых ноль, значит сломался разбор,
            # а не сеть, и это видно сразу.
            log.info("TennisExplorer %s: готово %d (недоигранных %d), "
                     "пропущено как незавершённые %d", d.date(), day,
                     unfinished, skipped)
        except Exception:  # noqa: BLE001
            log.error("результаты за %s не собрались:\n%s", d.date(),
                      traceback.format_exc())
    return found


def _tokens(name: str) -> set[str]:
    """Слова имени в нормальном виде: 'Kopriva V. (4)' -> {'kopriva', 'v'}."""
    try:
        from bot_merged import remove_accents  # noqa: PLC0415
        name = remove_accents(name)
    except Exception:  # noqa: BLE001
        pass
    return {t for t in re.split(r"[^a-z]+", name.lower()) if t}


def _same_player(surname: str, their_name: str) -> bool:
    """Наша фамилия и имя из TennisExplorer — про одного человека?

    Сначала сравниваем по целым словам: 'tu' совпадёт с 'Tu J.', но не с
    'Tursunov'. Раньше стояла отсечка «фамилия короче трёх букв», и она
    выбрасывала короткие фамилии целиком — а их в теннисе много: Tu, Wu,
    Xu, An. Подстрока остаётся запасным вариантом для составных и
    дефисных фамилий ('Auger-Aliassime'), где разбиение по словам не
    совпадает с нашим написанием.
    """
    if not surname:
        return False
    if surname in _tokens(their_name):
        return True
    return len(surname) >= 5 and surname in _norm(their_name)


def find_result(p1: str, p2: str, results: list[tuple]):
    """Как match_result, но возвращает (индекс, перевёрнут_ли).

    Индекс нужен, чтобы вызывающий мог изъять найденный результат из списка:
    один матч на TennisExplorer не должен закрывать несколько наших записей.

    Берём только два первых поля кортежа: у fetch_results их четыре (имена,
    счёт и присуждённый победитель), а распаковка по трём именам роняла
    перезакрытие журнала с `too many values to unpack`.
    """
    l1, l2 = _last_name(p1), _last_name(p2)
    if not l1 or not l2:
        return None
    for i, row in enumerate(results):
        n1, n2 = row[0], row[1]
        if _same_player(l1, n1) and _same_player(l2, n2):
            return i, False
        if _same_player(l1, n2) and _same_player(l2, n1):
            return i, True
    return None


def match_result(p1: str, p2: str, results: list[tuple]):
    """Ищет матч среди результатов. Возвращает (счёт, перевёрнут_ли) или None."""
    got = find_result(p1, p2, results)
    if got is None:
        return None
    idx, flipped = got
    return results[idx][2], flipped


def looks_finished(score: str) -> bool:
    """Похож ли записанный счёт на доигранный матч.

    Нужно, чтобы найти в журнале следы старой ошибки: страница результатов
    TennisExplorer показывает и живые матчи, и по ним в журнал уходил счёт
    на момент обхода — «6-4, 2-1» с победителем, назначенным по двум
    сыгранным геймам.

    Признаки доигранного: победитель взял хотя бы два сета, и в каждом сете
    кто-то дошёл до шести (или до десяти в решающем тайбрейке). Счёт с
    пометкой отказа сюда не относится — он законно неполный.
    """
    if not score:
        return False
    if UNFINISHED_RE.search(score):
        return True
    from bot_merged import parse_match_result  # noqa: PLC0415

    s1, s2, _g1, _g2, sets = parse_match_result(score)
    if not sets or max(s1, s2) < 2:
        return False
    return all(max(a, b) >= 6 for a, b, _tb in sets)


def outcome_from_score(score: str, reversed_order: bool = False,
                      ret_winner: str = "") -> dict:
    """Счёт -> сеты, геймы, разница. Всё от лица первого игрока.

    Тайбрейки и итог по сетам разбирает parse_match_result из основного бота —
    та самая функция, которую пришлось чинить: '7-63' там читается как 7-6(3),
    а не как 7 против 63.

    У недоигранного матча (снятие, неявка) стоит флаг void, и форы с
    тоталами по нему уходят в возврат — так их считает Pinnacle.

    Победитель по счёту не вычисляется: 6-3, 2-1 могло закончиться снятием
    как раз ведущего, и записанный «победитель» был бы выдумкой. Но если
    TennisExplorer сам назвал, кому присуждён матч (`ret_winner`), он
    ставится — тогда ставка на победителя рассчитывается, а не возвращается.
    Условие Pinnacle: доигран хотя бы один полный сет. Снятие в первом сете
    аннулирует вообще всё, поэтому там победитель снимается обратно.
    """
    from bot_merged import parse_match_result  # noqa: PLC0415

    void = bool(UNFINISHED_RE.search(score or ""))
    s1, s2, g1, g2, sets = parse_match_result(score)
    won_by = ret_winner if ret_winner in ("p1", "p2") else ""
    if reversed_order:
        s1, s2, g1, g2 = s2, s1, g2, g1
        sets = [(b, a, tb) for a, b, tb in sets]
        # Порядок игроков перевёрнут относительно таблицы — победителя тоже
        if won_by:
            won_by = "p2" if won_by == "p1" else "p1"
    if void and won_by and completed_sets(score) < 1:
        # Снялись до конца первого сета: у Pinnacle аннулируется всё,
        # включая ставку на победителя
        won_by = ""
    pretty = ", ".join(f"{a}-{b}({tb})" if tb else f"{a}-{b}" for a, b, tb in sets)
    pretty = pretty or score
    if void and not UNFINISHED_RE.search(pretty):
        pretty = f"{pretty} ret."
    return {
        "score": pretty,
        "sets_p1": s1, "sets_p2": s2,
        "games_p1": g1, "games_p2": g2,
        "games_total": g1 + g2, "games_diff": g1 - g2,
        "winner": won_by if void else ("p1" if s1 > s2
                                      else ("p2" if s2 > s1 else "")),
        "void": void,
    }
