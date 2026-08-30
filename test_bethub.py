# -*- coding: utf-8 -*-
"""Подпись запросов к BetHub — сверка с эталонным вектором из спецификации.

Схему подписи в присланных примерах не описали вовсе, и подобрать её по
ответам сервера нельзя: и неверный заголовок, и неверная формула, и полное
отсутствие подписи дают одинаковый `authentication_failed`. Поэтому эталон
из OpenAPI (`x-hmac-golden-vector`) — единственная надёжная проверка, и она
работает без единого сетевого запроса.

    python3 test_bethub.py
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bethub  # noqa: E402

# x-hmac-golden-vector из bethub-pick-input-api-v1.yaml
ЭТАЛОН = {
    "secret": "test-secret",
    "method": "POST",
    "path": "/api/v1/pinnacle-prematch/sports",
    "timestamp": "1787461200",
    "idempotency_key": "",
    "raw_body": b"{}",
    "body_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "signature": "29ad948741e7b4b1f148a1ad2b75e2f31bfb891b6d45f8af4d9b49d1c0106d57",
}


def main() -> int:
    fails = []

    def check(what, got, want):
        ok = got == want
        print(f"  {'OK  ' if ok else 'FAIL'} {what}: {got!r}")
        if not ok:
            fails.append(f"{what}: ожидалось {want!r}, получено {got!r}")

    e = ЭТАЛОН
    print("эталонный вектор из спецификации")
    check("sha256 тела", hashlib.sha256(e["raw_body"]).hexdigest(),
          e["body_sha256"])
    check("подпись", bethub.sign(e["secret"], e["method"], e["path"],
                                 e["timestamp"], e["idempotency_key"],
                                 e["raw_body"]),
          e["signature"])

    print("каноническая строка: пять частей, пустой ключ идемпотентности")
    canon = bethub.canonical(e["method"], e["path"], e["timestamp"], "",
                             e["raw_body"])
    check("частей", len(canon.split("\n")), 5)
    check("четвёртая часть пуста", canon.split("\n")[3], "")
    check("пятая — sha256, а не тело", canon.split("\n")[4], e["body_sha256"])

    print("ключ идемпотентности входит в подпись")
    with_key = bethub.sign(e["secret"], e["method"], e["path"], e["timestamp"],
                           "abc", e["raw_body"])
    check("подпись меняется", with_key != e["signature"], True)

    print("секрет берётся как ASCII, а не декодируется из base64")
    import base64
    sec = "AHI0zEfywlai-UQ4eT5XhwgTO4IG2oe4jtCI92tGVcU"
    dec = base64.urlsafe_b64decode(sec + "=" * (-len(sec) % 4))
    check("секрет и правда 32 байта в base64url", len(dec), 32)
    a = bethub.sign(sec, "POST", e["path"], e["timestamp"], "", e["raw_body"])
    b = bethub.sign(dec.decode("latin1"), "POST", e["path"], e["timestamp"],
                    "", e["raw_body"])
    check("варианты дают разные подписи (важно не перепутать)", a != b, True)

    print("маршрутизация наших рынков")
    check("Games Hcap идёт в событие (Games)",
          bethub.МАРШРУТ["Games Hcap"], ("games", "SP"))
    check("Sets Hcap — на основное событие",
          bethub.МАРШРУТ["Sets Hcap"], ("main", "SP"))
    check("Total Sets — тотал основного события",
          bethub.МАРШРУТ["Total Sets"], ("main", "TO"))
    check("Moneyline известен (публикацию отключаем не здесь)",
          bethub.МАРШРУТ["Moneyline"], ("main", "ML"))

    print()
    if fails:
        print(f"ПРОВАЛЕНО: {len(fails)}")
        for f in fails:
            print("   ", f)
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
