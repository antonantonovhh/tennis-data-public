"""Телеграм-бот поверх парсера. python-telegram-bot v21+ (async).

Главная техническая тонкость: парсер синхронный, а Playwright.sync_api
падает, если его позвать из потока с работающим event loop. Поэтому вся
тяжёлая работа уходит в asyncio.to_thread() — там своего лупа нет, и
sync_playwright запускается нормально. Плюс семафор: два хромиума
параллельно на дешёвой VPS съедят память.

Переменные окружения (см. .env.example):
    TELEGRAM_BOT_TOKEN   обязателен
    ALLOWED_USER_IDS     через запятую; пусто = бот открыт всем
    TP_CACHE_DIR         папка кэша HTML (по умолчанию .cache/tennis)
    TP_CACHE_TTL         сек, по умолчанию 21600
    TP_MAX_CONCURRENCY   одновременных тяжёлых задач, по умолчанию 1
    TP_MODE              auto|static|render
    TP_TOUR              atp|wta
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import date

from telegram import BotCommand, InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .http import Fetcher
from .report import build_report, format_elo_telegram, format_telegram, json_safe
from .tennisabstract import load_ratings

log = logging.getLogger("tennis_bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USER_IDS", "")) if x.strip().isdigit()
}
CACHE_DIR = os.getenv("TP_CACHE_DIR", ".cache/tennis")
CACHE_TTL = int(os.getenv("TP_CACHE_TTL", 6 * 3600))
MAX_CONC = int(os.getenv("TP_MAX_CONCURRENCY", "1"))
MODE = os.getenv("TP_MODE", "auto")
TOUR = os.getenv("TP_TOUR", "atp")

SEM = asyncio.Semaphore(MAX_CONC)
FETCHER = Fetcher(cache_dir=CACHE_DIR, ttl_seconds=CACHE_TTL)

SURFACES = {"clay": "clay", "грунт": "clay", "hard": "hard", "хард": "hard",
            "grass": "grass", "трава": "grass"}

HELP = (
    "<b>Команды</b>\n\n"
    "<code>/h2h Игрок 1 | Игрок 2 [покрытие] [bo5]</code>\n"
    "  H2H, Elo/покрытия, yElo, усталость и прогноз.\n"
    "  Разделитель — <code>|</code> или <code>vs</code>.\n"
    "  Покрытие: clay/hard/grass (или грунт/хард/трава).\n\n"
    "<code>/elo Имя [, Имя2]</code> — только рейтинги.\n"
    "<code>/json Игрок 1 | Игрок 2</code> — полный отчёт файлом.\n"
    "<code>/refresh</code> — сбросить кэш HTML.\n"
    "<code>/whoami</code> — ваш user id (для белого списка).\n"
    "<code>/health</code> — состояние сервиса.\n\n"
    "<b>Примеры</b>\n"
    "<code>/h2h Jan Kumstat | Maxim Mrva clay</code>\n"
    "<code>/h2h Sinner vs Alcaraz hard bo5</code>\n"
    "<code>/elo Jan Kumstat</code>\n\n"
    "Первый запрос по паре идёт 30–60 с: история матчей рендерится в браузере."
)

START_TS = time.time()


# ------------------------------------------------------------------ доступ
def allowed(update: Update) -> bool:
    if not ALLOWED:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED)


async def deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    log.warning("Отказано в доступе: %s", uid)
    await update.effective_message.reply_text(
        f"Нет доступа. Ваш id: {uid} — добавьте его в ALLOWED_USER_IDS."
    )


# ------------------------------------------------------------------ разбор
def parse_h2h_args(text: str) -> tuple[str, str, str | None, int]:
    """'/h2h Jan Kumstat | Maxim Mrva clay bo5' -> (p1, p2, 'clay', 5)."""
    body = text.split(maxsplit=1)[1] if " " in text else ""
    body = body.strip()
    if not body:
        raise ValueError("Укажите двух игроков: /h2h Игрок 1 | Игрок 2")

    best_of = 3
    bo = re.search(r"\bbo([35])\b", body, re.I)
    if bo:
        best_of = int(bo.group(1))
        body = body[: bo.start()] + body[bo.end():]

    surface = None
    for word, canon in SURFACES.items():
        m = re.search(rf"\b{word}\b", body, re.I)
        if m:
            surface = canon
            body = body[: m.start()] + body[m.end():]
            break

    parts = re.split(r"\s*\|\s*|\s+vs\.?\s+|\s+против\s+", body.strip(), maxsplit=1, flags=re.I)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            "Не понял пару игроков. Формат: /h2h Игрок 1 | Игрок 2 [clay|hard|grass] [bo5]"
        )
    return parts[0].strip(), parts[1].strip(), surface, best_of


# ------------------------------------------------------------------ хендлеры
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(f"user id: <code>{u.id}</code>", parse_mode=ParseMode.HTML)


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    up = int(time.time() - START_TS)
    free = MAX_CONC - (MAX_CONC - SEM._value)  # noqa: SLF001 — для отладки сойдёт
    await update.message.reply_text(
        f"uptime {up // 3600}ч {(up % 3600) // 60}м\n"
        f"свободных слотов: {SEM._value}/{MAX_CONC}\n"  # noqa: SLF001
        f"режим: {MODE}, тур: {TOUR}\nкэш: {CACHE_DIR} (ttl {CACHE_TTL} с)"
    )


async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    n = 0
    from pathlib import Path

    p = Path(CACHE_DIR)
    if p.exists():
        for f in p.glob("*.html"):
            f.unlink()
            n += 1
    await update.message.reply_text(f"Кэш очищен, удалено файлов: {n}")


async def _keep_typing(ctx, chat_id: str | int, stop: asyncio.Event) -> None:
    """Телеграм гасит «печатает» через 5 с — обновляем, пока идёт работа."""
    while not stop.is_set():
        try:
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            continue


async def _run_report(p1, p2, surface, best_of):
    return await asyncio.to_thread(
        build_report,
        FETCHER, p1, p2,
        surface=surface, best_of=best_of, tour=TOUR,
        mode=MODE, headless=True, as_of=date.today(),
    )


async def _h2h(update: Update, ctx: ContextTypes.DEFAULT_TYPE, as_json: bool) -> None:
    if not allowed(update):
        return await deny(update)
    try:
        p1, p2, surface, best_of = parse_h2h_args(update.message.text)
    except ValueError as exc:
        return await update.message.reply_text(str(exc))

    queued = SEM.locked()
    status = await update.message.reply_text(
        ("В очереди…" if queued else "Собираю данные, 30–60 с…")
    )
    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(ctx, update.effective_chat.id, stop))

    try:
        async with SEM:
            if queued:
                await status.edit_text("Собираю данные, 30–60 с…")
            report = await _run_report(p1, p2, surface, best_of)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка отчёта %s vs %s", p1, p2)
        await status.edit_text(f"Не получилось: {type(exc).__name__}: {exc}"[:900])
        return
    finally:
        stop.set()
        await typing

    if as_json:
        blob = json.dumps(json_safe(report), ensure_ascii=False, indent=2, default=str)
        fname = f"{p1}_vs_{p2}".replace(" ", "_").lower() + ".json"
        await status.delete()
        await update.message.reply_document(
            InputFile(io.BytesIO(blob.encode("utf-8")), filename=fname)
        )
        return

    text = format_telegram(report)
    if len(text) > 4000:
        text = text[:3900] + "\n…"
    await status.edit_text(text, parse_mode=ParseMode.HTML,
                           disable_web_page_preview=True)


async def cmd_h2h(update, ctx):
    await _h2h(update, ctx, as_json=False)


async def cmd_json(update, ctx):
    await _h2h(update, ctx, as_json=True)


async def cmd_elo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    body = update.message.text.split(maxsplit=1)
    if len(body) < 2:
        return await update.message.reply_text("Формат: /elo Имя [, Имя2]")
    names = [n.strip() for n in re.split(r"\s*[,|]\s*", body[1]) if n.strip()]

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        ratings = await asyncio.to_thread(load_ratings, FETCHER, TOUR)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка загрузки Elo")
        return await update.message.reply_text(f"Не удалось загрузить рейтинги: {exc}"[:900])

    chunks = []
    for name in names[:5]:
        row = ratings.get(name)
        chunks.append(format_elo_telegram(row.as_dict()) if row
                      else f"<b>{name}</b> — не найден в таблице Elo")
    await update.message.reply_text("\n\n".join(chunks), parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободный текст с 'vs' или '|' трактуем как /h2h."""
    if not allowed(update):
        return
    text = update.message.text or ""
    if re.search(r"\s\|\s|\svs\.?\s", text, re.I):
        update.message.text = "/h2h " + text
        return await cmd_h2h(update, ctx)
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Необработанная ошибка", exc_info=ctx.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("h2h", "Сравнить двух игроков"),
        BotCommand("elo", "Elo и yElo игрока"),
        BotCommand("json", "Полный отчёт файлом"),
        BotCommand("refresh", "Сбросить кэш"),
        BotCommand("health", "Состояние сервиса"),
        BotCommand("whoami", "Мой user id"),
        BotCommand("help", "Справка"),
    ])
    log.info("Бот запущен. Белый список: %s", ALLOWED or "выключен (открыт всем)")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("TP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("h2h", cmd_h2h))
    app.add_handler(CommandHandler("json", cmd_json))
    app.add_handler(CommandHandler("elo", cmd_elo))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
