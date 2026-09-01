#!/usr/bin/env python3
"""Публикация женских прогнозов обходчика на bet-hub.com.

Берёт незакрытые ценные ставки из журнала WTA, находит те же матчи в линии
Pinnacle на стороне bet-hub и публикует выбор через /publish.

Публикуются два потока:
  * ценные ставки (`value_bets.csv`) — Sets Hcap, Games Hcap, Total Sets;
    Moneyline оттуда исключён;
  * исходы (`picks.csv`) — ТОЛЬКО те, где модель спорит с рынком
    (`agree == 'нет'`), то есть выбрала не фаворита букмекера. Это
    Moneyline по своей природе, и берутся они намеренно: именно эта
    выборка показывает, есть ли у модели собственное знание.

    python3 bethub_publish.py                 # сухой прогон, ничего не шлёт
    python3 bethub_publish.py --apply         # публиковать
    python3 bethub_publish.py --apply --limit 1   # начать с одной

Что важно знать про сопоставление:

**Порядок игроков не совпадает.** У нас «Anna Bondar — Cristina Bucsa», в
линии «Cristina Bucsa - Anna Bondar». Поэтому П1/П2 привязываются по ИМЕНИ
участника, а не по индексу: иначе фора уедет на другого игрока тихо и
незаметно, а ставка будет опубликована неверная.

**Геймы — отдельное событие.** Фора и тотал по геймам живут в событии
«Игрок (Games) - Игрок (Games)» со своим event_id, по сетам — на основном.

**Цены двигаются.** odds_policy по умолчанию `any`: публикуем по актуальной
цене, какой бы она ни была. Так решил владелец — расхождение на практике
копеечное (2.020 против 2.000), а `exact` отклонял бы почти всё.

**Очередь — по времени начала матча**, ближайшие первыми: квота позволяет
одну публикацию в минуту, и при сортировке по перевесу матч, начинающийся
через десять минут, мог простоять в очереди дольше собственного старта.
Уже начавшиеся отсеиваются до обращения к API.

**Повторный запуск ничего не задваивает**: опубликованное запоминается в
bethub_published.json, плюс на каждый запрос идёт Idempotency-Key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("TRA_TOUR", "wta")

import bethub  # noqa: E402

STATE = os.environ.get("BETHUB_STATE") or os.path.join(HERE,
                                                       "bethub_published.json")
# Кэш линий между запусками. Таймер запускает нас раз в 70 с, а квота
# провайдера поминутная: без кэша разметка тратила её на те же матчи заново
# и до самой публикации она не доживала. TTL держим коротким — цена всё
# равно перепроверяется на стороне bet-hub при публикации.
ODDS_CACHE = os.environ.get("BETHUB_ODDS_CACHE") or os.path.join(
    HERE, "bethub_odds_cache.json")
ODDS_TTL = int(os.environ.get("BETHUB_ODDS_TTL", "150"))
SUB_ID = int(os.environ.get("BETHUB_SUB_ID", "280140"))
# Сколько прогнозов рассылка может держать одновременно. Слот занимает
# опубликованный прогноз, чей матч ЕЩЁ НЕ НАЧАЛСЯ, и освобождается он в
# момент старта события, а не по его результату.
MAX_ACTIVE = int(os.environ.get("BETHUB_MAX_ACTIVE", "99"))

# Публикуем только эти рынки. Moneyline исключён намеренно.
MARKETS = ("Sets Hcap", "Games Hcap", "Total Sets")


# ----------------------------------------------------------------- имена
def norm(s: str) -> str:
    """Имя без диакритики, регистра и мусора — для сравнения."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "")
                if not unicodedata.combining(c))
    s = s.replace("(Games)", " ")
    return re.sub(r"[^a-z]", "", s.lower())


def surname(full: str) -> str:
    parts = [p for p in re.split(r"[\s.]+", (full or "").strip()) if len(p) > 1]
    return norm(parts[-1]) if parts else norm(full)


def same_player(ours: str, theirs: str) -> bool:
    """Тот же игрок? Сверяем и целиком, и по фамилии."""
    a, b = norm(ours), norm(theirs)
    if a and b and (a == b or a in b or b in a):
        return True
    return bool(surname(ours)) and surname(ours) == surname(theirs)


# ----------------------------------------------------------------- линии
def fmt_line(value: float) -> str:
    """Значение форы в том виде, как его подписывает Pinnacle.

    Целые идут без дробной части («+1», «-2»), половинки с ней («+1.5»).
    Знак у форы обязателен, у тотала его нет — там своя подпись.
    """
    if value == int(value):
        return f"{int(value):+d}"
    return f"{value:+.1f}"


def fmt_total(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def pf(v, default=0.0) -> float:
    """Число из ячейки CSV: десятичный разделитель — запятая."""
    if v in (None, ""):
        return default
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except ValueError:
        return default


def starts_at(row):
    """Время начала матча из журнала (UTC) или None, если не разобралось.

    Формат афиши — «25.08. 16:00», без года; разбирает его тот же
    parse_when, которым живёт весь обходчик, — он знает про переход года.
    """
    from tennisratioall.results import parse_when  # noqa: PLC0415

    return parse_when(row.get("when") or "")


def already_started(row, grace_min: int = 0) -> bool:
    """Матч уже начался? Тогда прематч-публикация бессмысленна.

    Проверяем у себя, не дожидаясь отказа от API: очередь идёт по одному
    прогнозу в минуту, и начавшийся матч, стоящий в её голове, съедал бы
    квоту впустую на каждом запуске, не пропуская вперёд живые.
    Неразобранное время начавшимся НЕ считаем — лучше попробовать и
    получить внятный отказ, чем молча выбросить прогноз.
    """
    t = starts_at(row)
    if not t:
        return False
    from datetime import timedelta  # noqa: PLC0415
    return datetime.now(timezone.utc) > t + timedelta(minutes=grace_min)


def find_selection(event: dict, bet_type: str, want_label: str,
                   want_player: str | None, label_is_player: bool = False):
    """Ищет в линии события нужную ячейку.

    Возвращает (line_id, title, outcome, label, participant, price) или None.
    Берём только период FT: наши ставки — на весь матч, а в линии рядом
    лежат те же рынки на первый сет, и перепутать их проще простого.

    `label_is_player` — режим для Moneyline: там в label лежит имя игрока в
    ИХ написании, и сверять его надо по фамилии, а не побуквенно.
    """
    for bt, period, title, line_id, rows in bethub.sections(event):
        if bt != bet_type or period != "FT":
            continue
        for row in rows:
            for cell in row:
                label = cell.get("label") or ""
                if label_is_player:
                    if not same_player(want_label, label):
                        continue
                elif label != want_label:
                    continue
                if want_player is not None and not same_player(
                        want_player, cell.get("participant") or ""):
                    continue
                return (line_id, title, cell["index"], label,
                        cell.get("participant") or "", pf(cell.get("price")))
    return None


# ----------------------------------------------------------------- журнал
def pending_bets():
    """Незакрытые ставки к публикации из обоих журналов.

    К каждой строке добавляется `_src`: он идёт в ключ учёта, иначе исход и
    ценная ставка одного матча затирали бы друг друга в bethub_published.json.
    """
    from tennisratioall import journal as J  # noqa: PLC0415

    out = []
    for r in J._read(J.VALUE_CSV, J.VALUE_FIELDS):
        if (r.get("status") or "pending") not in ("pending", ""):
            continue
        if r.get("market") not in MARKETS:
            continue
        out.append(dict(r, _src="value"))

    for r in J._read(J.PICKS_CSV, J.PICK_FIELDS):
        if (r.get("status") or "pending") not in ("pending", ""):
            continue
        # Только спор с рынком. Согласные с букмекером исходы не публикуем:
        # такую же ставку дал бы фаворит линии без всякой модели.
        if (r.get("agree") or "").strip().lower() != "нет":
            continue
        out.append(dict(r, _src="pick", market="Moneyline",
                        pick=r.get("side") or "", line=""))
    return out


def bet_key(r) -> str:
    return (f"{r.get('_src')}|{r['slug']}|{r.get('market')}|"
            f"{r.get('pick')}|{r.get('line')}")


def load_cache() -> dict:
    try:
        raw = json.load(open(ODDS_CACHE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    now = time.time()
    return {k: v for k, v in raw.items()
            if now - v.get("at", 0) < ODDS_TTL}


def save_cache(c: dict) -> None:
    tmp = f"{ODDS_CACHE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(c, fh, ensure_ascii=False)
        os.replace(tmp, ODDS_CACHE)
    except OSError:
        pass          # кэш — ускорение, а не данные: молча переживём


def load_done() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_done(d: dict) -> None:
    tmp = f"{STATE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


# ------------------------------------------------------------- поиск матча
def event_index(api, hours: int):
    """Все теннисные события ближайших часов: имя -> событие.

    Один проход по лигам вместо поиска на каждый матч: событий сотни, а
    запросов к чужому API должно быть как можно меньше.
    """
    events = []
    for lg in api.leagues("T", hours=hours):
        country, name = lg.get("country") or "", lg.get("name") or ""
        if "WTA" not in country.upper():
            continue
        try:
            events += api.events("T", country=country, league_name=name,
                                 hours=hours)
        except bethub.BetHubError as exc:
            print(f"  лига {country} / {name}: {exc}")
    return events


def split_name(ev_name: str):
    """«Игрок1 - Игрок2» -> (Игрок1, Игрок2). Разделитель — тире с пробелами."""
    parts = re.split(r"\s+[-–—]\s+", ev_name or "")
    return (parts[0], parts[1]) if len(parts) == 2 else (ev_name, "")


def match_event(events, p1: str, p2: str, games: bool):
    """Ищет событие пары. games=True — вариант «(Games)»."""
    hits = []
    for ev in events:
        name = ev.get("name") or ""
        is_games = "(Games)" in name
        if is_games != games:
            continue
        a, b = split_name(name)
        if not b:
            continue
        straight = same_player(p1, a) and same_player(p2, b)
        flipped = same_player(p1, b) and same_player(p2, a)
        if straight or flipped:
            hits.append(ev)
    # Неоднозначность — не публикуем: лучше пропустить, чем поставить не на тех
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- основное
def plan_one(row, events, cache, api):
    """Готовит публикацию одной ставки. Возвращает (данные, причина отказа)."""
    market, pick = row["market"], row["pick"]
    p1, p2 = row.get("p1", ""), row.get("p2", "")
    line = pf(row.get("line"))
    where, bet_type = bethub.МАРШРУТ[market]
    games = where == "games"

    ev = match_event(events, p1, p2, games)
    if ev is None:
        return None, f"событие не найдено ({'games' if games else 'main'})"

    eid = str(ev["event_id"])
    hit = cache.get(eid)
    if hit is not None and hit.get("at", 0) and \
            time.time() - hit["at"] < ODDS_TTL:
        full = hit.get("data")
        if not full:
            return None, "линия не получена (из кэша)"
    else:
        try:
            cache[eid] = {"at": time.time(), "data": api.event(eid, odds=True)}
        except bethub.BetHubError as exc:
            # Квоту не кэшируем как «нет линии»: она поминутная и вернётся.
            # Поднимаем наружу, чтобы прогон остановился, а не молотил
            # оставшиеся сорок матчей в стену.
            if exc.api_code == "provider_free_quota_exhausted":
                raise
            # Отрицательный результат тоже кэшируем: незачем через минуту
            # снова платить квотой за тот же отказ
            cache[eid] = {"at": time.time(), "data": None}
            return None, f"линия не получена: {exc.api_code}"
        full = cache[eid]["data"]
    if not full:
        return None, "линия не получена"

    label_is_player = False
    if bet_type == "ML":
        # Исход: в линии подпись — имя игрока, участника у ячейки нет
        want_label = row.get("player") or (p1 if pick == "П1" else p2)
        want_player = None
        label_is_player = True
    elif bet_type == "TO":
        want_label = ("Over " if pick == "ТБ" else "Under ") + fmt_total(line)
        want_player = None
    else:
        # П1/П2 — это НАШ порядок игроков; в линию идём по имени
        want_player = p1 if pick == "П1" else p2
        if games:
            want_player = f"{want_player} (Games)"
        want_label = fmt_line(line)

    got = find_selection(full, bet_type, want_label, want_player,
                         label_is_player)
    if got is None:
        return None, f"нет такого исхода в линии ({bet_type} {want_label})"

    line_id, title, outcome, label, participant, price = got
    ours = pf(row.get("odds"))
    return {
        "row": row, "event_id": eid, "event_name": ev.get("name"),
        "line_id": line_id, "bet_type": bet_type, "outcome": outcome,
        "title": title, "label": label, "participant": participant,
        "their_price": price, "our_price": ours,
        "drift": (price - ours) if (price and ours) else 0.0,
    }, ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="публиковать")
    ap.add_argument("--limit", type=int, default=0,
                    help="не больше N публикаций за прогон")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--sales-type", default="free",
                    choices=["free", "paid", "vip"])
    # any по решению владельца: расхождение цен на практике копеечное, а
    # better_or_equal отклонял бы половину при любом движении линии вниз.
    ap.add_argument("--odds-policy", default="any",
                    choices=["exact", "better_or_equal", "any"])
    ap.add_argument("--sub-id", type=int, default=SUB_ID)
    ap.add_argument("--hours", type=int, default=48)
    args = ap.parse_args()

    api = bethub.BetHub()
    done = load_done()

    allbets = pending_bets()
    bets = [b for b in allbets if bet_key(b) not in done]
    print(f"незакрытых ставок к публикации: {len(bets)} "
          f"(уже опубликовано ранее: {len(done)})")
    if not bets:
        return 0

    # Лимит рассылки проверяем У СЕБЯ, до единого обращения к API. Иначе
    # выходит затор: публикатор берёт один прогноз за запуск, API отвечает
    # publication_rejected, и таймер каждые 70 с повторяет ТОТ ЖЕ прогноз,
    # пока не начнётся его матч. 29.08.2026 так встало на сутки — один
    # прогноз получил 481 отказ подряд, а 33 живых стояли за ним и
    # протухли. Каждая такая попытка ещё и тратит квоту провайдера.
    active = [b for b in allbets if bet_key(b) in done and not already_started(b)]
    free = MAX_ACTIVE - len(active)
    if free <= 0:
        nxt = sorted(t for t in (starts_at(b) for b in active) if t)
        when = (f", ближайший освободится в {nxt[0]:%d.%m %H:%M} UTC"
                if nxt else "")
        print(f"лимит рассылки исчерпан: занято {len(active)} слотов из "
              f"{MAX_ACTIVE}{when}.")
        print("Слот освобождается в момент НАЧАЛА события, а не по его "
              "результату — жду, к API не обращаюсь.")
        return 0
    print(f"свободных слотов: {free} из {MAX_ACTIVE}")

    print(f"читаю афишу bet-hub за {args.hours} ч…")
    events = event_index(api, args.hours)
    print(f"женских событий в линии: {len(events)}\n")

    # Начавшиеся матчи вон из очереди: публиковать их поздно, а стоя в
    # голове очереди (они же самые ранние) они бы жгли квоту на каждом
    # запуске и не пускали вперёд те, что вот-вот начнутся.
    stale = [b for b in bets if already_started(b)]
    bets = [b for b in bets if not already_started(b)]
    if stale:
        print(f"пропускаю уже начавшихся: {len(stale)}")

    # Очередь по времени начала: ближайшее событие публикуется первым.
    # Матчи без разобранного времени уходят в конец — они не срочные, а
    # вперёд пускать надо то, что вот-вот стартует. При равном времени
    # первым идёт больший перевес.
    def order(r):
        t = starts_at(r)
        return (0, t.timestamp(), -pf(r.get("edge"))) if t \
            else (1, 0.0, -pf(r.get("edge")))

    bets.sort(key=order)

    # Сколько планов вообще нужно. Планирование само тратит квоту провайдера
    # (за каждым матчем — запрос линии), поэтому при --limit не размечаем
    # всю афишу: иначе на саму публикацию квоты уже не остаётся, чем и
    # объяснялся отказ provider_free_quota_exhausted на первом же прогоне.
    need = args.limit if (args.apply and args.limit) else 0

    cache = load_cache()
    if cache:
        print(f"линий в кэше: {len(cache)}")
    ready, skipped = [], []
    quota_hit = False
    for row in bets:
        if need and len(ready) >= need:
            break
        try:
            plan, why = plan_one(row, events, cache, api)
        except bethub.BetHubError as exc:
            if exc.api_code != "provider_free_quota_exhausted":
                raise
            quota_hit = True
            print("  квота провайдера исчерпана — останавливаю разметку "
                  "(она поминутная, повторите позже)")
            break
        if plan:
            ready.append(plan)
        else:
            skipped.append((row, why))

    save_cache(cache)
    tail = " (разметка прервана по квоте)" if quota_hit else ""
    print(f"готово к публикации: {len(ready)}, пропущено: {len(skipped)}{tail}\n")
    for p in ready:
        r = p["row"]
        drift = p["drift"]
        mark = "=" if abs(drift) < 0.005 else ("↑" if drift > 0 else "↓")
        left = starts_at(r)
        mins = ((left - datetime.now(timezone.utc)).total_seconds() / 60
                if left else None)
        eta = f"через {mins:.0f} мин" if mins is not None else "время неизвестно"
        print(f"  {r['p1']} — {r['p2']}  [{r.get('when') or '?'}, {eta}]")
        src = "исход" if r.get("_src") == "pick" else "ценная"
        print(f"      [{src}] {r['market']} {r['pick']} {r.get('line')} -> "
              f"{p['title']} «{p['label']}» {p['participant']}")
        print(f"      наш кэф {p['our_price']:.3f} / у них "
              f"{p['their_price']:.3f} {mark}  перевес {pf(r.get('edge'))*100:+.1f}%")

    if skipped:
        print("\nпропущено:")
        seen = {}
        for r, why in skipped:
            seen.setdefault(why, []).append(f"{r['p1']} — {r['p2']}")
        for why, who in seen.items():
            print(f"  {why}: {len(who)}")
            for w in sorted(set(who))[:3]:
                print(f"      {w}")

    if not args.apply:
        print(f"\nсухой прогон. Публиковать — добавьте --apply")
        return 0

    # Больше свободных слотов не публикуем, даже если --limit разрешает:
    # лишнее всё равно отвергнут, а квота провайдера будет потрачена.
    todo = ready[:args.limit] if args.limit else ready
    if len(todo) > free:
        print(f"свободных слотов {free} — публикую столько, "
              f"остальное в следующий раз")
        todo = todo[:free]
    print(f"\nпубликую {len(todo)} из {len(ready)}…")
    ok = fail = 0
    for p in todo:
        r = p["row"]
        key = bet_key(r)
        try:
            data = api.publish(
                event_id=p["event_id"], line_id=p["line_id"],
                bet_type=p["bet_type"], outcome=p["outcome"],
                title=p["title"], label=p["label"],
                participant=p["participant"], sub_id=args.sub_id,
                stake=args.stake, sales_type=args.sales_type,
                odds_policy=args.odds_policy,
                comment=f"модель: {pf(r.get('sim_prob'))*100:.0f}%, "
                        f"перевес {pf(r.get('edge'))*100:+.1f}%")
        except bethub.BetHubError as exc:
            # publication_rejected при живых слотах по нашему счёту означает,
            # что счёт разошёлся с биржей (прогноз удалили руками, событие
            # сняли). Это не поломка: прекращаем прогон спокойно, иначе
            # таймер будет долбить один и тот же прогноз каждые 70 секунд.
            if exc.api_code == "publication_rejected":
                # Причина — в details.reason, и она бывает разной: полный
                # лимит активных прогнозов или «Unsettled tips found», когда
                # биржа требует сначала рассчитать старые. Сообщение при этом
                # всегда одинаковое, так что без reason отказы неотличимы.
                why = (exc.details or {}).get("reason") or "причина не указана"
                print(f"  отказ: {r['p1']} — {r['p2']} "
                      f"{r['market']} {r['pick']} {r.get('line')} — {why}")
                print("Прогон прекращаю: повторять то же самое каждые 70 с "
                      "бессмысленно, а квота провайдера общая.")
                break
            fail += 1
            print(f"  ОТКАЗ {r['p1']} — {r['p2']} {r['market']} {r['pick']} "
                  f"{r.get('line')}: {exc}")
            continue
        ok += 1
        done[key] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "event_id": p["event_id"], "response": data}
        save_done(done)
        print(f"  ok  {r['p1']} — {r['p2']} {p['title']} «{p['label']}»")

    print(f"\nопубликовано {ok}, отказов {fail}")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
