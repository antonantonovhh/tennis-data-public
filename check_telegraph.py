#!/usr/bin/env python3
"""Почему отчёт пришёл сообщением, а не статьёй.

Проверяет всю цепочку по шагам и на каждом говорит, что делать. Ставится
рядом с ботом, запускается на сервере:

    python3 /opt/tennis_bot/check_telegraph.py           # только проверки
    python3 /opt/tennis_bot/check_telegraph.py --publish # ещё и тестовая статья

Важно: смотреть надо окружение ТОГО процесса, в котором крутится бот.
Переменная, выставленная в вашей сессии, до systemd-службы не доходит —
это самая частая причина.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK, BAD, WARN = "  ✅", "  ❌", "  ⚠️ "


def step(n, title):
    print(f"\n{n}. {title}")


def find_service() -> str | None:
    """Ищет systemd-службу, у которой в команде запуска есть наш каталог."""
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--all", "--no-legend",
             "--plain", "--no-pager"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        if not name.endswith(".service"):
            continue
        try:
            props = subprocess.run(
                ["systemctl", "show", name, "-p", "ExecStart", "--no-pager"],
                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        if HERE in props or "tennis" in name.lower():
            return name
    return None


def service_env(service: str) -> str:
    try:
        return subprocess.run(["systemctl", "show", service, "-p", "Environment",
                               "--no-pager"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publish", action="store_true",
                    help="опубликовать тестовую страницу (она останется висеть)")
    args = ap.parse_args()

    print("=" * 62)
    print("  Диагностика публикации в Telegraph")
    print("=" * 62)

    # ---------------------------------------------------------- 1
    step(1, "Переменная TP_TELEGRAPH в ЭТОЙ сессии")
    val = os.environ.get("TP_TELEGRAPH", "")
    if val in ("1", "true", "yes"):
        print(f"{OK} TP_TELEGRAPH={val!r}")
    elif val:
        print(f"{BAD} TP_TELEGRAPH={val!r} — принимаются только 1, true, yes")
    else:
        print(f"{WARN}не выставлена (в этой сессии)")
        print("     Само по себе не страшно — важно окружение процесса бота, шаг 2.")

    # ---------------------------------------------------------- 2
    step(2, "Окружение процесса бота")
    service = find_service()
    if not service:
        print(f"{WARN}systemd-службу не нашёл. Если бот запущен иначе (screen, tmux,")
        print("     nohup), проверьте вручную:")
        print("       tr '\\0' '\\n' < /proc/<PID_бота>/environ | grep TP_")
    else:
        print(f"     служба: {service}")
        env = service_env(service)
        if "TP_TELEGRAPH" in env:
            print(f"{OK} переменная в юните есть")
            print(f"     {env}")
        else:
            print(f"{BAD} TP_TELEGRAPH в юните НЕТ — вот причина")
            print("     Лечится так:")
            print(f"       systemctl edit {service}")
            print("     и в открывшийся файл:")
            print("       [Service]")
            print("       Environment=TP_TELEGRAPH=1")
            print("     затем:")
            print(f"       systemctl daemon-reload && systemctl restart {service}")

    # ---------------------------------------------------------- 3
    step(3, "Модуль и его настройки")
    try:
        from tennis_parser import integration as I
        from tennis_parser import telegraph as T
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} импорт не удался: {exc}")
        return 1
    print(f"{OK if I.USE_TELEGRAPH else BAD} USE_TELEGRAPH = {I.USE_TELEGRAPH}")
    if not I.USE_TELEGRAPH:
        print("     Значение читается ОДИН РАЗ при импорте. Выставить переменную")
        print("     после старта бота недостаточно — нужен перезапуск.")

    # ---------------------------------------------------------- 4
    step(4, "Токен telegra.ph")
    print(f"     файл: {T.TOKEN_FILE}")
    if os.environ.get("TP_TELEGRAPH_TOKEN"):
        print(f"{OK} берётся из TP_TELEGRAPH_TOKEN")
    elif os.path.exists(T.TOKEN_FILE):
        try:
            size = len(open(T.TOKEN_FILE, encoding="utf-8").read().strip())
            mode = oct(os.stat(T.TOKEN_FILE).st_mode & 0o777)
            print(f"{OK} есть ({size} символов, права {mode})")
        except OSError as exc:
            print(f"{BAD} не читается: {exc}")
    else:
        print(f"{WARN}нет — создастся при первой публикации")
        d = os.path.dirname(T.TOKEN_FILE)
        if not os.access(d, os.W_OK):
            print(f"{BAD} но каталог {d} недоступен на запись!")
            print("     Токен будет создаваться заново при каждом запуске.")
            print(f"     Лечится: chown -R <пользователь_бота> {d}")

    # ---------------------------------------------------------- 5
    step(5, "Доступность api.telegra.ph")
    # Мало того, что хост ответил: блокирующий прокси тоже отвечает, только
    # своей заглушкой. Поэтому смотрим тело — настоящий API отдаёт JSON с
    # ключом "ok" даже на заведомо неверный токен.
    import json
    import urllib.request
    reachable = False
    try:
        req = urllib.request.Request("https://api.telegra.ph/getAccountInfo?access_token=x",
                                     headers={"User-Agent": "tennis-parser"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
        data = json.loads(body)
        if "ok" in data:
            reachable = True
            print(f"{OK} настоящий API ответил (ok={data['ok']})")
        else:
            print(f"{BAD} ответ не похож на Telegraph: {body[:120]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} не достучаться: {type(exc).__name__}: {str(exc)[:100]}")
        print("     Похоже на блокировку или прокси-заглушку. Варианты: прокси")
        print("     для процесса бота либо оставить обычные сообщения.")

    # ---------------------------------------------------------- 6
    if args.publish:
        step(6, "Тестовая публикация")
        url = T.publish("Проверка связи",
                        "<p>Если вы это читаете, публикация работает.</p>"
                        "<pre>таблица  сохраняет  выравнивание</pre>")
        if url:
            print(f"{OK} {url}")
            print("     Откройте ссылку в Telegram — должна развернуться Instant View.")
        else:
            print(f"{BAD} не удалось, подробности в шагах выше")
    else:
        step(6, "Тестовая публикация")
        print("     пропущена, запустите с --publish")

    print("\n" + "=" * 62)
    if I.USE_TELEGRAPH and reachable:
        print("  Всё на месте. Если отчёт всё равно приходит сообщениями —")
        print("  бот не перезапускался после правки окружения.")
    elif not I.USE_TELEGRAPH:
        print("  Причина: TP_TELEGRAPH не видна процессу. См. шаги 2 и 3.")
    else:
        print("  Причина: api.telegra.ph недоступен. См. шаг 5.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
