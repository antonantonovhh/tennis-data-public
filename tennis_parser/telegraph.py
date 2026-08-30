"""Публикация отчёта страницей на telegra.ph.

Зачем: отчёт со статистикой и симуляцией — это 8-12 блоков, которые в чат
уезжают несколькими сообщениями подряд и листаются неудобно. Telegraph отдаёт
одну ссылку, которая в Telegram разворачивается как Instant View.

Что важно знать про telegra.ph:
  * страницы ПУБЛИЧНЫЕ. Ссылку не угадать (в адресе случайный хвост), но и
    защиты никакой: у кого ссылка — тот и читает. Для ставок это стоит держать
    в голове;
  * API не требует ни бота, ни ключа. Аккаунт создаётся одним запросом,
    возвращает access_token, его и храним в файле рядом;
  * набор тегов ограничен. Наш отчёт состоит из <b>, <i> и <pre>, все три
    поддерживаются, поэтому вёрстка переносится один в один;
  * редактировать страницу может только владелец токена. Потеряете токен —
    старые страницы останутся висеть, но править их будет нечем.

Если telegra.ph недоступен (а он местами блокируется), publish() вернёт None,
и вызывающий код должен спокойно отправить обычные сообщения.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from html.parser import HTMLParser

log = logging.getLogger(__name__)

API = "https://api.telegra.ph"
TIMEOUT = 15

# что telegra.ph вообще принимает
ALLOWED_TAGS = {
    "a", "aside", "b", "blockquote", "br", "code", "em", "figcaption", "figure",
    "h3", "h4", "hr", "i", "iframe", "img", "li", "ol", "p", "pre", "s",
    "strong", "u", "ul", "video",
}
# наши теги, которых в списке нет, но смысл сохраняем
TAG_ALIAS = {"strong": "b", "em": "i"}

TOKEN_FILE = os.environ.get(
    "TP_TELEGRAPH_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".telegraph_token"),
)


# ------------------------------------------------------------------ разметка
class _NodeBuilder(HTMLParser):
    """HTML нашего отчёта -> дерево узлов Telegraph.

    Разбираем парсером, а не регулярками: у нас встречается вложенность вида
    <b>...</b> внутри <p>, и склейка строками ломалась бы на первом же
    неожиданном теге.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root: list = []
        self.stack: list[list] = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = TAG_ALIAS.get(tag, tag)
        if tag == "br":
            self.stack[-1].append({"tag": "br"})
            return
        if tag not in ALLOWED_TAGS:
            # неизвестный тег не роняет разбор — просто разворачиваем его
            # содержимое в текущий уровень
            self.stack.append(self.stack[-1])
            return
        node = {"tag": tag, "children": []}
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                node["attrs"] = {"href": href}
        self.stack[-1].append(node)
        self.stack.append(node["children"])

    def handle_endtag(self, tag):
        if len(self.stack) > 1:
            self.stack.pop()

    def handle_data(self, data):
        if data:
            self.stack[-1].append(data)


def html_to_nodes(html: str) -> list:
    """Разбивает отчёт на блоки и превращает в дерево Telegraph.

    Блоки разделены пустой строкой. Всё, что не <pre>, заворачивается в <p>,
    а одиночные переводы строк внутри абзаца становятся <br>: без этого
    Telegraph склеил бы строки в один поток.
    """
    out: list = []
    for block in (html or "").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if "<pre>" in block:
            src = block
        else:
            src = "<p>" + block.replace("\n", "<br>") + "</p>"
        b = _NodeBuilder()
        b.feed(src)
        b.close()
        out.extend(_clean(b.root))
    return out


def _clean(nodes: list) -> list:
    """Убирает пустые узлы — Telegraph отвергает пустой children у некоторых тегов."""
    res = []
    for n in nodes:
        if isinstance(n, str):
            if n.strip() or n == " ":
                res.append(n)
            continue
        ch = _clean(n.get("children") or [])
        if ch:
            n["children"] = ch
        else:
            n.pop("children", None)
            if n["tag"] not in ("br", "hr", "img"):
                continue
        res.append(n)
    return res


# ------------------------------------------------------------------ сеть
def _call(method: str, payload: dict) -> dict | None:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                 headers={"User-Agent": "tennis-parser"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        log.warning("telegra.ph %s не ответил: %s", method, exc)
        return None
    if not body.get("ok"):
        log.warning("telegra.ph %s отказал: %s", method, body.get("error"))
        return None
    return body.get("result")


def get_token(short_name: str = "tennis", author: str = "Tennis bot") -> str | None:
    """Токен из файла рядом с модулем; при отсутствии создаётся новый аккаунт."""
    env = os.environ.get("TP_TELEGRAPH_TOKEN")
    if env:
        return env
    try:
        if os.path.exists(TOKEN_FILE):
            tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
            if tok:
                return tok
    except OSError as exc:
        log.warning("не читается %s: %s", TOKEN_FILE, exc)

    res = _call("createAccount", {"short_name": short_name, "author_name": author})
    if not res or not res.get("access_token"):
        return None
    tok = res["access_token"]
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(tok)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError as exc:
        log.warning("токен не сохранён в %s: %s — при следующем запуске "
                    "создастся новый аккаунт", TOKEN_FILE, exc)
    return tok


def publish(title: str, html: str, *, author: str = "Tennis bot",
            author_url: str | None = None) -> str | None:
    """Публикует отчёт. Возвращает ссылку или None, если не вышло."""
    token = get_token(author=author)
    if not token:
        return None
    nodes = html_to_nodes(html)
    if not nodes:
        return None
    payload = {
        "access_token": token,
        "title": title[:256] or "Отчёт",
        "author_name": author[:128],
        "content": json.dumps(nodes, ensure_ascii=False),
        "return_content": "false",
    }
    if author_url:
        payload["author_url"] = author_url
    res = _call("createPage", payload)
    return res.get("url") if res else None
