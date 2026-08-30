#!/usr/bin/env python3
"""Публикация ставок основного бота на bet-hub.

Берёт ставки из `bets_db.json` — те, что попали туда после нажатия
«✅ Ставь!» — находит те же матчи в линии Pinnacle на стороне bet-hub и
публикует выбор со ставкой 5% банка.

Два потока, по рынку на рассылку. Смешивать их в одной нельзя: это разные
популяции со своими ROI, ровно как разведены журналы обходчика.

    --market "Moneyline"   -> BIGTENBETS.T.U  (280383)
    --market "Total Sets"  -> BIGTENBETS2.T.U (280395)

    python3 bethub_bot_publish.py --market "Total Sets"           # сухой
    python3 bethub_bot_publish.py --market "Total Sets" --apply
    python3 bethub_bot_publish.py --market "Total Sets" --seed --apply

Почему отдельный процесс, а не публикация прямо в обработчике кнопки
---------------------------------------------------------------------
`bot_merged.py` — однопоточный цикл на `getUpdates`. Публикация это два-три
сетевых вызова к чужому API, где штатный ответ — «квота исчерпана, приходи
через минуту». Втащить это в обработчик callback'а значит подвесить весь
бот на чужой квоте. Мгновенность даёт не врезка в бота, а systemd:
`bethub-bot-publish*.path` следит за `bets_db.json`.

Ставка 5% — на стороне bet-hub
------------------------------
`--stake` уходит в поле `publication.stake` и означает процент банка НА
СТОРОНЕ БИРЖИ. Локальные 1000 ₽ в `bets_db.json` этим не затрагиваются
вообще: бот считает свой банк как считал. Это разные учёты одной ставки.

Учёт опубликованного
--------------------
Ключ — `match_id|<рынок>|<прогноз>`, у каждого рынка СВОЙ файл (иначе два
процесса писали бы один json наперегонки). 25.08.2026 в этом проекте уже
меняли формат ключа на живую, старые записи перестали совпадать с новыми,
и семь прогнозов ушли в рассылку по второму разу; удалить публикацию через
API нельзя — метода нет. Поэтому: формат ключа не менять, а если придётся —
сначала мигрировать файл, потом выкладывать код.

`--seed` помечает всё, что сейчас в `bets_db.json`, как опубликованное,
ничего не отправляя. Нужен ровно один раз при заведении рассылки: в ней уже
лежат прогнозы, поставленные руками, и без этого первый же запуск отправил
бы их повторно.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bethub                                                  # noqa: E402
# Сопоставление имён, разбор линии и форматы подписей берём из женского
# публикатора, а не копируем: это самая выстраданная часть (порядок игроков
# в линии не совпадает с нашим, у ML подпись — имя игрока, период только FT).
# Единственная копия общего кода — правило проекта.
from bethub_publish import (find_selection, fmt_total,         # noqa: E402
                            match_event, pf)

DB_FILE = os.environ.get("BETHUB_BOT_DB") or os.path.join(HERE, "bets_db.json")

# Рынки бота -> рассылка по умолчанию. Реальный sub_id приходит из окружения
# (BETHUB_BOT_SUB_ID) — в юните он свой у каждого потока.
SUBS = {"Moneyline": 280383, "Total Sets": 280395}

# Мужские категории в линии bet-hub, по полю country. См. event_index.
LEAGUES = re.compile(os.environ.get("BETHUB_BOT_LEAGUES", r"^(ATP|ITF Men)\b"),
                     re.IGNORECASE)

# «ТБ 2.5 (сеты)» -> сторона и линия. Разбираем, а не сравниваем со строкой
# целиком: сегодня бот шлёт только ТБ 2.5, но ТМ или другая линия не должны
# ломать публикацию молча.
TOTAL_RE = re.compile(r"\b(ТБ|ТМ)\b\s*([\d]+(?:[.,][\d]+)?)")


def state_path(market: str) -> str:
    """Файл учёта. У Moneyline имя историческое, без суффикса.

    Менять имя нельзя по той же причине, что и формат ключа: файл — это
    единственное, что удерживает от повторной публикации.
    """
    env = os.environ.get("BETHUB_BOT_STATE")
    if env:
        return env
    if market == "Moneyline":
        return os.path.join(HERE, "bethub_bot_published.json")
    slug = market.lower().replace(" ", "_")
    return os.path.join(HERE, f"bethub_bot_published_{slug}.json")


# ------------------------------------------------------------------ журнал
def load_db() -> list:
    try:
        raw = json.load(open(DB_FILE, encoding="utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        print(f"не читается {DB_FILE}: {exc}")
        return []
    if isinstance(raw, list):
        return raw
    return raw.get("bets") or []


def parse_when(date_str: str):
    """«26.08. 19:30» -> datetime в UTC. Тот же формат, что у обходчика."""
    from tennisratioall.results import parse_when as _pw       # noqa: PLC0415
    return _pw(date_str or "")


def parse_total(pred: str):
    """«ТБ 2.5 (сеты)» -> ('ТБ', 2.5). None, если не разобралось."""
    m = TOTAL_RE.search(pred or "")
    if not m:
        return None
    return m.group(1), float(m.group(2).replace(",", "."))


def rows_for(db, market: str) -> list:
    """Ставки нужного рынка, ещё не сыгранные."""
    out = []
    for m in db:
        if m.get("resolved"):
            continue
        for b in m.get("bets", []):
            if b.get("type") != market:
                continue
            if (b.get("status") or "pending") not in ("pending", ""):
                continue
            pick = (b.get("prediction") or "").strip()
            if market == "Moneyline":
                if pick not in ("П1", "П2"):
                    continue
            elif not parse_total(pick):
                continue
            out.append({
                "match_id": m.get("match_id"), "when": m.get("date"),
                "match": m.get("match"), "tournament": m.get("tournament"),
                "p1": m.get("player1") or "", "p2": m.get("player2") or "",
                "pick": pick, "odds": pf(b.get("odds")),
            })
    return out


def key(r, market: str) -> str:
    """НЕ МЕНЯТЬ формат: см. модульную докстроку про двойную публикацию."""
    return f"{r['match_id']}|{market}|{r['pick']}"


def load_done(path: str) -> dict:
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return {}


def save_done(path: str, d: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def already_started(r, grace_min: int = 0) -> bool:
    """Матч начался — публиковать прематч поздно.

    Неразобранное время начавшимся НЕ считаем: лучше попробовать и получить
    внятный отказ от API, чем молча выбросить прогноз.
    """
    t = parse_when(r.get("when"))
    if not t:
        return False
    return datetime.now(timezone.utc) > t + timedelta(minutes=grace_min)


# -------------------------------------------------------------- линия ATP
def event_index(api, hours: int):
    """Мужские одиночные события ближайших часов.

    Фильтр ПОЛОЖИТЕЛЬНЫЙ, и это важно. Женский публикатор отбрасывает лиги
    по вхождению «WTA», но в линии соседствуют ещё и «ITF Women …» — под
    правило «не WTA» они подходят и попадают в мужской список. Проверено на
    живой линии: там оказывалась «Anastasia Zolotareva - Ariana Geerlings».
    Ошибочной публикации это бы не вызвало (сопоставляем по фамилиям, а при
    неоднозначности не публикуем вовсе), но каждая лишняя лига — это запрос
    к чужому API из общей поминутной квоты: 13 против 19 за проход.

    Категории в линии: ATP …, ATP Challenger …, ITF Men … / WTA …,
    ITF Women …. Парные («Doubles») бот не ставит — тоже мимо.

    Список настраивается через BETHUB_BOT_LEAGUES на случай, если биржа
    заведёт новую мужскую категорию. Пропущенное печатается в прогоне,
    чтобы такой сдвиг было видно, а не искать его потом.
    """
    events, skipped = [], []
    for lg in api.leagues("T", hours=hours):
        country, name = lg.get("country") or "", lg.get("name") or ""
        if not (LEAGUES.match(country) and "doubles" not in name.lower()):
            skipped.append(f"{country} / {name}")
            continue
        try:
            events += api.events("T", country=country, league_name=name,
                                 hours=hours)
        except bethub.BetHubError as exc:
            print(f"  лига {country} / {name}: {exc}")
    return events, skipped


def selection(r, market: str):
    """Что искать в линии: (bet_type, подпись, игрок, сверять ли по имени).

    У Moneyline подпись ячейки — имя игрока в ИХ написании, участника у
    ячейки нет, поэтому сверяем по фамилии. Имя берём из НАШЕЙ пары по
    П1/П2: порядок игроков в линии другой, по индексу ставка уехала бы на
    соперника тихо и незаметно.

    У тотала подпись — «Over 2.5» / «Under 2.5», игрок не при чём.
    """
    bet_type = bethub.МАРШРУТ[market][1]
    if bet_type == "ML":
        want = r["p1"] if r["pick"] == "П1" else r["p2"]
        return bet_type, want, None, True
    side, line = parse_total(r["pick"])
    head = "Over " if side == "ТБ" else "Under "
    return bet_type, head + fmt_total(line), None, False


def plan_one(r, market, events, api, cache):
    """Готовит публикацию одной ставки: (данные, причина отказа)."""
    ev = match_event(events, r["p1"], r["p2"], games=False)
    if ev is None:
        return None, "событие не найдено в линии"

    eid = str(ev["event_id"])
    if eid in cache:
        full = cache[eid]
    else:
        try:
            full = api.event(eid, odds=True)
        except bethub.BetHubError as exc:
            # Квоту наружу: она поминутная и вернётся, а молотить в стену
            # оставшимися матчами бессмысленно.
            if exc.api_code == "provider_free_quota_exhausted":
                raise
            cache[eid] = None
            return None, f"линия не получена: {exc.api_code}"
        cache[eid] = full
    if not full:
        return None, "линия не получена"

    bet_type, want_label, want_player, by_name = selection(r, market)
    got = find_selection(full, bet_type, want_label, want_player, by_name)
    if got is None:
        return None, f"нет исхода в линии ({bet_type} {want_label})"

    line_id, title, outcome, label, participant, price = got
    return {
        "row": r, "event_id": eid, "event_name": ev.get("name"),
        "line_id": line_id, "bet_type": bet_type, "outcome": outcome,
        "title": title, "label": label, "participant": participant,
        "their_price": price, "our_price": r["odds"],
        "drift": (price - r["odds"]) if (price and r["odds"]) else 0.0,
    }, ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", default="Moneyline", choices=list(SUBS),
                    help="рынок бота; у каждого своя рассылка и свой учёт")
    ap.add_argument("--apply", action="store_true", help="публиковать")
    ap.add_argument("--limit", type=int, default=0,
                    help="не больше N публикаций за прогон")
    ap.add_argument("--stake", type=float, default=5.0,
                    help="процент банка НА СТОРОНЕ bet-hub (по умолчанию 5)")
    ap.add_argument("--sales-type", default="free",
                    choices=["free", "paid", "vip"])
    ap.add_argument("--odds-policy", default="any",
                    choices=["exact", "better_or_equal", "any"])
    ap.add_argument("--sub-id", type=int, default=0,
                    help="по умолчанию BETHUB_BOT_SUB_ID, иначе по рынку")
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--seed", action="store_true",
                    help="пометить текущее как опубликованное, не отправляя")
    args = ap.parse_args()

    market = args.market
    # Пустая BETHUB_BOT_SUB_ID (`X=` в EnvironmentFile) — это не ноль, это
    # строка: int("") падает. Служба бы легла на ровном месте.
    env_sub = (os.environ.get("BETHUB_BOT_SUB_ID") or "").strip()
    sub_id = (args.sub_id or (int(env_sub) if env_sub.isdigit() else 0)
              or SUBS[market])
    state = state_path(market)

    db = load_db()
    rows = rows_for(db, market)
    done = load_done(state)
    fresh = [r for r in rows if key(r, market) not in done]

    print(f"рынок: {market}   рассылка: {sub_id}")
    print(f"матчей в bets_db: {len(db)}   несыгранных ставок: {len(rows)}   "
          f"новых: {len(fresh)}   в учёте: {len(done)}")

    if args.seed:
        for r in rows:
            done.setdefault(key(r, market),
                            {"seeded": True, "at": time.time(),
                             "match": r["match"], "pick": r["pick"]})
        print(f"\nпомечено как опубликованное: {len(rows)}")
        for r in rows:
            print(f"  {r['when']:<14} {r['match']:<45} {r['pick']}")
        if args.apply:
            save_done(state, done)
            print(f"\nзаписано в {state}")
        else:
            print("\nсухой прогон: файл не тронут, добавьте --apply")
        return 0

    if not fresh:
        print("нечего публиковать")
        return 0

    stale = [r for r in fresh if already_started(r)]
    fresh = [r for r in fresh if not already_started(r)]
    if stale:
        print(f"пропускаю уже начавшихся: {len(stale)}")
        for r in stale:
            print(f"  {r['when']:<14} {r['match']}")
    if not fresh:
        return 0

    # Очередь по времени начала: ближайший матч первым — квота пускает
    # примерно один прогноз в минуту, и матч через десять минут иначе
    # простоял бы в очереди дольше собственного старта.
    fresh.sort(key=lambda r: (parse_when(r.get("when"))
                              or datetime.max.replace(tzinfo=timezone.utc)))

    print(f"\nчитаю афишу bet-hub за {args.hours} ч…")
    api = bethub.BetHub()
    events, skipped = event_index(api, args.hours)
    print(f"мужских событий в линии: {len(events)}   "
          f"лиг пропущено: {len(skipped)}\n")

    cache, sent = {}, 0
    for r in fresh:
        if args.limit and sent >= args.limit:
            print(f"\nлимит {args.limit} достигнут")
            break
        try:
            plan, why = plan_one(r, market, events, api, cache)
        except bethub.BetHubError as exc:
            print(f"квота провайдера исчерпана, дальше в следующий запуск "
                  f"({exc.api_code})")
            break
        head = f"{r['when']:<14} {r['match']:<45} {r['pick']}"
        if not plan:
            print(f"  ✗ {head}  {why}")
            continue
        print(f"  → {head}  наш {r['odds']:.3f} / их "
              f"{plan['their_price']:.3f}  ({plan['drift']:+.3f})")
        if not args.apply:
            continue
        try:
            api.publish(event_id=plan["event_id"], line_id=plan["line_id"],
                        bet_type=plan["bet_type"], outcome=plan["outcome"],
                        title=plan["title"], label=plan["label"],
                        participant=plan["participant"],
                        sub_id=sub_id, stake=args.stake,
                        sales_type=args.sales_type,
                        odds_policy=args.odds_policy)
        except bethub.BetHubError as exc:
            print(f"    отказ: {exc.api_code}")
            if exc.api_code == "provider_free_quota_exhausted":
                break
            continue
        done[key(r, market)] = {"at": time.time(), "match": r["match"],
                                "pick": r["pick"],
                                "price": plan["their_price"],
                                "event_id": plan["event_id"]}
        save_done(state, done)
        sent += 1
        print("    опубликовано")

    if args.apply:
        print(f"\nопубликовано за прогон: {sent}")
    else:
        print("\nсухой прогон: ничего не отправлено, добавьте --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
