"""Обход афиши и обработка матчей.

Сюда вынесена вся логика прогона, чтобы её можно было гонять без телеграма и
без сети: discover и process подменяются в тестах.
"""

from __future__ import annotations

import html
import logging
import queue
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from . import journal, results as res_mod
from .store import (ALERT_GAP, FALLBACK_FULL, MAX_ATTEMPTS, MODE,
                    REPORT_HOUR, REQUIRE_ELO, SEND_PACE,
                    SIM_RUNS, STAKE, TOUR,
                    THROTTLE, WORKERS, Entry, MatchRef, Store, append_result)
from .value import describe, find_value, profit, settle

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ афиша
def discover() -> list[MatchRef]:
    """Матчи с афиши tennisratio.

    Переиспользуем parse_matches из основного бота: он уже умеет отсеивать
    фьючерсы, доставать турнир, дату и раунд. Импорт внутри функции — модуль
    бота на импорте тянет сеть и токены, а сканер должен уметь запускаться
    и без него (в тестах discover подменяется).
    """
    from tennis_parser.tennisratio import h2h_url  # noqa: PLC0415
    try:
        from bot_merged import parse_matches  # noqa: PLC0415
    except SystemExit as exc:
        # bot_merged на импорте делает sys.exit(), если не хватает переменных.
        # SystemExit не наследует Exception, поэтому без этого перехвата он
        # прошёл бы сквозь все обработчики и убил процесс молча
        raise RuntimeError(
            f"bot_merged не импортируется: {exc}. "
            "При ручном запуске переменные из systemd-юнита не подхватываются — "
            "положите их в .env рядом со скриптом либо выполните "
            "`set -a; . /opt/tennis_bot/.env; set +a` перед запуском."
        ) from None

    raw = parse_matches({}, {}, TOUR)
    refs = []
    for slug, data in raw.items():
        if "-vs-" not in slug:
            continue
        a, b = slug.split("-vs-", 1)
        p1 = " ".join(w.capitalize() for w in a.split("-") if w)
        p2 = " ".join(w.capitalize() for w in b.split("-") if w)
        # Адрес именно страницы сравнения: /h2h-compare/<slug>.html.
        # Ссылка с афиши ведёт на превью матча, а не на h2h, и подставлять её
        # сюда нельзя — build_report получал 404 по каждому матчу.
        # Строим через h2h_url, чтобы slug считался тем же slugify, что и в
        # парсере, а не нашей capitalize-реконструкцией имён.
        refs.append(MatchRef(
            slug=slug, p1=p1, p2=p2,
            url=h2h_url(p1, p2),
            tournament=(data.get("tournament") or "").strip(),
            when=(data.get("date") or "").strip(),
        ))
    return refs


# ------------------------------------------------------------------ обработка
def process(ref: MatchRef) -> dict:
    """Собирает статистику и симуляцию по одному матчу.

    Возвращает запись для журнала. Исключения наружу не гасим — их ловит
    воркер и решает, повторять ли.
    """
    try:
        from tennis_parser.integration import get_fetcher, parse_slot  # noqa: PLC0415
        from tennis_parser.report import build_report  # noqa: PLC0415
        from tennis_parser.simulation import build_simulation  # noqa: PLC0415
        from tennis_parser.tennisratio import (guess_best_of,  # noqa: PLC0415
                                               guess_surface)
    except SystemExit as exc:
        raise RuntimeError(f"модуль парсера не импортируется: {exc}") from None

    t0 = time.monotonic()
    # через общую очередь: нажатая кнопка и фоновый обход не должны лезть
    # в браузер одновременно
    with parse_slot():
        # Покрытие берём из названия турнира: «ATP Winston Salem (Hard)».
        # Без него build_report считал по всем покрытиям сразу — брал общий
        # Elo вместо hElo/cElo и общую статистику вместо грунтовой. На разнице
        # cElo и hElo в двести-триста пунктов это меняет прогноз до
        # неузнаваемости, а в отчёте выглядело безобидным «все покрытия».
        surface = guess_surface(ref.tournament) or guess_surface(ref.slug)
        # Формат матча тоже с афиши: на мужском «Шлеме» играют до трёх побед,
        # и это меняет вероятности, а не подпись. Без него симуляция считала
        # bo3 против пятисетовой линии Pinnacle (тотал 3.5/4.5, форы до ±2.5),
        # и вероятности к таким рынкам получались бессмысленные.
        best_of = guess_best_of(ref.tournament, TOUR)
        report = build_report(
            get_fetcher(), ref.p1, ref.p2,
            headless=True, as_of=date.today(),
            url=ref.url or None,
            surface=surface,
            best_of=best_of,
            # без этого Elo для женских матчей брался бы из мужской таблицы,
            # то есть игроков просто не находило бы и прогноз шёл на одной
            # статистике — ровно тот случай, который мы ловим меткой «без Elo»
            tour=TOUR,
        )
    sim = None
    try:
        sim = build_simulation(report, runs=SIM_RUNS)
    except Exception:  # noqa: BLE001
        # симуляция вторична: статистику терять из-за неё нельзя
        log.error("симуляция %s упала:\n%s", ref.slug, traceback.format_exc())

    rec = _record(ref, report, sim, time.monotonic() - t0)
    rec.setdefault("surface", surface)
    if not rec.get("surface"):
        rec["surface"] = surface
    # под подчёркиванием — то, что нужно отправителю, но не должно попасть
    # в журнал: append_result выкидывает ключи с подчёркиванием
    rec["_report"], rec["_sim"] = report, sim
    return rec


def _record(ref: MatchRef, report: dict, sim: dict | None, secs: float) -> dict:
    h = report.get("h2h") or {}
    fc = report.get("elo_forecast") or {}
    fat = report.get("fatigue") or {}
    edge = fat.get("edge") or {}

    rec = {
        "slug": ref.slug,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "p1": (h.get("player1") or {}).get("name") or ref.p1,
        "p2": (h.get("player2") or {}).get("name") or ref.p2,
        "tournament": ref.tournament,
        "when": ref.when,
        "url": ref.url,
        "surface": fc.get("surface"),
        "best_of": fc.get("best_of"),
        "elo_p1": fc.get("p1_win_prob"),
        "fresher": edge.get("fresher"),
        "fatigue_delta": edge.get("delta_fatigue"),
        "seconds": round(secs, 1),
    }
    if sim:
        m = sim["headline"]
        rec.update({
            "sim_p1": round(m["p1_win"], 4),
            "sim_p2": round(m["p2_win"], 4),
            "sim_runs": sim["runs"],
            "decider": round(m["sets_played"].get(sim["best_of"], 0.0), 4),
            "games_median": m["games_median"],
            # Без этой строки колонка sim_games_diff в matches_log.csv была
            # пуста во всех записях: журнал читает rec["games_diff_mean"], а
            # сюда его никто не клал. Прогноз по форам не с чем было сверить
            # задним числом — единственное число, которое это позволяет.
            "games_diff_mean": round(m["games_diff_mean"], 3),
        })
        st, el = sim["models"].get("stats"), sim["models"].get("elo")
        if st and el:
            rec["model_gap"] = round(abs(st["p1_win"] - el["p1_win"]), 4)
        rec["spw"] = [round(m["spw_by_set"][0][0], 4), round(m["spw_by_set"][1][0], 4)]
    return rec


def confidence(rec: dict) -> tuple[str, str] | None:
    """Насколько можно верить числу симуляции.

    Меряем расхождением статы и Elo. Когда два источника говорят разное,
    рабочая модель просто берёт середину — но середина между «76%» и «31%»
    это не «умеренный фаворит», это «неизвестно». Без такой пометки цифра
    читается как уверенный прогноз, каковым не является.

    Отдельный и куда более опасный случай — когда сравнивать НЕ С ЧЕМ.
    Таблица Elo на tennisabstract покрывает около 550 игроков, то есть
    примерно топ ATP; челленджерных и фьючерсных в ней нет. Достаточно
    одного такого в паре — и Elo-модель по матчу не строится, model_gap
    остаётся пустым, а проверка раньше просто возвращала None, то есть
    молчала. Получалось ровно наоборот задуманному: чем меньше мы знаем о
    матче, тем увереннее выглядела карточка.

    Замер на реальных данных (23.08.2026): медиана перевеса по value-ставкам
    с доступным Elo +13%, без него +40%, максимум +72%. Все десять самых
    жирных «находок» — матчи без Elo. Против Pinnacle таких перевесов не
    бывает: это не деньги, это оценка одной модели без второго мнения.
    """
    gap = rec.get("model_gap")
    if gap is None:
        return ("Elo недоступен",
                "нет рейтинга Elo хотя бы у одного игрока (таблица "
                "tennisabstract покрывает ~550 человек, челленджеры в неё не "
                "входят). Прогноз построен только на статистике, сверить его "
                "не с чем — перевес на таких матчах систематически завышен")
    if gap >= 0.30:
        return ("нет доверия",
                f"стата и Elo расходятся на {gap:.0%} — модель не определилась. "
                "Вероятность выше читать как «неизвестно», а не как прогноз")
    if gap >= 0.18:
        return ("слабое доверие",
                f"стата и Elo расходятся на {gap:.0%} — к вероятности выше "
                "относиться осторожно")
    return None


def alerts_for(rec: dict) -> list[str]:
    """Причины, по которым матч стоит показать человеку.

    Порог намеренно не про «кого модель считает фаворитом», а про
    рассогласование: именно оно означает, что смотреть надо глазами.
    """
    out = []
    sim_p1, elo_p1 = rec.get("sim_p1"), rec.get("elo_p1")
    if sim_p1 is not None and elo_p1 is not None and abs(sim_p1 - elo_p1) >= ALERT_GAP:
        out.append(f"симуляция {sim_p1:.0%} против Elo {elo_p1:.0%}")
    # про расхождение говорит confidence(), здесь бы дублировалось
    if (rec.get("fatigue_delta") or 0) >= 40:
        who = rec["p1"] if rec.get("fresher") == "p1" else rec["p2"]
        out.append(f"{who} заметно свежее (Δ {rec['fatigue_delta']:.0f})")
    return out


# ------------------------------------------------------------------ прогон
class Scanner:
    """Очередь матчей и пул воркеров.

    discover_fn / process_fn вынесены в параметры, чтобы прогон можно было
    целиком проверить без сети.
    """

    def __init__(self, say=None, store: Store | None = None,
                 discover_fn=discover, process_fn=process, menu=None):
        self.say = self._paced(say or (lambda *a, **k: None))
        # меню запоминает, какая карточка про какой матч — без этого ответ
        # на карточку не с чем сопоставить
        self.menu = menu
        self.store = store or Store()
        self.discover_fn = discover_fn
        self.process_fn = process_fn
        self._stop = threading.Event()

    def _paced(self, fn):
        """Выдержка между сообщениями, чтобы не словить лимит Telegram.

        Ждём только если предыдущее ушло слишком недавно: на обычном темпе
        парсинга задержка не срабатывает ни разу.
        """
        last = [0.0]
        lock = threading.Lock()

        def send(text, *a, **k):
            with lock:
                gap = time.monotonic() - last[0]
                if SEND_PACE and gap < SEND_PACE:
                    time.sleep(SEND_PACE - gap)
                last[0] = time.monotonic()
            try:
                return fn(text, *a, **k)
            except Exception:  # noqa: BLE001
                log.error("сообщение не ушло:\n%s", traceback.format_exc())
                return None

        return send

    # -------------------------------------------------------------- один круг
    def run_once(self) -> dict:
        try:
            refs = self.discover_fn()
        except Exception:  # noqa: BLE001
            log.error("афиша не прочиталась:\n%s", traceback.format_exc())
            return {"discovered": 0, "queued": 0, "done": 0, "failed": 0}

        # Сыгранные матчи уходят из афиши, и записи по ним больше некому
        # закрыть — чистим их сами, иначе счётчик «в очереди» показывает
        # висяки, которых давно нет (см. Store.prune).
        if self.store.prune():
            self.store.save()
        self._maybe_prune_cache()

        todo = [r for r in refs if self.store.needs_work(r)]
        log.info("афиша: %d матчей, к обработке %d", len(refs), len(todo))
        if not todo:
            return {"discovered": len(refs), "queued": 0, "done": 0, "failed": 0}

        # Матчи, ждущие только линию, парсинг не запускают — сообщать о
        # «начале обхода» ради двух запросов к Pinnacle незачем, оно
        # повторялось бы каждые десять минут
        fresh = [r for r in todo
                 if (self.store.get(r.slug) or Entry(slug=r.slug)).status
                 != "awaiting_odds"]
        if fresh and MODE not in ("silent", "alerts"):
            # без этой строки обход выглядит как зависший бот: в digest
            # первое сообщение приходит только в конце круга, а это десятки
            # минут. Оценка грубая, но объясняет тишину
            # Делить на WORKERS нельзя: настоящая параллельность ограничена
            # общим семафором парсинга (TP_PARSE_CONCURRENCY, по умолчанию 1),
            # через который ходят и обход, и кнопка. При WORKERS=2 и лимите 1
            # оценка получалась вдвое оптимистичнее правды.
            try:
                from tennis_parser.integration import PARSE_CONCURRENCY  # noqa: PLC0415
            except Exception:  # noqa: BLE001
                PARSE_CONCURRENCY = 1
            lanes = max(1, min(WORKERS, PARSE_CONCURRENCY))
            eta = len(fresh) * (45 + THROTTLE) / lanes / 60
            note = "" if lanes >= WORKERS else f" (лимит парсинга {lanes})"
            waiting = len(todo) - len(fresh)
            tail = f", плюс {waiting} ждут линию" if waiting else ""
            self.say(f"⏳ Начинаю обход: {len(fresh)} матч(ей){tail}, "
                     f"это примерно {eta:.0f} мин{note}")

        q: queue.Queue[MatchRef] = queue.Queue()
        for r in todo:
            q.put(r)

        results: list[dict] = []
        failures: list[tuple[MatchRef, str]] = []
        lock = threading.Lock()

        def worker():
            while not self._stop.is_set():
                try:
                    ref = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    self._handle(ref, results, failures, lock)
                finally:
                    q.task_done()
                if THROTTLE:
                    self._stop.wait(THROTTLE)

        threads = [threading.Thread(target=worker, daemon=True, name=f"tra-{i}")
                   for i in range(min(WORKERS, len(todo)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.store.save()
        self._report(results, failures, len(refs))
        return {"discovered": len(refs), "queued": len(todo),
                "done": len(results), "failed": len(failures)}

    def _handle(self, ref: MatchRef, results, failures, lock) -> None:
        e = self.store.get(ref.slug) or Entry(
            slug=ref.slug,
            first_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        # Матч уже посчитан и ждёт только котировок — незачем заново скрести
        # страницу и гонять симуляцию. Берём сохранённый слепок и спрашиваем
        # одну лишь линию. Раньше он проходил полный круг каждые десять минут
        # и каждый раз слал ту же карточку.
        if e.status == "awaiting_odds" and e.sim and e.rec:
            self._retry_odds(ref, e, lock, results)
            return

        e.attempts += 1
        e.last_try = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            rec = self.process_fn(ref)
        except Exception as exc:  # noqa: BLE001
            e.status = "failed" if e.attempts >= MAX_ATTEMPTS else "pending"
            e.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.error("матч %s упал (попытка %d/%d): %s",
                      ref.slug, e.attempts, MAX_ATTEMPTS, e.error)
            self.store.upsert(e)
            with lock:
                failures.append((ref, e.error))
            return

        # Матч без Elo дальше не идёт. model_gap считается только когда
        # построены ОБЕ модели — стата и Elo; пусто значит, что рейтинга нет
        # хотя бы у одного игрока (таблица tennisabstract это ~550 человек,
        # челленджеры в неё не входят). Прогноз тогда держится на одной
        # статистике, сверить его не с чем, и перевес выходит фантастическим:
        # замер 23.08.2026 — медиана +40% против +13% там, где Elo есть.
        #
        # Проверяем здесь, а не до разбора: до него известно только имя из
        # слага, а build_report ищет рейтинг по имени со страницы матча, и
        # предварительная проверка промахивалась бы на расхождении написаний.
        # Цена — разбор впустую, но таких матчей всего 4-6%.
        #
        # Пропуск полный: ни линии Pinnacle (экономит запрос к API), ни
        # записи в журнал, ни карточки в чат.
        if REQUIRE_ELO and rec.get("model_gap") is None:
            e.status = "skipped"
            e.error = "нет Elo"
            e.summary = {k: rec.get(k) for k in ("sim_p1", "surface", "when")}
            e.summary["odds"] = "skipped"
            e.sim, e.rec = {}, {}
            self.store.upsert(e)
            log.info("%s: пропускаю — нет Elo хотя бы у одного игрока",
                     ref.slug)
            return

        # линия Pinnacle: без неё матч не «готов», а ждёт котировок
        odds_state, odds, bets = self._attach_odds(ref, e, rec)
        e.status = "done" if odds_state != "waiting" else "awaiting_odds"
        e.error = ""
        e.summary = {k: rec.get(k) for k in
                     ("sim_p1", "elo_p1", "model_gap", "surface", "when")}
        e.summary["odds"] = odds_state
        if odds_state == "waiting":
            # сохраняем ровно столько, чтобы вернуться к матчу без пересчёта
            try:
                from tennis_parser.simulation import snapshot  # noqa: PLC0415
                e.sim = snapshot(rec["_sim"]) if rec.get("_sim") else {}
            except Exception:  # noqa: BLE001
                log.error("слепок симуляции не сделан:\n%s", traceback.format_exc())
                e.sim = {}
            e.rec = {k: v for k, v in rec.items() if not k.startswith("_")}
        else:
            e.sim, e.rec = {}, {}
        self.store.upsert(e)
        append_result(rec)
        try:
            journal.log_match(rec, odds, bets)
        except Exception:  # noqa: BLE001
            log.error("журнал не записан:\n%s", traceback.format_exc())
        if odds:
            self._record_pick(rec, odds)
        if bets:
            self._announce_value(ref, rec, bets)
        with lock:
            results.append(rec)

        if e.announced:
            return          # карточку по этому матчу уже слали
        if MODE == "all":
            self._send_full(ref, rec)
            e.announced = True
        elif MODE == "each":
            self._send_teaser(ref, rec)
            e.announced = True
        elif MODE in ("digest", "alerts"):
            why = alerts_for(rec)
            if why:
                self._say_tracked(self._alert_text(rec, why), ref, rec)
            e.announced = True
        self.store.upsert(e)

    # -------------------------------------------------------------- вывод
    def _alert_text(self, rec: dict, why: list[str]) -> str:
        head = f"🔍 <b>{rec['p1']}</b> — <b>{rec['p2']}</b>"
        if rec.get("tournament"):
            head += f"\n{rec['tournament']}"
        body = "\n".join(f"• {w}" for w in why)
        tail = ""
        if rec.get("sim_p1") is not None:
            tail = (f"\nСимуляция: {rec['sim_p1']:.0%} / {rec['sim_p2']:.0%}"
                    f"  (кэф {1/max(rec['sim_p1'],1e-9):.2f} / "
                    f"{1/max(rec['sim_p2'],1e-9):.2f})")
        link = f"\n<a href=\"{rec['url']}\">tennisratio</a>" if rec.get("url") else ""
        return f"{head}\n{body}{tail}{link}"

    def _retry_odds(self, ref: MatchRef, e: Entry, lock, results) -> None:
        """Ждущий матч: спрашиваем только линию, ничего не пересчитывая."""
        from tennis_parser.simulation import from_snapshot  # noqa: PLC0415

        rec = dict(e.rec)
        # Слепок бережёт разбор страницы и симуляцию — но не расписание.
        # `when`, `tournament` и `url` приходят с афиши, и на повторном круге
        # верны свежие: матч могли перенести, а после правки разбора карточки
        # в старом слепке вместо даты могло застрять имя игрока — и оно
        # переписывалось в журналы каждый круг.
        for field, fresh in (("when", ref.when), ("tournament", ref.tournament),
                             ("url", ref.url)):
            if fresh:
                rec[field] = fresh
        rec["_sim"] = from_snapshot(e.sim)
        e.last_try = datetime.now(timezone.utc).isoformat(timespec="seconds")

        odds_state, odds, bets = self._attach_odds(ref, e, rec)
        if odds_state == "waiting":
            self.store.upsert(e)
            return

        e.status = "done"
        e.summary["odds"] = odds_state
        e.sim, e.rec = {}, {}
        self.store.upsert(e)
        try:
            journal.log_match(rec, odds, bets)
        except Exception:  # noqa: BLE001
            log.error("журнал не записан:\n%s", traceback.format_exc())
        if odds:
            self._record_pick(rec, odds)
        if bets:
            self._announce_value(ref, rec, bets)
        with lock:
            results.append(rec)
        log.info("%s: линия дождалась, ценных ставок %d", ref.slug, len(bets))

    # -------------------------------------------------------------- линия
    def _attach_odds(self, ref: MatchRef, e: Entry, rec: dict):
        """Тянет линию и ищет ценность. Возвращает (состояние, кэфы, ставки)."""
        r = res_mod.fetch_odds(rec["p1"], rec["p2"])
        if r.state == res_mod.OddsResult.FOUND:
            try:
                # Без Elo второго мнения нет, и перевес систематически
                # завышен — ценность по таким матчам не ищем (см. REQUIRE_ELO).
                # Матч при этом считается и показывается как обычно, просто с
                # пометкой «без Elo»: скрывать его незачем, а ставить по нему
                # нельзя.
                if REQUIRE_ELO and rec.get("model_gap") is None:
                    log.info("%s: Elo недоступен — ценность не ищем", ref.slug)
                    bets = []
                else:
                    bets = find_value(rec.get("_sim"), r.odds)
            except Exception:  # noqa: BLE001
                log.error("поиск ценности упал:\n%s", traceback.format_exc())
                bets = []
            return "found", r.odds, bets

        if r.state == res_mod.OddsResult.NOT_OPEN:
            if res_mod.wait_expired(e.first_seen, ref.when):
                log.info("%s: линия так и не открылась за отведённое время", ref.slug)
                return "expired", None, []
            log.info("%s: линии пока нет (%s) — вернусь на следующем круге",
                     ref.slug, r.note)
            return "waiting", None, []

        log.warning("%s: линия не получена: %s", ref.slug, r.note)
        return "error", None, []

    def _announce_value(self, ref: MatchRef, rec: dict, bets: list[dict]) -> None:
        ids = journal.add_value_bets(rec, bets, STAKE)
        if not ids:
            return  # уже находили эти же ставки на прошлом круге
        head = (f"💎 <b>Найдена ценность</b>\n"
                f"{rec['p1']} — {rec['p2']}")
        if rec.get("tournament"):
            head += f"\n<i>{rec['tournament']}</i>"
        rows = []
        for b in bets:
            if f"{rec['slug']}|{b['market']}|{b['pick']}|{b.get('line')}" not in ids:
                continue
            rows.append(f"• <b>{describe(b)}</b> по {b['odds']}\n"
                        f"  модель {b['sim_prob']:.0%} (справедливый {b['fair_odds']}), "
                        f"перевес <b>{b['edge']:+.1%}</b>, Келли {b['kelly']:.1%}")
        if not rows:
            return
        self.say(head + "\n\n" + "\n".join(rows) +
                 "\n\n<i>Перевес по нашей модели, а она на данных не "
                 "калибровалась. Проверяйте цену перед ставкой.</i>")

    def _record_pick(self, rec: dict, odds: dict) -> None:
        """Ставка на исход по мнению модели — всегда, независимо от перевеса.

        Отдельная от value статистика: value-ставок мало и почти все они
        в форах и тоталах, а исход показывает саму модель без примеси того,
        как она выбирает рынки.
        """
        try:
            row = journal.add_pick(rec, odds, STAKE)
        except Exception:  # noqa: BLE001
            log.error("ставка на исход не записана:\n%s", traceback.format_exc())
            return
        if row:
            log.info("исход %s: %s %s по %s (рынок: %s)", rec["slug"],
                     row["side"], row["player"], row["odds"], row["market_side"])

    def _send_teaser(self, ref: MatchRef, rec: dict) -> None:
        """Компактная карточка по матчу: вероятности, кэфы, тотал сетов.

        При включённом Telegraph публикуются ДВЕ статьи — статистика и
        симуляция — и обе ссылки идут в карточке. Полные тексты в чат не
        шлём: на потоке матчей это простыня за простынёй.
        """
        sim = rec.get("_sim")
        report = rec.get("_report")
        if not sim:
            self.say(f"🎾 <b>{rec['p1']}</b> — <b>{rec['p2']}</b>\n"
                     "<i>симуляцию посчитать не вышло</i>")
            return

        stats_url = sim_url = None
        try:
            from tennis_parser.integration import (SHOW_MATCHES,  # noqa: PLC0415
                                                   USE_TELEGRAPH,
                                                   _publish_report)
            from tennis_parser.report import format_telegram  # noqa: PLC0415
            from tennis_parser.simulation import (  # noqa: PLC0415
                format_simulation_telegram)
            if USE_TELEGRAPH:
                if report:
                    try:
                        stats_url = _publish_report(
                            "stats",
                            format_telegram(report, show_matches=SHOW_MATCHES),
                            report, ref.p1, ref.p2, ref.tournament)
                    except Exception:  # noqa: BLE001
                        log.error("статья со статистикой %s не ушла:\n%s",
                                  ref.slug, traceback.format_exc())
                sim_url = _publish_report("sim", format_simulation_telegram(sim),
                                          report or {}, ref.p1, ref.p2,
                                          ref.tournament)
        except Exception:  # noqa: BLE001
            log.error("тизер %s: публикация не удалась:\n%s",
                      ref.slug, traceback.format_exc())

        links = []
        if stats_url:
            links.append(f'<a href="{stats_url}">📊 Статистика</a>')
        if sim_url:
            links.append(f'<a href="{sim_url}">🎲 Полная симуляция</a>')
        # Ссылки последними: иначе предупреждения о расхождении оказываются
        # под ними и теряются из виду
        text = self._with_context(self._compact(rec, sim), rec)
        if links:
            text += "\n\n" + "  ·  ".join(links)
        self._say_tracked(text, ref, rec)

        # Статьи не опубликовались — отдаём содержимое сообщениями, иначе
        # карточка остаётся без подробностей и матч фактически потерян.
        if not links and FALLBACK_FULL:
            self._send_full_text(ref, rec, sim)

    def _send_full_text(self, ref: MatchRef, rec: dict, sim: dict) -> None:
        """Полный отчёт сообщениями — запасной путь без Telegraph."""
        from tennis_parser.integration import SHOW_MATCHES, _split_message  # noqa: PLC0415
        from tennis_parser.report import format_telegram  # noqa: PLC0415
        from tennis_parser.simulation import (  # noqa: PLC0415
            format_simulation_telegram)

        report = rec.get("_report")
        parts = []
        if report:
            try:
                parts.append(format_telegram(report, show_matches=SHOW_MATCHES))
            except Exception:  # noqa: BLE001
                log.error("статистика не отформатировалась:\n%s",
                          traceback.format_exc())
        try:
            parts.append(format_simulation_telegram(sim))
        except Exception:  # noqa: BLE001
            log.error("симуляция не отформатировалась:\n%s", traceback.format_exc())

        for part in parts:
            for chunk in _split_message(part):
                self.say(chunk)

    def _say_tracked(self, text: str, ref: MatchRef, rec: dict) -> None:
        """Отправляет карточку и запоминает её message_id под матч.

        Без этого ответ на карточку не с чем сопоставить и меню с кнопкой
        кэфов не появится.
        """
        mid = self.say(text)
        if self.menu is not None:
            self.menu.track(mid, ref, rec)

    @staticmethod
    def _confidence_line(rec: dict) -> str:
        conf = confidence(rec)
        if not conf:
            return ""
        label, why = conf
        icon = "🚫" if label == "нет доверия" else "⚠️"
        return f"\n{icon} <b>{label}:</b> {why}"

    def _compact(self, rec: dict, sim: dict) -> str:
        """Та же карточка, что и с Telegraph, но без ссылки на статью."""
        m = sim["headline"]
        line = 2.5 if sim["best_of"] == 3 else 4.5
        over = sum(v for k, v in m["sets_played"].items() if k > line)
        fair = lambda p: f"{1 / p:.2f}" if p > 1e-9 else "—"  # noqa: E731
        return (f"🎲 <b>Симуляция</b> — {sim['runs']} прогонов, bo{sim['best_of']}\n"
                f"{rec['p1']} <b>{m['p1_win']:.0%}</b> (кэф {fair(m['p1_win'])}) · "
                f"{rec['p2']} <b>{m['p2_win']:.0%}</b> (кэф {fair(m['p2_win'])})\n"
                f"ТБ {line:g} сета: {over:.0%} (кэф {fair(over)})")

    def _with_context(self, text: str, rec: dict) -> str:
        """Дописывает турнир, ссылку на матч и сигналы, если они есть.

        На потоке матчей без турнира карточки сливаются в одну кашу, а без
        сигналов пришлось бы держать два режима сразу.
        """
        head = ""
        if rec.get("tournament"):
            head = f"<i>{rec['tournament']}</i>\n"
        why = alerts_for(rec)
        tail = ("\n" + "\n".join(f"⚠️ {w}" for w in why)) if why else ""
        tail = self._confidence_line(rec) + tail
        if rec.get("url"):
            tail += f"\n<a href=\"{rec['url']}\">tennisratio</a>"
        return f"{head}{text}{tail}"

    def _send_full(self, ref: MatchRef, rec: dict) -> None:
        """Режим all: полный отчёт по каждому матчу. Осторожно с объёмом."""
        from tennis_parser.integration import run_stats_parsing  # noqa: PLC0415
        from tennis_parser.tennisratio import guess_best_of  # noqa: PLC0415
        try:
            run_stats_parsing(lambda text, *a, **k: self.say(text), 0,
                              ref.p1, ref.p2, url=ref.url,
                              tournament=ref.tournament,
                              tour=TOUR,
                              best_of=guess_best_of(ref.tournament, TOUR))
        except Exception:  # noqa: BLE001
            log.error("полный отчёт %s не ушёл:\n%s", ref.slug, traceback.format_exc())

    def _report(self, results: list[dict], failures: list, total: int) -> None:
        if MODE == "silent" or not (results or failures):
            return  # круг без изменений — молчим, иначе капало бы каждые 10 мин
        if MODE in ("alerts", "all"):
            return
        lines = [f"📋 <b>Обход афиши</b>: {total} матчей, обработано {len(results)}"]
        if failures:
            lines.append(f"не вышло: {len(failures)}")
            # Самая частая ошибка прямо в сводке: без неё «обработано 0,
            # не вышло 17» не говорит ничего, и приходится лезть в состояние
            from collections import Counter  # noqa: PLC0415
            top, cnt = Counter(err for _, err in failures).most_common(1)[0]
            suffix = f" (×{cnt})" if cnt > 1 else ""
            lines.append(f"<code>{html.escape(top[:220])}</code>{suffix}")
        flagged = [r for r in results if alerts_for(r)]
        if flagged:
            lines.append(f"с сигналами: {len(flagged)}")
        c = self.store.counts()
        lines.append(f"<i>всего в базе: готово {c['done']}, "
                     f"неудач {c['failed']}, в очереди {c['pending']}</i>")
        self.say("\n".join(lines))

    # -------------------------------------------------------------- итоги
    def check_results(self) -> int:
        """Ищет результаты сыгранных матчей, закрывает ставки, пишет в чат."""
        # Сначала то, что закрывается без сети: ставки, у которых результат
        # матча уже лежит в журнале. Их поиск на TennisExplorer не найдёт —
        # строка журнала считается готовой и в очередь не попадает.
        closed = self._settle_known()

        # Журнал плюс матчи, у которых строки в журнале нет, а незакрытые
        # ставки есть. Без второго слагаемого такие ставки не закроются
        # никогда: искать результат было некому.
        pending = journal.unresolved_slugs() + journal.orphan_pending()
        if not pending:
            return closed
        try:
            found = res_mod.fetch_results()
        except Exception:  # noqa: BLE001
            log.error("результаты не собрались:\n%s", traceback.format_exc())
            return closed
        if not found:
            log.info("результатов не нашлось (в ожидании %d матчей)", len(pending))
            return closed

        # Один результат — одному матчу. Сопоставление идёт по фамилиям
        # подстрокой, и без этого ограничения удачное совпадение могло
        # достаться нескольким матчам подряд.
        remaining = list(found)
        unmatched = []
        for row in pending:
            if not row.get("slug"):
                continue
            found_at = res_mod.find_result(row.get("p1", ""), row.get("p2", ""),
                                           remaining)
            if found_at is None:
                unmatched.append(row)
                continue
            idx, flipped = found_at
            hit = remaining.pop(idx)
            score = hit[2]
            # Четвёртый элемент — кому присуждён недоигранный матч; у старых
            # кортежей его нет, поэтому берём осторожно
            ret_winner = hit[3] if len(hit) > 3 else ""
            try:
                outcome = res_mod.outcome_from_score(score, flipped,
                                                     ret_winner)
            except Exception:  # noqa: BLE001
                log.error("счёт %r не разобрался:\n%s", score, traceback.format_exc())
                continue
            if not outcome["winner"] and not outcome.get("void"):
                # Победителя нет и это не отказ — счёт разобрался криво.
                # Закрывать по нему нельзя, на следующем круге попробуем ещё.
                continue

            if not row.get("_orphan"):
                journal.log_result(row["slug"], outcome)
            pick = journal.resolve_pick(row["slug"], outcome)
            bets = journal.resolve_value_bets(
                row["slug"], outcome,
                lambda b: self._settle_row(b, outcome))
            self._announce_result(row, outcome, bets, pick)
            closed += 1
        closed += self._close_abandoned(unmatched)
        if closed:
            log.info("закрыто матчей: %d", closed)
        return closed

    def _close_abandoned(self, unmatched: list[dict]) -> int:
        """Закрывает возвратом матчи, которых так и не случилось.

        Отменённый матч TennisExplorer просто не публикует: искать его можно
        вечно, и ставка висит «ждём» до скончания века, портя картину в
        панели и в отчётах. Через ABANDON_HOURS после начала считаем, что
        матча не было, и возвращаем ставки — ровно как поступает букмекер.

        Вызывается только после удачной загрузки результатов: при недоступном
        TennisExplorer `check_results` выходит раньше, иначе один сетевой сбой
        разом закрыл бы возвратом всю афишу.
        """
        closed = 0
        for row in unmatched:
            when = row.get("when") or ""
            if not res_mod.abandoned(when):
                continue
            slug = row["slug"]
            # WARNING, а не INFO: если сюда посыплются матчи пачками, дело не
            # в отменах, а в том, что сломалось сопоставление имён.
            log.warning("%s (%s — %s, %s): результата нет спустя %.0f ч — "
                        "закрываю возвратом как несостоявшийся",
                        slug, row.get("p1"), row.get("p2"), when,
                        res_mod.ABANDON_HOURS)
            outcome = res_mod.cancelled_outcome()
            if not row.get("_orphan"):
                journal.log_result(slug, outcome)
            pick = journal.resolve_pick(slug, outcome)
            bets = journal.resolve_value_bets(
                slug, outcome, lambda b: ("refund", 0.0))
            if pick or bets:
                self._announce_result(row, outcome, bets, pick)
            closed += 1
        return closed

    def _settle_known(self) -> int:
        """Закрывает ставки, результат которых уже записан в журнале.

        Ничего не скачивает. Нужен, потому что обычный обход идёт по
        незакрытым строкам журнала, а тут строка закрыта — незакрыта
        ставка. Без этого прохода такая ставка висит «ждём» бесконечно,
        и в отчётах её видно только по счётчику ожидания.
        """
        closed = 0
        for slug, outcome in journal.stranded_pending():
            pick = journal.resolve_pick(slug, outcome)
            bets = journal.resolve_value_bets(
                slug, outcome, lambda b: self._settle_row(b, outcome))
            if not (pick or bets):
                continue
            row = {"p1": (pick or bets[0]).get("p1"),
                   "p2": (pick or bets[0]).get("p2"),
                   "slug": slug}
            self._announce_result(row, outcome, bets, pick)
            closed += 1
        if closed:
            log.info("закрыто по уже известному результату: %d", closed)
        return closed

    @staticmethod
    def _settle_row(row: dict, outcome: dict) -> tuple[str, float]:
        # значения приходят из CSV, где десятичный разделитель — запятая
        bet = {"market": row["market"], "pick": row["pick"],
               "line": journal.pf(row.get("line")),
               "odds": journal.pf(row.get("odds"), 0.0)}
        # Признак недоигранного матча берём из `void`, а НЕ поиском подстроки
        # «ret» в счёте. Неявка приходит со счётом «w.o.», подстроки там нет,
        # флаг не выставлялся — и форы считались по счёту 0:0, где проходит
        # любая плюсовая линия. 27.08.2026 так «выиграли» Sets Hcap П2 +1.5 и
        # Games Hcap П2 +4.5 на Ann Li — Maria Timofeeva, хотя у Pinnacle
        # неявка это возврат всего. `void` ставит UNFINISHED_RE, он знает и
        # про ret, и про w.o./walkover/def/abd/canc. Тот же флаг уже
        # используется в tennisratioall_run.py --rebuild-results.
        status = settle(bet, outcome["sets_p1"], outcome["sets_p2"],
                        outcome["games_p1"], outcome["games_p2"],
                        retired=bool(outcome.get("void")),
                        winner=outcome.get("winner", ""))
        stake = journal.pf(row.get("stake"), STAKE)
        return status, profit(bet, status, stake)

    def _announce_result(self, row: dict, outcome: dict, bets: list[dict],
                         pick: dict | None = None) -> None:
        void = bool(outcome.get("void"))
        cancelled = outcome.get("score") == res_mod.CANCELLED_SCORE
        if cancelled:
            # Счёта и геймов у несостоявшегося матча нет — строки с нулями
            # только сбивали бы с толку.
            self.say(f"↩️ <b>{row['p1']}</b> — <b>{row['p2']}</b>\n"
                     f"Матч не состоялся: результата нет спустя "
                     f"{res_mod.ABANDON_HOURS:.0f} ч. Ставки в возврат.")
            return
        lines = [
            f"🏁 <b>{row['p1']}</b> — <b>{row['p2']}</b>",
            f"Счёт: {outcome['score']} (по сетам "
            f"{outcome['sets_p1']}-{outcome['sets_p2']})",
            f"Геймы: {outcome['games_p1']}-{outcome['games_p2']} "
            f"(всего {outcome['games_total']}, разница "
            f"{outcome['games_diff']:+d})",
        ]
        win = ""
        if void:
            # Форы и тоталы у недоигранного матча Pinnacle возвращает всегда.
            # А вот победитель, если TennisExplorer его назвал и сыгран хотя
            # бы сет, засчитывается — тогда исход рассчитан, а не возвращён.
            if outcome.get("winner") in ("p1", "p2"):
                got = row["p1"] if outcome["winner"] == "p1" else row["p2"]
                lines.append("↩️ <b>Матч не доигран</b> — форы и тоталы в "
                             f"возврат, победа присуждена: <b>{got}</b>")
            else:
                lines.append("↩️ <b>Матч не доигран</b> — ставки в возврат")
                pick = None
        else:
            win = row["p1"] if outcome["winner"] == "p1" else row["p2"]
            lines.append(f"Победил: <b>{win}</b>")
        if pick:
            mark = "✅" if pick["status"] == "win" else "❌"
            # Помечаем оба случая, а не только спор: «с рынком» тоже
            # информация — по ней видно, что ставка ничего не доказывает,
            # такую же дал бы и букмекерский фаворит без всякой модели
            tag = ("с рынком" if pick.get("agree") == "да"
                   else "🔀 против рынка")
            lines.append(f"{mark} исход: {pick['player']} по {pick['odds']} "
                         f"<i>({tag})</i> → "
                         f"<b>{journal.pf(pick['profit'], 0):+.0f}</b>")
        elif not void:
            sim_p1 = journal.pf(row.get("sim_p1"), 0.0)
            if sim_p1:
                said = row["p1"] if sim_p1 >= 0.5 else row["p2"]
                mark = "✅" if said == win else "❌"
                lines.append(f"{mark} модель ставила на {said} "
                             f"({max(sim_p1, 1 - sim_p1):.0%})")

        if bets:
            lines.append("")
            total = 0.0
            for b in bets:
                icon = {"win": "✅", "loss": "❌"}.get(b["status"], "↩️")
                total += journal.pf(b.get("profit"), 0.0)
                # у форы знак обязателен: «П1 3.5» и «П1 -3.5» — разные ставки
                ln = journal.pf(b.get("line"))
                if ln is None:
                    tail = ""
                elif "Hcap" in b["market"]:
                    tail = f" {ln:+g}"
                else:
                    tail = f" {ln:g}"
                lines.append(f"{icon} {b['market']} {b['pick']}{tail}"
                             f" по {b['odds']}: "
                             f"<b>{journal.pf(b['profit'], 0.0):+.0f}</b>")
            if len(bets) > 1:
                lines.append(f"Итого: <b>{total:+.0f}</b>")
        self.say("\n".join(lines))

    # -------------------------------------------------------------- кэш
    def _maybe_prune_cache(self) -> None:
        """Чистка файлового кэша страниц — не чаще раза в сутки.

        Круг идёт каждые десять минут, а перебирать сотни файлов на диске
        так часто незачем: кэш растёт медленнее. Отметку держим в состоянии,
        а не в памяти, иначе перезапуск службы запускал бы чистку заново.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        if self.store.meta.get("cache_pruned") == today:
            return
        try:
            from tennis_parser.http import prune_cache  # noqa: PLC0415
            prune_cache()
        except Exception:  # noqa: BLE001
            # Чистка кэша — дело третьестепенное: обход из-за неё падать
            # не должен, но и молча пропадать она тоже не должна.
            log.error("чистка кэша не удалась:\n%s", traceback.format_exc())
            return
        self.store.meta["cache_pruned"] = today
        self.store.save()

    # -------------------------------------------------------------- отчёты
    def maybe_send_reports(self) -> None:
        """Дневной, недельный и месячный отчёты — по одному разу за период.

        Отметки о том, что уже отправлено, лежат в состоянии, а не в памяти:
        иначе перезапуск службы в течение дня слал бы отчёт заново.
        """
        from datetime import datetime, timezone  # noqa: PLC0415

        from . import reports  # noqa: PLC0415

        now = datetime.now(timezone.utc)
        if now.hour < REPORT_HOUR:
            return
        today = now.date()
        marks = self.store.meta.setdefault("reports", {})

        due = [("day", today.isoformat())]
        if today.weekday() == 6:            # воскресенье — недельный
            due.append(("week", today.isoformat()))
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        if (nxt - timedelta(days=1)) == today:   # последний день месяца
            due.append(("month", today.isoformat()))

        for period, stamp in due:
            if marks.get(period) == stamp:
                continue
            try:
                text = reports.format_report(period)
            except Exception:  # noqa: BLE001
                log.error("отчёт %s не собрался:\n%s", period, traceback.format_exc())
                continue
            self.say(text)
            marks[period] = stamp
            self.store.save()
            log.info("отчёт %s отправлен", period)

    # -------------------------------------------------------------- цикл
    def run_forever(self, interval: int) -> None:
        log.info("сканер запущен: интервал %d с, воркеров %d, режим %s",
                 interval, WORKERS, MODE)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                log.error("круг упал целиком:\n%s", traceback.format_exc())
            try:
                closed = self.check_results()
                # Второй заход через минуту, если что-то закрылось: результаты
                # на TennisExplorer появляются пачками, и матч, доигравший
                # минуту назад, попадёт уже в следующую выборку.
                if closed:
                    self._stop.wait(60)
                    self.check_results()
            except Exception:  # noqa: BLE001
                log.error("проверка результатов упала:\n%s", traceback.format_exc())
            try:
                self.maybe_send_reports()
            except Exception:  # noqa: BLE001
                log.error("отчёты не отправились:\n%s", traceback.format_exc())
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()
