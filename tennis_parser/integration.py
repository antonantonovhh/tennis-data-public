"""Мост между пакетом tennis_parser и старым ботом (bot.py на requests+потоках).

Старый бот синхронный и работает на потоках, поэтому здесь ничего асинхронного:
просто функция, которую он запускает в threading.Thread. Внутри — семафор,
чтобы два клика по кнопке не подняли два Chromium одновременно.
"""

from __future__ import annotations

import contextlib
import logging
import re
import os
import threading
import time
import traceback
from datetime import date

from .http import Fetcher
from .report import build_report, format_telegram
from .simulation import DEFAULT_RUNS, build_simulation, format_simulation_telegram
from .telegraph import publish as telegraph_publish
from .tennisratio import guess_surface

log = logging.getLogger("tennis_parser.integration")

# один тяжёлый парсинг за раз: рендер страницы — это отдельный Chromium
# Лимит одновременных парсингов, общий на кнопку и на обход афиши.
# Каждый парсинг поднимает headless-браузер, так что упирается всё в память
# сервера и в приличия по отношению к чужому сайту, а не в CPU.
try:
    PARSE_CONCURRENCY = max(1, min(int(os.environ.get("TP_PARSE_CONCURRENCY", 1)), 4))
except ValueError:
    PARSE_CONCURRENCY = 1
_SEM = threading.Semaphore(PARSE_CONCURRENCY)


# Блокировка между ПРОЦЕССАМИ. threading.Semaphore выше держит очередь
# только внутри одного процесса, а обходчиков теперь два — ATP и WTA, — и
# каждый поднял бы свой Chromium. На сервере 1 ядро и 2 ГБ памяти, два
# headless-браузера одновременно кладут его в OOM, причём убитым окажется
# случайный из них.
#
# Файловая блокировка на flock: снимается ядром сама, если процесс умер, —
# в отличие от «файл существует», который после падения оставил бы вечный
# замок. На Windows fcntl нет, но там второй обходчик и не запускается,
# поэтому блокировка молча вырождается в пустышку.
_LOCK_PATH = os.environ.get("TP_PARSE_LOCK") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".parse.lock")
try:
    import fcntl  # noqa: PLC0415
except ImportError:            # Windows / не-POSIX
    fcntl = None


@contextlib.contextmanager
def _cross_process_slot(timeout: float | None):
    if fcntl is None:
        yield
        return
    fh = open(_LOCK_PATH, "w")
    try:
        if timeout is None:
            fcntl.flock(fh, fcntl.LOCK_EX)          # ждём сколько нужно
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "не дождался межпроцессного места в очереди парсинга")
                    time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


@contextlib.contextmanager
def parse_slot(timeout: float | None = None):
    """Место в общей очереди парсинга.

    Сканер афиши обязан ходить через неё же, иначе фоновый обход и нажатая
    кнопка полезут в браузер одновременно и сложат сервер.

    Очередь двухуровневая: сначала семафор внутри процесса, затем flock —
    общий на все процессы на машине (обходчики ATP и WTA, оба бота).
    """
    got = _SEM.acquire(timeout=timeout) if timeout else _SEM.acquire()
    if not got:
        raise TimeoutError("не дождался места в очереди парсинга")
    try:
        with _cross_process_slot(timeout):
            yield
    finally:
        _SEM.release()
_FETCHER: Fetcher | None = None
_FETCHER_LOCK = threading.Lock()

TELEGRAM_LIMIT = 4000

# сколько последних матчей показывать по каждому игроку
try:
    SHOW_MATCHES = int(os.environ.get("TP_SHOW_MATCHES", 10))
except ValueError:
    SHOW_MATCHES = 10

# TP_TELEGRAPH=1 — отчёт уходит одной ссылкой на telegra.ph вместо простыни
# сообщений. Выключено по умолчанию: страницы там публичные.
USE_TELEGRAPH = os.environ.get("TP_TELEGRAPH", "") in ("1", "true", "yes")


def get_fetcher(cache_dir: str = ".cache/tennis", ttl: int = 6 * 3600) -> Fetcher:
    """Один Fetcher на процесс — общий кэш и общая сессия requests."""
    global _FETCHER
    with _FETCHER_LOCK:
        if _FETCHER is None:
            _FETCHER = Fetcher(cache_dir=cache_dir, ttl_seconds=ttl)
        return _FETCHER


def players_from_url(url: str) -> tuple[str, str] | None:
    """https://.../h2h-compare/jan-kumstat-vs-maxim-mrva.html -> ('Jan Kumstat', 'Maxim Mrva')"""
    m = re.search(r"/h2h-compare/([a-z0-9\-]+?)\.html", url, re.I)
    if not m:
        return None
    slug = m.group(1).lower()
    if "-vs-" not in slug:
        return None
    a, b = slug.split("-vs-", 1)
    cap = lambda s: " ".join(w.capitalize() for w in s.split("-") if w)  # noqa: E731
    p1, p2 = cap(a), cap(b)
    return (p1, p2) if p1 and p2 else None


def _split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет по границам блоков, не разрывая <pre>."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit and cur:
            chunks.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        chunks.append(cur)
    return chunks


_SURFACE_WORDS = [
    (re.compile(r"\b(grass|трава|травян)\w*", re.I), "grass"),
    (re.compile(r"\b(clay|грунт|земл)\w*", re.I), "clay"),
    (re.compile(r"\b(hard|хард|индор|indoor)\w*", re.I), "hard"),
    (re.compile(r"\b(carpet|ковёр|ковер)\w*", re.I), "carpet"),
]


def detect_surface(*sources: str | None) -> str | None:
    """Покрытие из любого текста: '(Hard)', 'Cincinnati, Hard', 'грунт'.

    Порядок источников задаёт приоритет — первый непустой выигрывает.
    Carpet на tennisratio отдельным фильтром не представлен, поэтому
    возвращаем его как есть, а фильтр просто не будет нажат.
    """
    for src in sources:
        if not src:
            continue
        for rx, name in _SURFACE_WORDS:
            if rx.search(src):
                return name
    return None


def diagnose() -> str:
    """Конкретика вместо «проверь Chromium»: что именно не так в окружении."""
    import shutil
    import sys

    lines = [f"Python: <code>{sys.executable}</code>"]
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        lines.append(f"❌ Playwright не установлен в этот интерпретатор: {exc}")
        lines.append(f"Ставить так:\n<code>{sys.executable} -m pip install playwright</code>")
        return "\n".join(lines)

    lines.append("✅ Playwright импортируется")
    try:
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        if path and shutil.which(path) or (path and __import__("os").path.exists(path)):
            lines.append("✅ Chromium на месте")
            lines.append("Значит, дело не в браузере: вёрстка сайта могла "
                         "поменяться либо страница не успела прогрузиться.")
        else:
            lines.append("❌ Chromium не скачан")
            lines.append(f"<code>{sys.executable} -m playwright install --with-deps chromium</code>")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ Chromium не запускается: {str(exc)[:200]}")
        lines.append(f"<code>{sys.executable} -m playwright install --with-deps chromium</code>")
    return "\n".join(lines)


def run_stats_parsing(
    send_notification,
    chat_id,
    url: str,
    p1: str | None = None,
    p2: str | None = None,
    *,
    tournament: str = "",
    surface: str | None = None,
    reply_to=None,
    mode: str = "auto",
    tour: str = "atp",
    best_of: int = 3,
    want_comparison: bool | None = None,
    want_simulation: bool | None = None,
    sim_runs: int | None = None,
) -> None:
    """Точка входа для кнопки «Статистика + симуляция».

    send_notification — функция из бота: (text, chat_id, reply_markup=None, reply_to=None).
    Ничего не возвращает: всё уходит сообщениями в чат.

    Симуляция считается из уже собранного отчёта, без новых запросов, поэтому
    добавляет к времени работы кнопки меньше секунды. Если она падает —
    статистика всё равно уходит в чат: парсинг важнее, и терять его из-за
    ошибки в счётчике нельзя.
    """
    def say(text, **kw):
        try:
            send_notification(text, chat_id, reply_to=reply_to, **kw)
        except TypeError:
            # на случай, если send_notification в боте ещё без reply_to
            send_notification(text, chat_id)

    if not (p1 and p2):
        pair = players_from_url(url)
        if not pair:
            say("❌ Не смог вытащить имена игроков из ссылки.")
            return
        p1, p2 = pair

    # покрытие: явный параметр -> название турнира -> название с сайта.
    # Нужно и для прогноза по Elo, и чтобы отфильтровать блоки сравнения.
    surface = surface or detect_surface(tournament) or guess_surface(tournament)
    if surface:
        log.info("покрытие определено: %s (из %r)", surface, (tournament or "")[:60])
    else:
        log.info("покрытие не определено — сравнение по всем покрытиям")
    if want_comparison is None:
        # TP_SKIP_COMPARISON=1 — быстрее примерно на треть
        want_comparison = os.environ.get("TP_SKIP_COMPARISON", "") not in ("1", "true", "yes")

    queued = not _SEM.acquire(blocking=False)
    if queued:
        say("⏳ Уже идёт другой парсинг, встал в очередь…")
        _SEM.acquire()

    started = time.monotonic()
    try:
        report = build_report(
            get_fetcher(), p1, p2,
            surface=surface,
            want_comparison=want_comparison,
            best_of=best_of,
            tour=tour,
            mode=mode,
            headless=True,
            as_of=date.today(),
            url=url,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Парсинг %s vs %s упал:\n%s", p1, p2, traceback.format_exc())
        say(f"❌ Парсинг не удался: <code>{type(exc).__name__}: {str(exc)[:300]}</code>")
        return
    finally:
        _SEM.release()

    try:
        text = format_telegram(report, show_matches=SHOW_MATCHES)
    except Exception as exc:  # noqa: BLE001
        log.exception("Форматирование отчёта упало")
        say(f"❌ Данные собраны, но не отформатировались: {exc}")
        return

    # Статистика уходит сразу после парсинга, симуляция — следом, своей
    # публикацией. Двумя статьями, а не одной: парсинг занимает 30-60 секунд,
    # и заставлять ждать ещё и счёт симуляции незачем.
    _deliver(say, "stats", text, report, None, p1, p2, tournament)

    log.info("отчёт %s vs %s готов за %.1f с", p1, p2, time.monotonic() - started)

    if want_simulation is None:
        want_simulation = os.environ.get("TP_SKIP_SIMULATION", "") not in ("1", "true", "yes")
    if want_simulation:
        sim, sim_text = _build_simulation_text(report, p1, p2, runs=sim_runs)
        if sim_text:
            _deliver(say, "sim", sim_text, report, sim, p1, p2, tournament)
        elif sim is None:
            say("ℹ️ Симуляцию не считал: нет ни показателей подачи/приёма, "
                "ни Elo-прогноза.")

    n1 = len(report["_matches"]["p1"])
    n2 = len(report["_matches"]["p2"])
    if n1 == 0 and n2 == 0:
        say("ℹ️ История матчей пришла пустой.\n" + diagnose())


def _build_simulation_text(report: dict, p1: str, p2: str,
                           runs: int | None = None) -> tuple[dict | None, str]:
    """Считает Монте-Карло и форматирует. Ошибка счётчика не должна уносить
    с собой статистику, поэтому всё в try и наружу отдаётся пустая строка."""
    if runs is None:
        try:
            runs = int(os.environ.get("TP_SIM_RUNS", DEFAULT_RUNS))
        except ValueError:
            runs = DEFAULT_RUNS
    runs = min(max(runs, 500), 200_000)

    t0 = time.monotonic()
    try:
        sim = build_simulation(report, runs=runs)
    except Exception:  # noqa: BLE001
        log.error("Симуляция %s vs %s упала:\n%s", p1, p2, traceback.format_exc())
        return None, ""
    if sim is None:
        return None, ""
    try:
        text = format_simulation_telegram(sim)
    except Exception:  # noqa: BLE001
        log.error("Форматирование симуляции упало:\n%s", traceback.format_exc())
        return sim, (f"⚠️ Симуляция посчиталась, но не отформатировалась. "
                     f"Итог: {sim['p1_name']} {sim['headline']['p1_win']:.1%} · "
                     f"{sim['p2_name']} {sim['headline']['p2_win']:.1%}")
    log.info("симуляция %s vs %s: %d прогонов за %.2f с",
             p1, p2, runs, time.monotonic() - t0)
    return sim, text


ARTICLE_TITLES = {
    "stats": "статистика",
    "sim": "симуляция",
}


def _deliver(say, kind: str, body: str, report: dict, sim: dict | None,
             p1: str, p2: str, tournament: str) -> None:
    """Одна часть отчёта: статьёй на telegra.ph либо обычными сообщениями.

    Откат здесь, а не у вызывающего: если публикация не удалась, содержимое
    всё равно должно дойти — терять готовый отчёт из-за недоступного сайта
    нельзя.
    """
    if USE_TELEGRAPH:
        url = _publish_report(kind, body, report, p1, p2, tournament)
        if url:
            say(_telegraph_teaser(kind, report, sim, url))
            log.info("%s %s vs %s опубликована: %s", ARTICLE_TITLES[kind], p1, p2, url)
            return
        say(f"ℹ️ Не вышло опубликовать «{ARTICLE_TITLES[kind]}» на telegra.ph, "
            "шлю сообщениями.")
    for chunk in _split_message(body):
        say(chunk)


def _publish_report(kind: str, body: str, report: dict,
                    p1: str, p2: str, tournament: str) -> str | None:
    h = report.get("h2h") or {}
    n1 = (h.get("player1") or {}).get("name") or p1
    n2 = (h.get("player2") or {}).get("name") or p2
    title = f"{n1} — {n2} · {ARTICLE_TITLES.get(kind, '')}".strip(" ·")
    if tournament:
        title += f" · {tournament}"
    src = h.get("source_url")
    if src and kind == "stats":
        body += f'\n\n<i>Источник: <a href="{src}">tennisratio</a></i>'
    try:
        return telegraph_publish(title, body, author="Tennis bot")
    except Exception:  # noqa: BLE001
        log.error("Публикация упала:\n%s", traceback.format_exc())
        return None


def _telegraph_teaser(kind: str, report: dict, sim: dict | None, url: str) -> str:
    """Короткая выжимка со ссылкой: главное видно сразу, детали — по ссылке."""
    h = report.get("h2h") or {}
    n1 = (h.get("player1") or {}).get("name") or "Игрок 1"
    n2 = (h.get("player2") or {}).get("name") or "Игрок 2"

    if kind == "sim" and sim:
        m = sim["headline"]
        lines = [f"🎲 <b>Симуляция</b> — {sim['runs']} прогонов, bo{sim['best_of']}",
                 f"{_e(n1)} <b>{m['p1_win']:.0%}</b> (кэф {_fair(m['p1_win'])}) · "
                 f"{_e(n2)} <b>{m['p2_win']:.0%}</b> (кэф {_fair(m['p2_win'])})"]
        st, el = sim["models"].get("stats"), sim["models"].get("elo")
        if st and el and abs(st["p1_win"] - el["p1_win"]) > 0.20:
            lines.append("⚠️ стата и Elo расходятся — смотрите разбор моделей")
        # линия тотала сетов у формата своя: в bo3 это 2.5, в bo5 — 4.5.
        # Считать её от числа побед было ошибкой: порог 1.5 проходили оба
        # исхода, и ТБ выходил 100%
        line = 2.5 if sim["best_of"] == 3 else 4.5
        over = sum(v for k, v in m["sets_played"].items() if k > line)
        lines.append(f"ТБ {line:g} сета: {over:.0%} (кэф {_fair(over)})")
        lines.append(f'\n📄 <a href="{url}">Полная симуляция</a>')
        return "\n".join(lines)

    lines = [f"🎾 <b>{_e(n1)}</b> vs <b>{_e(n2)}</b>"]
    fc = report.get("elo_forecast") or {}
    if fc.get("p1_win_prob") is not None:
        surf = fc.get("surface")
        tail = f", {surf}" if surf else ""
        lines.append(f"Elo: {fc['p1_win_prob']:.0%} / {fc['p2_win_prob']:.0%}"
                     f" (bo{fc.get('best_of', 3)}{tail})")
    edge = (report.get("fatigue") or {}).get("edge") or {}
    if edge.get("fresher") in ("p1", "p2"):
        who = n1 if edge["fresher"] == "p1" else n2
        lines.append(f"Свежее: {_e(who)} (Δ {abs(edge['delta_fatigue'])} п.)")
    lines.append(f'\n📄 <a href="{url}">Полная статистика</a>')
    return "\n".join(lines)


def _fair(p: float) -> str:
    return f"{1 / p:.2f}" if p > 1e-9 else "—"


def _e(x) -> str:
    import html as _h
    return _h.escape(str(x))
