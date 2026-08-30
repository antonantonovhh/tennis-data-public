"""HTTP-слой: сессия с ретраями, кэшем на диске и вежливыми паузами."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DEFAULT_CACHE = Path(".cache/tennis")

# Сколько дней держать файлы кэша. Самый долгий TTL здесь — 6 часов у HTML
# и JSON, у рендера вообще 30 минут, так что трое суток это двенадцатикратный
# запас: удалить то, что кому-то ещё нужно, невозможно.
CACHE_KEEP_DAYS = int(os.environ.get("TP_CACHE_KEEP_DAYS", "3"))


def prune_cache(cache_dir: Path | str = DEFAULT_CACHE,
                days: int | None = None) -> tuple[int, int]:
    """Удаляет из кэша файлы старше `days` дней. Возвращает (файлов, байт).

    Кэш складывается по хешу URL и НИКОГДА сам не убирается: TTL проверяется
    только при чтении, а протухший файл остаётся лежать вечно. Каждый день
    афиши — это новые адреса, то есть новые файлы, и каталог растёт линейно.
    Замер 23.08.2026: 340 файлов и 286 МБ за три дня, порядка 100-170 МБ в
    сутки, из них две трети — снимки страниц после рендера Playwright.

    Обходим рекурсивно: рядом с .cache/tennis лежит .cache/tennis/render,
    и именно он занимает больше всего.
    """
    root = Path(cache_dir)
    if not root.exists():
        return 0, 0
    days = CACHE_KEEP_DAYS if days is None else days
    cutoff = time.time() - days * 86400
    files = bytes_freed = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
            if stat.st_mtime >= cutoff:
                continue
            size = stat.st_size
            p.unlink()
        except OSError as exc:      # файл занят или уже исчез — не беда
            log.debug("кэш: %s не удалён (%s)", p, exc)
            continue
        files += 1
        bytes_freed += size
    if files:
        log.info("кэш почищен: удалено %d файлов старше %d дн., освобождено %.0f МБ",
                 files, days, bytes_freed / 1048576)
    return files, bytes_freed


class Fetcher:
    """Обёртка над requests.Session.

    Кэш обязателен по-хорошему: Elo-таблицы обновляются раз в неделю,
    нет смысла дёргать сайт на каждый запуск.
    """

    def __init__(
        self,
        cache_dir: Path | str | None = DEFAULT_CACHE,
        ttl_seconds: int = 6 * 3600,
        min_delay: float = 1.0,
        max_delay: float = 2.5,
        timeout: int = 30,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self._last_request = 0.0

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -------------------------------------------------- internals
    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{key}.html"

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        wait = random.uniform(self.min_delay, self.max_delay) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    # -------------------------------------------------- public
    def get(self, url: str, *, force: bool = False, params: dict | None = None) -> str:
        cp = self._cache_path(url if not params else f"{url}?{sorted(params.items())}")
        if cp and cp.exists() and not force:
            age = time.time() - cp.stat().st_mtime
            if age < self.ttl:
                log.debug("cache hit (%.0f s old): %s", age, url)
                return cp.read_text(encoding="utf-8")

        self._throttle()
        log.info("GET %s", url)
        resp = self.session.get(url, timeout=self.timeout, params=params)
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        html = resp.text
        if cp:
            cp.write_text(html, encoding="utf-8")
        return html

    def get_json(
        self, url: str, *, params: dict | None = None, headers: dict | None = None,
        ttl: int | None = None,
    ) -> dict | list:
        """JSON с тем же файловым кэшем, что и HTML: API отдаёт статистику
        за 52 недели, дёргать его на каждый клик незачем."""
        cache_key = f"{url}?{sorted((params or {}).items())}"
        cp = self._cache_path(cache_key)
        if cp:
            cp = cp.with_suffix(".json")
            if cp.exists():
                age = time.time() - cp.stat().st_mtime
                if age < (self.ttl if ttl is None else ttl):
                    log.debug("cache hit json (%.0f s): %s", age, url)
                    return json.loads(cp.read_text(encoding="utf-8"))

        self._throttle()
        log.info("GET(json) %s %s", url, params or "")
        hdrs = {"X-Requested-With": "XMLHttpRequest"}
        hdrs.update(headers or {})
        resp = self.session.get(url, timeout=self.timeout, params=params, headers=hdrs)
        resp.raise_for_status()
        data = resp.json()
        if cp:
            try:
                cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return data
