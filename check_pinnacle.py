#!/usr/bin/env python3
"""Проверка доступа к Pinnacle: прямой и через прокси.

    ./venv/bin/python3 check_pinnacle.py
    PIN_PROXY=http://user:pass@host:port ./venv/bin/python3 check_pinnacle.py

Показывает внешний IP, состояние отступа и отвечает ли API. Если прямой
доступ забанен, а через прокси работает — значит проблема в IP сервера,
и прокси надо прописать в .env.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

for line in (open(os.path.join(HERE, ".env"), encoding="utf-8")
             if os.path.exists(os.path.join(HERE, ".env")) else []):
    line = line.strip().removeprefix("export ")
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

import requests  # noqa: E402

from tennis_parser import pinnacle_guard as pg  # noqa: E402

API = "https://guest.api.arcadia.pinnacle.com/0.1/sports/33/matchups?all=false"
BULK_URLS = [
    ("вся линия", "https://guest.api.arcadia.pinnacle.com/0.1/sports/33/"
                  "markets/straight?primaryOnly=false"),
    ("вся линия (без параметра)",
     "https://guest.api.arcadia.pinnacle.com/0.1/sports/33/markets/straight"),
    ("витрина", "https://guest.api.arcadia.pinnacle.com/0.1/sports/33/markets/"
                "highlighted/straight?primaryOnly=false"),
]


def headers():
    """Ровно те заголовки, что шлёт бот.

    Раньше проверка использовала свой урезанный набор и могла показать успех
    там, где бот получал 401 — то есть врать в самую неподходящую сторону.
    """
    # bot_merged на импорте требует TELEGRAM_TOKEN, а для проверки доступа
    # он ни к чему — выдёргиваем только нужную функцию
    src = open(os.path.join(HERE, "bot_merged.py"), encoding="utf-8").read()
    ns = {"os": os, "__file__": os.path.join(HERE, "bot_merged.py")}
    exec(src[src.index("def _device_uuid"):src.index("PIN_SPORT_TENNIS")], ns)  # noqa: S102
    _device_uuid = ns["_device_uuid"]
    # Только из окружения. Скрипт диагностический: если ключа нет, он обязан
    # это показать, а не подставить свой и отрапортовать «всё работает».
    key = os.environ.get("PIN_API_KEY", "")
    return {
        "User-Agent": os.environ.get("PIN_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-Key": key,
        "X-Device-UUID": os.environ.get("PIN_DEVICE_UUID", _device_uuid()),
        "Referer": "https://www.pinnacle.com/",
        "sec-ch-ua": ('"Not=A?Brand";v="99", "Microsoft Edge";v="151", '
                      '"Chromium";v="151"'),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


HEAD = None  # заполняется в main(), чтобы импорт бота случился после .env


def ext_ip(proxies):
    try:
        r = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
        return r.text.strip()
    except Exception as exc:  # noqa: BLE001
        return f"не определить ({type(exc).__name__})"


def probe(label, proxies):
    print(f"\n{label}")
    print(f"  внешний IP: {ext_ip(proxies)}")
    try:
        r = requests.get(API, headers=HEAD, proxies=proxies, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ соединение не удалось: {type(exc).__name__}: {str(exc)[:90]}")
        return False
    print(f"  HTTP {r.status_code}  (список матчапов)")
    if r.status_code == 200:
        try:
            data = r.json()
            n = len(data if isinstance(data, list)
                    else data.get("matchups", data.get("data", [])))
            print(f"  ✅ API отвечает, матчей в линии: {n}")
            # пакетный эндпоинт — на нём держится вся экономия запросов
            best = 0
            for label, url in BULK_URLS:
                try:
                    rb = requests.get(url, headers=HEAD, proxies=proxies, timeout=20)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⚠️ пакет «{label}»: {type(exc).__name__}")
                    continue
                if rb.status_code != 200:
                    print(f"  ⚠️ пакет «{label}»: HTTP {rb.status_code}")
                    continue
                try:
                    db = rb.json()
                except Exception:  # noqa: BLE001
                    print(f"  ⚠️ пакет «{label}»: ответ не JSON")
                    continue
                mk = db if isinstance(db, list) else db.get("markets", [])
                ids = {str(m.get("matchupId") or m.get("matchup_id"))
                       for m in mk if isinstance(m, dict)}
                ids.discard("None")
                best = max(best, len(ids))
                cover = len(ids) / n * 100 if n else 0
                print(f"  ✅ пакет «{label}»: {len(mk)} рынков по {len(ids)} "
                      f"матчам ({cover:.0f}% линии)")
            if best < 20:
                print("  ⚠️ пакет накрывает мало матчей — остальные пойдут "
                      "поштучно, держите PIN_MIN_INTERVAL повыше")
            return True
        except Exception:  # noqa: BLE001
            print(f"  ⚠️ ответ не JSON: {r.text[:80]}")
            return False
    if r.status_code == 401:
        print("  ❌ 401 — не принят ключ API. Это НЕ бан по IP:")
        print("     с любого адреса будет тот же ответ. Нужно обновить")
        print("     PIN_API_KEY, инструкция ниже.")
        return "auth"
    if r.status_code == 403:
        print("  ❌ 403 — доступ запрещён этому адресу (похоже на бан по IP)")
    elif r.status_code == 429:
        print("  ❌ 429 — превышена частота запросов")
    else:
        print(f"  ❌ неожиданный ответ: {r.text[:100]}")
    return False


def normalize(raw: str) -> str:
    """Приводит запись прокси к виду, понятному requests.

    Продавцы обычно отдают строку «IP:PORT:LOGIN:PASSWORD» — в таком виде
    requests её не примет, нужен URL со схемой.
    """
    raw = raw.strip()
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return raw


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Проверка доступа к Pinnacle напрямую и через прокси.")
    ap.add_argument("--proxy", help="проверить конкретный прокси, не трогая .env. "
                                    "Принимает и URL, и формат IP:PORT:LOGIN:PASS")
    ap.add_argument("--reset", action="store_true",
                    help="сбросить накопленный отступ после блокировок")
    args = ap.parse_args()
    if args.reset:
        from tennis_parser import pinnacle_guard as _g
        st = _g._read(_g.STATE)
        was = st.get("block_streak", 0)
        st.update(block_streak=0, cooldown_until=0)
        _g._write(_g.STATE, st)
        print(f"Отступ сброшен (было блокировок подряд: {was}).")
        return 0
    if args.proxy:
        os.environ["PIN_PROXY"] = normalize(args.proxy)

    global HEAD
    try:
        HEAD = headers()
    except Exception as exc:  # noqa: BLE001
        print(f"Не собрать заголовки: {exc}")
        return 1

    print("=" * 58)
    print("  Доступ к Pinnacle")
    print("=" * 58)
    print(f"  ключ: ...{HEAD['X-API-Key'][-8:]}  ·  "
          f"device: {HEAD['X-Device-UUID']}")

    st = pg.status()
    print(f"\nОтступ: {'нет' if not st['cooldown_left'] else str(st['cooldown_left'] // 60) + ' мин'}"
          f"  ·  блокировок подряд: {st['block_streak']}")
    if st.get("cooldown_left"):
        print(f"  поставил: {st.get('by_proc') or '?'} "
              f"(pid {st.get('by_pid') or '?'}, версия правил "
              f"{st.get('by_version') or 'до 3'})")
        if st.get("stale_writer"):
            print("  ОТСТУП ПОСТАВЛЕН ПРОЦЕССОМ НА СТАРОМ КОДЕ.")
            print("     systemctl start не трогает уже работающую службу,")
            print("     нужен restart:")
            print("       systemctl restart tennis-bot tennisratioall")
    print(f"Кэш матчапов: {st['matchups']} шт"
          + (f", возраст {st['cache_age']} с" if st["cache_age"] is not None else ""))
    print(f"Настройка: {pg.proxy_note()}")

    direct = probe("НАПРЯМУЮ", None)

    proxies = pg.proxies()
    if proxies:
        via = probe("ЧЕРЕЗ PIN_PROXY", proxies)
    else:
        via = None
        print("\nЧЕРЕЗ ПРОКСИ\n  PIN_PROXY не задан — пропускаю")
        print("  Проверить покупку, ничего не меняя в .env:")
        print("    ./venv/bin/python3 check_pinnacle.py --proxy IP:PORT:LOGIN:PASS")

    print("\n" + "=" * 58)
    if direct == "auth" or via == "auth":
        print("  Дело НЕ в IP: оба адреса получают 401, то есть API не принял")
        print("  ключ. Прокси тут ничего не решит.")
        print()
        print("  Как обновить ключ:")
        print("   1. Откройте https://www.pinnacle.com в Chrome")
        print("   2. F12 -> Network -> фильтр Fetch/XHR, обновите страницу")
        print("   3. Кликните любой запрос к guest.api.arcadia.pinnacle.com")
        print("   4. В Request Headers найдите X-API-Key, скопируйте значение")
        print("   5. Впишите в /opt/tennis_bot/.env:")
        print("        PIN_API_KEY=<значение>")
        print("   6. systemctl restart tennis-bot tennisratioall")
        print()
        print("  Отступ после ложных «блокировок» стоит сбросить:")
        print("    ./venv/bin/python3 check_pinnacle.py --reset")
    elif direct:
        print("  Прямой доступ работает. Прокси не нужен.")
    elif via:
        print("  Прямой забанен, прокси работает.")
        print("  Пропишите в /opt/tennis_bot/.env:")
        print(f"    PIN_PROXY={pg.proxy_note().replace('через прокси ', '')}")
        print("  (с настоящим паролем вместо ***), затем:")
        print("    systemctl restart tennis-bot tennisratioall")
    elif proxies:
        print("  Не работает ни прямо, ни через прокси.")
        print("  Проверьте сам прокси: curl -x $PIN_PROXY https://api.ipify.org")
    else:
        print("  Прямой доступ забанен. Варианты:")
        print("   1. Подождать — баны Pinnacle обычно временные.")
        print("   2. Прописать PIN_PROXY в .env.")
        print("   3. Поднять PIN_MIN_INTERVAL, чтобы не поймать снова.")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
