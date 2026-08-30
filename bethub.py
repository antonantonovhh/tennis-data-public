#!/usr/bin/env python3
"""Клиент BetHub Pick Input API v1 — публикация прогнозов на bet-hub.com.

Присланные в чат примеры из документации неполны: одного `X-Api-Key` хватает
только токену без усиленной защиты. У нашего токена включён HMAC, и без
подписи любой запрос отбивается с `authentication_failed`.

Схема подписи (спецификация OpenAPI 3.1, раздел x-hmac-golden-vector):

    X-Signature = lowercase hex HMAC-SHA256(secret, canonical)

    canonical = <МЕТОД> \\n <ПУТЬ> \\n <X-Timestamp> \\n <Idempotency-Key> \\n <sha256(тело)>

Грабли, каждая из которых стоила бы часа отладки:
  * подписывается не тело, а его sha256 в hex;
  * путь идёт с префиксом `/api` и без query;
  * секрет берётся как ASCII-строка, а не декодируется из base64 (хотя по
    виду это base64url от 32 байт);
  * `Idempotency-Key` участвует в строке всегда: нет ключа — пустая строка,
    но перевод строки на месте;
  * подписываются ТОЧНЫЕ байты тела, поэтому JSON сериализуется один раз и
    в сокет уходит ровно та же строка;
  * часы должны совпасть с сервером в пределах 10 секунд;
  * Cloudflare отбивает urllib по отпечатку клиента (error 1010) — только
    requests с браузерным User-Agent.

Проверено эталонным вектором из спецификации (см. test_bethub.py) и живым
вызовом /sports.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid

import requests

BASE = "https://new.bet-hub.com"
API_PREFIX = "/api"

# Тот же UA, что у остальных скрейперов проекта
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Коды рынков Pinnacle в терминах bet-hub, подсмотрены в ответе /event:
#   ML — победитель, SP — фора, TO — тотал, IT — индивидуальный тотал.
# Форы и тоталы по ГЕЙМАМ живут отдельным событием «Игрок (Games)», а по
# сетам — на основном. Это ключевая особенность разбора, см. МАРШРУТ ниже.
BET_ML, BET_SP, BET_TO, BET_IT = "ML", "SP", "TO", "IT"

# Куда какой наш рынок отправлять: (событие, bet_type)
#   'main'  — обычное событие «Игрок1 - Игрок2»
#   'games' — событие «Игрок1 (Games) - Игрок2 (Games)»
МАРШРУТ = {
    "Moneyline":  ("main", BET_ML),
    "Sets Hcap":  ("main", BET_SP),
    "Total Sets": ("main", BET_TO),
    "Games Hcap": ("games", BET_SP),
    "Total Games": ("games", BET_TO),
}


def canonical(method: str, path: str, ts: str, idem: str, body: bytes) -> str:
    """Каноническая строка ровно по спецификации: пять частей через \\n."""
    return "\n".join([method.upper(), path, ts, idem or "",
                      hashlib.sha256(body).hexdigest()])


def sign(secret: str, method: str, path: str, ts: str, idem: str,
         body: bytes) -> str:
    msg = canonical(method, path, ts, idem, body).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class BetHubError(RuntimeError):
    def __init__(self, code, payload):
        err = (payload or {}).get("error") or {}
        self.api_code = err.get("code") or f"http_{code}"
        self.details = err.get("details") or {}
        super().__init__(f"{self.api_code}: {err.get('message') or payload}")


class BetHub:
    """Тонкая обёртка: подпись, темп запросов, разбор конверта ответа."""

    def __init__(self, api_key: str = "", secret: str = "",
                 pace: float = 1.1, timeout: int = 40):
        self.api_key = api_key or os.environ.get("BETHUB_API_KEY", "")
        self.secret = secret or os.environ.get("BETHUB_API_SECRET", "")
        if not self.api_key:
            raise SystemExit("нет BETHUB_API_KEY")
        self.pace = pace
        self.timeout = timeout
        self._last = 0.0
        self.s = requests.Session()

    def call(self, endpoint: str, body=None, idem: str = ""):
        """endpoint — путь после /api, например '/v1/pinnacle-prematch/sports'.

        Возвращает содержимое `data`. Ошибку поднимает исключением, чтобы
        вызывающий код не проверял success на каждом шаге.
        """
        gap = self.pace - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

        path = API_PREFIX + endpoint
        raw = json.dumps(body if body is not None else {},
                         separators=(",", ":"), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json",
                   "X-Api-Key": self.api_key, "User-Agent": UA}
        if idem:
            headers["Idempotency-Key"] = idem
        if self.secret:
            ts = str(int(time.time()))
            headers["X-Timestamp"] = ts
            headers["X-Signature"] = sign(self.secret, "POST", path, ts,
                                          idem, raw)
        r = self.s.post(BASE + path, data=raw, headers=headers,
                        timeout=self.timeout)
        try:
            payload = r.json()
        except ValueError:
            raise BetHubError(r.status_code, {"error": {"message": r.text[:300]}})
        if not payload.get("success"):
            raise BetHubError(r.status_code, payload)
        return payload.get("data")

    # ------------------------------------------------------------ навигация
    def sports(self):
        return self.call("/v1/pinnacle-prematch/sports")

    def leagues(self, sport_code="T", hours=48, search=""):
        return self.call("/v1/pinnacle-prematch/leagues",
                         {"sport_code": sport_code, "search": search,
                          "hours": hours})

    def events(self, sport_code="T", country="", league_name="", search="",
               hours=48):
        body = {"sport_code": sport_code, "search": search, "hours": hours}
        if country:
            body["country"] = country
        if league_name:
            body["league_name"] = league_name
        return self.call("/v1/pinnacle-prematch/events", body)

    def event(self, event_id, odds=True, period_id=None, paid_overage=False):
        """Событие с линией. Тратит квоту провайдера — зря не дёргать."""
        return self.call("/v1/pinnacle-prematch/event",
                         {"event_id": event_id, "odds": odds,
                          "period_id": period_id,
                          "billing": {"allow_paid_overage": paid_overage}})

    # ---------------------------------------------------------- публикация
    def publish(self, *, event_id, line_id, bet_type, outcome, title, label,
                participant, sub_id, stake, sales_type="free",
                odds_policy="better_or_equal", comment="",
                export_telegram=False, paid_overage=False, idem=""):
        """Публикует один прогноз с полной верификацией линии.

        `Idempotency-Key` обязателен по-хорошему: повтор в течение 15 минут
        не создаёт вторую публикацию и не тратит биллинг заново. Без него
        сетевой таймаут с последующим ретраем опубликовал бы прогноз дважды.
        """
        body = {
            "event_id": event_id, "bet_type": bet_type, "outcome": outcome,
            "title": title, "label": label, "participant": participant or "",
            "sub_id": sub_id,
            "publication": {"stake": stake, "sales_type": sales_type,
                            "odds_policy": odds_policy, "comment": comment,
                            "export_telegram": export_telegram},
            "billing": {"allow_paid_overage": paid_overage},
        }
        if line_id is not None:
            body["line_id"] = line_id
        return self.call("/v1/pinnacle-prematch/publish", body,
                         idem=idem or new_idem())


def new_idem() -> str:
    return uuid.uuid4().hex


def sections(event: dict):
    """Секции линии события: [(bet_type, period, title, line_id, ряды)]."""
    odds = (event or {}).get("odds") or {}
    if not isinstance(odds, dict):
        return []
    return [(s["betType"], s["period"], s["title"], s["lineId"], s["odds"])
            for s in odds.get("oddsSections") or []]
