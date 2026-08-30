"""Общий на все процессы кэш и ограничитель запросов к Pinnacle.

Что пошло не так без него
--------------------------
`get_pinnacle_odds` на КАЖДЫЙ матч заново скачивал полный список матчапов, а
если тот не отдавался — обходил все лиги по отдельному запросу. Обход афиши из
17 матчей превращался в сотни обращений подряд, плюс основной бот дёргал API
своим чередом. Итог предсказуем: блокировка.

Что здесь есть
--------------
1. Кэш списка матчапов на диске. Список общий для всех матчей дня, качать его
   заново ради каждого — чистая трата. Один запрос вместо семнадцати.

2. Ограничитель: минимум секунд между обращениями. Состояние в файле, а не в
   памяти, потому что процессов два (основной бот и обход афиши), а лимит у
   Pinnacle общий — он считает по IP, а не по процессу.

3. Отступ при блокировке, удваивающийся с каждым разом. Пока идёт отступ,
   запросы не делаются вовсе: долбиться в заблокированный API — надёжный
   способ продлить блокировку.

Файл состояния делится между процессами, поэтому все операции короткие и под
файловой блокировкой.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("PIN_GUARD_STATE") or os.path.join(HERE, ".pinnacle_guard.json")
CACHE = os.environ.get("PIN_MATCHUPS_CACHE") or os.path.join(HERE, ".pinnacle_matchups.json")
BULK = os.environ.get("PIN_BULK_CACHE") or os.path.join(HERE, ".pinnacle_bulk.json")


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Сколько живёт список матчапов. Состав матчей на день меняется медленно —
# гораздо медленнее цен, — поэтому полчаса тут ничего не портят, а запросов
# к API экономят на порядок: было двенадцать обновлений в час, стало два.
# Плата за это — матч, выставленный Pinnacle только что, найдётся не сразу,
# а в пределах получаса. Для бота это неважно: он всё равно переспрашивает
# линию каждый круг и ждёт её до TRA_ODDS_WAIT_HOURS.
CACHE_TTL = _f("PIN_CACHE_TTL", 1800)
# Цены живут меньше состава матчей, поэтому кэш котировок короче
BULK_TTL = _f("PIN_BULK_TTL", 120)
# Минимум секунд между любыми обращениями к API. Применяется ко всем
# запросам, включая markets/straight — а их по два на матч, то есть около
# тридцати за круг из 17 матчей. Отсюда и цена интервала: 20 секунд стоят
# примерно 12 минут круга, 60 секунд — уже 35, и обход перестаёт успевать.
MIN_INTERVAL = _f("PIN_MIN_INTERVAL", 20)

# Версия правил обращения с API. Поднимается, когда меняется поведение,
# за которое можно получить отступ. Нужна, чтобы поймать процесс, который
# после обновления не перезапустили и который живёт по старым правилам.
GUARD_VERSION = 3
# первый отступ после блокировки; дальше удваивается
COOLDOWN_BASE = _f("PIN_COOLDOWN_BASE", 900)
COOLDOWN_MAX = _f("PIN_COOLDOWN_MAX", 21600)


# ------------------------------------------------------------------ прокси
def proxies() -> dict | None:
    """Прокси для запросов к Pinnacle, если он задан.

    Отдельно от общесистемных HTTP_PROXY намеренно: банят обычно только
    Pinnacle, а гонять через посредника ещё и рендеринг страниц tennisratio
    незачем — это и медленнее, и лишняя точка отказа.

    Форматы: http://user:pass@host:port или socks5://host:port
    (для socks нужен requests[socks], то есть пакет PySocks).
    """
    url = os.environ.get("PIN_PROXY", "").strip()
    if not url:
        return None
    return {"http": url, "https": url}


def proxy_note() -> str:
    url = os.environ.get("PIN_PROXY", "").strip()
    if not url:
        return "прямое соединение"
    # пароль в логи не пишем
    safe = re.sub(r"//[^@/]*@", "//***@", url)
    return f"через прокси {safe}"


# ------------------------------------------------------------------ файлы
def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _write(path: str, data) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("не записан %s: %s", path, exc)


# ------------------------------------------------------------------ отступ
def cooldown_left() -> float:
    """Сколько секунд ещё нельзя обращаться к API."""
    st = _read(STATE)
    until = float(st.get("cooldown_until") or 0)
    return max(0.0, until - time.time())


def report_block() -> float:
    """Зафиксировать блокировку. Возвращает длительность нового отступа."""
    st = _read(STATE)
    streak = int(st.get("block_streak") or 0) + 1
    delay = min(COOLDOWN_BASE * (2 ** (streak - 1)), COOLDOWN_MAX)
    # немного случайности, чтобы два процесса не вышли из отступа синхронно
    delay += random.uniform(0, delay * 0.1)
    # Пишем, КТО поставил отступ. Файл общий на оба процесса, и когда один
    # из них работает на старом коде, он ставит отступ, а второй молча его
    # соблюдает — выяснить это без отметки было невозможно.
    st.update(block_streak=streak, cooldown_until=time.time() + delay,
              last_block=time.time(), by_pid=os.getpid(),
              by_proc=os.path.basename(sys.argv[0] or "?"),
              guard_version=GUARD_VERSION)
    _write(STATE, st)
    log.warning("Pinnacle заблокировал (подряд %d) — отступ %.0f мин",
                streak, delay / 60)
    return delay


def report_ok() -> None:
    """Успешный ответ обнуляет счётчик блокировок."""
    st = _read(STATE)
    if st.get("block_streak"):
        st["block_streak"] = 0
        st["cooldown_until"] = 0
        _write(STATE, st)
        log.info("Pinnacle снова отвечает — отступ снят")


def wait_turn(timeout: float = 30) -> bool:
    """Держит паузу между обращениями. False — сейчас нельзя (идёт отступ).

    Не сон на всю длительность отступа: часы блокировки нельзя проспать внутри
    обработки матча, вызывающий должен просто отложить его на следующий круг.
    """
    left = cooldown_left()
    if left > 0:
        log.info("Pinnacle в отступе ещё %.0f мин — пропускаю", left / 60)
        return False

    st = _read(STATE)
    last = float(st.get("last_request") or 0)
    gap = time.time() - last
    if gap < MIN_INTERVAL:
        nap = min(MIN_INTERVAL - gap, timeout)
        time.sleep(nap)
    st = _read(STATE)          # перечитываем: другой процесс мог успеть
    st["last_request"] = time.time()
    _write(STATE, st)
    return True


# ------------------------------------------------------------------ кэш
def get_matchups(fetch_fn):
    """Список матчапов: из кэша, если свежий, иначе через fetch_fn().

    fetch_fn отвечает за сам запрос и возвращает список либо пустое значение.
    Пустой ответ не кэшируем — иначе одна неудача заморозила бы пустоту на
    весь TTL.
    """
    cached = _read(CACHE)
    age = time.time() - float(cached.get("at") or 0)
    data = cached.get("data")
    if data and age < CACHE_TTL:
        log.debug("матчапы из кэша (возраст %.0f с, %d шт)", age, len(data))
        return data

    fresh = fetch_fn()
    if fresh:
        _write(CACHE, {"at": time.time(), "data": fresh})
        log.info("матчапы обновлены: %d шт", len(fresh))
        return fresh

    if data:
        log.warning("матчапы не скачались — беру просроченный кэш "
                    "(возраст %.0f мин)", age / 60)
        return data
    return []


def get_bulk(fetch_fn):
    """Котировки по всем матчам сразу. Кэш короче, чем у списка матчапов:
    состав матчей за день не меняется, а цены двигаются постоянно."""
    cached = _read(BULK)
    age = time.time() - float(cached.get("at") or 0)
    data = cached.get("data")
    if data and age < BULK_TTL:
        log.debug("котировки из кэша (возраст %.0f с, %d рынков)", age, len(data))
        return data
    fresh = fetch_fn()
    if fresh:
        _write(BULK, {"at": time.time(), "data": fresh})
        log.info("котировки обновлены пакетом: %d рынков", len(fresh))
        return fresh
    if data and age < BULK_TTL * 4:
        log.warning("котировки не скачались — беру кэш возрастом %.0f с", age)
        return data
    return []


def cache_age() -> float | None:
    at = float(_read(CACHE).get("at") or 0)
    return (time.time() - at) if at else None


def status() -> dict:
    st = _read(STATE)
    return {
        "by_pid": st.get("by_pid"),
        "by_proc": st.get("by_proc"),
        "by_version": st.get("guard_version"),
        "stale_writer": bool(st.get("cooldown_until", 0) > time.time()
                             and st.get("guard_version", 0) < GUARD_VERSION),
        "cooldown_left": round(cooldown_left()),
        "block_streak": st.get("block_streak") or 0,
        "cache_age": round(cache_age()) if cache_age() is not None else None,
        "matchups": len(_read(CACHE).get("data") or []),
    }
