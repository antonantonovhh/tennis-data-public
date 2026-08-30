import time
import json
import sys
import random
import threading
import subprocess
import requests
import unicodedata
import re
import urllib3
import logging
import os
import csv
import datetime
import uuid
from bs4 import BeautifulSoup
import smtplib
from email.message import EmailMessage

# === ПАРСЕР СТАТИСТИКИ (пакет tennis_parser рядом с этим файлом) ===
try:
    from tennis_parser.integration import run_stats_parsing, players_from_url
    STATS_PARSER_OK = True
    STATS_PARSER_ERR = ""
except Exception as _e:  # пакета нет / не поставлены зависимости — бот всё равно стартует
    run_stats_parsing = None
    players_from_url = None
    STATS_PARSER_OK = False
    STATS_PARSER_ERR = str(_e)
# ==================================================================

# Отключаем предупреждения SSL для Pinnacle
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL = "https://tennisratio.com/atp-matches.html"
BASE_URL = "https://tennisratio.com"
# Афиша по турам. Структура страниц одинаковая — те же ссылки
# /h2h-compare/<slug>.html, — поэтому разбор один на оба.
TOUR_URL = {
    "atp": "https://www.tennisratio.com/atp-matches.html",
    "wta": "https://www.tennisratio.com/wta-matches.html",
}
CHECK_INTERVAL = 60
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Секреты берутся ТОЛЬКО из окружения. Не вписывай их сюда: файл легко
# уходит в чат/репозиторий вместе с токеном.
def _load_env_file(path=None):
    """Подхватывает .env рядом со скриптом, если переменных нет в окружении.

    Нужно потому, что Environment= в systemd-юните действует только на службу:
    стоит перенести настройки в .env и забыть про EnvironmentFile — и бот
    молча остаётся без токена. Здесь же он читает файл сам, а окружение
    по-прежнему главнее файла.
    """
    path = path or os.environ.get("TP_ENV_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return None
    n = 0
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
                n += 1
    except OSError:
        return None
    return f"{path} ({n} перем.)" if n else None


ENV_SOURCE = _load_env_file()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
if not TELEGRAM_TOKEN:
    sys.exit(
        "Не задан TELEGRAM_TOKEN.\n"
        "  Искал в окружении и в .env рядом со скриптом"
        f" ({'файл прочитан: ' + ENV_SOURCE if ENV_SOURCE else 'файла нет'}).\n"
        "  Если переменные лежат в .env, в юните должна быть строка:\n"
        "    EnvironmentFile=/opt/tennis_bot/.env\n"
        "  и затем: systemctl daemon-reload && systemctl restart <служба>")
BET_AMOUNT = 1000 # Фиксированная ставка 1000₽

DB_FILE = "bets_db.json"
CSV_FILE = "bets_history.csv"
log = logging.getLogger(__name__)
STATE_FILE = "bot_state.json" # Файл для сохранения состояния ожидания

# === НАСТРОЙКИ ПОЧТЫ ДЛЯ ОТПРАВКИ CSV ===
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")
# Данные для отправки с Gmail (пароль приложения, НЕ пароль от аккаунта)
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
# .replace(" ", "") оставлен: Google показывает пароль группами по 4 символа
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "").replace(" ", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))  # SSL порт
EMAIL_ENABLED = bool(SENDER_EMAIL and SENDER_PASSWORD and RECEIVER_EMAIL)
# ========================================

# === НАСТРОЙКИ GEMINI (симуляция матча) ===
# Ключ берется из переменной окружения GEMINI_API_KEY, либо впиши прямо сюда.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.1-pro-preview"
GEMINI_RESEARCH_AGENT = "deep-research-preview-04-2026"  # есть еще deep-research-max-preview-04-2026 (дольше и дороже)
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
SIM_COUNT = 10000
# ==========================================

# === НАСТРОЙКИ GEMINI (симуляция матча) ===
# Ключ берётся из переменной окружения GEMINI_API_KEY, либо впиши прямо сюда.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_RESEARCH_AGENT = "deep-research-preview-04-2026"   # быстрый вариант
GEMINI_RESEARCH_AGENT_MAX = "deep-research-max-preview-04-2026"  # максимально подробный
GEMINI_MODEL = "gemini-3.1-pro-preview"
SIMULATION_RUNS = 10000
# ==========================================

# === НАСТРОЙКИ CLAUDE (анализ матча) ===
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Для официального API — https://api.anthropic.com
# Для шлюза-посредника (New API / one-api и т.п.) — адрес сервиса, напр. https://vibecode-api.online
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
ANTHROPIC_API_URL = f"{ANTHROPIC_BASE_URL}/v1/messages"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")
CLAUDE_CODE_TOOL = "code_execution_20260120"   # запасной вариант: code_execution_20250825
# ==========================================

def load_state():
    """Загружает состояние ожидающих матчей из файла"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка загрузки состояний: {e}")
    return {"pending_tasks": {}, "awaiting_bets": {}}

def save_state(state_dict):
    """Сохраняет состояние ожидающих матчей в файл.
    Сливает с уже сохраненным состоянием: часть вызовов передает не все ключи,
    и без слияния они затирали бы, например, link_actions (кнопки под ссылкой)."""
    try:
        current = load_state()
    except Exception:
        current = {}
    current.update(state_dict or {})
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=4)

RU_WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
RU_MONTHS_GENITIVE = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]

def build_report_filename(prefix="bets_history"):
    """Формирует имя файла отчета с датой, днем недели и месяцем, например bets_history_Пятница_15_августа_2026.csv"""
    now = get_msk_time()
    weekday = RU_WEEKDAYS[now.weekday()]
    month = RU_MONTHS_GENITIVE[now.month - 1]
    return f"{prefix}_{weekday}_{now.day:02d}_{month}_{now.year}.csv"

def send_email_with_csv(period_name, csv_path=None, filename=None):
    """Отправляет CSV файл на указанную почту. Если csv_path/filename не заданы — отправляет
    актуальный полный bets_history.csv под его обычным именем (поведение по умолчанию)."""
    if not EMAIL_ENABLED:
        print("ℹ️ Почта не настроена (SENDER_EMAIL/SENDER_PASSWORD/RECEIVER_EMAIL) — пропускаю отправку CSV.")
        return False
    csv_path = csv_path or CSV_FILE
    filename = filename or CSV_FILE
    if not os.path.exists(csv_path) or "your_email" in SENDER_EMAIL:
        print("⚠️ Почта не отправлена: файл не существует или не настроены данные SMTP SENDER_EMAIL.")
        return

    try:
        msg = EmailMessage()
        msg['Subject'] = f"Теннис Бот: Статистика ставок ({period_name})"
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg.set_content(f"Во вложении файл истории ставок за {period_name}.")

        with open(csv_path, 'rb') as f:
            file_data = f.read()
            msg.add_attachment(file_data, maintype='text', subtype='csv', filename=filename)

        # Подключение к серверу и отправка (используем SSL)
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        print(f"📧 Файл {filename} успешно отправлен на {RECEIVER_EMAIL}")
    except Exception as e:
        print(f"❌ Ошибка при отправке email: {e}")

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка загрузки БД: {e}")
    return {"bets": []}

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def init_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Турнир", "Дата и время", "Событие", "Прогноз", "Ставка", "Коэф.", "Букм.", "Прибыль", "Счёт", "Сумма геймов", "Разница геймов"])

def log_to_csv(bet_data):
    with open(CSV_FILE, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            bet_data.get("tournament", ""), bet_data.get("date", ""), bet_data.get("match", ""),
            bet_data.get("prediction", ""), f"{BET_AMOUNT}₽", f"{bet_data.get('odds', 0):.3f}",
            "Pin", "В игре", "", "", ""
        ])

def regenerate_csv_from_db():
    db = load_db()
    with open(CSV_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Турнир", "Дата и время", "Событие", "Прогноз", "Ставка", "Коэф.", "Букм.", "Прибыль", "Счёт", "Сумма геймов", "Разница геймов"])
        for match in db["bets"]:
            for bet in match["bets"]:
                prof_str = "В игре"
                if bet["status"] == "win": prof_str = f"+{bet['profit']:.2f}₽"
                elif bet["status"] in ["loss", "refund"]: prof_str = f"{bet['profit']:.2f}₽"
                
                games_diff = ""
                total_games = ""
                if match["resolved"]:
                    g1, g2 = match.get("games_p1", 0), match.get("games_p2", 0)
                    total_games = str(g1 + g2)
                    diff = (g2 - g1) if (("П2" in bet["prediction"]) or ("Ф2" in bet["prediction"])) else (g1 - g2)
                    games_diff = f"+{diff}" if diff > 0 else str(diff)
                    
                writer.writerow([
                    match.get("tournament", ""), match.get("date", ""), match.get("match", ""),
                    bet.get("prediction", ""), f"{BET_AMOUNT}₽", f"{bet.get('odds', 0):.3f}", "Pin",
                    prof_str, pretty_score(match.get("score", "")), total_games, games_diff
                ])

def am_to_dec(price):
    if price is None: return "-"
    try:
        p = float(price)
        if p > 0: return f"{(p / 100) + 1:.3f}"
        elif p < 0: return f"{(100 / abs(p)) + 1:.3f}"
        return "1.00"
    except:
        return str(price)

def send_notification(text, chat_id=CHAT_ID, reply_markup=None, reply_to=None):
    """reply_to — message_id исходного сообщения; ответ прилетит веткой к нему."""
    print(text)
    if TELEGRAM_TOKEN and chat_id:
        tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup: payload["reply_markup"] = reply_markup
        if reply_to:
            payload["reply_to_message_id"] = reply_to
            # если исходное сообщение удалили, Telegram иначе вернёт ошибку и ничего не пришлёт
            payload["allow_sending_without_reply"] = True
        try: requests.post(tg_url, json=payload, timeout=10)
        except: pass

def send_photo(photo_path, chat_id=CHAT_ID, caption=""):
    """Отправляет файл-картинку (например, скриншот страницы) в Telegram."""
    if not (TELEGRAM_TOKEN and chat_id):
        return
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(tg_url, data={"chat_id": chat_id, "caption": caption[:1024]}, files={"photo": f}, timeout=30)
        if resp.status_code != 200:
            print(f"⚠️ Telegram sendPhoto вернул {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Ошибка отправки фото в Telegram: {e}")
        send_notification(f"❌ Не удалось отправить скриншот: {e}", chat_id)

def take_screenshot(url, output_path="/tmp/screenshot.png", full_page=True, wait_selector=None, timeout_ms=30000, verify_any_text=None):
    """Открывает страницу в headless-браузере (Chromium через Playwright) и сохраняет скриншот.
    Нужен pip install playwright + playwright install chromium (см. инструкцию отдельно).
    verify_any_text — список строк; если ни одна не найдена на странице, считаем что попали не туда
    (например, на 404 или заглушку) и возвращаем ошибку, не присылая бесполезный скриншот.
    Возвращает {"success": True/False, "path"/"error": ...}."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"success": False, "error": f"Playwright не установлен в этом окружении Python.\nБот использует: {sys.executable}\nПоставь именно в него:\n{sys.executable} -m pip install playwright --break-system-packages\n{sys.executable} -m playwright install --with-deps chromium"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 1000}, user_agent=HEADERS["User-Agent"])
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                if wait_selector:
                    try: page.wait_for_selector(wait_selector, timeout=8000)
                    except Exception: pass

                if verify_any_text:
                    try: content = page.content()
                    except Exception: content = ""
                    if not any(t.lower() in content.lower() for t in verify_any_text):
                        return {"success": False, "error": "Страница открылась, но нужного контента (H2H) на ней нет."}

                page.screenshot(path=output_path, full_page=full_page)
                return {"success": True, "path": output_path}
            finally:
                browser.close()
    except Exception as e:
        return {"success": False, "error": str(e)}

SURFACE_RU_MAP = {"хард": "hard", "грунт": "clay", "трава": "grass", "ковер": "hard", "ковёр": "hard"}
FLASHSCORE_SURFACES = {"hard", "clay", "grass"}
FS_MOBILE_TENNIS = "https://m.flashscoreusa.com/tennis/"

def find_flashscore_match_id(p1, p2):
    """Ищет ID матча на Flashscore по легкой мобильной версии, где обычным HTTP-запросом (без браузера)
    отдается список всех матчей дня в виде:
        14:00 Darderi L. (Ita) - Hijikata R. (Aus)  ->  /game/xxwgU4tP/
    Это надежнее поиска через поисковик: не зависит от индексации и выдачи.
    Возвращает (match_id, строка_с_именами) либо (None, None)."""
    p1_last = remove_accents(p1.split()[-1].lower())
    p2_last = remove_accents(p2.split()[-1].lower())

    for day in ("0", "1", "-1"):
        try:
            resp = requests.get(FS_MOBILE_TENNIS, params={"d": day}, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"⚠️ Flashscore mobile вернул {resp.status_code} (день {day})")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                m = re.search(r'/game/([A-Za-z0-9]{6,})/?', a["href"])
                if not m: continue
                match_id = m.group(1)
                parent = a.find_parent(["div", "li", "td", "tr", "p"]) or a.parent
                line = remove_accents(parent.get_text(" ", strip=True).lower()) if parent else ""
                if p1_last in line and p2_last in line:
                    print(f"✅ Flashscore: найден матч {p1} vs {p2} (id={match_id}, день {day})")
                    return match_id, line
        except Exception as e:
            print(f"❌ Ошибка поиска матча на Flashscore (день {day}): {e}")
    return None, None

def find_flashscore_match_url(p1, p2):
    """Запасной путь: ищет страницу матча через поисковик (site:flashscoreusa.com).
    Возвращает базовый URL матча либо None."""
    query = f"site:flashscoreusa.com {p1} {p2} h2h tennis"
    try:
        resp = requests.get("https://html.duckduckgo.com/html/", params={"q": query}, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"⚠️ DuckDuckGo вернул {resp.status_code} при поиске матча Flashscore")
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            real_url = href
            m = re.search(r'uddg=([^&]+)', href)
            if m:
                real_url = requests.utils.unquote(m.group(1))
            if "flashscoreusa.com/game/tennis/" in real_url:
                base = re.match(r'(https?://[^/]+/game/tennis/[^/]+/[^/]+)/?', real_url)
                if base:
                    return base.group(1)
    except Exception as e:
        print(f"❌ Ошибка поиска матча на Flashscore: {e}")
    return None

def normalize_surface(surface):
    surf = SURFACE_RU_MAP.get((surface or "").lower(), (surface or "").lower())
    return surf if surf in FLASHSCORE_SURFACES else ""

def build_flashscore_h2h_url(match_base_url, surface=""):
    """Достраивает URL до вкладки H2H с нужным покрытием, отталкиваясь от базового URL матча."""
    surf = normalize_surface(surface)
    return f"{match_base_url}/h2h/{surf}/" if surf else f"{match_base_url}/h2h/"

def build_flashscore_match_urls(match_id):
    """Варианты адреса страницы матча по его ID (Flashscore со временем менял схему URL)."""
    return [
        f"https://www.flashscoreusa.com/match/{match_id}/",
        f"https://www.flashscoreusa.com/match/tennis/{match_id}/",
        f"https://m.flashscoreusa.com/game/{match_id}/",
    ]

def guess_surface_for_players(p1, p2, latest_matches):
    """Пытается определить покрытие матча по уже спарсенным данным tennisratio.com (latest_matches),
    чтобы не заставлять пользователя каждый раз указывать покрытие вручную в команде /h2h."""
    p1_last = remove_accents(p1.split()[-1].lower())
    p2_last = remove_accents(p2.split()[-1].lower())
    for slug, data in latest_matches.items():
        if p1_last in slug and p2_last in slug:
            m = re.search(r'\((Hard|Clay|Grass|Carpet)\)', data.get("tournament", ""), re.IGNORECASE)
            if m: return m.group(1).lower()
    return ""

def screenshot_flashscore_h2h(match_id, surface="", output_path="/tmp/fs_h2h.png"):
    """Открывает страницу матча на Flashscore и КЛИКАЕТ по вкладке H2H и фильтру покрытия,
    вместо того чтобы угадывать прямой URL. Flashscore — SPA с cookie-баннером: собранная вручную
    ссылка вида /match/<id>/#/h2h/clay/ часто отдает пустую оболочку, поэтому вкладки надо
    переключать так же, как это делает человек.
    Всегда сохраняет скриншот (даже при неудаче) — чтобы было видно, что именно показал сайт."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"success": False, "error": f"Playwright не установлен в этом окружении Python.\nБот использует: {sys.executable}\nПоставь именно в него:\n{sys.executable} -m pip install playwright --break-system-packages\n{sys.executable} -m playwright install --with-deps chromium"}

    surf = normalize_surface(surface)
    diag = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 1400}, user_agent=HEADERS["User-Agent"],
                                        locale="en-US")
                opened = False
                for url in build_flashscore_match_urls(match_id):
                    try:
                        page.goto(url, timeout=45000, wait_until="domcontentloaded")
                        page.wait_for_timeout(2500)
                        title = (page.title() or "")[:80]
                        body = page.content()
                        diag.append(f"{url} -> {page.url} | {title}")
                        if "404" in title or "not found" in title.lower() or len(body) < 2000:
                            continue
                        opened = True
                        break
                    except Exception as e:
                        diag.append(f"{url} -> ошибка: {str(e)[:100]}")

                if not opened:
                    page.screenshot(path=output_path, full_page=False)
                    return {"success": False, "error": "Не удалось открыть страницу матча.", "path": output_path, "diag": diag}

                # Закрываем баннер согласия на куки — иначе он перекрывает контент на скриншоте
                for sel in ["#onetrust-accept-btn-handler", "button:has-text('I Accept')",
                            "button:has-text('Accept all')", "button:has-text('AGREE')",
                            "button:has-text('Accept')", "[id*='accept' i]"]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1500):
                            el.click(timeout=3000)
                            page.wait_for_timeout(1200)
                            diag.append(f"закрыт баннер: {sel}")
                            break
                    except Exception: pass

                # Переходим на вкладку H2H кликом
                clicked_h2h = False
                for sel in ["a[href*='h2h']", "[data-testid*='h2h' i]", "button:has-text('H2H')",
                            "a:has-text('H2H')", "text=H2H"]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=2500):
                            el.click(timeout=4000)
                            page.wait_for_timeout(2500)
                            clicked_h2h = True
                            diag.append(f"клик по H2H: {sel}")
                            break
                    except Exception: pass
                if not clicked_h2h:
                    diag.append("вкладку H2H кликнуть не удалось")

                # Выбираем нужное покрытие среди фильтров (ALL SURFACES / CLAY / GRASS / HARD)
                if surf:
                    for sel in [f"button:has-text('{surf.upper()}')", f"a:has-text('{surf.upper()}')",
                                f"text={surf.upper()}"]:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=2500):
                                el.click(timeout=4000)
                                page.wait_for_timeout(2000)
                                diag.append(f"выбрано покрытие: {surf}")
                                break
                        except Exception: pass

                try: page.wait_for_load_state("networkidle", timeout=8000)
                except Exception: pass

                page.screenshot(path=output_path, full_page=True)
                content = page.content().lower()
                has_h2h = any(t in content for t in ["head-to-head", "h2h", "last games"])
                return {"success": bool(has_h2h), "path": output_path, "url": page.url, "diag": diag,
                        "error": None if has_h2h else "Страница открылась, но блок H2H на ней не найден."}
            finally:
                browser.close()
    except Exception as e:
        return {"success": False, "error": str(e), "diag": diag}

def get_flashscore_h2h_screenshot(p1, p2, surface="", output_path=None):
    """Полный конвейер: находит ID матча на Flashscore по мобильной версии, открывает страницу
    в браузере и переключает вкладку H2H + покрытие кликами."""
    path = output_path or f"/tmp/flashscore_{uuid.uuid4().hex[:8]}.png"

    match_id, _ = find_flashscore_match_id(p1, p2)
    if not match_id:
        base = find_flashscore_match_url(p1, p2)
        if base:
            m = re.search(r'mid=([A-Za-z0-9]+)', base)
            if m: match_id = m.group(1)
    if not match_id:
        return {"success": False, "error": "Матч не найден на Flashscore (ни в списке матчей на сегодня/завтра/вчера, ни через поиск). Возможно, он еще не анонсирован."}

    result = screenshot_flashscore_h2h(match_id, surface, output_path=path)
    for line in result.get("diag", []):
        print(f"   FS: {line}")
    return result

def remove_accents(input_str):
    return "".join([c for c in unicodedata.normalize('NFKD', input_str) if not unicodedata.combining(c)])

def get_msk_time():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)

def get_surface_from_text(text):
    if not text: return ""
    text_lower = text.lower()
    if re.search(r'\b(grass|трава)\b', text_lower): return "Grass"
    if re.search(r'\b(clay|грунт)\b', text_lower): return "Clay"
    if re.search(r'\b(hard|хард)\b', text_lower): return "Hard"
    if re.search(r'\b(carpet|ковер)\b', text_lower): return "Carpet"
    return ""

# Афиша вёрстается двумя способами, и это уже ломало даты. В крупной
# карточке (`div.match-card`) ссылка «Match preview» лежит внутри карточки,
# и ближайший div — это она. В компактном списке строка матча — сама
# ссылка (`a.compact-row`), а ближайший div — `div.matches-compact`, общий на
# весь список: время, имена и раунд оттуда относятся к чужому матчу.
def _match_card(link):
    """Элемент, описывающий именно этот матч, а не блок со всеми сразу."""
    if "compact-row" in (link.get("class") or []):
        return link
    row = link.find_parent(class_="compact-row")
    if row is not None:
        return row
    card = link.find_parent(class_="match-card")
    if card is not None:
        return card
    parent = link.find_parent(['div', 'li', 'article', 'tr'])
    if parent is not None and "matches-compact" in (parent.get("class") or []):
        return link  # лучше ничего, чем время соседнего матча
    return parent


def _card_utc(link):
    """ISO-время начала матча из data-utc карточки — точный источник.

    Сайт держит его и на `span.match-date-display`, и на `span.compact-time`,
    поэтому это единственное, что одинаково работает в обеих вёрстках.
    """
    card = _match_card(link)
    if card is None:
        return ""
    if card.get("data-utc"):
        return card["data-utc"]
    el = card.find(attrs={"data-utc": True})
    return el.get("data-utc", "") if el is not None else ""


def _utc_stamp(iso_str):
    """ISO-время из data-utc / startDate в формат афиши: «24.08. 19:30».

    Именно UTC, а не московское. Сайт показывает время в UTC, в этом же виде
    оно легло во все журналы, и `tennisratioall.results.parse_when` читает
    строку как UTC. Перевод в MSK сдвинул бы историю на три часа и сломал бы
    правило «ждать линию не дольше часа после начала матча».
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt.strftime("%d.%m. %H:%M")
    except Exception:
        return ""


# «August Holmgren» — это игрок, а не дата. Одного слова-месяца мало:
# рядом обязано стоять число дня, иначе имя человека уезжает в поле
# даты — и дальше в журнал, в веб-панель и в телеграм.
_DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday"]
_MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november",
                "december"]


def _looks_like_date_text(txt):
    """Похож ли заголовок на дату, а не на имя игрока."""
    low = txt.lower()
    if any(k in low for k in ["vs", "atp", "wta", "challenger", "open",
                              "futures", "rank"]):
        return False
    # Границы слов — чтобы "mayo" не матчилось с "may"
    has_day = any(re.search(rf'\b{d}\b', low) for d in _DAY_NAMES)
    has_month = any(re.search(rf'\b{m}\b', low) for m in _MONTH_NAMES)
    if not (has_day or has_month):
        return False
    # Число дня (1..31) обязательно. У «August Holmgren 15:00» его нет:
    # 15:00 — это время, поэтому часы вырезаем перед проверкой.
    without_time = re.sub(r'\b\d{1,2}:\d{2}\b', ' ', txt)
    return bool(re.search(r'(?<!\d)([1-9]|[12]\d|3[01])(?!\d)', without_time))


def get_date_for_match(link, fallback_date=""):
    if not link: return fallback_date if fallback_date else "Неизвестная дата"
    try:
        # 0. Точное время из data-utc самой карточки
        utc = _card_utc(link)
        if utc:
            stamp = _utc_stamp(utc)
            if stamp:
                return stamp

        # 1. Дата внутри карточки именно этого матча
        card = _match_card(link)
        if card is not None:
            card_text = card.get_text(" ", strip=True)
            # Ищем формат "25.07." или "25.07.2026"
            match = re.search(r'(?<!\d)(\d{2}\.\d{2}\.(?:\d{4})?)(?!\d)', card_text)
            if match: return match.group(1)

        # 2. Если не нашли, идем вверх по дереву
        current_elem = link
        while True:
            prev_header = current_elem.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'b', 'strong', 'span'])
            if not prev_header: break
            txt = " ".join(prev_header.get_text(" ", strip=True).split())
            if len(txt) > 80:
                current_elem = prev_header; continue

            match = re.search(r'(?<!\d)(\d{2}\.\d{2}\.(?:\d{4})?)(?!\d)', txt)
            if match: return match.group(1)

            if _looks_like_date_text(txt):
                return txt.strip(":-|/ ")
            current_elem = prev_header
        return fallback_date if fallback_date else "Неизвестная дата"
    except: return fallback_date if fallback_date else "Неизвестная дата"

ROUND_PATTERNS = [
    r'Round of \d+', r'Quarterfinals?', r'Semifinals?', r'\bFinal\b',
    r'Round Robin', r'Qualifi(?:cation|er|ying)\w*', r'\bQ[1-4]\b', r'\bR\d{1,3}\b', r'Third Place'
]

def _iter_ld_events(node):
    """SportsEvent из ld+json, в какой бы обёртке они ни лежали.

    Афиша отдаёт их как ItemList → itemListElement. Раньше код разбирал
    только @graph и списки верхнего уровня, поэтому structured data не
    доходила до бота вообще: все даты угадывались по вёрстке, и на
    компактных строках афиши в поле даты уезжало имя игрока
    («August Holmgren 15:00» — это August Holmgren, а не август).
    """
    if isinstance(node, list):
        for it in node:
            for ev in _iter_ld_events(it):
                yield ev
        return
    if not isinstance(node, dict):
        return
    if node.get("@type") == "SportsEvent":
        yield node
    for key in ("@graph", "itemListElement", "mainEntity", "item"):
        if key in node:
            for ev in _iter_ld_events(node[key]):
                yield ev


def _slug_candidates(event_name):
    """Slug карточки h2h по имени события: у SportsEvent на афише нет url.

    Имя выглядит как "Toby Samuel vs Francesco Maestrelli - Us Open Qualies
    (Quarterfinal)", а порядок игроков в нём не совпадает с порядком в
    ссылке, поэтому возвращаем оба варианта — какой найдётся в разметке.
    """
    head = re.split(r'\s+[-\u2013\u2014]\s+', (event_name or "").strip())[0]
    parts = re.split(r'\s+vs\.?\s+', head, flags=re.IGNORECASE)
    if len(parts) != 2:
        return []
    a, b = [re.sub(r'[^a-z0-9]+', '-', remove_accents(x).lower()).strip('-')
            for x in parts]
    if not a or not b:
        return []
    return ["%s-vs-%s" % (a, b), "%s-vs-%s" % (b, a)]


def _round_from_event_name(event_name):
    """Раунд из хвоста имени события: "... (Quarterfinal)"."""
    m = re.search(r'\(([^()]+)\)\s*$', event_name or "")
    if not m:
        return ""
    tail = m.group(1).strip()
    for pat in ROUND_PATTERNS:
        hit = re.search(pat, tail, re.IGNORECASE)
        if hit:
            return hit.group(0)
    return ""


def get_round_for_match(link):
    """Извлекает раунд матча (Round of 32, Quarterfinal и т.д.) из карточки на странице."""
    if not link: return ""
    try:
        card = _match_card(link)
        text = card.get_text(" ", strip=True) if card is not None else link.get_text(" ", strip=True)
        for pat in ROUND_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            if m: return m.group(0)
    except: pass
    return ""

def get_time_for_match(link):
    """Извлекает время начала матча (ЧЧ:ММ) из карточки на странице, если оно указано в верстке."""
    if not link: return ""
    try:
        # data-utc точнее текста: в компактном списке ближайший div — это
        # блок со всеми матчами сразу, и оттуда бралось время соседа.
        utc = _card_utc(link)
        if utc:
            m = re.search(r'\b([01]\d|2[0-3]):([0-5]\d)\b',
                          _utc_stamp(utc))
            if m: return m.group(0)
        card = _match_card(link)
        text = card.get_text(" ", strip=True) if card is not None else link.get_text(" ", strip=True)
        m = re.search(r'\b([01]\d|2[0-3]):([0-5]\d)\b', text)
        if m: return m.group(0)
    except: pass
    return ""

def format_match_datetime(iso_str, fallback_date=""):
    """Преобразует ISO дату/время матча (из structured data сайта) в московское время формата ДД.ММ.ГГГГ ЧЧ:ММ.
    Раньше бот брал из startDate только дату (до 'T'), выбрасывая реальное время матча — из-за этого
    в CSV попадала либо дата без времени, либо (в ручном/авто-потоке ставок) вообще дата добавления ставки."""
    if not iso_str:
        return fallback_date if fallback_date else "Неизвестная дата"
    try:
        if "T" not in iso_str:
            y, mth, d = iso_str.split("-")
            return f"{d}.{mth}.{y}"
        clean = iso_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(clean)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=3)))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        try:
            date_part = iso_str.split("T")[0]
            y, m, d = date_part.split("-")
            return f"{d}.{m}.{y}"
        except Exception:
            return fallback_date if fallback_date else "Неизвестная дата"

def get_tournament_and_surface(link, fallback_tournament=""):
    if not link: return fallback_tournament if fallback_tournament else "📌 Другие матчи"
    try:
        tournament, surface = "", ""
        
        # Стратегия 1: Поиск турнира в соседних блоках над карточкой матча
        p = link.parent
        for _ in range(5):
            if not p or p.name == 'body': break
            prev = p.find_previous_sibling()
            while prev:
                txt = prev.get_text(" ", strip=True)
                txt_lower = txt.lower()
                if len(txt) < 150 and any(k in txt_lower for k in ["wimbledon", "challenger", "open", "m15", "m25", "atp", "wta", "championship", "itf", "cup", "qualies", "masters"]):
                    if not any(x in txt_lower for x in ["vs", "-vs-", "match preview", "h2h", "score", "rank"]):
                        tournament = txt
                        break
                prev = prev.find_previous_sibling()
            if tournament: break
            p = p.parent

        # Стратегия 2: Резервный классический поиск назад
        if not tournament:
            current_elem = link
            while True:
                prev_header = current_elem.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'b', 'strong'])
                if not prev_header: break
                txt = prev_header.get_text(" ", strip=True)
                txt_lower = txt.lower()
                if not txt or len(txt) > 150:
                    current_elem = prev_header; continue
                if any(x in txt_lower for x in ["vs", "-vs-", "match preview", "h2h", "preview", "score", "rank:"]):
                    current_elem = prev_header; continue
                if any(k in txt_lower for k in ["wimbledon", "challenger", "open", "m15", "m25", "atp", "wta", "championship", "itf", "cup", "qualies", "masters"]):
                    tournament = txt; break
                current_elem = prev_header

        if not tournament and fallback_tournament: 
            tournament = fallback_tournament

        # Извлекаем покрытие
        surface = get_surface_from_text(tournament) if tournament else ""
        if not surface:
            for prev in link.find_all_previous(['div', 'span', 'p']):
                if prev == link: continue
                txt = prev.get_text(" ", strip=True)
                if len(txt) < 40 and not any(x in txt.lower() for x in ["menu", "stats", "elo", "w/l", "h2h"]):
                    surf_check = get_surface_from_text(txt)
                    if surf_check:
                        surface = surf_check
                        break

        if tournament and tournament != "📌 Другие матчи":
            for w in ["clay", "hard", "grass", "carpet", "грунт", "хард", "ковер", "трава"]:
                tournament = re.sub(rf'(?i)\b{w}\b', '', tournament)
            tournament = re.sub(r'·?\s*\d+\s*matches?\s*▾?', '', tournament, flags=re.IGNORECASE)
            for pat in ROUND_PATTERNS:
                tournament = re.sub(pat, '', tournament, flags=re.IGNORECASE)
            tournament = re.sub(r'^[ \-\|/📊()\[\]▾•·]+|[ \-\|/📊()\[\]▾•·]+$', '', tournament).strip()
            tournament = re.sub(r'\s+', ' ', tournament)
            # Защита от задвоенного названия (склейка соседних блоков верстки).
            # Бывает не только 'Cancun Cancun', но и 'Challengers Cancun 2 Challenger Qualies Cancun 2' —
            # поэтому схлопываем и повторяющиеся словосочетания, а не только соседние слова.
            tournament = re.sub(r'\b(\w+)(?:\s+\1\b)+', r'\1', tournament, flags=re.IGNORECASE)
            words = tournament.split()
            for size in range(min(4, len(words) // 2), 0, -1):
                i = 0
                while i + size <= len(words):
                    chunk = [w.lower() for w in words[i:i+size]]
                    j = i + size
                    while j + size <= len(words):
                        if [w.lower() for w in words[j:j+size]] == chunk:
                            del words[j:j+size]
                        else:
                            j += 1
                    i += 1
            tournament = " ".join(words)

        if not tournament: tournament = "📌 Другие матчи"
        return f"{tournament} ({surface})" if surface else tournament
    except: 
        return fallback_tournament if fallback_tournament else "📌 Другие матчи"

# Адреса рейтингов по турам. Структура таблиц одинаковая, разбор общий.
YELO_URL_BY_TOUR = {
    "atp": "https://tennisabstract.com/reports/atp_season_yelo_ratings.html",
    "wta": "https://tennisabstract.com/reports/wta_season_yelo_ratings.html",
}
ELO_URL_BY_TOUR = {
    "atp": "https://tennisabstract.com/reports/atp_elo_ratings.html",
    "wta": "https://tennisabstract.com/reports/wta_elo_ratings.html",
}


def parse_yelo_ratings(tour="atp"):
    """yElo по игрокам. tour: atp | wta.

    Значение по умолчанию сохраняет прежнее поведение: основной бот зовёт
    функцию без аргумента и продолжает работать по мужскому туру.
    """
    yelo_url = YELO_URL_BY_TOUR.get(tour, YELO_URL_BY_TOUR["atp"])
    yelo_map = {}
    try:
        response = requests.get(yelo_url, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            tables = soup.find_all("table")
            if tables:
                table = max(tables, key=lambda t: len(t.find_all("tr")))
                rows = table.find_all("tr")
                for row in rows[1:]:
                    cols = row.find_all("td")
                    if len(cols) >= 5:
                        raw_name = cols[1].get_text(strip=True)
                        yelo_map["".join(c for c in remove_accents(raw_name).lower() if c.isalnum())] = cols[4].get_text(strip=True)
    except: pass
    return yelo_map

def parse_surface_elo_ratings(tour="atp"):
    """Elo по покрытиям. tour: atp | wta."""
    elo_url = ELO_URL_BY_TOUR.get(tour, ELO_URL_BY_TOUR["atp"])
    surface_elo_map = {}
    try:
        response = requests.get(elo_url, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            tables = soup.find_all("table")
            if tables:
                table = max(tables, key=lambda t: len(t.find_all("tr")))
                headers = [th.get_text(strip=True).lower().replace(".", "") for th in table.find_all("th")]
                p_idx, overall_idx, hard_idx, clay_idx, grass_idx = 1, 3, 5, 7, 9
                
                if "player" in headers:
                    try: p_idx = headers.index("player"); overall_idx = headers.index("elo")
                    except: pass
                    try: hard_idx = headers.index("helo"); clay_idx = headers.index("celo"); grass_idx = headers.index("gelo")
                    except: pass

                max_idx = max(p_idx, overall_idx, hard_idx, clay_idx, grass_idx)
                for row in table.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    if len(cols) > max_idx:
                        raw_name = cols[p_idx].get_text(strip=True)
                        norm_name = "".join(c for c in remove_accents(raw_name).lower() if c.isalnum())
                        surface_elo_map[norm_name] = {
                            "Overall": cols[overall_idx].get_text(strip=True),
                            "Hard": cols[hard_idx].get_text(strip=True),
                            "Clay": cols[clay_idx].get_text(strip=True),
                            "Grass": cols[grass_idx].get_text(strip=True)
                        }
    except: pass
    return surface_elo_map

def parse_matches(yelo_ratings, surface_elo_ratings, tour="atp"):
    """Афиша матчей. tour: atp | wta — страницы устроены одинаково.

    Аргумент со значением по умолчанию: основной бот зовёт функцию без него
    и продолжает работать по мужской афише ровно как раньше.
    """
    discovered_matches = {}
    try:
        url = TOUR_URL.get(tour, URL)
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200: return discovered_matches
        soup = BeautifulSoup(response.text, "html.parser")
        futures_slugs = set()
        all_match_links = []
        
        for a in soup.find_all("a"):
            href = a.get("href", "")
            text = a.get_text()
            if "match preview" in text.lower() or "-vs-" in href.lower() or "/atp/" in href or "/challenger/" in href:
                all_match_links.append(a)
                
        # Собираем приоритетные ссылки (Match preview), чтобы не промахиваться мимо карточек при DOM обходе
        slug_to_link = {}
        for link in all_match_links:
            href = link.get("href", "")
            slug = href.split("/")[-1].replace(".html", "").strip().lower() if href else ""
            if not slug: continue
            if slug not in slug_to_link or "match preview" in link.get_text().lower():
                slug_to_link[slug] = link
        
        for link in all_match_links:
            href = link.get("href", "")
            slug = href.split("/")[-1].replace(".html", "").strip().lower() if href else ""
            if not slug: continue
            is_future = False
            for parent in link.find_parents():
                p_text = parent.get_text(" ").lower()
                if any(k in p_text for k in ["futures", "m15", "m25", "challenger", "grand slam", "atp", "wta", "qualies"]):
                    if ("futures" in p_text or "m15" in p_text or "m25" in p_text) and "challenger" not in p_text and "grand slam" not in p_text:
                        is_future = True
                    break
            if is_future: futures_slugs.add(slug)

        script_tags = soup.find_all("script", type="application/ld+json")
        # Точные дата, время и раунд из structured data сайта. Ключ — slug
        # карточки h2h: у SportsEvent поля url нет, поэтому slug собираем из
        # имени события и сверяем с реальными ссылками на странице.
        ld_meta = {}
        for tag in script_tags:
            if not tag.string: continue
            try:
                payload = json.loads(tag.string)
            except Exception:
                continue
            for ev in _iter_ld_events(payload):
                name = (ev.get("name") or "").strip()
                for cand in _slug_candidates(name):
                    if cand in slug_to_link:
                        ld_meta[cand] = {"start": ev.get("startDate", ""),
                                         "round": _round_from_event_name(name)}
                        break

        for tag in script_tags:
            if not tag.string: continue
            try:
                data = json.loads(tag.string)
                items = list(_iter_ld_events(data))
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "SportsEvent":
                        match_name = item.get("name")
                        match_url = item.get("url", "")
                        json_date = item.get("startDate", "").split("T")[0]
                        # Без url slug пришлось бы лепить из названия, а по
                        # нему ключи не сходятся с остальным конвейером —
                        # такие матчи подберёт цикл по ссылкам ниже.
                        if not match_url: continue
                        if match_name:
                            json_slug = match_url.split("/")[-1].replace(".html", "").strip().lower() if match_url else ""
                            slug_key = json_slug if json_slug else match_name.strip().lower()
                            if slug_key in futures_slugs: continue
                            if "m15" in match_name.lower() or "m25" in match_name.lower() or "futures" in match_name.lower(): continue
                                
                            full_url = match_url if match_url.startswith("http") else f"{BASE_URL}{match_url}"
                            json_tournament = item.get("superEvent", {}).get("name", "") if isinstance(item.get("superEvent"), dict) else ""
                            
                            # Используем нашу заранее найденную ссылку карточки
                            html_link = slug_to_link.get(slug_key)
                            if not html_link:
                                html_link = soup.find("a", href=match_url) or soup.find("a", href=full_url)
                                
                            tournament = get_tournament_and_surface(html_link, fallback_tournament=json_tournament)
                            # Берем дату И время из structured data сайта (startDate), а не только дату
                            match_date = format_match_datetime(item.get("startDate", ""), fallback_date=json_date)
                            if match_date in ("", "Неизвестная дата"):
                                match_date = get_date_for_match(html_link, fallback_date=json_date)
                            match_round = get_round_for_match(html_link)
                            if match_round:
                                tournament = f"{tournament} · {match_round}"
                            discovered_matches[slug_key] = {"text": f'<a href="{full_url}">{match_name.strip()}</a>', "tournament": tournament, "date": match_date}
            except: continue

        for link in all_match_links:
            href = link.get("href", "")
            slug = href.split("/")[-1].replace(".html", "").strip().lower() if href else ""
            if not slug or slug in futures_slugs or slug in discovered_matches: continue
            if "-vs-" in slug:
                best_link = slug_to_link.get(slug, link)
                parts = slug.split("-vs-")
                player1 = " ".join([p.capitalize() for p in parts[0].split("-")])
                player2 = " ".join([p.capitalize() for p in parts[1].split("-")])
                full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                fb_tournament = get_tournament_and_surface(best_link)
                meta = ld_meta.get(slug, {})
                fb_date = _utc_stamp(meta.get("start", ""))
                if not fb_date:
                    fb_date = get_date_for_match(best_link)
                    fb_time = get_time_for_match(best_link)
                    # Дата из data-utc уже со временем — не приклеиваем второе
                    if fb_time and not re.search(r"\d{1,2}:\d{2}", fb_date):
                        fb_date = f"{fb_date} {fb_time}"
                fb_round = meta.get("round") or get_round_for_match(best_link)
                if fb_round: fb_tournament = f"{fb_tournament} · {fb_round}"
                discovered_matches[slug] = {"text": f'<a href="{full_url}">{player1} VS {player2}</a>', "tournament": fb_tournament, "date": fb_date}

        for slug, data in list(discovered_matches.items()):
            if "-vs-" in slug:
                parts = slug.split("-vs-")
                p1_norm = "".join(c for c in parts[0] if c.isalnum())
                p2_norm = "".join(c for c in parts[1] if c.isalnum())
                
                yelo1, yelo2 = yelo_ratings.get(p1_norm, "N/A"), yelo_ratings.get(p2_norm, "N/A")
                tour_name = data["tournament"]
                if "(Grass)" in tour_name: surf_key = "Grass"
                elif "(Clay)" in tour_name: surf_key = "Clay"
                elif "(Hard)" in tour_name: surf_key = "Hard"
                elif "(Carpet)" in tour_name: surf_key = "Carpet"
                else: surf_key = "Overall"
                
                if surf_key == "Carpet": data["text"] = f"{data['text']} 📊 <i>(yElo: {yelo1} vs {yelo2})</i>"
                else:
                    p1_profile, p2_profile = surface_elo_ratings.get(p1_norm, {}), surface_elo_ratings.get(p2_norm, {})
                    elo1 = p1_profile.get(surf_key, p1_profile.get("Overall", "N/A"))
                    elo2 = p2_profile.get(surf_key, p2_profile.get("Overall", "N/A"))
                    surf_label = f"{surf_key} Elo" if surf_key != "Overall" else "Elo"
                    data["text"] = f"{data['text']} 📊 <i>(yElo: {yelo1} vs {yelo2} | {surf_label}: {elo1} vs {elo2})</i>"
    except Exception as e: print(f"❌ Ошибка сети: {e}")
    return discovered_matches

def _name_score(our_name, their_name):
    """Насколько имя из нашей базы похоже на имя из линии букмекера.

    Фамилия одна и та же у обоих — значит, различать надо по имени: считаем
    совпавшие токены и отдельно совпадение первой буквы имени, потому что
    Pinnacle часто пишет 'M. Zverev', а у нас полное 'Mischa Zverev'.
    """
    ours = [t for t in re.split(r"[^a-zа-яё]+", remove_accents(our_name.lower())) if t]
    theirs = remove_accents((their_name or "").lower())
    score = 0
    for t in ours:
        if len(t) > 2 and t in theirs:
            score += 2
        elif t and re.search(rf"\b{re.escape(t[0])}\.?\b", theirs):
            score += 1
    return score


def check_odds_attribution(p1, p2, odds, elo_p1_prob=None):
    """Ищет признаки того, что кэфы привязаны не к тем игрокам.

    Ни одна из проверок не доказывает ошибку сама по себе — это сигналы, чтобы
    посмотреть глазами. Молча ставить на перепутанные кэфы хуже, чем получить
    лишнее предупреждение.
    """
    warns = []
    try:
        v1 = float(odds.get("p1")) if odds.get("p1") not in (None, "-") else None
        v2 = float(odds.get("p2")) if odds.get("p2") not in (None, "-") else None
    except (TypeError, ValueError):
        return ["не удалось разобрать кэфы на исход"]

    if v1 and v2:
        # маржа: у Pinnacle на теннис она обычно 2-4%, за пределами 0-12%
        # это либо кэфы из разных рынков, либо мусор в разборе
        margin = 1 / v1 + 1 / v2 - 1
        if not (-0.005 <= margin <= 0.12):
            warns.append(f"маржа {margin:+.1%} вне нормы (ждём 0-12%) — "
                         f"кэфы {v1} / {v2} могут быть из разных рынков")
        if abs(v1 - v2) < 0.02 and v1 < 1.5:
            warns.append(f"оба кэфа почти равны и низкие ({v1} / {v2}) — "
                         "похоже, одна и та же цена записана дважды")

    if v1 and v2 and elo_p1_prob is not None:
        market_p1 = (1 / v1) / (1 / v1 + 1 / v2)
        gap = market_p1 - elo_p1_prob
        if abs(gap) > 0.35:
            side = "выше" if gap > 0 else "ниже"
            warns.append(
                f"рынок даёт {p1} {market_p1:.0%}, наш Elo — {elo_p1_prob:.0%} "
                f"({side} на {abs(gap):.0%}). Либо реальная ценность, либо кэфы "
                "привязаны наоборот — сверьте с сайтом до ставки")
    return warns


def format_odds_attribution(p1, p2, odds, surface_elo_ratings=None, surface=None):
    """Строка про привязку кэфов под список ставок. Пусто, если всё в порядке."""
    elo_prob = None
    try:
        if surface_elo_ratings:
            # get_player_elo отдаёт (Elo по покрытию, общий Elo, подпись);
            # берём покрытие, если оно есть, иначе общий
            s1, o1, _ = get_player_elo(p1, surface_elo_ratings, surface or "")
            s2, o2, _ = get_player_elo(p2, surface_elo_ratings, surface or "")
            e1, e2 = (s1 or o1), (s2 or o2)
            if e1 and e2:
                elo_prob = 1 / (1 + 10 ** ((e2 - e1) / 400))
    except Exception:
        elo_prob = None

    warns = check_odds_attribution(p1, p2, odds, elo_prob)
    if not warns:
        return ""
    body = "\n".join(f"• {w}" for w in warns)
    return f"\n\n⚠️ <b>Проверьте привязку кэфов:</b>\n{body}"


def _device_uuid():
    """Идентификатор устройства в формате Pinnacle: 8-8-8-8 hex.

    Постоянный для установки: генерируется один раз и кладётся рядом. Новый
    UUID на каждый запрос выглядел бы со стороны как поток разных устройств
    с одного адреса — лишний повод присмотреться к нам.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tennis_parser", ".device_uuid")
    try:
        if os.path.exists(path):
            got = open(path, encoding="utf-8").read().strip()
            if got:
                return got
    except OSError:
        pass
    import uuid as _uuid
    h = _uuid.uuid4().hex
    val = "-".join(h[i:i + 8] for i in range(0, 32, 8))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(val)
    except OSError:
        pass
    return val


# Пакетная загрузка котировок: один запрос отдаёт рынки сразу по всем матчам
# вида спорта. Раньше на каждый матч уходило по два обращения — три десятка
# за круг, за что и прилетали блокировки.
PIN_SPORT_TENNIS = 33


def _bulk_markets(scraper, wanted=None):
    """{matchup_id: [рынки]} по всему теннису. Пусто, если не вышло.

    wanted — множество id (строками), ради которых пакет и качается. Как
    только они покрыты, перебор кандидатов прекращается: каждый следующий
    стоит отдельного слота в общем ограничителе (MIN_INTERVAL, 20 с по
    умолчанию). Без этого ручной запрос по одному матчу платил все три
    паузы — до минуты ожидания на пустом месте.
    """
    from tennis_parser import pinnacle_guard as _pg

    base = f"https://guest.api.arcadia.pinnacle.com/0.1/sports/{PIN_SPORT_TENNIS}"
    # Порядок важен. highlighted — это витрина сайта, всего несколько матчей;
    # на 118 матчах в линии она покрывала пять. Сначала пробуем варианты,
    # отдающие всю линию, и только потом откатываемся на витрину.
    CANDIDATES = [
        f"{base}/markets/straight?primaryOnly=false",
        f"{base}/markets/straight",
        f"{base}/markets/highlighted/straight?primaryOnly=false",
    ]

    def _fetch(url):
        if not _pg.wait_turn():
            return None
        try:
            r = scraper.get(url, verify=False, timeout=20)
        except Exception:
            return None
        if r.status_code == 401:
            log.warning("Pinnacle: 401 на пакетной загрузке — проверьте PIN_API_KEY")
            return None
        if r.status_code in (403, 429, 503):
            _pg.report_block()
            return None
        if r.status_code != 200:
            log.info("пакет %s: HTTP %s", url.rsplit("/", 1)[-1], r.status_code)
            return None
        try:
            data = r.json()
        except Exception:
            return None
        return data if isinstance(data, list) else data.get("markets", [])

    def download():
        best, best_ids = [], set()
        for url in CANDIDATES:
            got = _fetch(url)
            if not got:
                continue
            ids = {str(m.get("matchupId") or m.get("matchup_id"))
                   for m in got if isinstance(m, dict)}
            ids.discard("None")
            log.info("пакет %s -> %d рынков по %d матчам",
                     url.rsplit("/", 2)[-1][:40], len(got), len(ids))
            if len(got) > len(best):
                best, best_ids = got, ids
            # Нужные матчи уже в пакете — дальше не ходим. Порога в 20 матчей
            # для этого мало: на маленькой афише он не набирается никогда, и
            # запрос по одному матчу каждый раз перебирал всех кандидатов.
            if wanted and wanted <= best_ids:
                break
            # если накрыло заметную часть линии, дальше не ходим
            if len(ids) >= 20:
                break
        return best

    markets = _pg.get_bulk(download)
    grouped = {}
    for m in markets or []:
        mid = m.get("matchupId") or m.get("matchup_id")
        if mid is not None:
            grouped.setdefault(str(mid), []).append(m)
    return grouped


def get_pinnacle_odds(p1_key, p2_key, is_manual=False):
    try:
        scraper = requests.Session()
        # API Pinnacle требует ключ, который их собственный веб-фронтенд
        # подставляет в каждый запрос. Без него приходит 401 Unauthorized —
        # и это НЕ бан по IP, как легко подумать: с любого адреса будет то же
        # самое. Ключ публичный (лежит в JS их сайта) и со временем меняется,
        # поэтому вынесен в переменную: как обновить — см. README.
        # Заголовки повторяют то, что шлёт сам сайт Pinnacle — снято из
        # DevTools. Отклонения караются 401: браузер для такого GET НЕ шлёт
        # Origin, а мы слали, и это единственное, чем наш набор отличался.
        # Ключ берётся ТОЛЬКО из окружения. Раньше здесь лежало значение по
        # умолчанию, и всё работало на нём: в .env ключа не было ни на сервере,
        # ни локально. Держать учётные данные в коде нельзя — файл уходит в
        # репозиторий, а ключ вместе с ним. Пустое значение не молчит: без него
        # Pinnacle отвечает 401, и причину надо назвать сразу, иначе она
        # выглядит как бан по IP (см. комментарий выше).
        _api_key = os.environ.get("PIN_API_KEY", "")
        if not _api_key:
            log.error("PIN_API_KEY не задан — Pinnacle ответит 401. "
                      "Положите ключ в .env, см. .env.example")
        scraper.headers.update({
            "User-Agent": os.environ.get("PIN_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 "
                "Edg/151.0.0.0"),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": _api_key,
            "X-Device-UUID": os.environ.get("PIN_DEVICE_UUID", _device_uuid()),
            "Referer": "https://www.pinnacle.com/",
            "sec-ch-ua": ('"Not=A?Brand";v="99", "Microsoft Edge";v="151", '
                          '"Chromium";v="151"'),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
        # Прокси только для Pinnacle: если API забанил IP сервера, менять
        # выход в сеть для всего бота незачем — tennisratio и TennisExplorer
        # работают нормально и лишний посредник им только вредит.
        from tennis_parser import pinnacle_guard as _pgp
        _proxies = _pgp.proxies()
        if _proxies:
            scraper.proxies.update(_proxies)
        # Список матчапов один на все матчи дня, а качался он заново под
        # каждый — обход афиши из 17 матчей давал сотни обращений подряд и
        # приводил к блокировке. Теперь один запрос на TTL, общий для обоих
        # процессов через файл на диске.
        from tennis_parser import pinnacle_guard as _pg

        # Проверяем только отступ: саму паузу берём внутри загрузки, иначе
        # каждое попадание в кэш всё равно стоило бы полного интервала
        if _pg.cooldown_left() > 0:
            left = _pg.cooldown_left()
            msg = (f"⚠️ Pinnacle временно недоступен: отступ после блокировки, "
                   f"ещё {left / 60:.0f} мин.")
            if is_manual:
                return {"error": msg, "cooldown": left}
            return None

        def _get_matchup_list(session, url, timeout=15):
            """(список, код ответа, что пошло не так).

            Раньше здесь стоял голый `except: pass`, и любая неудача —
            таймаут, отвалившийся прокси, не-JSON в ответе — выглядела
            снаружи одинаково: пустой список. Дальше срабатывал запасной
            путь «беру просроченный кэш», и бот сутками работал на вчерашнем
            списке матчей, не сказав об этом ни слова. Причину теперь видно
            в логе.
            """
            try:
                r = session.get(url, verify=False, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                return None, None, f"{type(exc).__name__}: {str(exc)[:160]}"
            if r.status_code != 200:
                return None, r.status_code, f"HTTP {r.status_code}"
            try:
                rj = r.json()
            except Exception as exc:  # noqa: BLE001
                return None, 200, f"ответ не JSON: {str(exc)[:100]}"
            data = rj if isinstance(rj, list) else rj.get("matchups", rj.get("data", []))
            return (data or []), 200, ""

        def _download_matchups():
            if not _pg.wait_turn():
                log.warning("Pinnacle: идёт отступ — список матчапов "
                            "не запрашиваю")
                return []
            url = ("https://guest.api.arcadia.pinnacle.com/0.1/sports/33/"
                   "matchups?all=false")
            data, code, why = _get_matchup_list(scraper, url)

            if data is None and _proxies:
                # Прокси — самое хрупкое звено: он чужой и может молча
                # отваливаться, пока прямой доступ работает. Пробуем в обход
                # ОДИН раз и говорим об этом в лог: без записи разница между
                # «Pinnacle не отвечает» и «прокси не отвечает» не видна,
                # а лечится она по-разному.
                log.warning("Pinnacle через прокси не ответил (%s) — "
                            "пробую напрямую", why)
                direct = requests.Session()
                direct.headers.update(scraper.headers)
                direct.trust_env = False
                data2, code2, why2 = _get_matchup_list(direct, url)
                if data2 is not None:
                    log.warning("Pinnacle: напрямую работает, а через прокси "
                                "нет — проверьте PIN_PROXY (%s)", why)
                    data, code, why = data2, code2, why2
                else:
                    why = f"через прокси {why}; напрямую {why2}"
                    code = code or code2

            if data is None:
                if code == 401:
                    # Не бан: не принят ключ API. Отступ тут только навредит —
                    # ждать бессмысленно, надо обновить PIN_API_KEY.
                    log.error("Pinnacle: 401, ключ API не принят. "
                              "Обновите PIN_API_KEY (см. README).")
                elif code in (403, 429, 503):
                    log.error("Pinnacle ответил %s — считаю это блокировкой", code)
                    _pg.report_block()
                else:
                    log.error("Pinnacle: список матчапов не скачался — %s. "
                              "Проверка: check_pinnacle.py", why)
                return []

            if not data:
                log.warning("Pinnacle: HTTP 200, но матчей в списке ноль. "
                            "Это не сбой связи — линии просто нет.")
            return data

        m_data = _pg.get_matchups(_download_matchups)

        if not m_data:
            leagues_url = "https://guest.api.arcadia.pinnacle.com/0.1/sports/33/leagues?all=false"
            try:
                resp = scraper.get(leagues_url, verify=False, timeout=10)
                if resp.status_code == 200:
                    leagues_json = resp.json()
                    if isinstance(leagues_json, dict):
                        leagues_json = leagues_json.get("leagues", leagues_json.get("data", leagues_json.get("result", [])))
                    
                    league_ids = [str(lg.get("id")) for lg in leagues_json if isinstance(lg, dict) and lg.get("id")]
                    # Обход всех лиг — это десятки запросов подряд, ровно то,
                    # за что и прилетает блокировка. Берём только первые
                    # несколько и с паузой между ними.
                    league_ids = league_ids[:int(os.environ.get("PIN_MAX_LEAGUES", 8))]
                    for lg_id in league_ids:
                        if not _pg.wait_turn():
                            break
                        lg_url = f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{lg_id}/matchups"
                        lg_resp = scraper.get(lg_url, verify=False, timeout=5)
                        if lg_resp.status_code == 200:
                            lg_json = lg_resp.json()
                            if isinstance(lg_json, list): m_data.extend(lg_json)
                            elif isinstance(lg_json, dict): m_data.extend(lg_json.get("matchups", lg_json.get("data", [])))
            except Exception as exc:  # noqa: BLE001
                # Тоже был голый except: запасной путь мог падать молча,
                # и выглядело это как «лиг тоже нет»
                log.warning("Pinnacle: обход по лигам не удался — %s: %s",
                            type(exc).__name__, str(exc)[:160])
        
        if not m_data:
            # Отступ здесь НЕ ставим. Список бывает пустым по нескольким
            # причинам, и почти все они не про блокировку: идёт отступ и
            # wait_turn отказал, не принят ключ, сеть моргнула. Раньше эта
            # ветка ставила отступ вслепую — и получался самоподдерживающийся
            # цикл: отступ -> пустой список -> отступ вдвое длиннее.
            # Решение о блокировке принимается только там, где виден код
            # ответа HTTP.
            left = _pg.cooldown_left()
            if left > 0:
                msg = (f"⚠️ Pinnacle недоступен: идёт отступ после блокировки, "
                       f"ещё {left / 60:.0f} мин.")
            else:
                msg = ("⚠️ Список матчей от Pinnacle пуст. Если это повторяется, "
                       "проверьте ключ: check_pinnacle.py")
            log.warning(msg)
            if is_manual:
                return {"error": msg, "cooldown": left}
            return None
        _pg.report_ok()
        
        target_matchups = []
        p1_last = p1_key.split()[-1].lower()
        p2_last = p2_key.split()[-1].lower()

        for m in m_data:
            if m.get("type") != "matchup" or "participants" not in m: continue
            parts = m.get("participants", [])
            if len(parts) < 2: continue
            
            name1, name2 = parts[0].get("name", "").lower(), parts[1].get("name", "").lower()
            league_name = m.get("league", {}).get("name", "").lower()
            units = m.get("units", "").lower() if m.get("units") else ""
            
            forward = p1_last in name1 and p2_last in name2
            backward = p1_last in name2 and p2_last in name1
            if forward and backward:
                # обе ориентации подошли — у игроков одна фамилия (братья, частая
                # азиатская фамилия). Раньше молча брался первый вариант, и кэфы
                # могли уехать не тому игроку. Пробуем развести по имени.
                fwd = _name_score(p1_key, name1) + _name_score(p2_key, name2)
                bwd = _name_score(p1_key, name2) + _name_score(p2_key, name1)
                if fwd == bwd:
                    print(f"⚠️ Привязка кэфов неоднозначна: {p1_key} / {p2_key} "
                          f"против '{name1}' / '{name2}' — матч пропущен")
                    continue
                forward, backward = fwd > bwd, bwd > fwd
            if forward: target_matchups.append((m.get("id"), False, name1, league_name, units))
            elif backward: target_matchups.append((m.get("id"), True, name2, league_name, units))
                
        if not target_matchups:
            if is_manual: return {"error": f"🤷‍♂️ Матч не найден в линии Pinnacle."}
            return None

        result = {"p1": "-", "p2": "-", "h_sets": "-", "h_games": "-", "total_sets": "-", "error": None}
        bulk = _bulk_markets(
            scraper,
            {str(t[0]) for t in target_matchups if t[0] is not None})

        for match_id, is_reversed, main_name, league_name, units in target_matchups:
            is_sets = "sets" in units or "(sets)" in main_name or "(sets)" in league_name

            # Сначала смотрим в пакет: он один на весь теннис и уже скачан.
            # Поштучный запрос остаётся запасным путём — для матчей, которых
            # в пакете не оказалось (он отдаёт не всё подряд).
            markets = bulk.get(str(match_id))
            try:
                if markets is None:
                    if not _pg.wait_turn():
                        break
                    mk_url = f"https://guest.api.arcadia.pinnacle.com/0.1/matchups/{match_id}/markets/straight"
                    mk_resp = scraper.get(mk_url, verify=False, timeout=10)
                    if mk_resp.status_code != 200: continue
                    markets = mk_resp.json()
                if not isinstance(markets, list): continue
                
                for market in markets:
                    m_type, period = market.get("type"), market.get("period", 0)
                    if period != 0: continue 
                    prices = market.get("prices", [])
                    
                    if m_type == "moneyline":
                        for price in prices:
                            des, val = price.get("designation"), am_to_dec(price.get("price"))
                            if val == "-": continue
                            if (des == "home" and not is_reversed) or (des == "away" and is_reversed): result["p1"] = val
                            elif (des == "away" and not is_reversed) or (des == "home" and is_reversed): result["p2"] = val
                                    
                    elif m_type == "spread":
                        for price in prices:
                            pts, val = price.get("points"), am_to_dec(price.get("price"))
                            if pts is None or val == "-": continue
                            des = price.get("designation", "")
                            prefix = ("П1" if not is_reversed else "П2") if des == "home" else ("П2" if not is_reversed else "П1")
                            formatted = f"{prefix} {f'+{pts}' if pts > 0 else pts} ({val})"
                            
                            target_key = "h_sets" if is_sets else "h_games"
                            if result[target_key] == "-": result[target_key] = formatted
                            elif formatted not in result[target_key]: result[target_key] += f" | {formatted}"

                    elif m_type == "total" and is_sets:
                        for price in prices:
                            pts, val = price.get("points"), am_to_dec(price.get("price"))
                            if pts is None or val == "-": continue
                            des = price.get("designation", "")
                            prefix = "ТБ" if des == "over" else ("ТМ" if des == "under" else "")
                            if not prefix: continue
                            formatted = f"{prefix} {pts} ({val})"
                            
                            if result["total_sets"] == "-": result["total_sets"] = formatted
                            elif formatted not in result["total_sets"]: result["total_sets"] += f" | {formatted}"
            except Exception as exc:  # noqa: BLE001
                # Был голый except: pass. Любая ошибка разбора рынков молча
                # превращалась в «коэффициенты отсутствуют, линия закрыта» —
                # то есть в диагноз, который уводит в сторону от настоящей
                # причины. Матч по-прежнему пропускаем, но не бесследно.
                log.warning("Pinnacle: рынки матча %s не разобрались — %s: %s",
                            match_id, type(exc).__name__, str(exc)[:160])

        if result["p1"] == "-" and result["p2"] == "-" and result["h_sets"] == "-" and result["h_games"] == "-" and result["total_sets"] == "-":
            if is_manual: return {"error": f"⚠️ ID матча найден, но коэффициенты отсутствуют (возможно, линия закрыта)."}
            return None
        return result
    except Exception as e:
        if is_manual: return {"error": f"⚠️ Ошибка выполнения Pinnacle: {e}"}
        return None

def parse_odds_from_string(odds_string, target_handicap, is_fav_p1):
    if odds_string == "-": return None
    target_prefix = f"П1 {target_handicap} (" if is_fav_p1 else f"П2 {target_handicap} ("
    parts = odds_string.split("|")
    for part in parts:
        if target_prefix in part:
            match = re.search(r'\(([\d.]+)\)', part)
            if match: return float(match.group(1))
    return None

def parse_total_odds(odds_string, target_line, side="ТБ"):
    if odds_string == "-": return None
    target_prefix = f"{side} {target_line} ("
    parts = odds_string.split("|")
    for part in parts:
        part = part.strip()
        if part.startswith(target_prefix):
            match = re.search(r'\(([\d.]+)\)', part)
            if match: return float(match.group(1))
    return None

def calculate_potential_bets(p1, p2, odds, tournament=""):
    try:
        val1 = float(odds['p1']) if odds['p1'] != "-" else 999.0
        val2 = float(odds['p2']) if odds['p2'] != "-" else 999.0
    except: return []
        
    if val1 == 999.0 and val2 == 999.0: return []
    
    is_fav_p1 = val1 <= val2
    fav_name = "П1" if is_fav_p1 else "П2"
    fav_odds = val1 if is_fav_p1 else val2

    # обе цены и подразумеваемая вероятность кладутся в ставку: без них
    # задним числом нельзя проверить, к тем ли игрокам привязаны кэфы —
    # проигрыш выглядит одинаково при любой привязке
    market = {}
    if val1 != 999.0 and val2 != 999.0:
        inv1, inv2 = 1 / val1, 1 / val2
        market = {"odds_p1": val1, "odds_p2": val2,
                  "implied_p1": round(inv1 / (inv1 + inv2), 4),
                  "margin": round(inv1 + inv2 - 1, 4)}

    new_bets = []
    ml = {"type": "Moneyline", "prediction": fav_name, "odds": fav_odds,
          "stake": BET_AMOUNT, "status": "pending", "profit": 0}
    ml.update(market)
    new_bets.append(ml)
    
    # Линия тотала зависит от формата. Смысл ставки один и тот же — «фаворит
    # не пройдёт всухую», — но выражается он разной линией: в трёх сетах это
    # ТБ 2.5, в пяти ТБ 3.5. Раньше линия была зашита как «2.5», и на мужском
    # «Шлеме» parse_total_odds не находил её в линии Pinnacle (там 3.5 и 4.5)
    # и молча возвращал None: тотал не предлагался вовсе.
    # Расчёт ставки менять не нужно — line_val в resolve_match вытаскивается
    # из текста прогноза регуляркой, как и в CSV, панели и публикации.
    ts_line = "2.5" if match_best_of(tournament) == 3 else "3.5"
    total_odds = parse_total_odds(odds.get('total_sets', '-'), ts_line, "ТБ")
    if total_odds:
        new_bets.append({"type": "Total Sets", "prediction": f"ТБ {ts_line} (сеты)", "odds": total_odds, "stake": BET_AMOUNT, "status": "pending", "profit": 0})
        
    return new_bets

def save_approved_bets(match_id, date_str, tournament, match_name, p1, p2, bets):
    db = load_db()
    for b in db["bets"]:
        if b["match_id"] == match_id: return False 
        
    match_entry = {
        "match_id": match_id, "date": date_str, "tournament": tournament,
        "match": match_name, "player1": p1, "player2": p2, "added_ts": time.time(),
        "resolved": False, "score": "", "games_p1": 0, "games_p2": 0, "sets_p1": 0, "sets_p2": 0, "bets": bets
    }
    db["bets"].append(match_entry)
    save_db(db)
    
    for b in bets: 
        log_to_csv({
            "tournament": tournament, "date": date_str, "match": match_name, 
            "prediction": b["prediction"], "odds": b["odds"]
        })
    return True

TE_BASE = "https://www.tennisexplorer.com"
SURFACE_CACHE_FILE = "surface_cache.json"
SURFACE_COLORS = {"clay": "#E8833A", "hard": "#3D8FD1", "grass": "#3AA757", "indoors": "#8E7CC3", "carpet": "#8E7CC3", "": "#5A6B7A"}
SURFACE_RU = {"clay": "грунт", "hard": "хард", "grass": "трава", "indoors": "зал", "carpet": "ковёр", "": "?"}

def load_surface_cache():
    if os.path.exists(SURFACE_CACHE_FILE):
        try:
            with open(SURFACE_CACHE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {}

def save_surface_cache(cache):
    try:
        with open(SURFACE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить кэш покрытий: {e}")

def detect_surface_from_text(text):
    t = (text or "").lower()
    if "clay" in t or "antuka" in t: return "clay"
    if "grass" in t: return "grass"
    if "carpet" in t: return "carpet"
    if "indoor" in t: return "indoors"
    if "hard" in t: return "hard"
    return ""

def get_tournament_surface(tournament_name, tournament_href, cache):
    """Определяет покрытие турнира. TennisExplorer не пишет покрытие в строке матча,
    поэтому один раз ходим на страницу турнира и кэшируем результат в файл."""
    key = (tournament_name or tournament_href or "").strip().lower()
    if not key: return ""
    if key in cache: return cache[key]

    surface = detect_surface_from_text(tournament_name)
    if not surface and tournament_href:
        try:
            url = tournament_href if tournament_href.startswith("http") else f"{TE_BASE}{tournament_href}"
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                head = soup.find(["h1", "h2"])
                surface = detect_surface_from_text(head.get_text(" ", strip=True) if head else "")
                if not surface:
                    surface = detect_surface_from_text(soup.get_text(" ", strip=True)[:3000])
        except Exception as e:
            print(f"⚠️ Не удалось определить покрытие для {tournament_name}: {e}")

    cache[key] = surface
    return surface

def find_te_player_url(player_name):
    """Находит страницу игрока на TennisExplorer. Сначала пробуем прямой slug по фамилии,
    затем — поиск по сайту (у однофамильцев slug вида lastname-firstname или lastname2)."""
    parts = [p for p in re.split(r'[\s\-]+', player_name.strip()) if p]
    if not parts: return None
    last = remove_accents(parts[-1].lower())
    first = remove_accents(parts[0].lower())

    candidates = [f"{TE_BASE}/player/{last}/", f"{TE_BASE}/player/{last}-{first}/"]
    for url in candidates:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200: continue
            soup = BeautifulSoup(resp.text, "html.parser")
            head = soup.find(["h1", "h3"])
            title = remove_accents((head.get_text(" ", strip=True) if head else "").lower())
            if last in title and (first in title or len(parts) == 1):
                return url
        except Exception: pass

    try:
        resp = requests.get(f"{TE_BASE}/list-players/", params={"search-text-pl": player_name}, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                if "/player/" in a["href"]:
                    name_txt = remove_accents(a.get_text(" ", strip=True).lower())
                    if last in name_txt:
                        return a["href"] if a["href"].startswith("http") else f"{TE_BASE}{a['href']}"
    except Exception as e:
        print(f"⚠️ Поиск игрока {player_name} на TennisExplorer не удался: {e}")
    return None

def _parse_score_sets(score_text):
    """Разбирает счет в список сетов [(геймы1, геймы2, тайбрейк или None)].
    TennisExplorer пишет тайбрейк слитно: '7-64' = 7-6(4), '67-7' = 6(7)-7.
    Отличаем такие записи по правдоподобности: геймов в сете больше 15 не бывает,
    поэтому '64' — это не 64 гейма, а 6 геймов и тайбрейк 4. Без этого выигранный
    сет на тайбрейке засчитывался как проигранный."""
    sets = []
    for part in re.split(r'[,\s]+', (score_text or "").strip()):
        if not part: continue
        m = re.fullmatch(r'(\d{1,2})-(\d{1,2})\((\d{1,2})\)', part)
        if m:
            sets.append((int(m.group(1)), int(m.group(2)), m.group(3)))
            continue
        m = re.fullmatch(r'(\d{1,3})-(\d{1,3})', part)
        if not m: continue
        a_raw, b_raw = m.group(1), m.group(2)
        a, b, tb = int(a_raw), int(b_raw), None
        if b > 15 and len(b_raw) >= 2:
            b, tb = int(b_raw[0]), b_raw[1:]
        elif a > 15 and len(a_raw) >= 2:
            a, tb = int(a_raw[0]), a_raw[1:]
        sets.append((a, b, tb))
    return sets

def format_score(score_text, flip=False):
    """Приводит счет к читаемому виду ('7-6(4), 2-6, 6-4').
    flip=True разворачивает счет от лица второго игрока — нужно, когда игрок
    в исходной строке был справа, иначе проигранный матч выглядит как выигранный."""
    sets = _parse_score_sets(score_text)
    if not sets:
        return score_text or ""
    out = []
    for a, b, tb in sets:
        if flip: a, b = b, a
        out.append(f"{a}-{b}({tb})" if tb else f"{a}-{b}")
    return ", ".join(out)

def _strip_match_summary(sets, retired=False):
    """Убирает итог по сетам, который TennisExplorer ставит перед самими сетами.

    Счет приходит как '2-1, 4-6, 6-4, 7-6(3)': первый токен — это 2:1 по сетам,
    а не сет со счетом 2-1. Без этой чистки он считался за отдельный выигранный
    сет, из-за чего матч 2-0 превращался в три сета и тотал ТБ 2.5 не мог
    проиграть в принципе.

    Отбрасываем только когда токен действительно похож на итог: обе цифры не
    больше трех, тайбрейка нет, и посчитанные по остальным сетам победы дают
    ровно его. При отказе итог не сойдется (последний сет недоигран), поэтому
    там достаточно формы токена и хотя бы одного настоящего сета следом.
    """
    if len(sets) < 2:
        return sets
    a, b, tb = sets[0]
    rest = sets[1:]
    if tb is not None or max(a, b) > 3 or a + b == 0:
        return sets
    s1 = sum(1 for x, y, _ in rest if x > y)
    s2 = sum(1 for x, y, _ in rest if y > x)
    if (s1, s2) == (a, b):
        return rest
    if retired and any(max(x, y) >= 5 for x, y, _ in rest):
        return rest
    return sets


def _te_awarded(res1, res2):
    """Кому присуждён матч по колонке result. '' — обычный доигранный.

    У присуждённого матча TennisExplorer ставит в result 1:0, единица у
    прошедшего дальше. Пометки «ret.» в разметке при этом может не быть
    вовсе — у Garin — Samuel 27.08.2026 её не было, и снятие не
    распознавалось: итог 1:0 уезжал в список сетов четвёртым «сетом», а
    тотал по нему считался выигрышем вместо возврата.

    Обычный доигранный матч дать 1:0 не может: победа это 2-0, 2-1 или
    3-x. Поэтому признак однозначный. Так же его читает `_awarded()` в
    обходчике.
    """
    if (res1, res2) == (1, 0):
        return "p1"
    if (res1, res2) == (0, 1):
        return "p2"
    return ""


def _completed_sets(sets):
    """Сколько сетов доиграно до конца.

    Условие Pinnacle: ставка на победителя при снятии СТОИТ, только если
    сыгран хотя бы один полный сет. Снятие раньше аннулирует вообще всё.
    """
    n = 0
    for a, b, _ in sets:
        hi, lo = max(a, b), min(a, b)
        if (hi == 6 and lo <= 4) or (hi == 7 and lo in (5, 6)) or hi >= 10:
            n += 1
    return n


def parse_match_result(score_text):
    """Единственное место, где строка счета превращается в цифры для расчета.

    Возвращает (sets_p1, sets_p2, games_p1, games_p2, sets) — все от лица
    первого игрока. Тайбрейки '7-64' разбираются как 7-6(4), а не 7 против 64:
    раньше выигранный на тайбрейке сет уезжал сопернику и мог перевернуть исход
    матча.
    """
    retired = "ret" in (score_text or "").lower()
    sets = _strip_match_summary(_parse_score_sets(score_text), retired=retired)
    s1 = s2 = g1 = g2 = 0
    for a, b, _ in sets:
        g1 += a
        g2 += b
        if a > b: s1 += 1
        elif b > a: s2 += 1
    return s1, s2, g1, g2, sets


def flip_score(score_text):
    """Переворачивает счёт: '6-7(6),6-3,6-3' -> '7-6(6),3-6,3-6'.

    Нужно, когда источник перечислил игроков в обратном к нашему порядке.
    Разбор идёт через parse_match_result, поэтому итоговый токен по сетам
    и склеенные тайбрейки обрабатываются правильно, а не переставляются
    как попало вокруг дефиса.
    """
    _, _, _, _, sets = parse_match_result(score_text)
    if not sets:
        return score_text
    out = ",".join(f"{b}-{a}({tb})" if tb else f"{b}-{a}"
                   for a, b, tb in sets)
    # Пометку снятия переносим: она собирается из сетов заново, и без этого
    # у матча с обратным порядком игроков «ret.» терялась — снятие переставало
    # определяться (`is_retired` смотрит именно на строку), и тотал считался
    # по счёту вместо возврата.
    if "ret" in (score_text or "").lower():
        out += " ret."
    return out


def pretty_score(score_text, with_tiebreak=False):
    """Счёт для отчётов: '4-6, 6-4, 7-6' вместо сырого '2-1,4-6,6-4,7-63'.

    Убирает итог по сетам, который TennisExplorer ставит первым токеном, и
    склеенный разбор тайбрейка. Тайбрейк по умолчанию не показываем: в таблице
    он только мешает, а сетовый счёт от него не меняется.
    """
    _, _, _, _, sets = parse_match_result(score_text)
    if not sets:
        return score_text or ""
    out = []
    for a, b, tb in sets:
        out.append(f"{a}-{b}({tb})" if (with_tiebreak and tb) else f"{a}-{b}")
    return ", ".join(out)


def _sets_from_score(score_text):
    """Считает выигранные сеты по строке счета вида '6-4, 3-6, 7-5' (с тайбрейками '7-64')."""
    s1, s2, _, _, _ = parse_match_result(score_text)
    return s1, s2

def parse_te_last_matches(player_name, limit=10, cache=None):
    """Возвращает последние сыгранные матчи игрока с TennisExplorer:
    [{date, tournament, surface, opponent, round, score, won}]"""
    cache = cache if cache is not None else {}
    url = find_te_player_url(player_name)
    if not url:
        return [], f"Игрок {player_name} не найден на TennisExplorer"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return [], f"TennisExplorer вернул {resp.status_code} для {player_name}"
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        return [], f"Ошибка загрузки профиля {player_name}: {e}"

    p_last = remove_accents(player_name.split()[-1].lower())
    matches = []
    current_tournament, current_href = "", ""

    for tr in soup.find_all("tr"):
        head_cell = tr.find(["th"]) or (tr.find("td", class_=re.compile(r'\bhead\b|\bt-name\b')) if tr.find("td") else None)
        row_text = tr.get_text(" ", strip=True)

        link = tr.find("a", href=re.compile(r'/(atp|wta)-(men|women)/|/tournament/'))
        if link and not re.search(r'\d{1,2}\.\d{1,2}\.', row_text):
            current_tournament = link.get_text(" ", strip=True)
            current_href = link.get("href", "")
            continue
        if head_cell and not re.search(r'\d{1,2}\.\d{1,2}\.', row_text):
            txt = head_cell.get_text(" ", strip=True)
            if txt and len(txt) < 60:
                current_tournament = txt
                a = head_cell.find("a", href=True)
                current_href = a["href"] if a else ""
            continue

        tds = tr.find_all("td")
        if len(tds) < 3: continue
        date_m = re.search(r'(\d{1,2}\.\d{1,2}\.(?:\d{2,4})?)', tds[0].get_text(" ", strip=True))
        if not date_m: continue

        pair_txt, score_txt, round_txt = "", "", ""
        for td in tds[1:]:
            t = td.get_text(" ", strip=True)
            if not t: continue
            # Счет читаем БЕЗ разделителя: очки тайбрейка лежат в <sup>, и при обычном
            # get_text(" ") строка '7-6<sup>3</sup>, 6-2' превращалась в '7-6 3 , 6-2',
            # из-за чего в карточку попадал только первый сет.
            compact = re.sub(r'\s+', '', td.get_text("", strip=True))
            # Порядок ячеек у TennisExplorer плавает (бывают доп. колонки: квалификация, кэфы),
            # поэтому определяем каждую ячейку по её содержимому, а не по позиции.
            if not pair_txt and " - " in t and not re.match(r'^[\d\s\-,()]+$', t):
                pair_txt = t
            elif not score_txt and re.fullmatch(r'\d{1,2}-\d{1,2}\d{0,2}(?:,\d{1,2}-\d{1,2}\d{0,2})*', compact):
                score_txt = compact
            elif not round_txt and re.fullmatch(r'(F|SF|QF|R\d{1,3}|[1-4]R|Q\d?|BR|RR)', t.strip(), re.IGNORECASE):
                round_txt = t.strip().upper()
        if not pair_txt or " - " not in pair_txt: continue
        if not score_txt:
            # Счет мог оказаться в общей ячейке строки — ищем в тексте без пробелов
            row_compact = re.sub(r'\s+', '', tr.get_text("", strip=True))
            sm = re.search(r'\d{1,2}-\d{1,2}\d{0,2}(?:,\d{1,2}-\d{1,2}\d{0,2})*', row_compact)
            if sm: score_txt = sm.group(0)

        home, away = [x.strip() for x in pair_txt.split(" - ", 1)]
        is_home = p_last in remove_accents(home.lower())
        opponent = away if is_home else home
        s1, s2 = _sets_from_score(score_txt)
        if s1 == s2 == 0: continue
        won = (s1 > s2) if is_home else (s2 > s1)

        surface = get_tournament_surface(current_tournament, current_href, cache)
        matches.append({
            "date": date_m.group(1).rstrip("."), "tournament": current_tournament or "—",
            "surface": surface, "opponent": opponent, "round": round_txt,
            "score": format_score(score_txt, flip=not is_home), "won": won
        })
        if len(matches) >= limit: break

    return matches, None if matches else f"Не удалось разобрать матчи {player_name}"

def parse_te_h2h(p1, p2, cache=None):
    """Возвращает очные встречи двух игроков с TennisExplorer."""
    cache = cache if cache is not None else {}
    try:
        resp = requests.get(f"{TE_BASE}/head-to-head/", params={"pl1": p1, "pl2": p2}, headers=HEADERS, timeout=20)
        if resp.status_code != 200: return []
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"⚠️ H2H {p1} vs {p2} не загрузился: {e}")
        return []

    p1_last = remove_accents(p1.split()[-1].lower())
    p2_last = remove_accents(p2.split()[-1].lower())
    out = []
    for tr in soup.find_all("tr"):
        row = tr.get_text(" ", strip=True)
        if p1_last not in remove_accents(row.lower()) or p2_last not in remove_accents(row.lower()): continue
        date_m = re.search(r'(\d{1,2}\.\d{1,2}\.(?:\d{2,4})?)', row)
        row_compact = re.sub(r'\s+', '', tr.get_text("", strip=True))
        score_m = re.search(r'\d{1,2}-\d{1,2}\d{0,2}(?:,\d{1,2}-\d{1,2}\d{0,2})*', row_compact)
        pair_m = re.search(r'([A-Za-zÀ-ÿ\'\.\- ]+)\s-\s([A-Za-zÀ-ÿ\'\.\- ]+)', row)
        if not (date_m and score_m and pair_m): continue
        home = pair_m.group(1).strip()
        score_raw = score_m.group(0)
        s1, s2 = _sets_from_score(score_raw)
        if s1 == s2 == 0: continue
        p1_is_home = p1_last in remove_accents(home.lower())
        p1_won = (s1 > s2) if p1_is_home else (s2 > s1)
        tour_link = tr.find("a", href=re.compile(r'/(atp|wta)-(men|women)/'))
        tour_name = tour_link.get_text(" ", strip=True) if tour_link else ""
        round_txt = ""
        for td in tr.find_all("td"):
            t = td.get_text(" ", strip=True)
            if re.fullmatch(r'(F|SF|QF|R\d{1,3}|1R|2R|3R|4R|Q\d?|BR)', t.strip(), re.IGNORECASE):
                round_txt = t.strip().upper()
                break
        if not round_txt:
            rm = re.search(r'\b(F|SF|QF|R\d{1,3}|[1-4]R|Q\d?)\b', row)
            if rm: round_txt = rm.group(1).upper()
        out.append({
            "date": date_m.group(1).rstrip("."), "tournament": tour_name or "—",
            "surface": get_tournament_surface(tour_name, tour_link.get("href", "") if tour_link else "", cache),
            "score": format_score(score_raw, flip=not p1_is_home), "p1_won": p1_won, "round": round_txt
        })
        if len(out) >= 10: break
    return out

def _fmt_rows_html(rows, perspective_won_key="won"):
    out = []
    for m in rows:
        surf = (m.get("surface") or "").lower()
        color = SURFACE_COLORS.get(surf, SURFACE_COLORS[""])
        won = m.get(perspective_won_key)
        badge = "W" if won else "L"
        badge_bg = "#2E9E5B" if won else "#D5493F"
        tour = (m.get("tournament") or "—")
        tour_short = (tour[:22] + "…") if len(tour) > 23 else tour
        opp = m.get("opponent", "")
        mid = f"<div class='opp'>{opp}</div>" if opp else ""
        meta = []
        if m.get("round"): meta.append(m["round"])
        if m.get("opp_elo"): meta.append(f"{m.get('opp_elo_label') or 'Elo'} {m['opp_elo']}")
        rnd = f"<span class='round'>{' · '.join(meta)}</span>" if meta else ""
        out.append(f"""
        <div class="row">
          <div class="date">{m.get('date','')}</div>
          <div class="surf" style="background:{color}">{tour_short}<span class="sname">{SURFACE_RU.get(surf,'?')}</span></div>
          <div class="mid">{mid}{rnd}</div>
          <div class="score">{m.get('score','')}</div>
          <div class="wl" style="background:{badge_bg}">{badge}</div>
        </div>""")
    return "\n".join(out) if out else "<div class='empty'>Нет данных</div>"

def _elo_badge_html(elo):
    """Плашка с Elo в заголовке игрока. elo = (elo_по_покрытию, общий_elo, подпись_покрытия)."""
    if not elo: return ""
    surf_elo, overall, surf_label = elo
    if not surf_elo and not overall:
        return "<span class='elo elo-none'>Elo: нет данных</span>"
    parts = []
    if surf_elo:
        label = f"{surf_label} Elo" if surf_label else "Elo"
        parts.append(f"<span class='elo'>{label}: <b>{surf_elo}</b></span>")
    if overall:
        parts.append(f"<span class='elo elo-dim'>Elo общий: <b>{overall}</b></span>")
    return "".join(parts)

def get_short_name_elo(short_name, surface_elo_ratings, surface=""):
    """Ищет Elo по сокращенному имени соперника вида 'Vandecasteele Q.' / 'De Minaur A.'.
    В таблице tennisabstract имена полные ('Quentin Vandecasteele'), поэтому сопоставляем
    по фамилии + первой букве имени. Если под условие подходит больше одного игрока,
    рейтинг не показываем — лучше пусто, чем чужой рейтинг однофамильца."""
    if not surface_elo_ratings or not short_name: return None, ""
    tokens = [t for t in short_name.replace(",", " ").split() if t]
    if not tokens: return None, ""

    initials = []
    while tokens and re.fullmatch(r'[A-Za-zÀ-ÿ](?:[-\.][A-Za-zÀ-ÿ])*\.?', tokens[-1]):
        initials.insert(0, tokens.pop())
        if len(tokens) <= 1: break
    if not tokens: return None, ""

    surname = "".join(c for c in remove_accents(" ".join(tokens)).lower() if c.isalnum())
    initial = ""
    if initials:
        initial = "".join(c for c in remove_accents(initials[0]).lower() if c.isalnum())[:1]
    if not surname: return None, ""

    hits = [v for k, v in surface_elo_ratings.items()
            if k.endswith(surname) and (not initial or k.startswith(initial))]
    if len(hits) != 1:
        hits = [v for k, v in surface_elo_ratings.items()
                if k.startswith(surname) and (not initial or k.endswith(initial))] if not hits else hits
    if len(hits) != 1: return None, ""

    data = hits[0]
    surf = normalize_surface(surface)
    key = {"hard": "Hard", "clay": "Clay", "grass": "Grass"}.get(surf)
    val = data.get(key) if key else None
    if val not in ("", "-", None):
        return val, f"{key} Elo"
    # По этому покрытию данных нет — показываем общий рейтинг и честно это подписываем
    val = data.get("Overall")
    if val in ("", "-", None): return None, ""
    return val, "Elo общий"

def get_player_elo(player_name, surface_elo_ratings, surface=""):
    """Возвращает (Elo по покрытию матча, общий Elo, подпись покрытия) для игрока.
    Использует уже загружаемую ботом таблицу tennisabstract.com/reports/atp_elo_ratings.html
    (колонки hElo/cElo/gElo). Имя нормализуем так же, как при парсинге таблицы."""
    if not surface_elo_ratings: return None, None, ""
    norm = "".join(c for c in remove_accents(player_name).lower() if c.isalnum())
    data = surface_elo_ratings.get(norm)
    if not data:
        # Запасной поиск по фамилии — в таблице имя может быть записано иначе
        last = "".join(c for c in remove_accents(player_name.split()[-1]).lower() if c.isalnum())
        hits = [v for k, v in surface_elo_ratings.items() if k.endswith(last) or k.startswith(last)]
        if len(hits) == 1: data = hits[0]
    if not data: return None, None, ""

    surf = normalize_surface(surface)
    key = {"hard": "Hard", "clay": "Clay", "grass": "Grass"}.get(surf)
    surf_elo = data.get(key) if key else None
    if surf_elo in ("", "-", None): surf_elo = None
    overall = data.get("Overall") or None
    if overall in ("", "-"): overall = None
    return surf_elo, overall, (key if key else "")

def build_h2h_card_html(p1, p2, m1, m2, h2h, tournament="", match_date="", elo1=None, elo2=None, surface=""):
    """Собирает HTML-карточку: последние матчи обоих игроков + очные встречи.
    Цвета покрытий: грунт — оранжевый, хард — голубой, трава — зелёный."""
    legend = "".join(
        f"<span class='leg'><i style='background:{SURFACE_COLORS[k]}'></i>{SURFACE_RU[k]}</span>"
        for k in ("clay", "hard", "grass")
    )
    h2h_block = ""
    if h2h:
        h2h_rows = []
        for m in h2h:
            surf = (m.get("surface") or "").lower()
            color = SURFACE_COLORS.get(surf, SURFACE_COLORS[""])
            winner = p1 if m["p1_won"] else p2
            rnd = f"<span class='round'>{m.get('round','')}</span>" if m.get("round") else ""
            badge = "W" if m["p1_won"] else "L"
            badge_bg = "#2E9E5B" if m["p1_won"] else "#D5493F"
            h2h_rows.append(f"""
            <div class="row">
              <div class="date">{m['date']}</div>
              <div class="surf" style="background:{color}">{(m['tournament'][:22])}<span class="sname">{SURFACE_RU.get(surf,'?')}</span></div>
              <div class="mid"><div class="opp">Победил: {winner}</div>{rnd}</div>
              <div class="score">{m['score']}</div>
              <div class="wl" style="background:{badge_bg}">{badge}</div>
            </div>""")
        h2h_block = f"<div class='sec'>ОЧНЫЕ ВСТРЕЧИ · W/L ДЛЯ {p1.upper()}</div>{''.join(h2h_rows)}"
    else:
        h2h_block = "<div class='sec'>ОЧНЫЕ ВСТРЕЧИ</div><div class='empty'>Очных встреч не найдено</div>"

    subtitle = " · ".join([x for x in (tournament, match_date) if x])
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    * {{ box-sizing: border-box; }}
    body {{ margin:0; padding:18px; background:#0B1A28; color:#E9EEF3;
           font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; width:780px; }}
    h1 {{ font-size:19px; margin:0 0 4px 0; }}
    .sub {{ color:#8FA3B5; font-size:12px; margin-bottom:12px; }}
    .legend {{ margin-bottom:14px; font-size:11px; color:#B9C7D4; }}
    .leg {{ margin-right:12px; }}
    .leg i {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:middle; }}
    .sec {{ background:#16324B; color:#CFE0EE; font-size:11px; letter-spacing:.6px;
            padding:7px 10px; border-radius:5px; margin:16px 0 8px; font-weight:600;
            display:flex; align-items:center; gap:14px; }}
    .sec .elo:first-of-type {{ margin-left:auto; }}
    .elo {{ font-weight:500; letter-spacing:0; color:#DCE9F5; font-size:11px; white-space:nowrap; }}
    .elo b {{ color:#FFFFFF; }}
    .elo-dim {{ color:#8FA9C0; }}
    .elo-none {{ color:#8FA9C0; font-style:italic; margin-left:auto; }}
    .row {{ display:flex; align-items:center; gap:10px; padding:7px 6px;
            border-bottom:1px solid #16283A; font-size:12.5px; }}
    .date {{ width:78px; color:#9FB2C2; flex:none; }}
    .surf {{ width:132px; flex:none; color:#fff; font-weight:700; font-size:10.5px;
             padding:5px 7px; border-radius:4px; line-height:1.25; }}
    .surf .sname {{ display:block; font-weight:500; opacity:.9; font-size:9.5px; text-transform:uppercase; }}
    .mid {{ flex:1; min-width:0; }}
    .opp {{ font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    .round {{ color:#8FA3B5; font-size:10.5px; }}
    .score {{ width:150px; text-align:right; color:#DCE6EF; flex:none; font-variant-numeric:tabular-nums; }}
    .wl {{ width:34px; text-align:center; flex:none; color:#fff; font-weight:700;
           padding:4px 0; border-radius:4px; font-size:11px; }}
    .empty {{ color:#7C8FA0; font-size:12px; padding:8px 6px; }}
    </style></head><body>
      <h1>{p1} — {p2}</h1>
      <div class="sub">{subtitle}</div>
      <div class="legend">{legend}</div>
      <div class="sec">ПОСЛЕДНИЕ МАТЧИ: {p1.upper()}{_elo_badge_html(elo1)}</div>
      {_fmt_rows_html(m1)}
      <div class="sec">ПОСЛЕДНИЕ МАТЧИ: {p2.upper()}{_elo_badge_html(elo2)}</div>
      {_fmt_rows_html(m2)}
      {h2h_block}
    </body></html>"""

def render_html_to_png(html, output_path):
    """Рендерит локальный HTML в PNG через headless-браузер (сеть не нужна)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"success": False, "error": f"Playwright не установлен. Бот использует: {sys.executable}"}
    html_path = f"/tmp/card_{uuid.uuid4().hex[:8]}.html"
    try:
        with open(html_path, "w", encoding="utf-8") as f: f.write(html)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                page = browser.new_page(viewport={"width": 800, "height": 600}, device_scale_factor=2)
                page.goto(f"file://{html_path}", wait_until="load", timeout=30000)
                page.wait_for_timeout(300)
                try:
                    h = page.evaluate("Math.ceil(document.body.getBoundingClientRect().height)")
                    if isinstance(h, (int, float)) and h > 50:
                        page.set_viewport_size({"width": 800, "height": int(h) + 10})
                        page.wait_for_timeout(150)
                except Exception: pass
                page.screenshot(path=output_path, full_page=True)
                return {"success": True, "path": output_path}
            finally:
                browser.close()
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        try: os.remove(html_path)
        except Exception: pass

def match_best_of(tournament):
    """5 сетов на мужском «Шлеме», иначе 3.

    Логика одна на весь проект — tennis_parser.tennisratio.guess_best_of;
    здесь только обёртка, чтобы не тянуть импорт в каждое место вызова.
    Если пакет недоступен, возвращаем 3: это прежнее поведение, а не поломка.
    """
    try:
        from tennis_parser.tennisratio import guess_best_of
        return guess_best_of(tournament)
    except Exception:
        return 3

def _p_set_from_p_match(p_match, best_of=3):
    """Подбирает вероятность выигрыша отдельного сета, дающую заданную вероятность победы в матче."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if best_of == 3:
            pm = mid**2 * (3 - 2*mid)
        else:
            pm = mid**3 * (10 - 15*mid + 6*mid**2)
        if pm < p_match: lo = mid
        else: hi = mid
    return (lo + hi) / 2

def run_local_monte_carlo(elo1, elo2, runs=SIMULATION_RUNS, best_of=3, seed=None):
    """Локальная симуляция матча методом Монте-Карло по рейтингам Elo.
    Считается честно, прямо здесь: вероятность сета выводится из Elo, затем разыгрываются
    сеты и счёт по геймам. Нужна как мгновенная опора и для сверки с ответом Gemini."""
    try:
        e1, e2 = float(elo1), float(elo2)
    except (TypeError, ValueError):
        return None
    rnd = random.Random(seed)
    p_match = 1.0 / (1.0 + 10 ** ((e2 - e1) / 400.0))
    p_set = _p_set_from_p_match(p_match, best_of)
    need = 2 if best_of == 3 else 3
    # Ориентир по геймам у формата свой: в bo3 матч это ~22 гейма, в bo5 —
    # почти вдвое больше (по симуляции около 38). Зашитые 22.5 на пятисетовом
    # матче показывали «ТБ 22.5: 97%» — правда, но бесполезная: столько
    # геймов там играется почти всегда, и как ориентир строка не работала.
    games_line = 22.5 if best_of == 3 else 37.5

    # Чем ровнее силы, тем чаще упорные сеты — веса счетов зависят от p_set
    edge = abs(p_set - 0.5)
    close = [(7, 6), (7, 5), (6, 4)]
    mid_s = [(6, 3), (6, 2)]
    easy = [(6, 1), (6, 0)]
    w_close = max(0.10, min(0.45, 0.45 - edge * 0.5))
    w_easy = max(0.20, min(0.50, 0.2 + edge * 0.6))

    def sample_games(winner_strong):
        r = rnd.random()
        if r < w_close: pool = close
        elif r < w_close + (0.55 - w_easy if winner_strong else 0.35): pool = mid_s
        else: pool = easy if winner_strong else mid_s
        return rnd.choice(pool)

    wins1 = 0
    # Раньше словарь был зашит под bo3 («2:0», «2:1», «0:2», «1:2»), и на
    # пятисетовом матче все счета уходили мимо: карточка показывала нули.
    sets_dist = {}
    total_games_sum = 0
    long_matches = 0     # сетов сыграно больше минимума (см. total_sets_line)
    games_over_225 = 0

    for _ in range(runs):
        s1 = s2 = 0
        games1 = games2 = 0
        while s1 < need and s2 < need:
            if rnd.random() < p_set:
                s1 += 1
                a, b = sample_games(p_set > 0.5)
                games1 += a; games2 += b
            else:
                s2 += 1
                a, b = sample_games(p_set < 0.5)
                games2 += a; games1 += b
        if s1 > s2: wins1 += 1
        key = f"{s1}:{s2}"
        sets_dist[key] = sets_dist.get(key, 0) + 1
        if s1 + s2 > need: long_matches += 1
        tg = games1 + games2
        total_games_sum += tg
        if tg > games_line: games_over_225 += 1

    p1_win = wins1 / runs
    p_over_line = long_matches / runs

    def _dist_order(k):
        """Сначала победы первого (по сетам соперника), потом второго."""
        a, b = (int(x) for x in k.split(":"))
        return (0, b) if a > b else (1, -a)

    return {
        "runs": runs,
        "best_of": best_of,
        # Линия тотала у формата своя, и она совпадает с той, что публикует
        # Pinnacle: в bo3 это 2.5 (нужен третий сет), в bo5 — 3.5 (нужен
        # четвёртый). В обоих случаях это «сетов сыграно больше минимума».
        "total_sets_line": need + 0.5,
        "elo1": e1, "elo2": e2,
        "p_set": round(p_set, 4),
        "p1_win": round(p1_win, 4),
        "p2_win": round(1 - p1_win, 4),
        "fair_odds_p1": round(1 / p1_win, 3) if p1_win > 0 else None,
        "fair_odds_p2": round(1 / (1 - p1_win), 3) if p1_win < 1 else None,
        # Имя ключа историческое: смысл — «тотал сетов больше линии», а сама
        # линия лежит рядом в total_sets_line.
        "p_tb25_sets": round(p_over_line, 4),
        "fair_odds_tb25": round(1 / p_over_line, 3) if p_over_line > 0 else None,
        "sets_dist": {k: round(sets_dist[k] / runs, 4)
                      for k in sorted(sets_dist, key=_dist_order)},
        "avg_games": round(total_games_sum / runs, 2),
        # Имя ключа историческое: смысл — «геймов больше линии», а сама линия
        # лежит рядом в games_line (как и у тотала сетов выше).
        "games_line": games_line,
        "p_games_over_225": round(games_over_225 / runs, 4),
    }

def format_simulation_text(sim, p1, p2, waiting_for=""):
    if not sim: return ""
    d = sim["sets_dist"]
    line = sim.get("total_sets_line", 2.5)
    dist = " · ".join(f"{k} {v*100:.0f}%" for k, v in d.items())
    tail = f"\n\n⏳ <i>Это быстрый расчёт по Elo. Жду ответ {waiting_for} — придёт отдельным сообщением.</i>" if waiting_for else ""
    return (
        f"📈 <b>Локальная симуляция ({sim['runs']} прогонов, bo{sim.get('best_of', 3)})</b>\n"
        f"Победа {p1}: <b>{sim['p1_win']*100:.1f}%</b> (спр. кэф {sim['fair_odds_p1']})\n"
        f"Победа {p2}: <b>{sim['p2_win']*100:.1f}%</b> (спр. кэф {sim['fair_odds_p2']})\n"
        f"ТБ {line:g} сета (матч от {int(line) + 1} сетов): <b>{sim['p_tb25_sets']*100:.1f}%</b> (спр. кэф {sim['fair_odds_tb25']})\n"
        f"Счёт по сетам: {dist}\n"
        f"Геймов в среднем: {sim['avg_games']} (ТБ {sim.get('games_line', 22.5):g}: {sim['p_games_over_225']*100:.0f}%)"
        f"{tail}"
    )

def gemini_deep_research(prompt, api_key=None, agent=None, timeout_sec=1800, poll_sec=15,
                         thinking_level="high", progress_cb=None):
    """Запускает задачу в Gemini Deep Research (Interactions API) и ждёт результат.
    Соответствует режиму в приложении: Deep Research + "Расширенный" (thinking_level=high).
    progress_cb — колбэк для сообщений о ходе: задача идёт минутами, и без них
    невозможно отличить нормальную работу от зависшего запроса."""
    api_key = api_key or GEMINI_API_KEY
    agent = agent or GEMINI_RESEARCH_AGENT
    if not api_key:
        return {"success": False, "error": "Не задан GEMINI_API_KEY (переменная окружения или константа в коде)."}

    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    body = {"input": prompt, "agent": agent, "background": True}
    if thinking_level:
        body["generation_config"] = {"thinking_level": thinking_level}

    interaction_id = None
    try:
        resp = requests.post(f"{GEMINI_API_BASE}/interactions", headers=headers, json=body, timeout=60)
        # Если агент не принимает generation_config — повторяем без него, а не падаем
        if resp.status_code == 400 and "generation_config" in body:
            print(f"⚠️ Gemini не принял thinking_level, повторяю без него: {resp.text[:200]}")
            body.pop("generation_config")
            resp = requests.post(f"{GEMINI_API_BASE}/interactions", headers=headers, json=body, timeout=60)
        if resp.status_code not in (200, 201):
            return {"success": False, "error": f"Gemini вернул {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        interaction_id = data.get("id") or data.get("name", "").split("/")[-1]
        if not interaction_id:
            return {"success": False, "error": f"Не удалось получить id задачи: {resp.text[:300]}"}
    except Exception as e:
        return {"success": False, "error": f"Ошибка запуска Gemini: {e}"}

    if progress_cb:
        progress_cb(f"✅ Задача Gemini создана (id {interaction_id}). Жду результат, "
                    f"максимум {timeout_sec // 60} мин.")

    deadline = time.time() + timeout_sec
    started, last_ping, last_status = time.time(), time.time(), ""
    while time.time() < deadline:
        time.sleep(poll_sec)
        try:
            r = requests.get(f"{GEMINI_API_BASE}/interactions/{interaction_id}", headers=headers, timeout=60)
            if r.status_code != 200:
                print(f"⚠️ Gemini poll {r.status_code}: {r.text[:200]}")
                continue
            data = r.json()
            status = data.get("status")
            if status != last_status:
                last_status = status
                print(f"ℹ️ Gemini {interaction_id}: статус {status}")
            # Раз в 3 минуты сообщаем, что задача жива
            if progress_cb and time.time() - last_ping >= 180:
                last_ping = time.time()
                progress_cb(f"⏳ Gemini всё ещё работает ({int((time.time()-started)//60)} мин, статус: {status or '—'})")
            if status == "completed":
                return {"success": True, "text": _extract_gemini_text(data), "id": interaction_id}
            if status == "failed":
                return {"success": False, "error": f"Задача Gemini завершилась ошибкой: {str(data.get('error'))[:300]}"}
        except Exception as e:
            print(f"⚠️ Ошибка опроса Gemini: {e}")
    return {"success": False, "error": f"Gemini не ответил за {timeout_sec // 60} мин (задача {interaction_id})."}

def _extract_gemini_text(data):
    """Собирает текст отчёта из ответа Interactions API.
    Deep Research возвращает отчёт НЕСКОЛЬКИМИ шагами, поэтому идём по всем по порядку:
    раньше бралcя только последний шаг, и ответ приходил с середины (например, с пункта 3.3)."""
    chunks, seen = [], set()
    for step in (data.get("steps") or []):
        for c in (step.get("content") or []):
            t = (c.get("text") or "").strip()
            if not t or t in seen: continue
            seen.add(t)
            chunks.append(t)
    if not chunks:
        for key in ("output_text", "text", "output"):
            v = data.get(key)
            if isinstance(v, str) and v.strip(): chunks.append(v.strip())
    if not chunks:
        chunks.append(json.dumps(data, ensure_ascii=False)[:1500])
    return "\n\n".join(chunks).strip()

def build_detailed_prompt(p1, p2, tournament, match_date, elo1, elo2, m1, m2, h2h, sim, odds=None, page_stats=""):
    """Развёрнутое задание: отдаём Gemini все наши данные (форма, Elo, очные, линия Pinnacle)
    и просим прогнать симуляции кодом + сверить с нашей локальной моделью.
    В отличие от короткого запроса, тут Gemini не тратит время на сбор того, что у нас уже есть."""
    def form_lines(name, rows):
        if not rows: return f"{name}: нет данных по последним матчам"
        out = [f"{name} — последние {len(rows)} матчей:"]
        for r in rows:
            elo_txt = f", {r.get('opp_elo_label') or 'Elo'} соперника {r.get('opp_elo')}" if r.get("opp_elo") else ""
            out.append(f"  {r.get('date','')} {r.get('tournament','')} ({r.get('surface','?')}), "
                       f"{'победа над' if r.get('won') else 'поражение от'} {r.get('opponent','')} "
                       f"{r.get('score','')}{elo_txt}")
        return "\n".join(out)

    h2h_txt = "Очных встреч не найдено."
    if h2h:
        h2h_txt = "Очные встречи:\n" + "\n".join(
            f"  {x['date']} {x['tournament']} ({x.get('surface','?')}), "
            f"{'победил ' + p1 if x['p1_won'] else 'победил ' + p2}, {x['score']}" for x in h2h)

    odds_txt = ""
    if odds:
        odds_txt = (f"\nТекущая линия Pinnacle: П1 {odds.get('p1','-')}, П2 {odds.get('p2','-')}, "
                    f"тотал сетов: {odds.get('total_sets','-')}\n")

    e1 = (elo1[0] or elo1[1]) if elo1 else None
    e2 = (elo2[0] or elo2[1]) if elo2 else None
    baseline = ""
    if sim:
        baseline = (f"\nМоя локальная симуляция ({sim['runs']} прогонов, модель на чистом Elo) дала: "
                    f"победа {p1} {sim['p1_win']*100:.1f}%, ТБ {sim.get('total_sets_line', 2.5):g} сета {sim['p_tb25_sets']*100:.1f}%, "
                    f"средний тотал геймов {sim['avg_games']}. Сравни со своим результатом и объясни расхождения.\n")

    # Требование «оцени ТБ 2.5 сета» из задания убрано намеренно: линия у
    # формата своя (2.5 в bo3, 3.5 в bo5), и на «Шлеме» модель рассуждала бы
    # о рынке, которого в линии нет. Числа из нашей симуляции подставляются
    # выше уже правильной линией (total_sets_line).
    return f"""Ты — аналитик теннисных ставок. Проанализируй предстоящий матч и дай вероятностную оценку.

МАТЧ: {p1} против {p2}
Турнир: {tournament or 'неизвестен'}
Дата: {match_date or 'неизвестна'}
Elo по покрытию матча: {p1} = {e1 or 'нет данных'}, {p2} = {e2 or 'нет данных'}
{odds_txt}
{form_lines(p1, m1)}

{form_lines(p2, m2)}

{h2h_txt}
{baseline}
{f'СТАТИСТИКА СО СТРАНИЦЫ МАТЧА (tennisratio):{chr(10)}{page_stats}{chr(10)}' if page_stats else ''}
ЗАДАЧИ:
1. Напиши и ВЫПОЛНИ код (code execution), который прогонит {SIMULATION_RUNS} симуляций этого матча
   методом Монте-Карло. Модель строй на Elo по покрытию, но откалибруй её с учётом формы,
   очных встреч и уровня соперников (Elo соперников указан выше). Разыгрывай матч по сетам и геймам.
   Приведи фактические числа, полученные ИЗ ЗАПУЩЕННОГО КОДА, а не оценённые на глаз.
2. Выведи: вероятность победы каждого, распределение счёта по сетам,
   средний тотал геймов, справедливые коэффициенты (1/вероятность).
3. Поищи свежую информацию по обоим игрокам: травмы, снятия, физическое состояние,
   смена покрытия и часовых поясов, усталость от плотного календаря. Учти это в выводах.
4. Если указана линия Pinnacle — укажи, есть ли перевес (value) и в какую сторону.
5. В конце дай краткий вывод: на что ставить и почему, либо почему ставить не стоит.

Ответ на РУССКОМ языке. Формат — компактный, без markdown-таблиц, до 3000 знаков.
Отдельной строкой укажи, сколько симуляций реально было выполнено кодом."""

def build_simulation_prompt(p1, p2, tournament="", match_date="", include_context=True):
    """Формирует запрос ровно в том виде, как он набирается в приложении Gemini:
    'сделай 10000 симуляций матча X vs Y'. Турнир/дату добавляем отдельной строкой,
    только если они известны — чтобы Gemini не искал не тот матч (у игроков бывает
    несколько встреч, а имена в challenger-туре часто совпадают)."""
    prompt = f"сделай {SIMULATION_RUNS} симуляций матча {p1} vs {p2}"
    if include_context:
        extra = " ".join(x for x in (tournament, match_date) if x)
        if extra.strip():
            prompt += f"\n\n(матч: {extra.strip()})"
    return prompt

def get_players_form_card(p1, p2, tournament="", match_date="", output_path=None, surface_elo_ratings=None):
    """Полный конвейер: тянет последние 10 матчей каждого игрока + очные встречи
    с TennisExplorer, добавляет Elo по покрытию матча (tennisabstract) и рисует карточку."""
    path = output_path or f"/tmp/form_{uuid.uuid4().hex[:8]}.png"
    cache = load_surface_cache()

    m1, err1 = parse_te_last_matches(p1, 10, cache)
    m2, err2 = parse_te_last_matches(p2, 10, cache)
    h2h = parse_te_h2h(p1, p2, cache)
    save_surface_cache(cache)

    if not m1 and not m2:
        return {"success": False, "error": f"Не удалось получить матчи: {err1 or ''} {err2 or ''}".strip()}

    # Покрытие текущего матча: из строки турнира, иначе — по последнему сыгранному матчу
    sm = re.search(r'\((Hard|Clay|Grass|Carpet)\)', tournament or "", re.IGNORECASE)
    surface = sm.group(1).lower() if sm else detect_surface_from_text(tournament)
    if not surface:
        surface = (m1[0]["surface"] if m1 else (m2[0]["surface"] if m2 else ""))

    if surface_elo_ratings is None:
        surface_elo_ratings = parse_surface_elo_ratings()
    elo1 = get_player_elo(p1, surface_elo_ratings, surface)
    elo2 = get_player_elo(p2, surface_elo_ratings, surface)

    # Elo соперников считаем по покрытию того матча, в котором они встречались
    for row in (m1 + m2):
        row["opp_elo"], row["opp_elo_label"] = get_short_name_elo(
            row.get("opponent", ""), surface_elo_ratings, row.get("surface", ""))

    html = build_h2h_card_html(p1, p2, m1, m2, h2h, tournament, match_date, elo1, elo2, surface)
    result = render_html_to_png(html, path)
    if result["success"]:
        result["note"] = f"{p1}: {len(m1)} матчей, {p2}: {len(m2)} матчей, очных: {len(h2h)}, покрытие: {surface or '?'}"
        if err1: result["note"] += f" | {err1}"
        if err2: result["note"] += f" | {err2}"
    return result

def md_to_telegram_html(text):
    """Приводит markdown из ответа модели к разметке Telegram.
    Telegram не понимает ### и **, поэтому в сообщении они видны как есть.
    Заодно чистим служебные сноски вида [cite: 6], которые оставляет Deep Research."""
    if not text: return ""
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r'\[cite[^\]]*\]', '', t)              # [cite: 6]
    t = re.sub(r'```[a-zA-Z]*\n?', '', t)             # обрамление блоков кода
    t = re.sub(r'^\s{0,3}#{1,6}\s*(.+)$', r'<b>\1</b>', t, flags=re.MULTILINE)   # заголовки
    t = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b>\1</b>', t, flags=re.DOTALL)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t, flags=re.DOTALL)               # жирный
    t = re.sub(r'(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])', r'<i>\1</i>', t)  # курсив
    t = re.sub(r'^\s*[-–—]\s+', '• ', t, flags=re.MULTILINE)                     # маркеры списка
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()

def send_long_message(text, chat_id, prefix="", convert_markdown=True):
    """Шлёт длинный текст в Telegram кусками (лимит сообщения ~4096 символов).
    Разметку модели переводим в формат Telegram и следим, чтобы теги не разрывались
    между сообщениями — иначе Telegram отвергает сообщение с ошибкой разбора HTML."""
    if convert_markdown:
        text = md_to_telegram_html(text)
    text = (prefix + text) if prefix else text

    def balance(chunk):
        out = chunk
        for tag in ("b", "i", "code"):
            opened = len(re.findall(rf'<{tag}>', out))
            closed = len(re.findall(rf'</{tag}>', out))
            if opened > closed:
                out += f"</{tag}>" * (opened - closed)
        return out

    carry = ""
    while len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        if cut == -1: cut = 3900
        chunk = carry + text[:cut]
        send_notification(balance(chunk), chat_id)
        # Если тег остался открытым, продолжаем его в следующем сообщении
        carry = ""
        for tag in ("b", "i", "code"):
            if len(re.findall(rf'<{tag}>', chunk)) > len(re.findall(rf'</{tag}>', chunk)):
                carry += f"<{tag}>"
        text = text[cut:].strip()
    if text: send_notification(balance(carry + text), chat_id)

def run_gemini_simulation(p1, p2, tournament, match_date, chat_id, surface_elo_ratings=None, use_max=False):
    """Отправляет в Gemini запрос вида 'сделай 10000 симуляций матча X vs Y'
    в режиме Deep Research + Расширенный (thinking_level=high). Данные Gemini собирает сам.
    Параллельно шлём свою локальную симуляцию по Elo — как быстрый ориентир для сверки.
    Вызывать в отдельном потоке: Deep Research занимает единицы минут."""
    try:
        # Быстрый ориентир по Elo (не заменяет Gemini, но приходит сразу)
        try:
            ratings = surface_elo_ratings if surface_elo_ratings is not None else parse_surface_elo_ratings()
            sm = re.search(r'\((Hard|Clay|Grass|Carpet)\)', tournament or "", re.IGNORECASE)
            surface = sm.group(1).lower() if sm else detect_surface_from_text(tournament)
            elo1 = get_player_elo(p1, ratings, surface)
            elo2 = get_player_elo(p2, ratings, surface)
            e1 = (elo1[0] or elo1[1]) if elo1 else None
            e2 = (elo2[0] or elo2[1]) if elo2 else None
            sim = run_local_monte_carlo(e1, e2, best_of=match_best_of(tournament)) if (e1 and e2) else None
            if sim:
                send_notification(format_simulation_text(sim, p1, p2, waiting_for="Gemini"), chat_id)
        except Exception as e:
            print(f"⚠️ Локальная симуляция не выполнена: {e}")

        prompt = build_simulation_prompt(p1, p2, tournament, match_date)
        agent = GEMINI_RESEARCH_AGENT_MAX if use_max else GEMINI_RESEARCH_AGENT
        print(f"🤖 Запрос в Gemini ({agent}): {prompt}")
        # Max-режим делает до ~160 поисков, поэтому ждём дольше обычного
        result = gemini_deep_research(prompt, agent=agent, thinking_level="high",
                                      timeout_sec=3600 if use_max else 1800,
                                      progress_cb=lambda m: send_notification(m, chat_id))

        if result["success"]:
            send_long_message(result["text"], chat_id, prefix=f"🤖 <b>Gemini: {p1} — {p2}</b>\n\n")
        else:
            send_notification(f"❌ Gemini не отработал: {result['error']}", chat_id)
    except Exception as e:
        print(f"❌ Ошибка симуляции Gemini: {e}")
        send_notification(f"❌ Ошибка при симуляции: {e}", chat_id)

def run_gemini_deep_analysis(p1, p2, tournament, match_date, chat_id, surface_elo_ratings=None, use_max=False, match_url=""):
    """Подробный разбор: собираем свои данные (форма, Elo, очные, линия Pinnacle, локальная
    симуляция) и отдаём их Gemini Deep Research. Тяжелее и дороже короткой симуляции,
    зато Gemini не тратит проходы на сбор того, что у нас уже есть.
    Вызывать в отдельном потоке."""
    try:
        cache = load_surface_cache()
        m1, err1 = parse_te_last_matches(p1, 10, cache)
        m2, err2 = parse_te_last_matches(p2, 10, cache)
        h2h = parse_te_h2h(p1, p2, cache)
        save_surface_cache(cache)

        sm = re.search(r'\((Hard|Clay|Grass|Carpet)\)', tournament or "", re.IGNORECASE)
        surface = sm.group(1).lower() if sm else detect_surface_from_text(tournament)
        if not surface:
            surface = (m1[0]["surface"] if m1 else (m2[0]["surface"] if m2 else ""))

        ratings = surface_elo_ratings if surface_elo_ratings is not None else parse_surface_elo_ratings()
        elo1 = get_player_elo(p1, ratings, surface)
        elo2 = get_player_elo(p2, ratings, surface)
        for row in (m1 + m2):
            row["opp_elo"], row["opp_elo_label"] = get_short_name_elo(
                row.get("opponent", ""), ratings, row.get("surface", ""))

        e1 = (elo1[0] or elo1[1]) if elo1 else None
        e2 = (elo2[0] or elo2[1]) if elo2 else None
        sim = run_local_monte_carlo(e1, e2, best_of=match_best_of(tournament)) if (e1 and e2) else None

        page_stats, page_err = fetch_tennisratio_stats(match_url)

        collected = f"📦 Собрал данные: {p1} — {len(m1)} матчей, {p2} — {len(m2)} матчей, очных: {len(h2h)}"
        if page_stats: collected += f"\n🔗 Со страницы матча снято: {len(page_stats)} символов"
        if page_err: collected += f"\n⚠️ {page_err}"
        if err1 or err2: collected += f"\n⚠️ {err1 or ''} {err2 or ''}".rstrip()
        send_notification(collected, chat_id)
        if sim:
            send_notification(format_simulation_text(sim, p1, p2, waiting_for="Gemini"), chat_id)

        odds = get_pinnacle_odds(p1, p2, is_manual=True)
        if odds and odds.get("error"): odds = None

        prompt = build_detailed_prompt(p1, p2, tournament, match_date, elo1, elo2, m1, m2, h2h, sim, odds, page_stats)
        agent = GEMINI_RESEARCH_AGENT_MAX if use_max else GEMINI_RESEARCH_AGENT
        print(f"🤖 Подробный разбор в Gemini ({agent}), длина промпта {len(prompt)} симв.")
        result = gemini_deep_research(prompt, agent=agent, thinking_level="high",
                                      progress_cb=lambda m: send_notification(m, chat_id))

        if result["success"]:
            send_long_message(result["text"], chat_id, prefix=f"🔬 <b>Разбор Gemini: {p1} — {p2}</b>\n\n")
        else:
            send_notification(f"❌ Gemini не отработал: {result['error']}", chat_id)
    except Exception as e:
        print(f"❌ Ошибка разбора Gemini: {e}")
        send_notification(f"❌ Ошибка при разборе: {e}", chat_id)

def fetch_tennisratio_stats(url, max_chars=6000):
    """Забирает статистику со страницы матча tennisratio по присланной ссылке.
    Таблицы там подгружаются джаваскриптом (обычный requests видит пустые блоки),
    поэтому рендерим страницу в headless-браузере и снимаем уже готовый текст."""
    if not url:
        return "", "Ссылка на матч не сохранена"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", f"Playwright не установлен ({sys.executable})"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 1600}, user_agent=HEADERS["User-Agent"])
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                try: page.wait_for_load_state("networkidle", timeout=15000)
                except Exception: pass
                page.wait_for_timeout(2500)
                text = page.evaluate("document.body.innerText") or ""
            finally:
                browser.close()
    except Exception as e:
        return "", f"Не удалось открыть страницу: {e}"

    lines, seen = [], set()
    for raw in text.splitlines():
        s = " ".join(raw.split())
        if not s or len(s) < 2: continue
        low = s.lower()
        # Выкидываем навигацию, рекламу и повторы — иначе половина промпта уйдёт на мусор
        if any(k in low for k in ("cookie", "subscribe", "advertis", "sign in", "log in",
                                  "privacy", "terms of", "menu", "©")): continue
        if s in seen: continue
        seen.add(s)
        lines.append(s)

    cleaned = "\n".join(lines)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n…(обрезано)"
    if len(cleaned) < 200:
        return cleaned, "Со страницы удалось снять очень мало текста — возможно, статистика не прогрузилась"
    return cleaned, None

def call_claude(prompt, api_key=None, model=None, max_tokens=8000, timeout=600,
                web_search=True, code_execution=True, max_searches=8):
    """Запрос к Claude (Anthropic Messages API).
    Подключаем два серверных инструмента:
      - code_execution — Claude реально выполняет Python и считает симуляции, а не оценивает на глаз;
      - web_search — ищет травмы, снятия и свежие новости.
    Оба выполняются на стороне Anthropic, поэтому ответ приходит одним запросом."""
    api_key = api_key or ANTHROPIC_API_KEY
    model = model or CLAUDE_MODEL
    if not api_key:
        return {"success": False, "error": "Не задан ANTHROPIC_API_KEY (переменная окружения или константа в коде)."}
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    def build_tools(code_tool_type, with_search, with_code):
        tools = []
        if with_code: tools.append({"type": code_tool_type, "name": "code_execution"})
        if with_search: tools.append({"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches})
        return tools

    # Пробуем по убыванию возможностей: новый инструмент кода -> старый -> только поиск -> без инструментов
    attempts = [
        build_tools(CLAUDE_CODE_TOOL, web_search, code_execution),
        build_tools("code_execution_20250825", web_search, code_execution),
        build_tools(CLAUDE_CODE_TOOL, web_search, False),
        [],
    ]
    seen, last_err = set(), None
    for tools in attempts:
        key = json.dumps(tools, sort_keys=True)
        if key in seen: continue
        seen.add(key)
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if tools: body["tools"] = tools
        try:
            resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=timeout)
        except Exception as e:
            return {"success": False, "error": f"Ошибка запроса к Claude: {e}"}

        if resp.status_code == 400 and tools:
            last_err = resp.text[:300]
            print(f"⚠️ Claude не принял набор инструментов {[t['type'] for t in tools]}: {last_err}")
            continue
        if resp.status_code != 200:
            return {"success": False, "error": f"Claude вернул {resp.status_code}: {resp.text[:300]}"}

        data = resp.json()
        content = data.get("content", [])
        parts = [b.get("text", "") for b in content if b.get("type") == "text"]
        searches = sum(1 for b in content if b.get("type") == "server_tool_use" and b.get("name") == "web_search")
        code_runs = sum(1 for b in content if b.get("type") == "server_tool_use" and b.get("name") == "code_execution")
        text = "\n".join(x for x in parts if x).strip()
        if not text:
            return {"success": False, "error": f"Пустой ответ Claude: {json.dumps(data)[:300]}"}
        return {"success": True, "text": text, "usage": data.get("usage", {}),
                "searches": searches, "code_runs": code_runs,
                "tools": [t["type"] for t in tools]}

    return {"success": False, "error": f"Claude отклонил все варианты запроса. Последняя ошибка: {last_err}"}

def build_claude_prompt(p1, p2, tournament, match_date, elo1, elo2, m1, m2, h2h, sim, odds, page_stats):
    """Промпт для Claude: наши данные + статистика со страницы матча tennisratio.
    У Claude подключены исполнение кода и веб-поиск, поэтому он считает симуляции сам,
    а наша локальная модель на чистом Elo идёт как точка сверки."""
    def form_lines(name, rows):
        if not rows: return f"{name}: нет данных по последним матчам"
        out = [f"{name} — последние {len(rows)} матчей:"]
        for r in rows:
            elo_txt = f", {r.get('opp_elo_label') or 'Elo'} соперника {r.get('opp_elo')}" if r.get("opp_elo") else ""
            out.append(f"  {r.get('date','')} {r.get('tournament','')} ({r.get('surface','?')}), "
                       f"{'победа над' if r.get('won') else 'поражение от'} {r.get('opponent','')} "
                       f"{r.get('score','')}{elo_txt}")
        return "\n".join(out)

    h2h_txt = "Очных встреч не найдено."
    if h2h:
        h2h_txt = "Очные встречи:\n" + "\n".join(
            f"  {x['date']} {x['tournament']} ({x.get('surface','?')}), "
            f"{'победил ' + p1 if x['p1_won'] else 'победил ' + p2}, {x['score']}" for x in h2h)

    odds_txt = "Линия Pinnacle недоступна."
    if odds:
        odds_txt = (f"Линия Pinnacle: П1 {odds.get('p1','-')}, П2 {odds.get('p2','-')}, "
                    f"тотал сетов: {odds.get('total_sets','-')}")

    e1 = (elo1[0] or elo1[1]) if elo1 else None
    e2 = (elo2[0] or elo2[1]) if elo2 else None

    sim_txt = "Симуляция не выполнена (нет Elo одного из игроков)."
    if sim:
        d = sim["sets_dist"]
        sim_txt = (
            f"Монте-Карло, {sim['runs']} прогонов (модель на Elo по покрытию, посчитано локально):\n"
            f"  победа {p1}: {sim['p1_win']*100:.1f}% (спр. кэф {sim['fair_odds_p1']})\n"
            f"  победа {p2}: {sim['p2_win']*100:.1f}% (спр. кэф {sim['fair_odds_p2']})\n"
            f"  ТБ {sim.get('total_sets_line', 2.5):g} сета: {sim['p_tb25_sets']*100:.1f}% (спр. кэф {sim['fair_odds_tb25']})\n"
            # Ключи распределения зависят от формата (в bo5 это 3:0…0:3),
            # поэтому перечисляем что есть, а не фиксированную четвёрку:
            # обращение по d['2:0'] на пятисетовом матче падало бы KeyError.
            f"  счёт по сетам: {', '.join(f'{k} {v*100:.0f}%' for k, v in d.items())}\n"
            f"  средний тотал геймов: {sim['avg_games']} (ТБ {sim.get('games_line', 22.5):g}: {sim['p_games_over_225']*100:.0f}%)")

    page_block = f"\n\nСТАТИСТИКА СО СТРАНИЦЫ МАТЧА (tennisratio):\n{page_stats}" if page_stats else ""

    return f"""Ты — аналитик теннисных ставок. Разбери матч и дай практический вывод.

МАТЧ: {p1} против {p2}
Турнир: {tournament or 'неизвестен'}
Дата: {match_date or 'неизвестна'}
Elo по покрытию матча: {p1} = {e1 or 'нет данных'}, {p2} = {e2 or 'нет данных'}
{odds_txt}

{form_lines(p1, m1)}

{form_lines(p2, m2)}

{h2h_txt}

МОЯ ЛОКАЛЬНАЯ СИМУЛЯЦИЯ (для сверки, модель на чистом Elo):
{sim_txt}{page_block}

ЗАДАЧИ:
1. Прогони {SIMULATION_RUNS} симуляций этого матча методом Монте-Карло.
   Базу строй на Elo по покрытию, но откалибруй модель с учётом формы, уровня соперников
   (их Elo указан), очных встреч и статистики со страницы матча. Разыгрывай матч по сетам и геймам.
   ЕСЛИ у тебя есть инструмент исполнения кода — выполни расчёт им.
   ЕСЛИ инструмент недоступен — НЕ СЧИТАЙ ПРИБЛИЗИТЕЛЬНО и не выводи придуманных чисел.
   Вместо этого верни готовый Python-скрипт в блоке ```python — я выполню его на своей стороне
   и пришлю тебе вывод. Скрипт должен:
     - использовать только стандартную библиотеку (random, math, json, statistics);
     - не обращаться к сети, файлам и системе;
     - печатать результат в stdout в виде JSON: вероятности победы каждого,
       распределение счёта по сетам, средний тотал геймов, справедливые коэффициенты;
     - отработать меньше чем за минуту.
   В этом случае в ответе не давай выводов по матчу — только код и краткое пояснение модели.
2. Если расчёт выполнен — выведи: вероятность победы каждого, распределение счёта
   по сетам, средний тотал геймов, справедливые коэффициенты (1/вероятность),
   и сравни с моей локальной симуляцией, объяснив расхождения.
3. Если доступен веб-поиск — найди травмы, снятия, состояние после последних матчей и новости
   за последние дни. Укажи, что нашёл и как это влияет на оценку.
4. Сравни справедливые коэффициенты с линией Pinnacle и укажи, есть ли перевес (value).
5. Вывод: ставить или нет, на что именно и почему. Назови главные риски.

Если каких-то данных не хватает — прямо скажи об этом, не додумывай.
Ответ на РУССКОМ, компактно, без markdown-таблиц, до 2500 знаков."""

def run_claude_simulation(p1, p2, tournament, match_date, chat_id, surface_elo_ratings=None, match_url=""):
    """Собирает наши данные + статистику со страницы матча по присланной ссылке
    и отдаёт всё это Claude. Вызывать в отдельном потоке."""
    try:
        cache = load_surface_cache()
        m1, err1 = parse_te_last_matches(p1, 10, cache)
        m2, err2 = parse_te_last_matches(p2, 10, cache)
        h2h = parse_te_h2h(p1, p2, cache)
        save_surface_cache(cache)

        sm = re.search(r'\((Hard|Clay|Grass|Carpet)\)', tournament or "", re.IGNORECASE)
        surface = sm.group(1).lower() if sm else detect_surface_from_text(tournament)
        if not surface:
            surface = (m1[0]["surface"] if m1 else (m2[0]["surface"] if m2 else ""))

        ratings = surface_elo_ratings if surface_elo_ratings is not None else parse_surface_elo_ratings()
        elo1 = get_player_elo(p1, ratings, surface)
        elo2 = get_player_elo(p2, ratings, surface)
        for row in (m1 + m2):
            row["opp_elo"], row["opp_elo_label"] = get_short_name_elo(
                row.get("opponent", ""), ratings, row.get("surface", ""))

        e1 = (elo1[0] or elo1[1]) if elo1 else None
        e2 = (elo2[0] or elo2[1]) if elo2 else None
        sim = run_local_monte_carlo(e1, e2, best_of=match_best_of(tournament)) if (e1 and e2) else None

        page_stats, page_err = fetch_tennisratio_stats(match_url)

        status = f"📦 Данные: {p1} — {len(m1)} матчей, {p2} — {len(m2)} матчей, очных: {len(h2h)}"
        status += f"\n🔗 Со страницы матча снято: {len(page_stats)} символов" if page_stats else ""
        if page_err: status += f"\n⚠️ {page_err}"
        if err1 or err2: status += f"\n⚠️ {err1 or ''} {err2 or ''}".rstrip()
        send_notification(status, chat_id)
        if sim:
            send_notification(format_simulation_text(sim, p1, p2, waiting_for="Claude"), chat_id)

        odds = get_pinnacle_odds(p1, p2, is_manual=True)
        if odds and odds.get("error"): odds = None

        prompt = build_claude_prompt(p1, p2, tournament, match_date, elo1, elo2, m1, m2, h2h, sim, odds, page_stats)
        print(f"🧠 Запрос в Claude ({CLAUDE_MODEL}), длина промпта {len(prompt)} симв.")
        result = call_claude(prompt)

        if not result["success"]:
            send_notification(f"❌ Claude не отработал: {result['error']}", chat_id)
            return

        # Если исполнение кода недоступно (например, через шлюз-посредник), Claude присылает
        # скрипт — выполняем его сами и возвращаем вывод на интерпретацию.
        if not result.get("code_runs"):
            code = extract_python_code(result["text"])
            if code:
                send_notification(f"⚙️ Claude не может выполнить код сам — запускаю его скрипт "
                                  f"({len(code)} символов) на сервере...", chat_id)
                out, code_err = run_generated_code(code)
                if code_err:
                    send_notification(f"⚠️ Скрипт не отработал: {code_err}\nПрошу Claude сделать вывод без него.", chat_id)
                    followup = (f"{prompt}\n\n---\nЯ попытался выполнить твой скрипт, но получил ошибку:\n"
                                f"{code_err}\n\nСделай выводы по матчу, опираясь на мою локальную симуляцию "
                                f"и данные выше. Кода больше не присылай.")
                else:
                    send_notification(f"✅ Скрипт выполнен, получено {len(out)} символов результата.", chat_id)
                    followup = (f"{prompt}\n\n---\nЯ выполнил твой скрипт на своей стороне. Его вывод:\n"
                                f"{out}\n\nТеперь дай окончательный разбор матча по задачам 2–6, "
                                f"опираясь на ЭТИ числа. Кода больше не присылай.")
                result2 = call_claude(followup)
                if result2["success"]:
                    result = result2
                    result["local_code_run"] = not bool(code_err)
                else:
                    send_notification(f"⚠️ Второй запрос не прошёл: {result2['error']}\nПоказываю первый ответ.", chat_id)

        if result["success"]:
            marks = []
            if result.get("code_runs"): marks.append(f"запусков кода у Claude: {result['code_runs']}")
            if result.get("local_code_run"): marks.append("скрипт выполнен на сервере")
            if result.get("searches"): marks.append(f"веб-поисков: {result['searches']}")
            head = f"🧠 <b>Claude ({CLAUDE_MODEL}): {p1} — {p2}</b>"
            head += f"\n<i>{', '.join(marks)}</i>\n\n" if marks else "\n<i>инструменты не задействованы</i>\n\n"
            send_long_message(result["text"], chat_id, prefix=head)
        else:
            send_notification(f"❌ Claude не отработал: {result['error']}", chat_id)
    except Exception as e:
        print(f"❌ Ошибка анализа Claude: {e}")
        send_notification(f"❌ Ошибка при анализе Claude: {e}", chat_id)

CODE_BLOCK_RE = re.compile(r'```(?:python|py)?\s*\n(.*?)```', re.DOTALL | re.IGNORECASE)
# Конструкции, которых в скрипте симуляции быть не должно: код приходит от модели
# и выполняется на сервере, поэтому всё, что лезет в сеть, файлы или систему, блокируем.
CODE_DENYLIST = [
    "subprocess", "os.system", "os.popen", "os.remove", "os.rmdir", "shutil",
    "socket", "requests", "urllib", "httpx", "ftplib", "smtplib", "paramiko",
    "open(", "eval(", "exec(", "compile(", "__import__", "importlib",
    "pathlib", "pickle", "ctypes", "sys.exit", "globals(", "locals(",
]

def extract_python_code(text):
    """Достаёт первый блок Python-кода из ответа модели."""
    blocks = CODE_BLOCK_RE.findall(text or "")
    for b in blocks:
        if b.strip():
            return b.strip()
    return ""

def run_generated_code(code, timeout=120):
    """Выполняет сгенерированный моделью скрипт симуляции в отдельном процессе.
    Код приходит от Claude, а не от постороннего, но всё равно проверяем его по стоп-списку
    и запускаем с таймаутом в отдельном процессе — чтобы случайный бесконечный цикл
    или обращение к сети не подвесили бота."""
    if not code:
        return "", "Claude не прислал код"
    found = [w for w in CODE_DENYLIST if w in code]
    if found:
        return "", f"В коде есть запрещённые конструкции: {', '.join(found[:5])}"

    path = f"/tmp/sim_{uuid.uuid4().hex[:8]}.py"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=timeout,
            cwd="/tmp", env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return out, f"Скрипт завершился с ошибкой (код {proc.returncode}): {err[:500]}"
        if not out:
            return "", "Скрипт отработал, но ничего не вывел"
        return out[:6000], None
    except subprocess.TimeoutExpired:
        return "", f"Скрипт не уложился в {timeout} секунд"
    except Exception as e:
        return "", f"Не удалось выполнить скрипт: {e}"
    finally:
        try: os.remove(path)
        except Exception: pass

def run_local_simulation_only(p1, p2, tournament, match_date, chat_id, surface_elo_ratings=None):
    """Только локальная симуляция по Elo: мгновенно, без обращения к внешним моделям и без затрат."""
    try:
        ratings = surface_elo_ratings if surface_elo_ratings is not None else parse_surface_elo_ratings()
        sm = re.search(r'\((Hard|Clay|Grass|Carpet)\)', tournament or "", re.IGNORECASE)
        surface = sm.group(1).lower() if sm else detect_surface_from_text(tournament)
        elo1 = get_player_elo(p1, ratings, surface)
        elo2 = get_player_elo(p2, ratings, surface)
        e1 = (elo1[0] or elo1[1]) if elo1 else None
        e2 = (elo2[0] or elo2[1]) if elo2 else None
        if not (e1 and e2):
            missing = p1 if not e1 else p2
            send_notification(f"ℹ️ Не нашёл Elo для «{missing}» — симуляция невозможна.", chat_id)
            return
        sim = run_local_monte_carlo(e1, e2, best_of=match_best_of(tournament))
        label = {"hard": "Hard", "clay": "Clay", "grass": "Grass"}.get(normalize_surface(surface), "общий")
        send_notification(f"{format_simulation_text(sim, p1, p2)}\n\n"
                          f"<i>{label} Elo: {p1} {e1} · {p2} {e2}</i>", chat_id)
    except Exception as e:
        send_notification(f"❌ Ошибка симуляции: {e}", chat_id)

def _te_row_scores(row):
    """Числа из строки матча на TennisExplorer: сеты игрока и геймы по сетам.

    Вынесена на уровень модуля из check_tennis_explorer_results, чтобы на неё
    можно было написать тест: разбор счёта тут уже ломался дважды, а изнутри
    другой функции его не проверить.

    Фильтр `\\d{1,3}`, а не `\\d{1,2}`, и это не запас на всякий случай.
    Сет с тайбрейком размечен как `<td class="score">6<sup>10</sup></td>`,
    то есть текстом это «610». Двузначный фильтр такую ячейку выбрасывал,
    списки геймов по двум строкам матча получались разной длины (4 против 3),
    проверка `len(games1) == len(games2)` не проходила — и матч не попадал в
    результаты ВОВСЕ. Снаружи это выглядело как «ставка навсегда висит в
    игре», хотя матч на сайте есть и по именам находится.

    Ниже по течению склеенный тайбрейк ждут именно в таком виде:
    `_parse_score_sets` разбирает «7-610» как 7-6(10), там тоже `\\d{1,3}`.
    """
    scores = []
    for td in row.find_all("td"):
        cls = " ".join(td.get("class", []) or [])
        if any(k in cls for k in ("t-name", "first", "flag", "coupon")):
            continue
        txt = td.get_text(strip=True)
        if re.fullmatch(r'\d{1,3}', txt):
            scores.append(int(txt))
    return scores


def _is_te_head_row(tr):
    """Строка-заголовок турнира на TennisExplorer, а не строка игрока.

    Заголовок («Prague 2 challenger») несёт такой же td.t-name, как и игрок,
    поэтому проверка «есть имя» его не отсеивает. Раньше это ломало разбивку
    на пары: строки идут заголовок / игрок1 / игрок2 / заголовок…, а шаг
    idx += 2 склеивал заголовок с первым игроком, а его соперника — со
    следующим заголовком. Настоящая пара не образовывалась, и ПЕРВЫЙ МАТЧ
    КАЖДОГО ТУРНИРА молча терялся: ставка навсегда оставалась «в игре».

    Признаков три, и хватает любого:
      * класс 'head' у строки;
      * colspan у ячейки с именем (у игрока его нет);
      * ссылка ведёт на турнир (/prague-2-challenger/2026/atp-men/), а не на
        участника — так выглядит вторая разновидность заголовка, у которой
        класс обычный ('one') и colspan отсутствует, поэтому по первым двум
        признакам она не ловится.

    Участник — это и /player/, и /doubles-team/: пары идут такими же двумя
    строками, и отбрасывать их нельзя (проверять по одному лишь /player/ —
    значит потерять весь парный разряд, это около 60 матчей в день).

    Строку без ссылки считаем участником: у малоизвестных игроков профиля на
    сайте может не быть, и терять такие матчи нельзя.
    """
    if "head" in (tr.get("class") or []):
        return True
    td = tr.find("td", class_=re.compile(r"\bt-name\b"))
    if td is None:
        return False
    if td.get("colspan"):
        return True
    a = td.find("a")
    href = (a.get("href") or "") if a else ""
    if not href:
        return False
    return not ("/player/" in href or "/doubles-team/" in href)


def check_tennis_explorer_results():
    """Ищет результаты (счет) сыгранных матчей на tennisexplorer.com и закрывает соответствующие ставки.
    Разметка результатов на tennisexplorer.com — это таблица table.result, где каждый матч занимает
    ДВЕ строки (по одной на игрока), а не одну строку с готовым текстом счета, как предполагалось раньше —
    из-за этого поиск результатов не находил вообще ничего. Если после этой правки результаты все еще не
    подтягиваются, смотри в консоли строки "TennisExplorer: найдено N завершенных матчей" — они покажут,
    доходит ли бот до сайта и парсит ли он хоть что-то (сайт мог снова поменять верстку или блокировать запросы)."""
    db = load_db()
    unresolved = [m for m in db["bets"] if not m["resolved"]]
    if not unresolved: return

    dates_to_check = set()
    now = get_msk_time()
    for i in range(4):
        dates_to_check.add((now - datetime.timedelta(days=i)).strftime("%Y-%m-%d"))

    found_matches = []  # список (игрок1, игрок2, счет по сетам "6-3,4-6,6-4")

    for date_str in sorted(dates_to_check):
        try:
            y, m, d = date_str.split("-")
            te_url = f"https://www.tennisexplorer.com/results/?type=all&year={y}&month={m}&day={d}"
            resp = requests.get(te_url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"⚠️ TennisExplorer: HTTP {resp.status_code} за {date_str}")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            day_found = 0

            # Основной способ: table.result, матч = две соседние строки (игрок1 / игрок2)
            for table in soup.find_all("table", class_=re.compile(r"\bresult\b")):
                # Заголовки турниров выкидываем ДО разбивки на пары, иначе они
                # сбивают её по фазе — см. _is_te_head_row.
                rows = [tr for tr in table.find_all("tr")
                        if not _is_te_head_row(tr)]
                idx = 0
                while idx < len(rows) - 1:
                    row1, row2 = rows[idx], rows[idx + 1]
                    name1_td = row1.find("td", class_=re.compile(r"\bt-name\b"))
                    name2_td = row2.find("td", class_=re.compile(r"\bt-name\b"))
                    if not (name1_td and name2_td):
                        idx += 1
                        continue

                    set_scores = _te_row_scores

                    games1, games2 = set_scores(row1), set_scores(row2)
                    if games1 and games2 and len(games1) == len(games2):
                        score_str = ",".join(f"{a}-{b}" for a, b in zip(games1, games2))
                        # Первая числовая ячейка — колонка result (итог по
                        # сетам). Если там 1:0, матч присуждён: доигран он не
                        # был. Пометки «ret.» в разметке может не быть, а
                        # ниже по течению снятие определяется именно по ней —
                        # дописываем сами, иначе итог 1:0 уедет в сеты.
                        awarded = _te_awarded(games1[0], games2[0])
                        if awarded and "ret" not in score_str.lower():
                            score_str += " ret."
                        found_matches.append((name1_td.get_text(" ", strip=True), name2_td.get_text(" ", strip=True), score_str, awarded))
                        day_found += 1
                    idx += 2

            # Запасной способ: вдруг счет все же лежит одной строкой ("Игрок1 - Игрок2" + td.result)
            if day_found == 0:
                for tr in soup.find_all("tr"):
                    name_td = tr.find("td", class_=re.compile(r"\bt-name\b"))
                    res_td = tr.find("td", class_=re.compile(r"\bresult\b"))
                    if name_td and res_td:
                        names_text = name_td.get_text(" ", strip=True)
                        score = res_td.get_text(" ", strip=True)
                        if " - " in names_text and score:
                            n1, n2 = [p.strip() for p in names_text.split(" - ", 1)]
                            found_matches.append((n1, n2, score, ""))
                            day_found += 1

            print(f"ℹ️ TennisExplorer {date_str}: найдено {day_found} завершенных матчей")
        except Exception as e:
            print(f"❌ Ошибка проверки TennisExplorer за {date_str}: {e}")

    if not found_matches:
        print("⚠️ TennisExplorer: результаты не найдены ни за один день — возможно, сайт снова изменил верстку или блокирует запросы бота.")

    settled_any = False
    for match in unresolved:
        if match["resolved"]: continue
        p1_last = remove_accents(match["player1"].split()[-1].lower())
        p2_last = remove_accents(match["player2"].split()[-1].lower())

        for n1, n2, score_str, awarded in found_matches:
            n1_norm, n2_norm = remove_accents(n1.lower()), remove_accents(n2.lower())
            same_order = p1_last in n1_norm and p2_last in n2_norm
            reversed_order = p1_last in n2_norm and p2_last in n1_norm
            if (same_order or reversed_order) and score_str and not score_str.replace(" ", "").replace("-", "").isalpha():
                # TennisExplorer выводит игроков в своём порядке — часто
                # победителем вперёд, а не так, как матч записан у нас.
                # reversed_order раньше вычислялся и не использовался: счёт
                # уходил в расчёт как есть, и все ставки матча считались
                # наоборот. Проигрыш по П2 при реально выигравшем П2 — это
                # не косметика, это неверные деньги в отчёте.
                # Порядок игроков у TennisExplorer свой — переворачиваем
                # вместе со счётом и присуждённого победителя, иначе
                # исход снятого матча уедет на соперника.
                won = awarded
                if reversed_order and won:
                    won = "p2" if won == "p1" else "p1"
                resolve_match(match, flip_score(score_str) if reversed_order
                              else score_str, won)
                settled_any = True
                break

    save_db(db)
    # Только после save_db и только если что-то закрылось: пересборка читает
    # базу с диска и переписывает весь журнал целиком.
    if settled_any:
        regenerate_csv_from_db()

def resolve_match(match, score_str, awarded=""):
    # В базу кладём разобранный счёт, а не сырую строку TennisExplorer.
    # Сырая («2-1,66-7,6-3,6-3») содержит итог по сетам первым токеном и
    # склеенный тайбрейк, и каждый потребитель — чат, CSV, панель — разбирал
    # её у себя. Достаточно было забыть одно место, чтобы «6-7(6)» снова
    # показалось как «66-7». Приводим один раз здесь.
    match["score_raw"] = score_str
    match["score"] = pretty_score(score_str, with_tiebreak=True) or score_str
    match["resolved"] = True
    match["resolved_ts"] = time.time()
    
    is_retired = "ret" in score_str.lower()
    sets_p1, sets_p2, games_p1, games_p2, parsed_sets = parse_match_result(score_str)

    match["games_p1"] = games_p1
    match["games_p2"] = games_p2
    match["sets_p1"] = sets_p1
    match["sets_p2"] = sets_p2
    games_diff_p1 = games_p1 - games_p2
    
    # Победитель снятого матча — только присуждённый и только если доигран
    # хотя бы один полный сет. Иначе аннулируется всё, включая исход.
    ml_winner = awarded if (is_retired and _completed_sets(parsed_sets) >= 1) else ""

    pretty = ", ".join(f"{a}-{b}({tb})" if tb else f"{a}-{b}" for a, b, tb in parsed_sets)
    msg_lines = [f"✅ <b>Матч завершен:</b>\n🎾 {match['match']}\n"
                 f"Счет: {pretty or score_str} (по сетам {sets_p1}-{sets_p2})"
                 f"{' — отказ' if is_retired else ''}\n"]
    
    for bet in match["bets"]:
        pred = bet["prediction"]
        bet_won, is_refund = False, False
        
        if is_retired:
            # Правила Pinnacle, те же что у обходчика (tennisratioall/value.py):
            #   * ставка на победителя СТОИТ, если доигран хотя бы один полный
            #     сет — снявшийся объявляется проигравшим независимо от счёта;
            #   * фора и тотал аннулируются ВСЕГДА;
            #   * снятие до конца первого сета аннулирует вообще всё.
            # Победителя по счёту не вычислить: сняться может и ведущий
            # (Kwon — Lajovic 25.08.2026). Его называет колонка result.
            if bet["type"] == "Moneyline" and ml_winner in ("p1", "p2"):
                bet_won = ("П1" in pred) == (ml_winner == "p1")
            else:
                is_refund = True
        else:
            if bet["type"] == "Moneyline":
                if "П1" in pred and sets_p1 > sets_p2: bet_won = True
                elif "П2" in pred and sets_p2 > sets_p1: bet_won = True
            elif bet["type"] == "Games Hcap" or bet["type"] == "Sets Hcap":
                # Оставлено для ставок, добавленных до перехода на "ТБ (сеты)"
                h_match = re.search(r'([+-]?\d+\.?\d*)', pred)
                if h_match:
                    h_val = float(h_match.group(1))
                    if bet["type"] == "Games Hcap":
                        result_diff = (games_diff_p1 + h_val) if "Ф1" in pred else ((-games_diff_p1) + h_val)
                    else: 
                        sets_diff_p1 = sets_p1 - sets_p2
                        result_diff = (sets_diff_p1 + h_val) if "Ф1" in pred else ((-sets_diff_p1) + h_val)

                    if result_diff > 0: bet_won = True
                    elif result_diff == 0: is_refund = True
            elif bet["type"] == "Total Sets":
                total_played = sets_p1 + sets_p2
                line_match = re.search(r'([\d.]+)', pred)
                line_val = float(line_match.group(1)) if line_match else 2.5
                is_over = "ТБ" in pred
                if is_over:
                    if total_played > line_val: bet_won = True
                    elif total_played == line_val: is_refund = True
                else:
                    if total_played < line_val: bet_won = True
                    elif total_played == line_val: is_refund = True
                
        if is_refund:
            bet["status"] = "refund"; bet["profit"] = 0
            res_str = "🔄 Возврат"; prof_str = "0₽"
        elif bet_won:
            bet["status"] = "win"; bet["profit"] = bet["stake"] * (bet["odds"] - 1.0)
            res_str = "✅ Выигрыш"; prof_str = f"+{bet['profit']:.2f}₽"
        else:
            bet["status"] = "loss"; bet["profit"] = -bet["stake"]
            res_str = "❌ Проигрыш"; prof_str = f"{bet['profit']:.2f}₽"
            
        msg_lines.append(f"• {bet['type']} ({pred}): {res_str} ({prof_str})")
        
    send_notification("\n".join(msg_lines))
    # CSV пересобирает вызывающая сторона ПОСЛЕ save_db(). Здесь этого делать
    # нельзя: regenerate_csv_from_db() перечитывает базу с диска, а расчёт
    # правит её в памяти и сохраняет только в конце цикла по матчам. Журнал
    # собирался из ещё не сохранённой базы и отставал на один расчёт —
    # в bets_history.csv закрытый матч так и висел «В игре».

def build_period_report_csv(period_rows, stats, total_bets, total_wins, total_turnover, total_profit, period_name):
    """Строит CSV-отчет за период: сами ставки + сводный блок статистики (ROI, ставки, победы/поражения по типам)."""
    path = f"/tmp/bets_report_{uuid.uuid4().hex[:8]}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Турнир", "Дата и время", "Событие", "Прогноз", "Ставка", "Коэф.", "Букм.", "Прибыль", "Счёт", "Сумма геймов", "Разница геймов"])
        for match, bet in period_rows:
            prof_str = "0₽"
            if bet["status"] == "win": prof_str = f"+{bet['profit']:.2f}₽"
            elif bet["status"] in ["loss", "refund"]: prof_str = f"{bet['profit']:.2f}₽"

            games_diff, total_games = "", ""
            if match["resolved"]:
                g1, g2 = match.get("games_p1", 0), match.get("games_p2", 0)
                total_games = str(g1 + g2)
                diff = (g2 - g1) if (("П2" in bet["prediction"]) or ("Ф2" in bet["prediction"])) else (g1 - g2)
                games_diff = f"+{diff}" if diff > 0 else str(diff)

            writer.writerow([
                match.get("tournament", ""), match.get("date", ""), match.get("match", ""),
                bet.get("prediction", ""), f"{bet.get('stake', BET_AMOUNT)}₽", f"{bet.get('odds', 0):.3f}", "Pin",
                prof_str, pretty_score(match.get("score", "")), total_games, games_diff
            ])

        writer.writerow([])
        writer.writerow([f"СТАТИСТИКА ЗА {period_name.upper()}"])
        writer.writerow(["Тип ставки", "Ставок", "Выигрышей", "Проигрышей", "Оборот", "Прибыль", "ROI"])
        for stype, s in stats.items():
            if s["bets"] == 0: continue
            roi = (s["profit"] / s["turnover"] * 100) if s["turnover"] > 0 else 0
            losses = s["bets"] - s["wins"]
            writer.writerow([stype, s["bets"], s["wins"], losses, f"{s['turnover']}₽", f"{s['profit']:.2f}₽", f"{roi:.2f}%"])
        if total_bets > 0:
            total_roi = (total_profit / total_turnover * 100) if total_turnover > 0 else 0
            total_losses = total_bets - total_wins
            writer.writerow(["ИТОГО", total_bets, total_wins, total_losses, f"{total_turnover}₽", f"{total_profit:.2f}₽", f"{total_roi:.2f}%"])
    return path

def generate_report(period_name, days_back):
    db = load_db()
    now_ts = time.time()
    period_sec = days_back * 86400 if days_back else 0
    
    stats = {
        "Moneyline": {"bets": 0, "wins": 0, "profit": 0, "turnover": 0},
        "Games Hcap": {"bets": 0, "wins": 0, "profit": 0, "turnover": 0},
        "Sets Hcap": {"bets": 0, "wins": 0, "profit": 0, "turnover": 0},
        "Total Sets": {"bets": 0, "wins": 0, "profit": 0, "turnover": 0},
    }
    total_profit, total_turnover, total_bets, total_wins = 0, 0, 0, 0
    period_rows = []  # (match, bet) пары, попавшие в отчет за период — идут в CSV-вложение

    for match in db["bets"]:
        if not match["resolved"]: continue
        # Фильтруем по дате РАСЧЕТА ставки (resolved_ts), а не по дате её добавления (added_ts).
        # Раньше ставка, сделанная давно, но рассчитавшаяся только сейчас (например, из-за задержки
        # поиска результатов), никогда не попадала ни в один периодический отчет — это и искажало ROI.
        match_ts = match.get("resolved_ts", match["added_ts"])
        if days_back == 30: 
            msk_now = get_msk_time()
            dt = datetime.datetime.fromtimestamp(match_ts)
            if dt.month != msk_now.month or dt.year != msk_now.year: continue
        elif days_back > 0: 
            if now_ts - match_ts > period_sec: continue
            
        for bet in match["bets"]:
            if bet["status"] == "refund":
                period_rows.append((match, bet))
                continue 
            
            b_type = bet["type"]
            if b_type not in stats:
                stats[b_type] = {"bets": 0, "wins": 0, "profit": 0, "turnover": 0}
            stats[b_type]["bets"] += 1
            stats[b_type]["turnover"] += bet["stake"]
            stats[b_type]["profit"] += bet["profit"]
            
            total_bets += 1
            total_turnover += bet["stake"]
            total_profit += bet["profit"]
            if bet["status"] == "win":
                stats[b_type]["wins"] += 1
                total_wins += 1

            period_rows.append((match, bet))

    msg = f"📊 <b>Отчет по ставкам за {period_name}</b>\n\n"
    for stype, s in stats.items():
        if s["bets"] == 0: continue
        roi = (s["profit"] / s["turnover"] * 100) if s["turnover"] > 0 else 0
        wr = (s["wins"] / s["bets"] * 100) if s["bets"] > 0 else 0
        msg += f"🔹 <b>{stype}</b>:\nСтавок: {s['bets']} (Зашло {s['wins']}, {wr:.1f}%)\nОборот: {s['turnover']}₽\nПрибыль: {s['profit']:.2f}₽\nROI: {roi:.2f}%\n\n"
        
    if total_bets > 0:
        total_roi = (total_profit / total_turnover * 100)
        msg += f"📈 <b>ИТОГО:</b>\nВсего: {total_bets}\nОборот: {total_turnover}₽\nПрибыль: <b>{total_profit:.2f}₽</b>\nROI: <b>{total_roi:.2f}%</b>"
    else:
        msg += "Нет рассчитанных ставок за этот период."
        
    send_notification(msg)

    report_path = build_period_report_csv(period_rows, stats, total_bets, total_wins, total_turnover, total_profit, period_name)
    send_email_with_csv(period_name, csv_path=report_path, filename=build_report_filename())
    try: os.remove(report_path)
    except: pass

def monitor():
    print("📢 Инициализация системы...")
    init_csv()
    db = load_db()
    print(f"✅ Загружена история ставок: {len(db['bets'])} матчей.")

    bot_state = load_state()
    pending_tasks = bot_state.get("pending_tasks", {})
    awaiting_bets = bot_state.get("awaiting_bets", {})
    link_actions = bot_state.get("link_actions", {})

    # Сообщение о старте шлем СРАЗУ, до тяжелых сетевых парсингов (рейтинги + список матчей).
    # Раньше оно отправлялось только после них, поэтому при медленном/недоступном tennisratio.com
    # бот молчал минутами, а при ошибке внутри парсинга уведомление не приходило вообще.
    send_notification(
        f"✅ Бот запущен!\nЗагружаю базу матчей...\n"
        f"⏳ В ожидании кэфов после рестарта: {len(pending_tasks)}\n\n"
        "<b>Команды:</b>\n/pending — Ожидание кэфов\n/memory — Память\n/results — Проверить результаты вручную\n/screenshot <ссылка> — Скриншот страницы\n/h2h Игрок1 - Игрок2 — Форма (10 матчей) + H2H\n/claudetest — Проверить ключ и инструменты Claude\n\n<i>По ссылке на матч: Кэфы · H2H · Статистика + симуляция · Симуляция · Разбор · Claude</i>"
    )

    try:
        yelo_ratings = parse_yelo_ratings()
        surface_elo_ratings = parse_surface_elo_ratings()
        initial_matches = parse_matches(yelo_ratings, surface_elo_ratings)
    except Exception as e:
        print(f"❌ Ошибка загрузки базы матчей: {e}")
        send_notification(f"⚠️ Не удалось загрузить базу матчей при старте: {e}\nБот продолжит работу и повторит попытку позже.")
        yelo_ratings, surface_elo_ratings, initial_matches = {}, {}, {}

    seen_slugs = set(initial_matches.keys())
    latest_matches = initial_matches
    print(f"✅ База теннисных матчей загружена: {len(seen_slugs)} шт.")
    send_notification(f"🎾 База матчей загружена: {len(seen_slugs)} шт. Ожидаю ссылки.")
    
    tg_offset = None
    update_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    
    last_elo_update = time.time()
    last_pinnacle_check = time.time()
    last_te_check = time.time()
    last_tennis_check = time.time()
    
    report_flags = {"daily": -1, "weekly": -1, "monthly": -1}

    while True:
        time.sleep(3)
        current_time = time.time()
        now_msk = get_msk_time()

        params = {"timeout": 5, "allowed_updates": ["message", "callback_query"]}
        if tg_offset: params["offset"] = tg_offset
        
        try:
            resp = requests.get(update_url, params=params, timeout=10).json()
            if resp.get("ok"):
                for update in resp.get("result", []):
                    tg_offset = update["update_id"] + 1
                    
                    if "callback_query" in update:
                        cq = update["callback_query"]
                        data = cq.get("data", "")
                        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                        
                        if data.startswith(("odds_", "h2h_", "sim_", "deep_", "claude_", "local_", "stats_")):
                            action, act_id = data.split("_", 1)
                            info = link_actions.get(act_id)
                            if not info:
                                send_notification("❌ Данные по этому матчу устарели. Пришли ссылку заново.", chat_id)
                            else:
                                p1, p2 = info["p1"], info["p2"]
                                real_tournament, real_date = info["tournament"], info["date"]
                                slug = info["slug"]

                                if action == "stats":
                                    if not STATS_PARSER_OK:
                                        send_notification(
                                            "❌ Модуль статистики и симуляции не подключён.\n"
                                            f"<code>{STATS_PARSER_ERR[:300]}</code>\n"
                                            "Положи папку <code>tennis_parser/</code> рядом с ботом и поставь зависимости.",
                                            chat_id
                                        )
                                    else:
                                        send_notification(
                                            f"📊 Парсю статистику {p1} vs {p2} и считаю симуляцию…\n"
                                            "<i>История матчей рендерится в браузере, 30–60 с. Симуляция посчитается следом, из тех же данных.</i>",
                                            chat_id
                                        )
                                        # покрытие берём у штатного детектора бота:
                                        # он уже умеет лезть на страницу турнира и кэширует результат
                                        detected_surface = ""
                                        try:
                                            detected_surface = get_surface_from_text(real_tournament) or ""
                                            if not detected_surface:
                                                cache = load_surface_cache()
                                                detected_surface = get_tournament_surface(
                                                    real_tournament, info.get("tournament_href", ""), cache) or ""
                                                save_surface_cache(cache)
                                        except Exception as e:
                                            print(f"Не удалось определить покрытие: {e}")

                                        threading.Thread(
                                            target=run_stats_parsing,
                                            args=(send_notification, chat_id, info.get("url", "")),
                                            kwargs={
                                                "p1": p1, "p2": p2,
                                                "tournament": real_tournament,
                                                "surface": (detected_surface or "").lower() or None,
                                                "reply_to": info.get("message_id"),
                                            },
                                            daemon=True
                                        ).start()

                                elif action == "local":
                                    threading.Thread(
                                        target=run_local_simulation_only,
                                        args=(p1, p2, real_tournament, real_date, chat_id, surface_elo_ratings),
                                        daemon=True
                                    ).start()

                                elif action == "claude":
                                    send_notification(f"🧠 Собираю данные и статистику со страницы для {p1} vs {p2}...", chat_id)
                                    threading.Thread(
                                        target=run_claude_simulation,
                                        args=(p1, p2, real_tournament, real_date, chat_id,
                                              surface_elo_ratings, info.get("url", "")),
                                        daemon=True
                                    ).start()

                                elif action == "sim":
                                    send_notification(f"🎲 Считаю симуляцию матча {p1} vs {p2}...\n"
                                                      "<i>Режим максимальной глубины — может занять до 15–20 минут.</i>", chat_id)
                                    threading.Thread(
                                        target=run_gemini_simulation,
                                        args=(p1, p2, real_tournament, real_date, chat_id, surface_elo_ratings, True),
                                        daemon=True
                                    ).start()

                                elif action == "deep":
                                    send_notification(f"🔬 Собираю данные и запускаю разбор {p1} vs {p2}...\n"
                                                      "<i>Займёт несколько минут.</i>", chat_id)
                                    threading.Thread(
                                        target=run_gemini_deep_analysis,
                                        args=(p1, p2, real_tournament, real_date, chat_id,
                                              surface_elo_ratings, False, info.get("url", "")),
                                        daemon=True
                                    ).start()

                                elif action == "h2h":
                                    send_notification(f"🔎 Собираю форму и H2H: {p1} vs {p2}...", chat_id)
                                    try:
                                        card = get_players_form_card(p1, p2, tournament=real_tournament, match_date=real_date, surface_elo_ratings=surface_elo_ratings)
                                        if card["success"]:
                                            send_photo(card["path"], chat_id, caption=f"Форма и H2H: {p1} vs {p2}")
                                            print(f"   Карточка: {card.get('note','')}")
                                        else:
                                            send_notification(f"ℹ️ Форму игроков собрать не удалось: {card['error']}", chat_id)
                                        if card.get("path") and os.path.exists(card["path"]):
                                            try: os.remove(card["path"])
                                            except: pass
                                    except Exception as e:
                                        send_notification(f"ℹ️ Не удалось собрать форму игроков: {e}", chat_id)
                                else:
                                    send_notification(f"⏳ Ищу кэфы для {p1} vs {p2}...", chat_id)
                                    odds = get_pinnacle_odds(p1, p2, is_manual=True)
                                    if odds and not odds.get("error"):
                                        match_name = f"{p1} - {p2}"
                                        potential_bets = calculate_potential_bets(p1, p2, odds, real_tournament)
                                        if potential_bets:
                                            bet_id = str(uuid.uuid4())[:8]
                                            awaiting_bets[bet_id] = {
                                                "p1": p1, "p2": p2, "tournament": real_tournament,
                                                "date": real_date, "match_name": match_name, "bets": potential_bets
                                            }
                                            save_state({"pending_tasks": pending_tasks, "awaiting_bets": awaiting_bets, "link_actions": link_actions})
                                            bets_text = "\n".join([f"🔹 {b['type']}: <b>{b['prediction']}</b> (Кэф: {b['odds']})" for b in potential_bets])
                                            rm = {"inline_keyboard": [[{"text": "✅ Ставь!", "callback_data": f"bet_{bet_id}"}]]}
                                            attrib = format_odds_attribution(p1, p2, odds, surface_elo_ratings)
                                            send_notification(f"🎯 <b>Кэфы найдены!</b>\n🎾 {match_name}\n\n{bets_text}{attrib}\n\n👉 <i>Жми кнопку:</i>", chat_id, rm)
                                        else:
                                            send_notification("⚠️ Нужные форы/исходы не доступны.", chat_id)
                                    else:
                                        err_text = odds["error"] if odds and odds.get("error") else "Кэфы пока не опубликованы."
                                        send_notification(f"ℹ️ {err_text}\nПоставил на авто-ожидание.", chat_id)
                                        pending_tasks[slug] = {"chat_id": chat_id, "players": (p1, p2), "date": real_date, "tournament": real_tournament}
                                        save_state({"pending_tasks": pending_tasks, "awaiting_bets": awaiting_bets, "link_actions": link_actions})

                        elif data.startswith("bet_"):
                            bet_id = data.split("_")[1]
                            if bet_id in awaiting_bets:
                                info = awaiting_bets[bet_id]
                                match_id = f"{info['match_name']}_{info['date']}".replace(" ", "_")
                                
                                if save_approved_bets(match_id, info['date'], info['tournament'], info['match_name'], info['p1'], info['p2'], info['bets']):
                                    send_notification(f"✅ <b>Успешно!</b>\nСтавки на <b>{info['match_name']}</b> занесены в базу и таблицу.", chat_id)
                                else:
                                    send_notification(f"⚠️ Эти ставки уже были занесены ранее.", chat_id)
                                del awaiting_bets[bet_id]
                                save_state({"pending_tasks": pending_tasks, "awaiting_bets": awaiting_bets})
                            else:
                                send_notification("❌ Эта ставка уже обработана или срок её действия истек.", chat_id)
                                
                        # timeout обязателен: без него requests ждёт ответа
                        # вечно (poll с бесконечным таймаутом), и весь цикл
                        # monitor() встаёт молча — служба «active», процесс жив,
                        # но матчи не рассчитываются и афиша не обновляется.
                        # 26.08.2026 бот так простоял 9 часов после нажатия
                        # кнопки отчёта. Это был единственный вызов к Telegram
                        # без таймаута, остальные с 10/30 с.
                        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cq.get("id")}, timeout=10)
                        except: pass
                        continue

                    msg = update.get("message", {})
                    text, chat_id = msg.get("text", "") or "", str(msg.get("chat", {}).get("id", ""))
                    
                    if text == "/pending" and chat_id:
                        lines = [f"⏳ <b>Матчей в ожидании кэфов:</b> {len(pending_tasks)}\n"]
                        if pending_tasks:
                            for slug, info in pending_tasks.items():
                                p1, p2 = info["players"]
                                lines.append(f"  • {p1} vs {p2} (добавлен: {info['date']})")
                        send_notification("\n".join(lines), chat_id)

                    elif text == "/memory" and chat_id:
                        lines = [f"🧠 <b>Матчей в памяти:</b> {len(latest_matches)}\n"]
                        for slug, data in latest_matches.items():
                            clean_text = re.sub(r'<[^>]+>', '', data["text"]).split("📊")[0].strip()
                            lines.append(f"  • {data['date']} | {clean_text}")
                            
                        msg_str = "\n".join(lines)
                        while len(msg_str) > 4000:
                            idx = msg_str.rfind('\n', 0, 4000)
                            if idx == -1: idx = 4000
                            send_notification(msg_str[:idx], chat_id)
                            msg_str = msg_str[idx:].strip()
                        if msg_str: send_notification(msg_str, chat_id)

                    elif text == "/claudetest" and chat_id:
                        send_notification(f"🧪 Проверяю Claude API...\nАдрес: <code>{ANTHROPIC_API_URL}</code>\n"
                                          f"Модель: <code>{CLAUDE_MODEL}</code>\nКлюч: "
                                          f"{'задан' if ANTHROPIC_API_KEY else '<b>НЕ ЗАДАН</b>'}", chat_id)
                        # Шаг 1: простой запрос без инструментов — проверяем ключ и адрес
                        base = call_claude("Ответь одним словом: работает", max_tokens=100,
                                           web_search=False, code_execution=False)
                        if not base["success"]:
                            send_notification(f"❌ Базовый запрос не прошёл:\n{base['error']}", chat_id)
                        else:
                            send_notification(f"✅ Базовый запрос прошёл. Ответ: {base['text'][:200]}", chat_id)
                            # Шаг 2: проверяем, поддерживает ли шлюз серверные инструменты
                            adv = call_claude(
                                "Выполни кодом: посчитай сумму чисел от 1 до 100 и напиши только результат.",
                                max_tokens=2000)
                            if adv["success"]:
                                send_notification(
                                    f"Инструменты: {', '.join(adv.get('tools') or []) or 'не приняты'}\n"
                                    f"Запусков кода: {adv.get('code_runs', 0)}, поисков: {adv.get('searches', 0)}\n"
                                    f"Ответ: {adv['text'][:300]}\n\n"
                                    + ("✅ Исполнение кода работает — симуляции будут считаться точно."
                                       if adv.get("code_runs") else
                                       "⚠️ Код не выполнялся. Симуляции Claude будут неточными — "
                                       "проверь, поддерживает ли шлюз серверные инструменты."), chat_id)
                            else:
                                send_notification(f"⚠️ С инструментами не сработало:\n{adv['error']}", chat_id)

                    elif text == "/results" and chat_id:
                        db_before = load_db()
                        unresolved_before = {m["match_id"] for m in db_before["bets"] if not m["resolved"]}
                        send_notification(f"🔎 Проверяю результаты вручную (не завершено: {len(unresolved_before)})...", chat_id)
                        check_tennis_explorer_results()
                        db_after = load_db()
                        resolved_now = [m for m in db_after["bets"] if m["match_id"] in unresolved_before and m["resolved"]]
                        if resolved_now:
                            send_notification(f"✅ Проверка завершена. Закрыто матчей: {len(resolved_now)}.", chat_id)
                        else:
                            send_notification("ℹ️ Проверка завершена. Новых результатов не найдено (подробности — в консоли/логах бота).", chat_id)

                    elif text.lower().startswith("/screenshot") and chat_id:
                        parts = text.split(maxsplit=1)
                        if len(parts) < 2 or not parts[1].strip():
                            send_notification("Использование: /screenshot <ссылка>", chat_id)
                        else:
                            target_url = parts[1].strip()
                            if not target_url.startswith("http"):
                                target_url = "https://" + target_url
                            send_notification(f"📸 Открываю страницу и делаю скриншот...\n{target_url}", chat_id)
                            shot_path = f"/tmp/screenshot_{uuid.uuid4().hex[:8]}.png"
                            result = take_screenshot(target_url, output_path=shot_path)
                            if result["success"]:
                                send_photo(result["path"], chat_id, caption=target_url[:1000])
                                try: os.remove(result["path"])
                                except: pass
                            else:
                                send_notification(f"❌ Не удалось сделать скриншот: {result['error']}", chat_id)

                    elif text.lower().startswith("/h2h") and chat_id:
                        rest = text.split(maxsplit=1)
                        rest = rest[1].strip() if len(rest) > 1 else ""
                        # Покрытие больше не нужно указывать: оно показывается по каждому матчу цветом
                        for s in ["hard", "clay", "grass", "хард", "грунт", "трава", "ковер", "ковёр"]:
                            if rest.lower().endswith(s):
                                rest = rest[:-(len(s))].strip()
                                break
                        if " - " not in rest:
                            send_notification("Использование: /h2h Игрок1 - Игрок2\nНапример: /h2h Nicolas Kicker - Marco Cecchinato", chat_id)
                        else:
                            p1_name, p2_name = [x.strip() for x in rest.split(" - ", 1)]
                            send_notification(f"🔎 Собираю форму и H2H: {p1_name} vs {p2_name}...", chat_id)
                            result = get_players_form_card(p1_name, p2_name, surface_elo_ratings=surface_elo_ratings)
                            if result["success"]:
                                send_photo(result["path"], chat_id, caption=f"Форма и H2H: {p1_name} vs {p2_name}")
                                print(f"   Карточка: {result.get('note','')}")
                            else:
                                send_notification(f"❌ {result['error']}", chat_id)
                            if result.get("path") and os.path.exists(result["path"]):
                                try: os.remove(result["path"])
                                except: pass
                            
                    elif text.lower().startswith("ставь ") and chat_id:
                        parts = text.split()
                        if len(parts) > 1:
                            bet_id = parts[1]
                            if bet_id in awaiting_bets:
                                info = awaiting_bets[bet_id]
                                match_id = f"{info['match_name']}_{info['date']}".replace(" ", "_")
                                if save_approved_bets(match_id, info['date'], info['tournament'], info['match_name'], info['p1'], info['p2'], info['bets']):
                                    send_notification(f"✅ <b>Успешно!</b>\nСтавки на <b>{info['match_name']}</b> занесены.", chat_id)
                                else:
                                    send_notification(f"⚠️ Эти ставки уже были занесены ранее.", chat_id)
                                del awaiting_bets[bet_id]
                                save_state({"pending_tasks": pending_tasks, "awaiting_bets": awaiting_bets})
                            else:
                                send_notification("❌ ID ставки не найден.", chat_id)
                            
                    elif "tennisratio.com" in text and chat_id:
                        slug = text.split("/")[-1].replace(".html", "").strip().lower()
                        if "-vs-" in slug:
                            parts = slug.split("-vs-")
                            p1 = " ".join([p.capitalize() for p in parts[0].split("-")])
                            p2 = " ".join([p.capitalize() for p in parts[1].split("-")])
                            
                            # Ищем реальные турнир/раунд/дату и время матча (а не заглушку "Турнир из Ссылки")
                            match_info = latest_matches.get(slug)
                            if not match_info:
                                fresh_matches = parse_matches(yelo_ratings, surface_elo_ratings)
                                if fresh_matches:
                                    latest_matches = fresh_matches
                                    seen_slugs.update(fresh_matches.keys())
                                    match_info = latest_matches.get(slug)
                            real_tournament = match_info["tournament"] if match_info else "Неизвестный турнир"
                            real_date = match_info["date"] if match_info else now_msk.strftime("%d.%m.%Y")

                            # Вместо автозапуска — предлагаем выбрать действие кнопками
                            act_id = str(uuid.uuid4())[:8]
                            url_m = re.search(r'https?://\S+', text)
                            match_url = url_m.group(0).rstrip('.,;)') if url_m else text.strip()
                            link_actions[act_id] = {
                                "p1": p1, "p2": p2, "slug": slug, "url": match_url,
                                "tournament": real_tournament, "date": real_date, "chat_id": chat_id,
                                # нужен, чтобы прислать разбор ответом на саму ссылку
                                "message_id": msg.get("message_id")
                            }
                            save_state({"pending_tasks": pending_tasks, "awaiting_bets": awaiting_bets, "link_actions": link_actions})

                            reply_markup = {"inline_keyboard": [[
                                {"text": "💰 Кэфы", "callback_data": f"odds_{act_id}"},
                                {"text": "📊 H2H", "callback_data": f"h2h_{act_id}"},
                            ], [
                                {"text": "📊 Статистика + симуляция", "callback_data": f"stats_{act_id}"},
                            ], [
                                {"text": "📈 Локальная симуляция (мгновенно)", "callback_data": f"local_{act_id}"},
                            ], [
                                {"text": "🎲 Симуляция Gemini (макс. глубина)", "callback_data": f"sim_{act_id}"},
                            ], [
                                {"text": "🔬 Разбор Gemini (с нашими данными)", "callback_data": f"deep_{act_id}"},
                            ], [
                                {"text": "🧠 Симуляция Claude", "callback_data": f"claude_{act_id}"},
                            ]]}
                            info_line = f"🎾 <b>{p1} — {p2}</b>\n{real_tournament}\n{real_date}\n\n👉 <i>Что показать?</i>"
                            send_notification(info_line, chat_id, reply_markup)
        except: pass

        if current_time - last_tennis_check >= CHECK_INTERVAL:
            last_tennis_check = current_time
            current_matches = parse_matches(yelo_ratings, surface_elo_ratings)
            if current_matches:
                latest_matches = current_matches
                new_slugs = set(current_matches.keys()) - seen_slugs
                if new_slugs:
                    grouped_matches = {}
                    for slug in new_slugs:
                        m_date = current_matches[slug].get("date", "Неизвестная дата")
                        tournament = current_matches[slug]["tournament"]
                        if m_date not in grouped_matches: grouped_matches[m_date] = {}
                        if tournament not in grouped_matches[m_date]: grouped_matches[m_date][tournament] = []
                        grouped_matches[m_date][tournament].append(current_matches[slug]["text"])
                    
                    message_lines = ["🟢 <b>Обнаружены новые матчи:</b>\n"]
                    for m_date, tournaments in grouped_matches.items():
                        message_lines.append(f"📅 <b>{m_date}</b>")
                        for tournament, matches in tournaments.items():
                            message_lines.append(f"  <b>🏟️ {tournament} · {len(matches)} шт:</b>")
                            for m in matches: message_lines.append(f"    • {m}")
                        message_lines.append("")
                    send_notification("\n".join(message_lines).strip())
                    seen_slugs.update(new_slugs)

        if current_time - last_elo_update >= 86400:
            yelo_ratings = parse_yelo_ratings() or yelo_ratings
            surface_elo_ratings = parse_surface_elo_ratings() or surface_elo_ratings
            last_elo_update = current_time

        if current_time - last_pinnacle_check >= 1800:
            last_pinnacle_check = current_time
            state_changed = False
            for slug, info in list(pending_tasks.items()):
                p1, p2 = info["players"]
                odds = get_pinnacle_odds(p1, p2, is_manual=False)
                if odds and not odds.get("error"):
                    match_name = f"{p1} - {p2}"
                    # Турнир нужен ДО расчёта ставок: от него зависит формат
                    # матча, а значит и линия тотала сетов. Раньше он брался
                    # уже после и только для карточки.
                    fresh_info = latest_matches.get(slug)
                    real_tournament = fresh_info["tournament"] if fresh_info else info.get("tournament", "Неизвестный турнир")
                    real_date = fresh_info["date"] if fresh_info else info.get("date", "")
                    potential_bets = calculate_potential_bets(p1, p2, odds, real_tournament)

                    if potential_bets:
                        bet_id = str(uuid.uuid4())[:8]
                        awaiting_bets[bet_id] = {
                            "p1": p1, "p2": p2, "tournament": real_tournament, 
                            "date": real_date, "match_name": match_name, "bets": potential_bets
                        }
                        
                        bets_text = "\n".join([f"🔹 {b['type']}: <b>{b['prediction']}</b> (Кэф: {b['odds']})" for b in potential_bets])
                        bets_text += format_odds_attribution(p1, p2, odds, surface_elo_ratings)
                        reply_markup = {"inline_keyboard": [[{"text": "✅ Ставь!", "callback_data": f"bet_{bet_id}"}]]}
                        
                        msg_final = f"🚨 <b>ЛИНИЯ ОТКРЫЛАСЬ!</b> 🚨\n🎾 {match_name}\n\n{bets_text}\n\n👉 <i>Жми кнопку:</i>"
                        send_notification(msg_final, info["chat_id"], reply_markup)
                        
                    del pending_tasks[slug]
                    state_changed = True
                    
            if state_changed:
                save_state({"pending_tasks": pending_tasks, "awaiting_bets": awaiting_bets})

        if current_time - last_te_check >= 3600:
            last_te_check = current_time
            check_tennis_explorer_results()

        if now_msk.hour == 21 and now_msk.minute == 0 and report_flags["daily"] != now_msk.day:
            generate_report("Сегодня", 1)
            report_flags["daily"] = now_msk.day
            
        if now_msk.weekday() == 6 and now_msk.hour == 21 and now_msk.minute == 5 and report_flags["weekly"] != now_msk.day:
            generate_report("Неделю", 7)
            report_flags["weekly"] = now_msk.day

        if now_msk.day == 1 and now_msk.hour == 21 and now_msk.minute == 10 and report_flags["monthly"] != now_msk.month:
            generate_report("Прошлый Месяц", 30)
            report_flags["monthly"] = now_msk.month

if __name__ == "__main__":
    try:
        monitor()
    except KeyboardInterrupt:
        print("Остановлено вручную.")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            send_notification(f"💥 <b>Бот упал с ошибкой:</b>\n<code>{str(e)[:500]}</code>\nСмотри логи: journalctl -u имя_службы -n 50")
        except Exception:
            pass
        raise
