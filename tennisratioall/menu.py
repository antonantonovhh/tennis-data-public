"""Меню по ответу на карточку матча.

Как это работает
----------------
Вешать клавиатуру на каждую карточку в режиме `each` — это сотня клавиатур
в ленте. Вместо этого при отправке запоминается message_id карточки, и когда
человек отвечает на неё, бот присылает меню веткой к его ответу.

Пока кнопка одна — кэфы на Pinnacle, та же функция, что была в основном боте.
Добавить вторую — это строка в MENU и ветка в on_callback.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import traceback

from . import telegram as tg
from .store import HERE

log = logging.getLogger(__name__)

# сколько соответствий message_id -> матч держим. Карточек за день бывает
# под сотню, но ответить могут и на вчерашнюю, поэтому запас
MAX_TRACKED = 2000

# связка переживает перезапуск: демон рестартится чаще, чем устаревают
# карточки, и терять возможность ответить на утреннюю — обидно
# Суффикс тура: связка «слаг матча -> id сообщения» у каждого бота своя,
# иначе WTA-бот отвечал бы на сообщения из чата ATP-бота. См. store.TOUR.
from .store import SUFFIX as _SUF  # noqa: E402

LINKS_FILE = (os.environ.get("TRA_LINKS")
              or os.path.join(HERE, f"tennisratioall_links{_SUF}.json"))


class Menu:
    """Связка «сообщение -> матч» и обработка нажатий."""

    def __init__(self, store=None, path: str = LINKS_FILE):
        self.store = store
        self.path = path
        self._lock = threading.Lock()
        # message_id -> {"key","slug","p1","p2","tournament","url"}
        self._by_message: dict[int, dict] = {}
        # короткий ключ -> то же самое. Именно ключ, а не slug: в callback_data
        # у Telegram всего 64 байта, а slug вроде
        # "some-very-long-name-vs-another-very-long-name" в них не влезает
        self._by_key: dict[str, dict] = {}
        self._next = 1
        self._load()

    # ------------------------------------------------------------- диск
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            raw = json.load(open(self.path, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("связка карточек не читается (%s) — начинаю с чистого", exc)
            return
        self._by_message = {int(k): v for k, v in (raw.get("by_message") or {}).items()}
        self._by_key = raw.get("by_key") or {}
        self._next = int(raw.get("next", 1))
        log.info("связка карточек: %d сообщений", len(self._by_message))

    def _save(self) -> None:
        data = {"next": self._next,
                "by_message": {str(k): v for k, v in self._by_message.items()},
                "by_key": self._by_key}
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("связка карточек не сохранена: %s", exc)

    # ------------------------------------------------------------- учёт
    def track(self, message_id: int | None, ref, rec: dict | None = None) -> None:
        if not message_id:
            return
        with self._lock:
            prev = self._by_key_for_slug(ref.slug)
            key = prev["key"] if prev else str(self._next)
            if not prev:
                self._next += 1
            info = {"key": key, "slug": ref.slug,
                    "p1": (rec or {}).get("p1") or ref.p1,
                    "p2": (rec or {}).get("p2") or ref.p2,
                    "tournament": ref.tournament, "url": ref.url}
            self._by_message[message_id] = info
            self._by_key[key] = info
            if len(self._by_message) > MAX_TRACKED:
                # выкидываем самые старые: dict сохраняет порядок вставки
                for old in list(self._by_message)[:len(self._by_message) - MAX_TRACKED]:
                    self._by_message.pop(old, None)
        self._save()

    def _by_key_for_slug(self, slug: str) -> dict | None:
        for info in self._by_key.values():
            if info.get("slug") == slug:
                return info
        return None

    def lookup_message(self, message_id: int) -> dict | None:
        with self._lock:
            return self._by_message.get(message_id)

    def lookup_key(self, key: str) -> dict | None:
        with self._lock:
            return self._by_key.get(key)

    # ------------------------------------------------------------- ответ
    def on_message(self, msg: dict) -> None:
        """Ответ на карточку -> меню. На обычные сообщения не реагируем."""
        reply = msg.get("reply_to_message")
        if not reply:
            return
        info = self.lookup_message(reply.get("message_id"))
        if not info:
            # ответ на что-то другое: молчим, чтобы не мешать переписке
            return
        tg.send(
            f"🎾 <b>{info['p1']}</b> — <b>{info['p2']}</b>\nЧто сделать?",
            chat_id=str(msg["chat"]["id"]),
            reply_to=msg.get("message_id"),
            reply_markup={"inline_keyboard": [[
                {"text": "💰 Кэфы на Pinnacle",
                 "callback_data": f"odds|{info['key']}"},
            ]]},
        )

    # ------------------------------------------------------------- кнопка
    def on_callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
        msg_id = (cq.get("message") or {}).get("message_id")

        if not data.startswith("odds|"):
            tg.answer_callback(cq["id"])
            return

        info = self.lookup_key(data.split("|", 1)[1])
        if not info:
            tg.answer_callback(cq["id"], "Матч потерялся, пришлите ссылку")
            return

        tg.answer_callback(cq["id"], "Смотрю линию…")
        threading.Thread(
            target=self._fetch_odds, args=(info, chat_id, msg_id),
            daemon=True, name="tra-odds").start()

    def _fetch_odds(self, info: dict, chat_id: str, reply_to: int | None) -> None:
        """Тянет линию Pinnacle. В отдельном потоке: запрос небыстрый, а
        колбэк надо погасить сразу, иначе кнопка крутится."""
        try:
            from bot_merged import (calculate_potential_bets,  # noqa: PLC0415
                                    format_odds_attribution, get_pinnacle_odds)
        except Exception:  # noqa: BLE001
            tg.send("❌ Модуль котировок недоступен.", chat_id=chat_id,
                    reply_to=reply_to)
            return

        try:
            odds = get_pinnacle_odds(info["p1"], info["p2"], is_manual=True)
        except Exception as exc:  # noqa: BLE001
            log.error("Pinnacle упал:\n%s", traceback.format_exc())
            tg.send(f"❌ Не удалось получить линию: <code>{type(exc).__name__}</code>",
                    chat_id=chat_id, reply_to=reply_to)
            return

        if not odds:
            tg.send("🤷 Матча нет в линии Pinnacle.", chat_id=chat_id, reply_to=reply_to)
            return
        if odds.get("error"):
            tg.send(odds["error"], chat_id=chat_id, reply_to=reply_to)
            return

        lines = [f"💰 <b>{info['p1']}</b> — <b>{info['p2']}</b>",
                 f"П1 <b>{odds.get('p1', '—')}</b> · П2 <b>{odds.get('p2', '—')}</b>"]
        for key, label in (("total_sets", "Тотал сетов"),
                           ("total_games", "Тотал геймов"),
                           ("handicap", "Фора")):
            if odds.get(key):
                lines.append(f"{label}: {odds[key]}")

        try:
            bets = calculate_potential_bets(info["p1"], info["p2"], odds)
            if bets:
                lines.append("")
                lines += [f"🔹 {b['type']}: <b>{b['prediction']}</b> "
                          f"(кэф {b['odds']})" for b in bets]
        except Exception:  # noqa: BLE001
            log.error("расчёт ставок упал:\n%s", traceback.format_exc())

        try:
            warn = format_odds_attribution(info["p1"], info["p2"], odds)
            if warn:
                lines.append(warn)
        except Exception:  # noqa: BLE001
            pass

        tg.send("\n".join(lines), chat_id=chat_id, reply_to=reply_to)
