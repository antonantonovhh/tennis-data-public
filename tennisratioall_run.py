#!/usr/bin/env python3
"""Запуск обхода афиши: один круг или демоном.

    python3 tennisratioall_run.py --once            # один проход, в консоль
    python3 tennisratioall_run.py --once --telegram # то же, с отправкой в чат
    python3 tennisratioall_run.py --daemon          # крутиться постоянно
    python3 tennisratioall_run.py --status          # что уже посчитано
    python3 tennisratioall_run.py --reset-failed    # снять неудачи для повтора

Настройки — переменными окружения, см. README.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load_env_file(path: str | None = None) -> str | None:
    """Подхватывает .env рядом со скриптом.

    Нужно, чтобы ручной запуск вёл себя так же, как служба: Environment= в
    юните на процесс из шелла не действует, и без этого команда из README
    падает с «не задан TELEGRAM_TOKEN», хотя в systemd всё настроено.

    Уже выставленные переменные не трогаем — окружение важнее файла.
    """
    path = path or os.environ.get("TRA_ENV_FILE") or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return None
    loaded = 0
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            # systemd кавычки не снимает, но люди их пишут — снимем сами
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
                loaded += 1
    except OSError:
        return None
    return f"{path} ({loaded} перем.)" if loaded else None


_ENV_SRC = load_env_file()


def _pick_tour_early() -> None:
    """Ставит TRA_TOUR из --tour ДО импорта пакета.

    Пути к файлам состояния и журналов вычисляются на импорте store.py, а
    argparse отрабатывает сильно позже — если ждать его, WTA-обход успеет
    открыть мужские файлы. Поэтому argv просматриваем руками, а argparse
    ниже оставляем для справки и проверки значения.
    """
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        val = None
        if a.startswith("--tour="):
            val = a.split("=", 1)[1]
        elif a == "--tour" and i + 1 < len(argv):
            val = argv[i + 1]
        if val:
            os.environ["TRA_TOUR"] = val.strip().lower()
            return


_pick_tour_early()

from tennisratioall import telegram as tg          # noqa: E402
from tennisratioall.menu import Menu                # noqa: E402
from tennisratioall.scanner import Scanner          # noqa: E402
from tennisratioall.store import (MODE, POLL_INTERVAL, RESULTS_FILE,  # noqa: E402
                                  STATE_FILE, TOUR, WORKERS, Store)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MARK_FILE = os.environ.get("TRA_STARTUP_MARK") or os.path.join(
    HERE, "tennisratioall", ".last_startup")


def startup_notice(store, own_token: bool, buttons: bool) -> None:
    """Сообщение о запуске: чем оно полезно, кроме приветствия.

    Оно подтверждает три вещи разом — служба поднялась, токен и чат рабочие,
    настройки именно те, что вы правили. Без него после рестарта первые
    двадцать минут тишины неотличимы от сломанного бота.

    При падении с Restart=always служба поднимается каждые 30 секунд, поэтому
    чаще раза в 10 минут не пишем.
    """
    import time as _t  # noqa: PLC0415
    try:
        if os.path.exists(MARK_FILE):
            if _t.time() - os.path.getmtime(MARK_FILE) < 600:
                return
    except OSError:
        pass

    from tennisratioall.store import (ALERT_GAP, MODE, POLL_INTERVAL,  # noqa: PLC0415
                                      SIM_RUNS, THROTTLE, WORKERS)
    telegraph = os.environ.get("TP_TELEGRAPH", "") in ("1", "true", "yes")
    c = store.counts()

    lines = [
        "🟢 <b>tennisratioall запущен</b>",
        f"режим: <b>{MODE}</b> · прогонов: {SIM_RUNS} · порог сигнала: {ALERT_GAP:.0%}",
        f"круг каждые {POLL_INTERVAL // 60} мин · воркеров {WORKERS} · "
        f"пауза {THROTTLE} с",
        f"статьи на telegra.ph: {'да' if telegraph else 'нет'}",
        f"кнопки: {'да' if buttons else 'нет'}"
        + ("" if own_token else " ⚠️ на общем токене с основным ботом"),
    ]
    if sum(c.values()):
        lines.append(f"<i>в базе: готово {c['done']}, неудач {c['failed']}, "
                     f"в очереди {c['pending']}</i>")
    try:
        from tennis_parser import pinnacle_guard as pg  # noqa: PLC0415
        left = pg.cooldown_left()
        if left > 0:
            lines.append(f"⚠️ <b>Pinnacle в отступе</b> ещё {left // 60:.0f} мин — "
                         "кэфы пока не запрашиваю")
    except Exception:  # noqa: BLE001
        pass
    if MODE == "digest":
        lines.append("<i>обход идёт молча, сводка придёт в конце круга</i>")

    tg.send("\n".join(lines))
    try:
        os.makedirs(os.path.dirname(MARK_FILE), exist_ok=True)
        open(MARK_FILE, "w").close()
    except OSError:
        pass


def make_say(to_telegram: bool):
    """Отправитель. В консольном режиме отдаёт фиктивный message_id, чтобы
    учёт карточек работал одинаково в обоих режимах."""
    if not to_telegram:
        import re
        counter = [0]

        def to_console(text, *a, **k):
            counter[0] += 1
            print(re.sub(r"</?[a-z][^>]*>", "", text), "\n" + "-" * 50)
            return counter[0]

        return to_console
    return lambda text, *a, **k: tg.send(text)


def cmd_status(store: Store) -> int:
    try:
        from tennis_parser import pinnacle_guard as pg
        st = pg.status()
        line = (f"Pinnacle: матчапов в кэше {st['matchups']}, "
                f"возраст {st['cache_age']} с")
        if st["cooldown_left"]:
            line += (f"\n  ⚠️ ОТСТУП после блокировки: ещё "
                     f"{st['cooldown_left'] // 60} мин (подряд {st['block_streak']})")
        print(line + "\n")
    except Exception:
        pass
    c = store.counts()
    print(f"состояние: {STATE_FILE}")
    print(f"журнал:    {RESULTS_FILE}")
    print(f"\nготово {c['done']}  неудач {c['failed']}  "
          f"в очереди {c['pending']}  пропущено {c['skipped']}")
    bad = [(s, e) for s, e in store.entries.items() if e.status == "failed"]
    if bad:
        print(f"\nнеудачные ({len(bad)}):")
        for slug, e in bad[:20]:
            print(f"  {slug:<44} попыток {e.attempts}  {e.error[:60]}")
        if len(bad) > 20:
            print(f"  ... ещё {len(bad) - 20}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="один проход")
    g.add_argument("--daemon", action="store_true", help="крутиться постоянно")
    g.add_argument("--status", action="store_true", help="что уже посчитано")
    g.add_argument("--check-results", action="store_true",
                   help="только проверить результаты сыгранных матчей")
    g.add_argument("--report", choices=["day", "week", "month", "all"],
                   help="показать отчёт за период")
    g.add_argument("--fix-csv", action="store_true",
                   help="перевести накопленные CSV в формат для Excel")
    g.add_argument("--recompute", action="store_true",
                   help="пересчитать все матчи заново (после правок в модели)")
    g.add_argument("--fix-log", action="store_true",
                   help="убрать из журнала результаты, записанные не тем матчам")
    g.add_argument("--backfill-picks", action="store_true",
                   help="восстановить ставки на исход по накопленному журналу")
    g.add_argument("--diag-results", action="store_true",
                   help="почему матчи не закрываются: что скачалось и что не сошлось")
    g.add_argument("--cancel", nargs="+", metavar="SLUG_ИЛИ_ФАМИЛИЯ",
                   help="матч отменён: закрыть ставки возвратом, не дожидаясь "
                        "таймаута (TRA_ABANDON_HOURS)")
    g.add_argument("--reset-settled", action="store_true",
                   help="снять расчёт со ставок, у которых в журнале нет результата")
    g.add_argument("--reset-failed", action="store_true",
                   help="снять статус failed, чтобы попробовать заново")
    g.add_argument("--diag-queue", action="store_true",
                   help="почему матчи висят в очереди: что из них есть "
                        "в афише, а что осталось от старых прогонов")
    g.add_argument("--resettle", action="store_true",
                   help="пересчитать ставки по УЖЕ записанным результатам "
                        "(после правки правил расчёта)")
    g.add_argument("--rebuild-results", action="store_true",
                   help="перезакрыть матчи заново с TennisExplorer "
                        "(после починки разбора); без --apply только показывает")
    ap.add_argument("--tour", choices=["atp", "wta"], default=None,
                    help="какую афишу обходить (по умолчанию atp). "
                         "У каждого тура свои файлы состояния и журналов")
    ap.add_argument("--telegram", action="store_true", help="слать в чат")
    ap.add_argument("--find", nargs="+", metavar="ФАМИЛИЯ",
                    help="что есть на TennisExplorer по этим игрокам "
                         "(с --diag-results)")
    ap.add_argument("--days", type=int, default=4,
                    help="за сколько дней тянуть результаты "
                         "(с --diag-results и --rebuild-results)")
    ap.add_argument("--apply", action="store_true",
                    help="с --rebuild-results: действительно переписать, "
                         "а не только показать")
    ap.add_argument("--prune", action="store_true",
                    help="с --diag-queue: удалить из состояния матчи, "
                         "которых нет в афише и время которых прошло")
    ap.add_argument("--drop-unverified", action="store_true",
                    help="с --rebuild-results: снять и те результаты, "
                         "которые не удалось перепроверить")
    ap.add_argument("--csv", action="store_true",
                    help="с --report также выгрузить CSV")
    ap.add_argument("--no-buttons", action="store_true",
                    help="не опрашивать апдейты (кнопки не будут работать)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if _ENV_SRC:
        logging.getLogger(__name__).info("окружение из %s", _ENV_SRC)
    # Тур печатаем всегда: перепутать обход и потом искать, почему пустой
    # журнал, — самая дешёвая ошибка из возможных
    logging.getLogger(__name__).info(
        "тур: %s | состояние: %s", TOUR.upper(), os.path.basename(STATE_FILE))

    store = Store()
    if args.status:
        return cmd_status(store)
    if args.reset_failed:
        n = 0
        for e in store.entries.values():
            if e.status == "failed":
                e.status, e.attempts, e.error = "pending", 0, ""
                n += 1
        store.save()
        print(f"снято неудач: {n}")
        return 0

    menu = Menu(store)
    updates = None
    if args.telegram and not args.no_buttons:
        updates = tg.Updates(on_message=menu.on_message, on_callback=menu.on_callback)
        if not updates.start():
            updates = None

    if args.telegram:
        _, _, own = tg._token()
        startup_notice(store, own_token=own, buttons=updates is not None)

    sc = Scanner(say=make_say(args.telegram), store=store, menu=menu)
    if args.cancel:
        # Отменённый матч TennisExplorer не публикует вовсе, поэтому сам он
        # закроется только по таймауту в двое суток. Когда отмена уже видна
        # глазами (Flashscore пишет Canceled), ждать незачем.
        from tennisratioall import journal as J
        from tennisratioall import results as R

        want = [w.strip().lower() for w in args.cancel if w.strip()]
        pending = J.unresolved_slugs() + J.orphan_pending()
        hits = [r for r in pending
                if any(w in (r.get("slug") or "").lower()
                       or w in f"{r.get('p1')} {r.get('p2')}".lower()
                       for w in want)]
        if not hits:
            print("не нашёл незакрытых матчей по:", ", ".join(want))
            print("Ищутся и slug, и фамилии. Уже закрытые матчи тут не видны.")
            return 1
        print(f"найдено матчей: {len(hits)}")
        for r in hits:
            print(f"  {r['slug']}  {r.get('p1')} — {r.get('p2')}  "
                  f"{r.get('when') or '?'}")
        if not args.apply:
            print("\nсухой прогон. Записать — добавьте --apply")
            return 0
        outcome = R.cancelled_outcome()
        for r in hits:
            if not r.get("_orphan"):
                J.log_result(r["slug"], outcome)
            pick = J.resolve_pick(r["slug"], outcome)
            bets = J.resolve_value_bets(r["slug"], outcome,
                                        lambda b: ("refund", 0.0))
            print(f"{r['slug']}: исход {'закрыт' if pick else 'нет'}, "
                  f"ценных ставок закрыто {len(bets)}")
        print(f"готово: {len(hits)} матчей в возврат")
        return 0
    if args.reset_settled:
        # После --fix-log в журнале не осталось результата, а ставки по этим
        # матчам продолжают числиться рассчитанными — причём рассчитаны они
        # были по тому самому неверному счёту. Возвращаем их в ожидание.
        from tennisratioall import journal as J

        resolved = {r["slug"] for r in J._read(J.LOG_CSV, J.LOG_FIELDS)
                    if r.get("resolved_at")}
        total = 0
        for path, fields in ((J.VALUE_CSV, J.VALUE_FIELDS),
                             (J.PICKS_CSV, J.PICK_FIELDS)):
            rows = J._read(path, fields)
            n = 0
            for r in rows:
                if r.get("status") in ("win", "loss", "push", "refund") \
                        and r.get("slug") not in resolved:
                    r.update(status="pending", profit=0, resolved_at="",
                             score="", sets_p1="", sets_p2="",
                             games_p1="", games_p2="")
                    n += 1
            if n:
                J._write(path, fields, rows)
            print(f"{os.path.basename(path)}: снято с расчёта {n}")
            total += n
        print(f"\nВсего: {total}. Закроются заново на следующем круге.")
        return 0

    if args.diag_queue:
        # «В очереди» на панели — это счётчик записей состояния со статусом
        # pending, а не длина живой очереди. Обход берёт в работу только те
        # матчи, которые ЕСТЬ В СЕГОДНЯШНЕЙ АФИШЕ: см. run_once, там
        # todo = [r for r in refs if needs_work(r)]. Запись матча, который
        # с афиши уже пропал (сыгран, снят, турнир закончился), в refs не
        # попадёт никогда — и будет висеть в счётчике вечно, хотя очередь
        # на самом деле пуста.
        from datetime import datetime as _dt, timezone as _tz

        from tennisratioall import results as _R
        from tennisratioall.scanner import discover
        from tennisratioall.store import MAX_ATTEMPTS

        c = store.counts()
        print(f"состояние: {STATE_FILE}")
        print(f"готово {c['done']}  ждут кэфы {c['awaiting_odds']}  "
              f"в очереди {c['pending']}  неудач {c['failed']}\n")

        pend = {s_: e for s_, e in store.entries.items() if e.status == "pending"}
        if not pend:
            print("Записей в статусе pending нет — очередь пуста.")
            return 0

        print("Читаю афишу…")
        try:
            refs = discover()
        except Exception as exc:  # noqa: BLE001
            print(f"  афиша не прочиталась: {exc}")
            print("  Без неё нельзя сказать, какие записи ещё актуальны.")
            return 1
        live = {r.slug: r for r in refs}
        print(f"  матчей в афише: {len(refs)}\n")

        now = _dt.now(_tz.utc)
        in_afisha, stale, exhausted = [], [], []
        for slug, e in pend.items():
            if e.attempts >= MAX_ATTEMPTS:
                # needs_work вернёт False, но статус остался pending:
                # такая запись не попадёт в работу и не станет failed
                exhausted.append((slug, e))
            elif slug in live:
                in_afisha.append((slug, e))
            else:
                stale.append((slug, e))

        print(f"Будут обработаны на следующем круге:  {len(in_afisha)}")
        for slug, e in in_afisha[:10]:
            ref = live[slug]
            print(f"  {ref.p1} — {ref.p2}   начало {ref.when or '?'}  "
                  f"попыток {e.attempts}")
        if len(in_afisha) > 10:
            print(f"  … ещё {len(in_afisha) - 10}")

        print(f"\nНЕТ в афише — висят зря:              {len(stale)}")
        for slug, e in stale[:10]:
            age = ""
            try:
                seen = _dt.fromisoformat(e.first_seen)
                seen = seen if seen.tzinfo else seen.replace(tzinfo=_tz.utc)
                age = f"  замечен {(now - seen).days} дн назад"
            except (TypeError, ValueError):
                pass
            when = (e.summary or {}).get("when") or ""
            print(f"  {slug[:52]}  попыток {e.attempts}{age}"
                  + (f"  матч был {when}" if when else ""))
        if len(stale) > 10:
            print(f"  … ещё {len(stale) - 10}")

        if exhausted:
            print(f"\nПопытки исчерпаны ({MAX_ATTEMPTS}), но статус pending: "
                  f"{len(exhausted)}")
            print("  Такие в работу не берутся и в «неудачи» не попадают —")
            print("  лечится --reset-failed после смены статуса или --prune.")

        if not (stale or exhausted):
            print("\nВсё, что висит, — живая очередь. Просто ждите круг:")
            print("  один матч это 45-60 с парсинга.")
            return 0

        if not args.prune:
            print("\nЭто разбор без записи. Убрать лишнее из состояния:")
            print("  python3 tennisratioall_run.py --diag-queue --prune")
            print("  (журнал, ставки и результаты не трогаются — только "
                  "очередь)")
            return 0

        import shutil
        if os.path.exists(STATE_FILE):
            shutil.copy2(STATE_FILE, f"{STATE_FILE}.bak")
            print(f"\nБэкап состояния: {STATE_FILE}.bak")
        dropped = 0
        for slug, e in stale + exhausted:
            # Матч, который ещё может вернуться в афишу (время не наступило),
            # не трогаем: он просто ждёт своего дня.
            when = _R.parse_when((e.summary or {}).get("when") or "")
            if when and when > now:
                continue
            store.entries.pop(slug, None)
            dropped += 1
        store.save()
        print(f"Удалено записей: {dropped}")
        print(f"Осталось в состоянии: {len(store.entries)}")
        return 0

    if args.resettle:
        # Отличие от --rebuild-results: тот сверяет СЧЁТ с TennisExplorer и
        # переписывает только разошедшиеся. Если поменялось ПРАВИЛО расчёта,
        # а счёт прежний, он не сделает ничего — «расходится: 0». Так и вышло
        # 27.08.2026 с неявкой: счёт «w.o.» записан верно, а форы по нему были
        # посчитаны выигрышем. Здесь результат не трогаем вовсе, только
        # пересчитываем ставки по тем колонкам, что уже лежат в строке.
        from tennisratioall import journal as J, results as R
        from tennisratioall.store import STAKE
        from tennisratioall.value import profit as _profit, settle as _settle

        # Победитель снятого матча лежит ТОЛЬКО в picks.csv: в VALUE_FIELDS
        # колонки winner нет вовсе. Без неё Moneyline при снятии уходил бы в
        # возврат, хотя по правилам Pinnacle он стоит, если доигран сет.
        # Берём по слагу из журнала исходов — матч тот же самый.
        winners = {r.get("slug"): (r.get("winner") or "")
                   for r in J._read(J.PICKS_CSV, J.PICK_FIELDS)}

        fixed = 0
        for path, fields, kind in ((J.VALUE_CSV, J.VALUE_FIELDS, "value"),
                                   (J.PICKS_CSV, J.PICK_FIELDS, "pick")):
            rows = J._read(path, fields)
            for r in rows:
                st = r.get("status") or "pending"
                if st in ("pending", ""):
                    continue
                score = r.get("score") or ""
                bet = {"market": r.get("market") or "Moneyline",
                       "pick": r.get("pick") or r.get("side") or "",
                       "line": J.pf(r.get("line")),
                       "odds": J.pf(r.get("odds"), 0.0)}
                # Недоигранным считается И отменённый матч. В строке журнала
                # флага void нет, только счёт, а «не состоялся» — наша
                # пометка, UNFINISHED_RE её не знает: без этой проверки
                # возвраты по отменённым превратились бы в проигрыши, то
                # есть ровно в ту же ошибку, ради которой ключ и написан.
                void = (bool(R.UNFINISHED_RE.search(score))
                        or score == R.CANCELLED_SCORE)
                new_st = _settle(
                    bet, int(J.pf(r.get("sets_p1"), 0)),
                    int(J.pf(r.get("sets_p2"), 0)),
                    int(J.pf(r.get("games_p1"), 0)),
                    int(J.pf(r.get("games_p2"), 0)),
                    retired=void,
                    winner=(r.get("winner")
                            or winners.get(r.get("slug"), "")))
                new_pr = _profit(bet, new_st,
                                 J.pf(r.get("stake"), STAKE))
                if new_st == st and abs(new_pr - J.pf(r.get("profit"), 0.0)) < 0.5:
                    continue
                print(f"  {kind:<5} {r.get('slug','')[:40]:<40} "
                      f"{bet['market']} {bet['pick']} {r.get('line') or ''}"
                      f"  «{score}»")
                print(f"        {st} {r.get('profit')}  ->  {new_st} {new_pr:+.0f}")
                r["status"], r["profit"] = new_st, J._out("profit", new_pr)
                fixed += 1
            if args.apply and fixed:
                J._write(path, fields, rows)
        print(f"\nстрок пересчитано: {fixed}")
        if not args.apply:
            print("Это разбор без записи. Применить: добавьте --apply")
        elif fixed:
            print("Записано. Службу можно поднимать.")
        return 0

    if args.rebuild_results:
        # Разбор таблицы TennisExplorer склеивал проигравшего одного матча
        # с победителем следующего: шапка турнира — такая же строка с
        # ячейкой t-name, и обход парами съедал её вместе с первым игроком.
        # Счёт при этом получался правдоподобный, поэтому проверки на
        # невозможные сеты такие записи не ловят — единственный способ
        # узнать правду — скачать результаты заново и сверить.
        import shutil

        from tennisratioall import journal as J, results as R
        from tennisratioall.store import STAKE
        from tennisratioall.value import profit as _profit, settle as _settle

        rows = J._read(J.LOG_CSV, J.LOG_FIELDS)
        done = [r for r in rows if r.get("slug") and r.get("resolved_at")]
        if not done:
            print("В журнале нет закрытых матчей — пересобирать нечего.")
            return 0
        print(f"Закрытых матчей в журнале: {len(done)}")
        print(f"Тяну результаты с TennisExplorer за {args.days} дн…")
        found = R.fetch_results(days_back=args.days)
        print(f"Скачано матчей: {len(found)}")
        if not found:
            print("Ничего не скачалось — прерываю, чтобы не стереть журнал.")
            return 1

        remaining = list(found)
        same, changed, missing = [], [], []
        for r in done:
            got = R.find_result(r.get("p1", ""), r.get("p2", ""), remaining)
            if got is None:
                missing.append(r)
                continue
            idx, flipped = got
            hit = remaining.pop(idx)
            score = hit[2]
            # Четвёртый элемент — кому присуждён недоигранный матч. Без него
            # перезакрытие снова отправляло бы снятия в возврат, стирая
            # только что применённое правило Pinnacle.
            ret_winner = hit[3] if len(hit) > 3 else ""
            try:
                out = R.outcome_from_score(score, flipped, ret_winner)
            except Exception as exc:  # noqa: BLE001
                print(f"  счёт {score!r} не разобрался: {exc}")
                missing.append(r)
                continue
            was_w, now_w = r.get("winner") or "", out["winner"] or ""
            was_s = (r.get("score") or "").replace(" ", "")
            now_s = (out["score"] or "").replace(" ", "")
            (same if (was_w == now_w and was_s == now_s) else changed).append(
                (r, out))

        print(f"\n  совпало с записанным:      {len(same)}")
        print(f"  РАСХОДИТСЯ с записанным:   {len(changed)}")
        print(f"  не нашлось (вне окна {args.days} дн): {len(missing)}")
        if changed:
            print("\nЧто изменится (первые 10):")
            for r, out in changed[:10]:
                print(f"  {r.get('p1')} — {r.get('p2')}")
                print(f"      было:  {r.get('score')!r:<24} "
                      f"победитель {r.get('winner') or '—'}")
                print(f"      стало: {out['score']!r:<24} "
                      f"победитель {out['winner'] or '—'}")
        if not args.apply:
            print("\nЭто разбор без записи. Чтобы применить:")
            print(f"  python3 tennisratioall_run.py --rebuild-results "
                  f"--days {args.days} --apply")
            if missing:
                print("  Добавьте --drop-unverified, чтобы заодно снять "
                      "непроверенные результаты:")
                print("  их правильность подтвердить нечем, а в статистику "
                      "они идут наравне с остальными.")
            return 0

        for path in (J.LOG_CSV, J.VALUE_CSV, J.PICKS_CSV):
            if os.path.exists(path):
                shutil.copy2(path, f"{path}.bak")
        print(f"\nБэкапы: *.bak рядом с {os.path.dirname(J.LOG_CSV)}")

        rewritten = resettled = dropped = 0
        for r, out in changed:
            slug = r["slug"]
            J.clear_result(slug)
            J.reopen_bets(slug)
            J.log_result(slug, out)
            J.resolve_pick(slug, out)
            bets = J.resolve_value_bets(slug, out, lambda b: (
                lambda st: (st, _profit(
                    {"odds": J.pf(b.get("odds"), 0.0)}, st,
                    J.pf(b.get("stake"), STAKE)))
            )(_settle(
                {"market": b["market"], "pick": b["pick"],
                 "line": J.pf(b.get("line"))},
                out["sets_p1"], out["sets_p2"],
                out["games_p1"], out["games_p2"],
                retired=bool(out.get("void")))))
            rewritten += 1
            resettled += len(bets)
        if args.drop_unverified:
            for r in missing:
                J.clear_result(r["slug"])
                J.reopen_bets(r["slug"])
                dropped += 1

        print(f"Переписано матчей:            {rewritten}")
        print(f"  пересчитано value-ставок:   {resettled}")
        if dropped:
            print(f"Снято непроверенных:          {dropped}")
            print("  Они вернулись в ожидание. Те, что вне окна "
                  "TennisExplorer, там и останутся —")
            print("  результата для них больше нет нигде. Погасите их "
                  "или расширьте --days.")
        print("\nСтатистика: tennisratioall_run.py --report all")
        return 0

    if args.diag_results:
        from tennisratioall import journal as J, results as R

        pending = J.unresolved_slugs()
        orphans = J.orphan_pending()
        if orphans:
            print(f"⚠️ матчей со ставками, но без строки в журнале: "
                  f"{len(orphans)} — их тоже проверяю")
            pending = pending + orphans
        allrows = J._read(J.LOG_CSV, J.LOG_FIELDS)
        resolved = [r for r in allrows if r.get("resolved_at")]
        print(f"Журнал: {len(allrows)} матчей, "
              f"закрыто {len(resolved)}, ждут результата {len(pending)}")

        no_odds = sum(1 for r in allrows if not r.get("mkt_p1"))
        print(f"  без кэфов рынка: {no_odds}")

        picks = J._read(J.PICKS_CSV, J.PICK_FIELDS)
        vals = J._read(J.VALUE_CSV, J.VALUE_FIELDS)
        print(f"Ставки на исход: {len(picks)}, "
              f"закрыто {sum(1 for r in picks if r.get('status') in ('win','loss'))}")
        print(f"Value-ставки: {len(vals)}, "
              f"закрыто {sum(1 for r in vals if r.get('status') in ('win','loss'))}")

        # Расхождение между journal и value — верный признак того, что
        # результаты где-то потерялись: закрываются они всегда вместе
        vslugs = {r["slug"] for r in vals if r.get("status") in ("win", "loss")}
        lslugs = {r["slug"] for r in resolved}
        orphan = vslugs - lslugs
        if orphan:
            print(f"\n⚠️ {len(orphan)} матчей: value-ставки рассчитаны, "
                  f"а в журнале результата нет.")
            print("   Так бывает после --fix-log: он снял подозрительные")
            print("   результаты из журнала, а ставки остались закрытыми")
            print("   по ним же. Их стоит пересчитать: --reset-settled")

        if not pending:
            print("\nЖдущих матчей нет — проверять нечего.")
            return 0

        from tennisratioall import results as _R
        from datetime import datetime as _d, timezone as _z
        _now = _d.now(_z.utc)
        past = [r for r in pending
                if (_R.parse_when(r.get("when") or "") or _now) < _now]
        print(f"  из них по времени уже должны быть сыграны: {len(past)}")
        for r in past[:5]:
            print(f"    {r.get('p1')} — {r.get('p2')}  ({r.get('when')})")

        print(f"\nТяну результаты с TennisExplorer за {args.days} дн…")
        found = R.fetch_results(days_back=args.days)

        if args.find:
            # Прямой поиск по фамилии: показывает, что вообще есть на TE,
            # и с кем игрок там сыгран. Отвечает на вопрос «почему мой матч
            # не находится» точнее любой статистики.
            print(f"\nПоиск по: {', '.join(args.find)}")
            for want in args.find:
                w = want.lower().strip()
                got = [(n1, n2, sc) for n1, n2, sc in found
                       if w in n1.lower() or w in n2.lower()]
                print(f"\n  «{want}»: найдено {len(got)}")
                for n1, n2, sc in got[:10]:
                    print(f"    {n1} — {n2}   {sc}")
                if not got:
                    print("    на TennisExplorer за этот период его нет")
            return 0
        print(f"Скачано матчей: {len(found)}")
        if not found:
            print("  ❌ Ничего не скачалось. Проверьте сеть и доступность")
            print("     tennisexplorer.com с сервера:")
            print("     curl -sI https://www.tennisexplorer.com/results/ | head -1")
            return 0
        for n1, n2, sc in found[:3]:
            print(f"  пример: {n1} — {n2}  {sc}")

        matched = unmatched = []
        matched, unmatched = [], []
        remaining = list(found)
        for row in pending:
            got = R.find_result(row.get("p1", ""), row.get("p2", ""), remaining)
            if got is None:
                unmatched.append(row)
            else:
                matched.append(row)
                remaining.pop(got[0])
        print(f"\nСошлось: {len(matched)} из {len(pending)}")
        if unmatched:
            # Отделяем «ещё не сыгран» от «не нашли»: без этого ноль
            # совпадений выглядит поломкой, хотя чаще всего это просто
            # сегодняшняя афиша, до которой время не дошло.
            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc)
            future, missing, unknown = [], [], []
            for row in unmatched:
                when = R.parse_when(row.get("when") or "")
                if when is None:
                    unknown.append(row)
                elif when > now:
                    future.append(row)
                else:
                    missing.append(row)

            if future:
                print(f"  ещё не сыграны: {len(future)} — это нормально")
                for row in future[:3]:
                    print(f"    {row.get('p1')} — {row.get('p2')}  "
                          f"({row.get('when')})")
            if missing:
                print(f"  ⚠️ сыграны, но не нашлись: {len(missing)}")
                # Показываем, ЧТО именно не сошлось: сами строки, фамилии,
                # которые из них извлеклись, и есть ли похожие на
                # TennisExplorer. Без этого «не нашлись» — тупик.
                for row in missing[:5]:
                    p1n, p2n = row.get("p1") or "", row.get("p2") or ""
                    l1, l2 = R._last_name(p1n), R._last_name(p2n)
                    print(f"    {p1n!r} — {p2n!r}")
                    print(f"      фамилии: {l1!r} / {l2!r}"
                          + ("   ⚠️ ПУСТО — сопоставлять нечем"
                             if not (l1 and l2) else ""))
                    near = [f"{a} — {b}" for a, b, _ in found
                            if R._same_player(l1, a) or R._same_player(l1, b)
                            or R._same_player(l2, a) or R._same_player(l2, b)]
                    if near:
                        print(f"      похожее на TE: {near[0]}")
                    else:
                        print("      на TE ни одного из двух игроков нет")
                empty = sum(1 for r in missing
                            if not (R._last_name(r.get("p1") or "")
                                    and R._last_name(r.get("p2") or "")))
                if empty:
                    print(f"\n    У {empty} записей пустые имена — такие строки")
                    print("    сопоставить невозможно. Лечится --recompute:")
                    print("    матчи пройдут круг заново и запишутся с именами.")
            else:
                print("  Ненайденных сыгранных матчей нет — всё в порядке.")
            if unknown:
                print(f"  без разобранной даты: {len(unknown)} — "
                      "сыграны они или нет, сказать нельзя")
                for row in unknown[:3]:
                    print(f"    {row.get('p1')} — {row.get('p2')}  "
                          f"дата: {row.get('when')!r}")
        return 0

    if args.backfill_picks:
        # В matches_log.csv уже лежит всё нужное: оценка модели, обе цены
        # рынка и результат. Значит статистику по исходам можно построить
        # задним числом, не дожидаясь новых матчей.
        from tennisratioall import journal as J
        from tennisratioall.store import STAKE

        rows = J._read(J.LOG_CSV, J.LOG_FIELDS)
        have = {r.get("slug") for r in J._read(J.PICKS_CSV, J.PICK_FIELDS)}
        added = closed = skipped = 0
        for r in rows:
            slug = r.get("slug")
            if not slug or slug in have:
                skipped += 1
                continue
            rec = {
                "slug": slug, "p1": r.get("p1"), "p2": r.get("p2"),
                "tournament": r.get("tournament", ""),
                "surface": r.get("surface", ""),
                "best_of": r.get("best_of", ""),
                "sim_p1": J.pf(r.get("sim_p1")),
                "sim_p2": J.pf(r.get("sim_p2")),
                "model_gap": r.get("model_gap", ""),
            }
            odds = {"p1": r.get("mkt_p1"), "p2": r.get("mkt_p2")}
            if not J.add_pick(rec, odds, STAKE):
                skipped += 1
                continue
            added += 1
            have.add(slug)
            winner = r.get("winner")
            if winner in ("p1", "p2"):
                outcome = {k: r.get(k) for k in
                           ("score", "sets_p1", "sets_p2", "games_p1",
                            "games_p2", "games_diff", "winner")}
                if J.resolve_pick(slug, outcome):
                    closed += 1

        print(f"Восстановлено ставок на исход: {added}")
        print(f"  из них уже с результатом:    {closed}")
        print(f"  пропущено (нет кэфов, нет slug или уже есть): {skipped}")
        if added:
            print(f"\nФайл: {J.PICKS_CSV}")
            print("Статистика: tennisratioall_run.py --report all")
        return 0

    if args.fix_log:
        # Из-за пустого slug один результат мог записаться сразу во все
        # строки журнала — в панели все матчи показывали один счёт.
        # Снимаем результаты у строк без slug и у дубликатов, чтобы они
        # закрылись заново по-честному.
        from collections import Counter
        from tennisratioall import journal as J
        rows = J._read(J.LOG_CSV, J.LOG_FIELDS)
        if not rows:
            print("Журнал пуст.")
            return 0
        from tennisratioall import results as _R
        scores = Counter(r.get("score") for r in rows if r.get("score"))
        cleared = live = dropped = 0
        keep = []
        for r in rows:
            if not r.get("slug"):
                dropped += 1
                continue
            sc = r.get("score")
            # один и тот же счёт больше чем у двух матчей — это не совпадение
            bad = bool(sc) and scores[sc] > 2
            # Счёт, который не может принадлежать доигранному матчу: так в
            # журнал попадали живые матчи со страницы результатов. Победитель
            # там назначен по двум сыгранным геймам, и вся статистика по
            # такой строке — выдумка. Снимаем, чтобы закрылись заново.
            unfinished = bool(sc) and not _R.looks_finished(sc)
            if bad or unfinished:
                for k in ("score", "sets_p1", "sets_p2", "games_p1", "games_p2",
                          "games_total", "games_diff", "winner", "resolved_at"):
                    r[k] = ""
                cleared += 1
                live += int(unfinished and not bad)
            keep.append(r)
        print(f"Строк без slug удалено: {dropped}")
        print(f"Подозрительных результатов снято: {cleared}")
        if live:
            print(f"  из них недоигранных (записан живой счёт): {live}")
            print("  Ставки по ним закрыты неверно — пересчитать: "
                  "--reset-settled")
        if scores:
            top, n = scores.most_common(1)[0]
            print(f"Самый частый счёт в журнале: {top!r} — {n} раз")
        if cleared or dropped:
            import shutil
            bak = J.LOG_CSV + ".bak"
            shutil.copy2(J.LOG_CSV, bak)
            J._write(J.LOG_CSV, J.LOG_FIELDS, keep)
            print(f"Записано, бэкап: {bak}")
            print("Матчи закроются заново на следующем круге.")
        return 0

    if args.recompute:
        # Записи, посчитанные старой версией, менять точечно нельзя: у них
        # другой прогноз целиком. Проще снять статус и дать боту пройти круг
        # заново — карточки придут повторно, зато с верными числами.
        from collections import Counter
        was = Counter(e.status for e in store.entries.values())
        print("Было:", ", ".join(f"{k} {v}" for k, v in sorted(was.items()))
              or "пусто")
        # Сбрасываем всё подряд, а не только «done»: перечислять статусы —
        # значит однажды забыть какой-то и молча ничего не пересчитать.
        n = 0
        for e in store.entries.values():
            e.status, e.attempts, e.announced = "pending", 0, False
            e.sim, e.rec, e.error = {}, {}, ""
            n += 1
        store.save()
        print(f"К пересчёту помечено матчей: {n}")
        if n:
            print("Следующий круг пройдёт по ним заново — это займёт "
                  f"примерно {n * 50 / 60:.0f} мин.")
        return 0

    if args.fix_csv:
        # Файлы, записанные до этой версии, лежат без BOM и с точкой как
        # десятичным разделителем: Excel читает их в cp1251 («П1» -> «Pц1»)
        # и превращает 2.5 в «02.май». Перечитываем и перезаписываем.
        from tennisratioall import journal as J
        for path, fields, name in ((J.VALUE_CSV, J.VALUE_FIELDS, "value_bets"),
                                   (J.LOG_CSV, J.LOG_FIELDS, "matches_log")):
            if not os.path.exists(path):
                print(f"  {name}: файла нет, пропускаю")
                continue
            rows = J._read(path, fields)
            if not rows:
                print(f"  {name}: пусто")
                continue
            import shutil as _sh
            bak = f"{path}.bak"
            _sh.copy2(path, bak)
            J._write(path, fields, rows)
            print(f"  {name}: {len(rows)} строк переписано, бэкап {bak}")
        print("\nГотово. Открывайте в Excel — кодировка и числа встанут на место.")
        return 0

    if args.report:
        from tennisratioall import reports
        import re as _re
        text = reports.format_report(args.report)
        if args.telegram:
            tg.send(text)
        else:
            print(_re.sub(r"</?[a-z][^>]*>", "", text))
        path = reports.export_csv(args.report)
        print(f"\nВыгрузка: {path}" if path else "\nВыгружать нечего.")
        return 0

    if args.check_results:
        n = sc.check_results()
        print(f"закрыто матчей: {n}")
        return 0

    if args.once:
        stats = sc.run_once()
        sc.check_results()
        print(f"\nафиша {stats['discovered']}, взято {stats['queued']}, "
              f"готово {stats['done']}, неудач {stats['failed']}")
        if updates:
            # даём время ответить на карточки, иначе процесс умрёт раньше
            hold = _env_int("TRA_HOLD_AFTER_ONCE", 120)
            print(f"жду ответы {hold} с (Ctrl-C чтобы выйти)…")
            try:
                time.sleep(hold)
            except KeyboardInterrupt:
                pass
            updates.stop()
        return 0

    print(f"демон: интервал {args.interval} с, воркеров {WORKERS}, режим {MODE}")
    try:
        sc.run_forever(args.interval)
    except KeyboardInterrupt:
        sc.stop()
        if updates:
            updates.stop()
        store.save()
        print("\nостановлен, состояние сохранено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
