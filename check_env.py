#!/usr/bin/env python3
"""Что видят службы: переменные из юнита, из .env и итог.

    ./venv/bin/python3 check_env.py

Показывает, откуда берётся каждая настройка и чего не хватает. Полезно
после переноса переменных из юнита в .env — самая частая поломка там в том,
что строку EnvironmentFile добавить забыли.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")
SERVICES = ("tennis-bot", "tennisratioall")
NEEDED = ("TELEGRAM_TOKEN", "CHAT_ID")
OPTIONAL = ("TRA_BOT_TOKEN", "TRA_CHAT_ID", "TRA_MODE", "TP_TELEGRAPH",
            "PIN_PROXY", "PIN_MIN_INTERVAL", "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY")


def read_env_file(path):
    got = {}
    if not os.path.exists(path):
        return None
    for raw in open(path, encoding="utf-8"):
        line = raw.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        got[k.strip()] = v.strip().strip("\"'")
    return got


def unit_info(name):
    def show(prop):
        try:
            return subprocess.run(["systemctl", "show", name, "-p", prop,
                                   "--value"], capture_output=True, text=True,
                                  timeout=8).stdout.strip()
        except Exception:
            return ""
    return {"active": show("ActiveState"), "envfile": show("EnvironmentFiles"),
            "env": show("Environment")}


def main() -> int:
    print("=" * 60)
    print("  Откуда службы берут настройки")
    print("=" * 60)

    filevars = read_env_file(ENV)
    print(f"\n.env: {ENV}")
    if filevars is None:
        print("  ❌ файла нет")
    else:
        mode = oct(os.stat(ENV).st_mode & 0o777)
        print(f"  ✅ есть, {len(filevars)} переменных, права {mode}")
        if mode not in ("0o600", "0o400"):
            print("  ⚠️ в файле токены — стоит закрыть: chmod 600 .env")
        bad = [k for k in filevars if not k.replace("_", "").isalnum()]
        if bad:
            print(f"  ⚠️ странные имена переменных: {bad[:3]}")

    for svc in SERVICES:
        info = unit_info(svc)
        print(f"\nСлужба {svc}: {info['active'] or 'не найдена'}")
        if not info["active"]:
            continue
        if info["envfile"]:
            print(f"  ✅ EnvironmentFile: {info['envfile']}")
        else:
            print("  ❌ EnvironmentFile НЕ задан")
            if filevars:
                print("     Настройки лежат в .env, но служба их не читает.")
                print("     systemctl edit --full " + svc)
                print("     в [Service] добавить:")
                print(f"       EnvironmentFile={ENV}")
                print("     затем: systemctl daemon-reload && "
                      f"systemctl restart {svc}")
        inline = [p.split("=")[0] for p in (info["env"] or "").split() if "=" in p]
        if inline:
            print(f"  Environment= в юните: {', '.join(inline[:6])}"
                  + (" …" if len(inline) > 6 else ""))
            dup = sorted(set(inline) & set(filevars or {}))
            if dup:
                print(f"  ⚠️ задано И в юните, И в .env: {', '.join(dup[:5])}")
                print("     Побеждает то, что ниже по юниту. Уберите дубли,")
                print("     иначе будете править .env без всякого эффекта.")

    print("\nИтоговые значения (окружение важнее файла):")
    merged = dict(filevars or {})
    merged.update({k: v for k, v in os.environ.items() if k in NEEDED + OPTIONAL})
    for key in NEEDED:
        val = merged.get(key)
        print(f"  {'✅' if val else '❌'} {key:<20} "
              f"{'задан' if val else 'НЕ ЗАДАН — бот не запустится'}")
    for key in OPTIONAL:
        if merged.get(key):
            shown = merged[key] if len(merged[key]) < 14 else merged[key][:10] + "…"
            print(f"  ·  {key:<20} {shown}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
