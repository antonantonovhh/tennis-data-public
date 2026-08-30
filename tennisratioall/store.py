"""tennisratioall — сбор статистики и симуляции по ВСЕМ матчам дня.

Отличие от кнопки в основном боте: там вы выбираете матч и ждёте один отчёт,
здесь бот сам обходит всю афишу и считает каждый матч, а при появлении новых
досчитывает их.

Что пришлось решать по дороге
------------------------------
**Время.** Страница h2h на tennisratio рендерится браузером, это 30-60 секунд
на матч. На афише бывает под сотню матчей, то есть последовательный обход —
это час-полтора. Поэтому очередь с несколькими воркерами и паузой между
запросами: параллелить сильно нельзя, сайт чужой.

**Поток сообщений.** Сто отчётов в чат — это не помощь, а спам. Поэтому всё
считается в хранилище, а в чат уходит сводка и точечные сигналы по матчам,
где модель заметно расходится с рынком или между собой. Режим настраивается.

**Падения.** Один матч не должен уносить прогон. Каждый обрабатывается
изолированно, ошибки копятся в состоянии, неудачные повторяются с отступом,
но не бесконечно.

**Повторы.** Матч считается один раз. Состояние переживает перезапуск, иначе
после рестарта бот заново пойдёт скрести всю афишу.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))

# Какой тур обходим: atp или wta. Влияет на три вещи — адрес афиши на
# tennisratio, таблицу Elo на tennisabstract и имена файлов с данными.
#
# Файлы разделены намеренно: смешивать мужские и женские матчи в одном
# журнале нельзя, иначе калибровка и ROI считаются по двум разным
# популяциям сразу. У ATP имена остаются прежними (без суффикса), чтобы
# накопленные данные никуда не переезжали, у остальных туров добавляется
# «_<тур>»: tennisratioall_state_wta.json и так далее.
#
# Переменную надо выставить ДО импорта пакета — пути считаются на импорте.
# Через tennisratioall_run.py --tour wta это делается автоматически.
TOUR = os.environ.get("TRA_TOUR", "atp").strip().lower()
if TOUR not in ("atp", "wta"):
    raise SystemExit(f"TRA_TOUR: ожидается atp или wta, получено {TOUR!r}")
SUFFIX = "" if TOUR == "atp" else f"_{TOUR}"

STATE_FILE = (os.environ.get("TRA_STATE")
              or os.path.join(HERE, f"tennisratioall_state{SUFFIX}.json"))
RESULTS_FILE = (os.environ.get("TRA_RESULTS")
                or os.path.join(HERE, f"tennisratioall_results{SUFFIX}.jsonl"))


def _env_int(name, default, lo=None, hi=None):
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


# сколько матчей скребём одновременно. Больше двух — уже неприлично по
# отношению к чужому сайту, и легко получить бан по IP
WORKERS = _env_int("TRA_WORKERS", 2, 1, 4)
# пауза между запусками задач, секунды: сглаживает нагрузку на источник
THROTTLE = _env_int("TRA_THROTTLE", 5, 0, 300)
# как часто перечитывать афишу
POLL_INTERVAL = _env_int("TRA_POLL", 600, 60, 86400)
# прогонов симуляции: на потоке матчей 10k избыточны, 5k дают ту же картину
SIM_RUNS = _env_int("TRA_SIM_RUNS", 5000, 500, 200_000)
# сколько раз пробовать матч, прежде чем оставить его в покое
MAX_ATTEMPTS = _env_int("TRA_MAX_ATTEMPTS", 3, 1, 10)
# через сколько дней выбрасывать запись из состояния.
# Матч живёт в афише один день: как только он с неё уходит, discover() его
# больше не возвращает, и запись невозможно ни доработать, ни закрыть — она
# просто копится. Особенно заметно после --reset-failed: он переводит
# неудачи обратно в pending, и если матч уже сыгран, счётчик «в очереди»
# застревает навсегда (так и получилось 15 висяков от 21.08).
# Значение по умолчанию — двойной запас к окну ожидания линии
# (TRA_ODDS_WAIT_HOURS, 36 ч): живой матч выкинуть не успеем, а мусор
# не залёживается. Нижняя граница по той же причине — меньше двух суток
# ставить нельзя.
STALE_DAYS = _env_int("TRA_STALE_DAYS", 3, 2, 60)
# Как часто сбрасывать состояние на диск по ходу круга, секунды.
# 0 — только в конце круга (прежнее поведение). См. Store.upsert.
AUTOSAVE_SEC = _env_int("TRA_AUTOSAVE_SEC", 5, 0, 3600)
# Не искать ценность там, где Elo недоступен хотя бы у одного игрока.
# Таблица tennisabstract покрывает ~550 человек (примерно топ ATP), и на
# челленджерах Elo-модель не строится вовсе. Тогда прогноз держится на одной
# статистике, сверить его не с чем, и перевес выходит фантастическим:
# замер 23.08.2026 — медиана +40% против +13% там, где Elo есть, максимум
# +72%. Против Pinnacle таких перевесов не бывает, это артефакт модели.
# Поставьте TRA_REQUIRE_ELO=0, чтобы вернуть прежнее поведение.
REQUIRE_ELO = os.environ.get("TRA_REQUIRE_ELO", "1") in ("1", "true", "yes")
# режим отчётности: digest — сводка + сигналы, alerts — только сигналы,
# all — полный отчёт по каждому матчу (осторожно), silent — только в файл
MODE = os.environ.get("TRA_MODE", "digest").strip().lower()

# минимум секунд между сообщениями в чат. Telegram режет ботов примерно на
# 20 сообщениях в минуту на чат; при обычном темпе парсинга (45 с на матч) это
# не грозит, но матчи из кэша посыпались бы очередью и словили лимит
SEND_PACE = float(os.environ.get("TRA_SEND_PACE", "3.5"))

# порог сигнала: насколько симуляция должна разойтись с Elo, чтобы написать
ALERT_GAP = float(os.environ.get("TRA_ALERT_GAP", "0.15"))

# условная ставка для журнала: реальные деньги ставите сами, здесь она нужна
# только чтобы прибыль и ROI считались в понятных числах
try:
    STAKE = float(os.environ.get("TRA_STAKE", "1000"))
except ValueError:
    STAKE = 1000.0

# час UTC, после которого отправляются периодические отчёты
REPORT_HOUR = _env_int("TRA_REPORT_HOUR", 18, 0, 23)

# Если Telegraph недоступен, слать полный отчёт сообщениями. Иначе карточка
# остаётся без подробностей и матч фактически теряется. На потоке матчей это
# заметный объём — можно выключить.
FALLBACK_FULL = os.environ.get("TRA_FALLBACK_FULL", "1") in ("1", "true", "yes")


# ------------------------------------------------------------------ состояние
@dataclass
class MatchRef:
    """Матч с афиши. Ключ — slug, он же в ссылке tennisratio."""
    slug: str
    p1: str
    p2: str
    url: str = ""
    tournament: str = ""
    when: str = ""

    @property
    def title(self) -> str:
        return f"{self.p1} — {self.p2}"


@dataclass
class Entry:
    """Что мы знаем о матче: статус, попытки, краткий итог.

    announced — карточка уже отправлена. Без этого флага матч, ждущий линию,
    заново слал карточку каждые десять минут: статус не «done», значит очередь
    берёт его снова, а отправка ничего не помнила.

    sim — компактный слепок распределений симуляции (гистограммы сетов, счётов
    и разницы геймов). Он позволяет переоценить ценность, когда линия наконец
    откроется, НЕ перезапуская ни парсинг страницы, ни сами прогоны.
    """
    slug: str
    status: str = "pending"       # pending | done | awaiting_odds | failed | skipped
    attempts: int = 0
    first_seen: str = ""
    last_try: str = ""
    error: str = ""
    announced: bool = False
    summary: dict = field(default_factory=dict)
    sim: dict = field(default_factory=dict)
    rec: dict = field(default_factory=dict)


class Store:
    """Состояние на диске. Пишем целиком и атомарно.

    Файл маленький (запись на матч — сотня байт), поэтому возни с частичной
    записью не стоит: временный файл плюс os.replace дают атомарность, и
    прерванный посреди записи процесс не оставит битый JSON.
    """

    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self._lock = threading.Lock()
        # защёлка автосохранения — отдельно от _lock, см. _save_throttled
        self._save_gate = threading.Lock()
        self._last_autosave = 0.0
        self.entries: dict[str, Entry] = {}
        # произвольные отметки, не привязанные к матчам: когда отправлялись
        # периодические отчёты и тому подобное
        self.meta: dict = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            raw = json.load(open(self.path, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.error("состояние %s не читается (%s) — начинаю с чистого", self.path, exc)
            return
        self.meta = raw.get("meta") or {}
        for slug, d in (raw.get("entries") or {}).items():
            d.pop("slug", None)
            self.entries[slug] = Entry(slug=slug, **d)
        log.info("состояние загружено: %d матчей", len(self.entries))

    def save(self) -> None:
        with self._lock:
            data = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "meta": self.meta,
                    "entries": {k: {kk: vv for kk, vv in asdict(v).items() if kk != "slug"}
                                for k, v in self.entries.items()}}
            tmp = f"{self.path}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.error("состояние не сохранено: %s", exc)

    def get(self, slug: str) -> Entry | None:
        return self.entries.get(slug)

    def upsert(self, e: Entry, *, persist: bool = True) -> None:
        """Кладёт запись и (по умолчанию) сбрасывает состояние на диск.

        Раньше save() звался только в конце круга, после t.join(). Круг из
        двух десятков матчей идёт около часа, и перезапуск службы в середине
        терял ВЕСЬ прогресс очереди: журналы (CSV) уцелевали, потому что
        пишутся сразу, а статусы и счётчики попыток обнулялись, и следующий
        круг шёл по всем матчам заново.

        Сохранение приторможено (TRA_AUTOSAVE_SEC, 5 с): пишем файл целиком,
        а вызовов из двух воркеров набегает под полсотни за круг. При потере
        процесса теряется максимум последние несколько секунд вместо часа.
        """
        with self._lock:
            self.entries[e.slug] = e
        if persist:
            self._save_throttled()

    def _save_throttled(self) -> None:
        """Сохраняет не чаще раза в AUTOSAVE_SEC секунд.

        Отдельная защёлка, а не self._lock: сохранение зовётся из воркеров,
        и ждать на том же замке, который держит save(), незачем. Брать
        self._lock здесь тем более нельзя — save() возьмёт его сам, а
        threading.Lock не реентрантный, и это был бы дедлок.
        """
        if not AUTOSAVE_SEC:
            return
        now = time.monotonic()
        with self._save_gate:
            if now - self._last_autosave < AUTOSAVE_SEC:
                return
            self._last_autosave = now
        self.save()

    def prune(self, days: int = STALE_DAYS) -> int:
        """Выбрасывает записи старше `days` дней. Возвращает, сколько убрал.

        Ориентируемся на самое позднее известное время (last_try, иначе
        first_seen). Если ни одно не разбирается — запись оставляем: лучше
        лишняя строка в состоянии, чем потерянный матч.

        Отчёты и статистика от этого не страдают: они строятся из
        value_bets.csv и matches_log.csv, а состояние — только бухгалтерия
        обхода.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        dead = []
        for slug, e in self.entries.items():
            stamp = None
            for raw in (e.last_try, e.first_seen):
                if not raw:
                    continue
                try:
                    got = datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if got.tzinfo is None:
                    got = got.replace(tzinfo=timezone.utc)
                stamp = got if stamp is None else max(stamp, got)
            if stamp is not None and stamp < cutoff:
                dead.append(slug)
        if dead:
            with self._lock:
                for slug in dead:
                    self.entries.pop(slug, None)
            log.info("из состояния убрано %d записей старше %d дн.",
                     len(dead), days)
        return len(dead)

    def needs_work(self, ref: MatchRef) -> bool:
        e = self.entries.get(ref.slug)
        if e is None:
            return True
        if e.status in ("done", "skipped"):
            # skipped — матч без Elo. Рейтинг за десять минут не появится
            # (таблица tennisabstract обновляется раз в неделю), поэтому
            # переразбирать его каждый круг — впустую жечь по три минуты
            # парсинга на матч.
            return False
        if e.status == "awaiting_odds":
            # статистика посчитана, ждём только линию — попытки не тратим,
            # иначе матч выпал бы из очереди раньше, чем откроются котировки
            return True
        return e.attempts < MAX_ATTEMPTS

    def counts(self) -> dict:
        out = {"done": 0, "failed": 0, "pending": 0, "skipped": 0,
               "awaiting_odds": 0}
        for e in self.entries.values():
            out[e.status] = out.get(e.status, 0) + 1
        return out


def append_result(record: dict, path: str = RESULTS_FILE) -> None:
    """Итог матча в JSONL — по строке на матч, удобно грепать и грузить."""
    # ключи с подчёркиванием — служебные (сырой отчёт, объект симуляции):
    # они нужны отправителю, но в журнале раздули бы строку до мегабайта
    clean = {k: v for k, v in record.items() if not k.startswith("_")}
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(clean, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.error("результат не записан: %s", exc)
