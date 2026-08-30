#!/usr/bin/env python3
"""Почему tennisratioall молчит.

Проходит цепочку от службы до отправки сообщения и на каждом шаге говорит,
что делать.

    python3 /opt/tennis_bot/check_tra.py            # проверки
    python3 /opt/tennis_bot/check_tra.py --send     # ещё и тестовое сообщение
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK, BAD, WARN, INFO = "  ✅", "  ❌", "  ⚠️ ", "     "
SERVICE = os.environ.get("TRA_SERVICE", "tennisratioall")


def sh(*args, timeout=10) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def step(n, title):
    print(f"\n{n}. {title}")


def api(token: str, method: str, payload: dict | None = None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true", help="отправить тестовое сообщение")
    ap.add_argument("--service", default=SERVICE)
    args = ap.parse_args()

    print("=" * 64)
    print("  Почему tennisratioall молчит")
    print("=" * 64)

    # ------------------------------------------------------------- 1
    step(1, f"Служба {args.service}")
    active = sh("systemctl", "is-active", args.service)
    if active == "active":
        print(f"{OK} работает")
        since = sh("systemctl", "show", args.service, "-p",
                   "ActiveEnterTimestamp", "--value")
        if since:
            print(f"{INFO}с {since}")
        n_restart = sh("systemctl", "show", args.service, "-p", "NRestarts", "--value")
        if n_restart and n_restart not in ("0", ""):
            print(f"{WARN}перезапусков: {n_restart} — значит падает и поднимается")
    else:
        print(f"{BAD} состояние: {active or 'не найдена'}")
        print(f"{INFO}journalctl -u {args.service} -n 40 --no-pager")

    log = sh("journalctl", "-u", args.service, "-n", "30", "--no-pager", timeout=15)
    if log:
        bad = [l for l in log.splitlines()
               if any(k in l for k in ("Traceback", "Error", "ERROR", "не импортируется",
                                       "Не задан", "упал"))]
        if bad:
            print(f"{WARN}в логе есть ошибки, последние:")
            for l in bad[-3:]:
                print(f"{INFO}{l[-160:]}")

    # ------------------------------------------------------------- 2
    step(2, "Переменные, которые видит служба")
    env = sh("systemctl", "show", args.service, "-p", "Environment", "--value")
    envfile = sh("systemctl", "show", args.service, "-p", "EnvironmentFiles", "--value")
    seen = {}
    for part in env.split():
        if "=" in part:
            k, _, v = part.partition("=")
            seen[k] = v
    if envfile:
        print(f"{INFO}EnvironmentFile: {envfile}")
        path = envfile.split()[0].lstrip("-")
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.removeprefix("export ").partition("=")
                    seen.setdefault(k.strip(), v.strip().strip("\"'"))
        else:
            print(f"{BAD} файл {path} не существует!")

    for key, need in (("TELEGRAM_TOKEN", True), ("TRA_BOT_TOKEN", True),
                      ("TRA_CHAT_ID", False), ("CHAT_ID", False),
                      ("TRA_MODE", False)):
        val = seen.get(key, "")
        if val:
            shown = val if key.endswith(("MODE", "CHAT_ID")) else val[:12] + "…"
            print(f"{OK} {key} = {shown}")
        elif need:
            print(f"{BAD} {key} не задана")
        else:
            print(f"{WARN}{key} не задана")

    main_tok = seen.get("TELEGRAM_TOKEN", "")
    tra_tok = seen.get("TRA_BOT_TOKEN", "")
    if tra_tok and tra_tok == main_tok:
        print(f"{BAD} TRA_BOT_TOKEN совпадает с TELEGRAM_TOKEN — это один и тот же")
        print(f"{INFO}бот, кнопки будут срабатывать через раз")

    # ------------------------------------------------------------- 3
    step(3, "Токен рабочий?")
    token = tra_tok or os.environ.get("TRA_BOT_TOKEN", "") or main_tok
    if not token:
        print(f"{BAD} токена нет, дальше проверять нечего")
        return 1
    me = api(token, "getMe")
    if me.get("ok"):
        u = me["result"]
        print(f"{OK} бот @{u.get('username')} ({u.get('first_name')})")
    else:
        print(f"{BAD} Telegram отказал: {me.get('description')}")
        print(f"{INFO}токен неверный или отозван")
        return 1

    # ------------------------------------------------------------- 4
    step(4, "Чат доступен?")
    chat = seen.get("TRA_CHAT_ID") or seen.get("CHAT_ID") or ""
    if not chat:
        print(f"{BAD} chat_id не задан — писать некуда")
        upd = api(token, "getUpdates", {"offset": -1, "timeout": 0})
        if upd.get("ok") and upd.get("result"):
            got = upd["result"][-1]
            cid = (((got.get("message") or {}).get("chat")) or {}).get("id")
            if cid:
                print(f"{INFO}похоже, ваш chat_id = {cid}")
        else:
            print(f"{INFO}нажмите Start у бота и напишите ему что-нибудь,")
            print(f"{INFO}затем запустите проверку ещё раз")
    else:
        r = api(token, "getChat", {"chat_id": chat})
        if r.get("ok"):
            print(f"{OK} чат {chat} доступен")
        else:
            print(f"{BAD} чат {chat}: {r.get('description')}")
            print(f"{INFO}частая причина — вы не нажали Start у НОВОГО бота:")
            print(f"{INFO}Telegram запрещает боту писать первым")

    # ------------------------------------------------------------- 5
    step(5, "Что уже посчитано")
    try:
        from tennisratioall.store import STATE_FILE, Store  # noqa: PLC0415
        st = Store()
        c = st.counts()
        print(f"{INFO}{STATE_FILE}")
        print(f"{OK if sum(c.values()) else WARN} готово {c['done']}  "
              f"неудач {c['failed']}  в очереди {c['pending']}")
        if c["pending"] and not c["done"]:
            print(f"{INFO}матчи в очереди, но ни один не досчитан — либо круг")
            print(f"{INFO}ещё идёт (это десятки минут), либо все падают:")
            print(f"{INFO}python3 tennisratioall_run.py --status")
    except Exception as exc:  # noqa: BLE001
        print(f"{WARN}состояние не прочиталось: {exc}")

    # ------------------------------------------------------------- 6
    step(6, "Режим вывода")
    mode = seen.get("TRA_MODE", "digest")
    print(f"{INFO}TRA_MODE = {mode}")
    if mode == "digest":
        print(f"{WARN}в этом режиме сообщение приходит ТОЛЬКО в конце круга.")
        print(f"{INFO}Обход 17 матчей — это минут 15-20 тишины, и это нормально.")
        print(f"{INFO}Хотите видеть каждый матч — поставьте TRA_MODE=each")
    elif mode == "silent":
        print(f"{BAD} режим silent: бот и не должен ничего писать")

    # ------------------------------------------------------------- 7
    if args.send:
        step(7, "Тестовое сообщение")
        if not chat:
            print(f"{BAD} некуда слать")
        else:
            r = api(token, "sendMessage",
                    {"chat_id": chat, "text": "✅ Проверка связи tennisratioall"})
            print(f"{OK} доставлено" if r.get("ok")
                  else f"{BAD} {r.get('description')}")
    else:
        step(7, "Тестовое сообщение")
        print(f"{INFO}пропущено, запустите с --send")

    print("\n" + "=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
