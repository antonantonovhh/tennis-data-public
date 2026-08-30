"""Телеграм для tennisratioall: отправка, ответы, кнопки.

Почему свой слой, а не send_notification из основного бота
-----------------------------------------------------------
Нужен message_id отправленного сообщения: без него нельзя понять, на какой
матч человек ответил. send_notification ответ Telegram выбрасывает.

Про токен
---------
**Двум процессам нельзя опрашивать getUpdates одним токеном.** Telegram отдаёт
апдейт только одному потребителю и возвращает 409 Conflict, а на практике это
выглядит как «кнопки то работают, то нет». Поэтому tennisratioall хочет свой
токен в TRA_BOT_TOKEN. Если его нет и берётся общий — в лог уходит громкое
предупреждение.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
TIMEOUT = 20


def _token() -> tuple[str, str, bool]:
    """(токен, chat_id, свой_ли_токен).

    «Свой» — значит отличный от токена основного бота. Мало проверить, что
    TRA_BOT_TOKEN задан: вписать туда тот же токен руками — самая вероятная
    ошибка при настройке, и последствия те же (два процесса дерутся за
    getUpdates, апдейты теряются через раз).
    """
    main_tok = os.environ.get("TELEGRAM_TOKEN", "").strip()
    tok = os.environ.get("TRA_BOT_TOKEN", "").strip()
    chat = os.environ.get("TRA_CHAT_ID", "").strip()
    own = bool(tok) and tok != main_tok
    if not tok:
        tok = main_tok
    if not chat:
        chat = os.environ.get("CHAT_ID", "").strip()
    return tok, chat, own


def _call(method: str, payload: dict, timeout: int = TIMEOUT) -> dict | None:
    tok, _, _ = _token()
    if not tok:
        return None
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API}/bot{tok}/{method}", data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram %s: %s", method, exc)
        return None
    if not body.get("ok"):
        log.warning("telegram %s отказал: %s", method, body.get("description"))
        return None
    return body.get("result")


def send(text: str, *, chat_id: str | None = None, reply_markup: dict | None = None,
         reply_to: int | None = None) -> int | None:
    """Шлёт сообщение и возвращает его message_id."""
    tok, chat, _ = _token()
    chat_id = chat_id or chat
    if not (tok and chat_id):
        print(text)
        return None
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    res = _call("sendMessage", payload)
    return (res or {}).get("message_id")


def answer_callback(callback_id: str, text: str = "") -> None:
    """Гасит «часики» на кнопке. Без этого она крутится до таймаута."""
    _call("answerCallbackQuery", {"callback_query_id": callback_id,
                                  "text": text[:200]}, timeout=10)


class Updates:
    """Длинный опрос getUpdates в отдельном потоке.

    offset хранится в памяти: терять его не страшно, потерянный апдейт — это
    не нажатая кнопка, человек нажмёт ещё раз. А вот дублировать обработку
    хуже, поэтому offset двигается сразу после чтения.
    """

    def __init__(self, on_message=None, on_callback=None, poll: int = 25):
        self.on_message = on_message
        self.on_callback = on_callback
        self.poll = poll
        self._stop = threading.Event()
        self._offset = 0
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        tok, chat, own = _token()
        if not tok:
            log.warning("нет токена — кнопки работать не будут")
            return False
        if not own:
            same = os.environ.get("TRA_BOT_TOKEN", "").strip() == \
                os.environ.get("TELEGRAM_TOKEN", "").strip() and \
                bool(os.environ.get("TRA_BOT_TOKEN", "").strip())
            why = ("TRA_BOT_TOKEN совпадает с TELEGRAM_TOKEN" if same
                   else "TRA_BOT_TOKEN не задан, взят общий TELEGRAM_TOKEN")
            log.warning("%s — это тот же бот, что и основной. Оба процесса "
                        "будут опрашивать getUpdates, Telegram отдаёт апдейт "
                        "только одному, и кнопки начнут срабатывать через раз. "
                        "Заведите отдельного бота у @BotFather.", why)
        # сбрасываем накопившиеся апдейты, иначе после простоя прилетит
        # пачка старых нажатий
        res = _call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
        if res:
            self._offset = res[-1]["update_id"] + 1
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="tra-updates")
        self._thread.start()
        log.info("опрос апдейтов запущен%s", "" if own else " (на общем токене!)")
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            res = _call("getUpdates",
                        {"offset": self._offset, "timeout": self.poll,
                         "allowed_updates": ["message", "callback_query"]},
                        timeout=self.poll + 10)
            if res is None:
                self._stop.wait(5)
                continue
            for upd in res:
                self._offset = max(self._offset, upd["update_id"] + 1)
                try:
                    self._dispatch(upd)
                except Exception:  # noqa: BLE001
                    import traceback
                    log.error("апдейт %s не обработан:\n%s",
                              upd.get("update_id"), traceback.format_exc())

    def _dispatch(self, upd: dict) -> None:
        if "callback_query" in upd and self.on_callback:
            self.on_callback(upd["callback_query"])
        elif "message" in upd and self.on_message:
            self.on_message(upd["message"])
