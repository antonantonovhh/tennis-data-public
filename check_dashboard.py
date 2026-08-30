#!/usr/bin/env python3
"""Почему панель не открывается снаружи.

    ./venv/bin/python3 check_dashboard.py

Проходит цепочку: процесс -> адрес прослушивания -> локальный ответ ->
файрвол. ERR_CONNECTION_REFUSED почти всегда значит, что до файрвола
дело даже не дошло — просто некому отвечать.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
for line in (open(os.path.join(HERE, ".env"), encoding="utf-8")
             if os.path.exists(os.path.join(HERE, ".env")) else []):
    line = line.strip().removeprefix("export ")
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

PORT = os.environ.get("DASH_PORT", "8800")
HOST = os.environ.get("DASH_HOST", "0.0.0.0")
TOKEN = os.environ.get("DASH_TOKEN", "")
OK, BAD, WARN, INFO = "  ✅", "  ❌", "  ⚠️ ", "     "


def sh(*a, timeout=8):
    try:
        return subprocess.run(a, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def main() -> int:
    print("=" * 60)
    print(f"  Панель на порту {PORT}")
    print("=" * 60)

    print("\n1. Кто слушает порт")
    listen = [l for l in sh("ss", "-tlnp").splitlines() if f":{PORT} " in l]
    if listen:
        for l in listen:
            print(f"{OK} {l.strip()[:100]}")
        only_local = all("127.0.0.1" in l for l in listen)
        if only_local:
            print(f"{WARN}слушает ТОЛЬКО 127.0.0.1 — снаружи не откроется.")
            print(f"{INFO}Либо DASH_HOST=0.0.0.0 в .env и перезапуск,")
            print(f"{INFO}либо ходите через туннель:")
            print(f"{INFO}  ssh -L {PORT}:127.0.0.1:{PORT} root@<ip>")
    else:
        print(f"{BAD} никто не слушает {PORT}")
        print(f"{INFO}Панель не запущена. Если запускали руками из терминала —")
        print(f"{INFO}она умерла вместе с сессией (SIGHUP при закрытии SSH).")
        print(f"{INFO}Ставьте службой, инструкция в TENNISRATIOALL.md")

    print("\n2. Служба")
    state = sh("systemctl", "is-active", "tra-dashboard").strip()
    if state:
        print(f"{OK if state == 'active' else BAD} tra-dashboard: {state}")
        if state != "active":
            print(f"{INFO}journalctl -u tra-dashboard -n 30 --no-pager")
    else:
        print(f"{WARN}службы tra-dashboard нет — панель запускается только руками")

    print("\n3. Ответ изнутри сервера")
    if TOKEN:
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/?token={TOKEN}", timeout=5)
            print(f"{OK} HTTP {r.status}, страница отдаётся")
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} {type(exc).__name__}: {str(exc)[:70]}")
    else:
        print(f"{WARN}DASH_TOKEN не задан в .env — токен генерируется заново")
        print(f"{INFO}при каждом запуске, и ссылка каждый раз новая.")
        print(f"{INFO}Задайте свой: DASH_TOKEN=<длинная строка>")

    print("\n4. Файрвол")
    ufw = sh("ufw", "status")
    if "Status: active" in ufw:
        if PORT in ufw:
            print(f"{OK} ufw активен, порт {PORT} открыт")
        else:
            print(f"{BAD} ufw активен, а порта {PORT} в правилах нет")
            print(f"{INFO}  ufw allow {PORT}/tcp")
    elif ufw:
        print(f"{OK} ufw выключен")
    else:
        print(f"{INFO}ufw не установлен — проверьте iptables/nftables")

    print("\n5. Внешний доступ")
    print(f"{INFO}Если пункты 1-4 в порядке, а снаружи всё равно отказ —")
    print(f"{INFO}порт закрыт файрволом хостера (панель управления VPS,")
    print(f"{INFO}security group). Порты кроме 22/80/443 там часто закрыты.")
    print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
