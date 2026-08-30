"""Сборка отчёта и форматтеры. Используется и CLI, и телеграм-ботом.

Вынесено отдельно, чтобы бот не дублировал логику из cli.py:
build_report() — единственное место, где склеиваются три источника.
"""

from __future__ import annotations

import html as _html
import logging
from datetime import date

from .comparison import RU as CMP_RU
from .comparison import sections as cmp_sections
from .comparison import surface_of
from .fatigue import compute_fatigue, fatigue_edge
from .http import Fetcher
from .tennisabstract import blended_elo, elo_win_probability, load_ratings
from .tennisratio import fetch_h2h

log = logging.getLogger(__name__)

SURFACE_RU = {"hard": "хард", "clay": "грунт", "grass": "трава"}


def build_report(
    fetcher: Fetcher,
    p1: str,
    p2: str,
    *,
    surface: str | None = None,
    surface_weight: float = 0.5,
    best_of: int = 3,
    tour: str = "atp",
    mode: str = "auto",
    headless: bool = True,
    force: bool = False,
    as_of: date | None = None,
    url: str | None = None,
    want_comparison: bool = True,
) -> dict:
    """Блокирующая функция. В боте вызывать через asyncio.to_thread()."""
    as_of = as_of or date.today()

    h2h = fetch_h2h(fetcher, p1, p2, mode=mode, headless=headless, force=force,
                    url=url, want_comparison=want_comparison,
                    surface=surface)
    ratings = load_ratings(fetcher, tour=tour, force=force)

    fat1 = compute_fatigue(h2h.matches_p1, as_of)
    fat2 = compute_fatigue(h2h.matches_p2, as_of)

    elo1 = ratings.get(h2h.player1.name or p1)
    elo2 = ratings.get(h2h.player2.name or p2)

    forecast = None
    if elo1 and elo2:
        a = blended_elo(elo1, surface, surface_weight)
        b = blended_elo(elo2, surface, surface_weight)
        if a is not None and b is not None:
            forecast = {
                "surface": surface,
                "elo_p1_used": round(a, 1),
                "elo_p2_used": round(b, 1),
                "p1_win_prob": round(elo_win_probability(a, b, best_of), 4),
                "p2_win_prob": round(1 - elo_win_probability(a, b, best_of), 4),
                "best_of": best_of,
            }

    return {
        "generated_at": as_of.isoformat(),
        # тур нужен форматтерам: подпись строки с рейтингом раньше была
        # захардкожена как «ATP», и в женских карточках стояло «ATP #257»
        "tour": tour,
        "h2h": h2h.as_dict(),
        "ratings": {
            "elo_updated": ratings.updated,
            "p1": elo1.as_dict() if elo1 else None,
            "p2": elo2.as_dict() if elo2 else None,
        },
        "fatigue": {"p1": fat1.as_dict(), "p2": fat2.as_dict(),
                    "edge": fatigue_edge(fat1, fat2)},
        "elo_forecast": forecast,
        "_matches": {"p1": h2h.matches_p1, "p2": h2h.matches_p2},  # объекты, не сериализуются
    }


def json_safe(report: dict) -> dict:
    """Копия отчёта без непечатаемых объектов — для json.dumps."""
    return {k: v for k, v in report.items() if not k.startswith("_")}


# ------------------------------------------------------------------ консоль
def format_console(r: dict) -> str:
    h = r["h2h"]
    p1, p2 = h["player1"], h["player2"]
    e1, e2 = r["ratings"]["p1"], r["ratings"]["p2"]
    f1, f2 = r["fatigue"]["p1"], r["fatigue"]["p2"]
    out = []

    def line(label, a, b):
        out.append(f"{label:<22} {str(a):>19} | {str(b):>19}")

    out.append("=" * 64)
    line("", p1["name"], p2["name"])
    out.append("-" * 64)
    line(_rank_label(r), f"#{p1['rank']}", f"#{p2['rank']}")
    line("52 нед.", f"{p1['wins_52w']}-{p1['losses_52w']}", f"{p2['wins_52w']}-{p2['losses_52w']}")
    if e1 and e2:
        line("Elo", e1["elo"], e2["elo"])
        line("hElo хард", e1["helo"], e2["helo"])
        line("cElo грунт", e1["celo"], e2["celo"])
        line("gElo трава", e1["gelo"], e2["gelo"])
        line("yElo", e1["yelo"], e2["yelo"])
    line("H2H", h["wins_p1"], h["wins_p2"])
    line("Отдых, дней", f1["days_rest"], f2["days_rest"])
    line("Матчей / 14 дн.", f1["matches_14d"], f2["matches_14d"])
    line("Минут / 14 дн.", f1["est_minutes_14d"], f2["est_minutes_14d"])
    line("Усталость", f"{f1['fatigue_score']} ({f1['fatigue_label']})",
         f"{f2['fatigue_score']} ({f2['fatigue_label']})")
    if r["elo_forecast"]:
        fc = r["elo_forecast"]
        line("Elo-вероятность", f"{fc['p1_win_prob']:.1%}", f"{fc['p2_win_prob']:.1%}")
    out.append("=" * 64)
    return "\n".join(out)


# ------------------------------------------------------------------ телеграм
def _rank_label(r: dict) -> str:
    """Подпись строки с рейтингом: ATP или WTA.

    Раньше стояло жёсткое «ATP», и женские карточки показывали «ATP #257».
    Тур кладёт в отчёт build_report; для старых отчётов без этого ключа
    остаётся прежнее поведение.
    """
    return (r.get("tour") or "atp").upper()


def _e(x) -> str:
    return _html.escape(str(x if x is not None else "—"))


def _short(name: str, width: int = 13) -> str:
    """'Benjamin Hassan' -> 'Hassan'. Только фамилия, без инициала.

    Инициал занимал три символа из тринадцати и при этом ничего не различал:
    в одном матче двух игроков различает уже фамилия. Освободившееся место
    уходит на саму фамилию — раньше «D. Strick…» обрезалась, теперь влезает
    целиком.

    Составные фамилии (van Assche, Hugues Herbert) сохраняются целиком:
    отбрасываем только первое слово, и только если слов больше одного.
    """
    parts = [p for p in name.split() if p]
    if len(parts) > 1:
        # Берём ПОСЛЕДНЕЕ слово, а не всё после имени: у «Filip Cristian
        # Jianu» фамилия Jianu, а не «Cristian Jianu». Частицы (van, de)
        # прицепляем обратно — «Luca Van Assche» -> «Van Assche».
        i = len(parts) - 1
        while i > 1 and parts[i - 1].lower().strip(".") in _NAME_PARTICLES:
            i -= 1
        name = " ".join(parts[i:])
    return name if len(name) <= width else name[: width - 1] + "…"


_NAME_PARTICLES = {"van", "von", "de", "del", "della", "di", "da", "dos",
                   "el", "al", "le", "la", "mc", "st"}


def _short_date(value) -> str:
    """Дата матча как «MM-DD».

    Год отбрасываем: все матчи из последних месяцев, и «2026-» съедало пять
    символов из двадцати четырёх ни за что.

    Match.date — это datetime.date (так его отдаёт _parse_date в
    tennisratio.py), но из встроенного в страницу JSON и из сохранённых
    слепков дата приезжает строкой. Раньше строковая ветка была
    единственной, и на настоящей дате форматирование падало с
    «'datetime.date' object has no attribute 'count'» — а вместе с ним
    отваливался весь отчёт и симуляция следом.
    """
    if value is None:
        return "?"
    if isinstance(value, date):        # datetime тоже сюда: он наследник date
        return value.strftime("%m-%d")
    text = str(value)
    return text[5:] if text.count("-") == 2 else text


def format_telegram(r: dict, *, show_matches: int = 3) -> str:
    """HTML для Telegram (parse_mode=HTML). Таблица — в <pre>, моноширинно."""
    h = r["h2h"]
    p1, p2 = h["player1"], h["player2"]
    e1, e2 = r["ratings"]["p1"], r["ratings"]["p2"]
    f1, f2 = r["fatigue"]["p1"], r["fatigue"]["p2"]

    head = f"<b>{_e(p1['name'])}</b>  vs  <b>{_e(p2['name'])}</b>"

    rows: list[tuple[str, str, str]] = []

    def add(label, a, b):
        rows.append((label, str(a if a is not None else "—"), str(b if b is not None else "—")))

    add(_rank_label(r), f"#{p1['rank']}" if p1["rank"] else None, f"#{p2['rank']}" if p2["rank"] else None)
    if p1["wins_52w"] is not None or p2["wins_52w"] is not None:
        add("52 нед.", f"{p1['wins_52w']}-{p1['losses_52w']}", f"{p2['wins_52w']}-{p2['losses_52w']}")
    def pts(row, key):
        """Только очки Elo. Место в рейтинге не показываем."""
        val = row.get(key)
        return f"{val:g}" if val is not None else None

    if e1 and e2:
        add("Elo", pts(e1, "elo"), pts(e2, "elo"))
        add("hElo хард", pts(e1, "helo"), pts(e2, "helo"))
        add("cElo грунт", pts(e1, "celo"), pts(e2, "celo"))
        add("gElo трава", pts(e1, "gelo"), pts(e2, "gelo"))
        add("yElo сезон", pts(e1, "yelo"), pts(e2, "yelo"))
        add("Пик Elo", pts(e1, "peak_elo"), pts(e2, "peak_elo"))
    add("H2H", h["wins_p1"], h["wins_p2"])
    add("Отдых, дн", f1["days_rest"], f2["days_rest"])
    add("Матчей 14д", f1["matches_14d"], f2["matches_14d"])
    add("Минут 14д", f1["est_minutes_14d"], f2["est_minutes_14d"])
    add("Подряд дн", f1["consecutive_days"], f2["consecutive_days"])
    add("Усталость", f1["fatigue_score"], f2["fatigue_score"])
    add("", f1["fatigue_label"], f2["fatigue_label"])
    if f1["win_streak"] is not None or f2["win_streak"] is not None:
        add("Серия", f1["win_streak"], f2["win_streak"])
    if r["elo_forecast"]:
        fc = r["elo_forecast"]
        add("Прогноз", f"{fc['p1_win_prob']:.1%}", f"{fc['p2_win_prob']:.1%}")

    # Ширину держим в TABLE_W: эта таблица читается чаще всех, и именно она
    # разъезжалась на телефоне, перенося фамилию второго игрока на строку
    # ниже. Имена уходят над блоком — в шапке они съедали половину места.
    # Колонка значений — ровно 6: столько занимает самое длинное число
    # (1438.3, 586.2, 38.2%). Всё, что шире, — это подписи вроде
    # «перегружен», их выносим отдельной строкой, иначе они раздували
    # колонку и метки слева сжимались до «hElo х» и «Прогно».
    cw = 6
    lw = TABLE_W - 2 * cw - 2
    table = ["-" * (lw + cw * 2 + 2)]
    for label, a, b in rows:
        if max(len(a), len(b)) > cw:
            # Строка с подписями («перегружен» / «устал»): метки у неё нет,
            # и пустая строка вместо метки смотрелась как обрыв таблицы.
            if label.strip():
                table.append(label[:lw])
            table.append(f"{a:>{TABLE_W // 2}}{b:>{TABLE_W - TABLE_W // 2}}")
            continue
        table.append(f"{label[:lw]:<{lw}} {a:>{cw}} {b:>{cw}}")

    parts = [head,
             f"<i>{_e(_short(p1['name'], 16))} / {_e(_short(p2['name'], 16))}</i>",
             f"<pre>{_e(chr(10).join(table))}</pre>"]

    if e1 and e2 and (e1.get("yelo") is None or e2.get("yelo") is None):
        parts.append("<i>Пустой yElo — игрок не набрал 5 побед в этом сезоне, "
                     "TennisAbstract его туда не включает.</i>")

    if r["elo_forecast"]:
        bo = r["elo_forecast"]["best_of"]
        fmt = f"bo{bo} — до {bo // 2 + 1} выигранных сетов"
        s = r["elo_forecast"].get("surface")
        if s:
            parts.append(f"Покрытие: <b>{_e(SURFACE_RU.get(s, s))}</b>\n"
                         f"Формат: <b>{fmt}</b>")
        else:
            parts.append(f"Формат: <b>{fmt}</b>")

    lm = h.get("last_meeting")
    if lm:
        parts.append(
            f"Последняя встреча: {_e(lm.get('when'))}, {_e(lm.get('tournament'))} — "
            f"победил {_e(lm.get('winner'))} {_e(lm.get('score'))}"
        )

    edge = r["fatigue"]["edge"]
    if edge["fresher"] != "равны":
        fresher = p1["name"] if edge["fresher"] == "p1" else p2["name"]
        parts.append(f"Свежее: <b>{_e(fresher)}</b> (Δ {abs(edge['delta_fatigue'])} п.)")

    for tag, fat in (("p1", f1), ("p2", f2)):
        notes = fat.get("notes") or []
        if notes:
            who = p1["name"] if tag == "p1" else p2["name"]
            parts.append(f"<i>{_e(who)}:</i> " + _e("; ".join(notes)))

    if show_matches and r.get("_matches"):
        for tag, card in (("p1", p1), ("p2", p2)):
            ms = r["_matches"][tag][:show_matches]
            if not ms:
                continue
            # ширина по факту, а не фиксированные 18 символов: счёт вида
            # '6-7(3) 7-6(6) 7-6(4)' обрезался прямо посреди тайбрейка
            sw = max((len(m.score or "") for m in ms), default=0)
            sw = min(max(sw, 8), 24)
            # Соперник получает то, что осталось, но не меньше шести букв —
            # иначе от фамилии остаётся многоточие.
            lines = []
            for m in ms:
                # локальное имя не date: оно бы затенило импорт datetime.date
                day = _short_date(m.date)
                # Цветом, а не буквой: серия из десяти строк читается одним
                # взглядом, W и L среди цифр глаз всё равно ищет
                res = "🟢" if m.won else ("🔴" if m.won is False else "⚪")
                # Фамилия, а не первые буквы имени: «Tomma…» не говорит
                # ничего, «Compagnucci» говорит всё
                rival = _short(m.rival or "", 14)
                # ширина даты фиксированная: у матча без даты стоит «?», и без
                # добивки он сдвигал счёт на четыре символа влево
                head_line = f"{day:<5} {res} {(m.score or ''):<{sw}}"
                # кружок занимает две колонки, отсюда +1 к длине
                if len(head_line) + 1 + 1 + len(rival) <= TABLE_W:
                    lines.append(f"{head_line} {rival}")
                else:
                    # Дата, счёт и соперник втроём в 24 символа не влезают.
                    # Резать фамилию до огрызка хуже, чем перенести её строкой
                    # ниже: список читается сверху вниз, отступ связывает.
                    lines.append(head_line.rstrip())
                    lines.append(f"        {rival}")
            parts.append(f"<b>{_e(card['name'])}</b>, последние матчи:\n"
                         f"<pre>{_e(chr(10).join(lines))}</pre>")

    cmp_data = h.get("comparison") or {}
    if cmp_sections(cmp_data):
        parts.extend(_comparison_blocks(cmp_data, p1["name"], p2["name"]))

    odds_block = _odds_block(h.get("api_p1"), h.get("api_p2"), p1["name"], p2["name"])
    if odds_block:
        parts.append(odds_block)

    upd = r["ratings"].get("elo_updated")
    if upd:
        parts.append(f"<i>Elo обновлён {_e(upd)}; расчёт на {_e(r['generated_at'])}</i>")

    return "\n\n".join(parts)


_CMP_SECTIONS = [
    ("overall", "Ключевые показатели (52 недели)"),
    ("serve", "Подача"),
    ("return", "Приём"),
    ("pressure", "Очки под давлением"),
]

# показатели, где меньше — лучше
_LOWER_IS_BETTER = {"df_per_game", "bps_to_defend_per_game"}
_PCT_KEYS = {
    "tiebreaks_won_pct", "first_serve_accuracy", "first_serve_points_won",
    "second_serve_points_won", "service_games_won", "break_points_saved",
    "return_first_serve_points", "return_second_serve_points",
    "return_games_won", "break_points_converted",
    "pressure_won_on_serve", "pressure_won_on_return",
}


def _fmt_cmp(key: str, val) -> str:
    if val is None:
        return "—"
    # проценты — всегда с одним знаком, иначе колонка прыгает: 74% против 62.3%
    return f"{val:.1f}%" if key in _PCT_KEYS else f"{val:g}"


# Предел ширины моноширинных блоков: Telegraph на телефоне показывает <pre>
# крупнее, чем чат Telegram, и всё длиннее переносится, ломая выравнивание.
TABLE_W = 24


def _comparison_blocks(cmp_data: dict, n1: str, n2: str) -> list[str]:
    """Секции сравнения таблицей. Стрелка отмечает, у кого показатель лучше."""
    blocks: list[str] = []
    data = cmp_sections(cmp_data)
    surf = surface_of(cmp_data)
    if surf and surf != "all":
        head = f"покрытие: {SURFACE_RU.get(surf, surf)}"
    else:
        head = "все покрытия"
    for group, title in _CMP_SECTIONS:
        items = data.get(group) or {}
        if not items:
            continue
        rows = []
        for key, pair in items.items():
            v1, v2 = pair.get("p1"), pair.get("p2")
            a, b = _fmt_cmp(key, v1), _fmt_cmp(key, v2)
            # стрелка, а не < или > : знак сравнения читался бы как
            # утверждение о величине и путал, ведь у части метрик лучше меньше
            # Кружок у каждого значения вместо одной стрелки посередине:
            # глазу не нужно решать, куда она указывает — зелёный сразу
            # виден на нужной стороне. Оба маркера всегда есть, поэтому
            # ширина строки не пляшет.
            m1 = m2 = "⚪"
            if v1 is not None and v2 is not None and v1 != v2:
                better_first = (v1 < v2) if key in _LOWER_IS_BETTER else (v1 > v2)
                m1, m2 = ("🟢", "🔴") if better_first else ("🔴", "🟢")
            rows.append((CMP_RU.get(key, key), a, b, m1, m2))

        lw = max(len(r[0]) for r in rows)
        cw = max(6, max(max(len(r[1]), len(r[2])) for r in rows))
        # Кружок в моноширинном шрифте занимает две колонки, а не одну —
        # это надо учитывать, иначе расчёт ширины врёт на четыре символа.
        visible = lw + 2 + (2 + cw) * 2
        header = ""

        if visible <= TABLE_W:
            lines = [f"{'':<{lw + 3}}{_short(n1, cw):>{cw}}"
                     f"{'':<3}{_short(n2, cw):>{cw}}",
                     "-" * (lw + cw * 2 + 6)]
            for label, a, b, m1, m2 in rows:
                lines.append(f"{label:<{lw}} {m1}{a:>{cw}} {m2}{b:>{cw}}")
        else:
            # Длинные названия (Dominance Efficiency и родня) в одну строку
            # с двумя колонками не помещаются — значения уходят под метку.
            # Имена игроков при этом выносятся над таблицей, иначе на них
            # не остаётся места.
            header = (f"<i>{_e(_short(n1, 18))} / {_e(_short(n2, 18))}</i>\n")
            lines = ["-" * min(TABLE_W, max(len(r[0]) for r in rows))]
            for label, a, b, m1, m2 in rows:
                lines.append(label)
                lines.append(f"  {m1}{a:>{cw}}  {m2}{b:>{cw}}")

        blocks.append(f"<b>{_e(title)}</b> <i>({_e(head)})</i>"
                      f"\n{header}<pre>{_e(chr(10).join(lines))}</pre>")
    if blocks:
        blocks.append("<i>🟢 лучший показатель, 🔴 худший, ⚪ поровну "
                      "(у двойных и отражаемых брейк-пойнтов лучше меньше).</i>")
    return blocks


def _odds_block(a1: dict | None, a2: dict | None, n1: str, n2: str) -> str | None:
    """Баланс по роли в котировках — данные есть только в API, в вёрстке их нет.

    Показывает, как игрок отрабатывает статус фаворита и андердога:
    92% побед в роли небольшого фаворита и 20% в роли андердога — разные
    вещи, которых не видно в общем проценте побед.
    """
    r1 = {o["label"]: o for o in (a1 or {}).get("odds_record", [])}
    r2 = {o["label"]: o for o in (a2 or {}).get("odds_record", [])}
    labels = [lab for lab in
              ["Явный фаворит", "Фаворит", "Небольшой фаворит",
               "Небольшой андердог", "Андердог", "Явный андердог"]
              if lab in r1 or lab in r2]
    if not labels:
        return None

    def cell(rec):
        return f"{rec['won']}/{rec['played']} {rec['pct']:g}%" if rec else "—"

    def rng(lab):
        """Диапазон кэфа роли — берём у того игрока, у кого он есть."""
        for src in (r1, r2):
            rec = src.get(lab)
            if rec and rec.get("odds_range"):
                return rec["odds_range"]
        return ""

    rows = [(lab, rng(lab), cell(r1.get(lab)), cell(r2.get(lab))) for lab in labels]
    lw = max(len(r[0]) for r in rows)
    rw = max((len(r[1]) for r in rows), default=0)
    cw = max(9, max(max(len(r[2]), len(r[3])) for r in rows))
    head_r = "кэф" if rw else ""
    lines = [f"{'':<{lw}}  {head_r:>{rw}}  {_short(n1, cw):>{cw}}  {_short(n2, cw):>{cw}}",
             "-" * (lw + rw + cw * 2 + 6)]
    for lab, rr, a, b in rows:
        lines.append(f"{lab:<{lw}}  {rr:>{rw}}  {a:>{cw}}  {b:>{cw}}")
    out = (f"<b>Баланс по роли в котировках</b>"
           f"\n<pre>{_e(chr(10).join(lines))}</pre>")
    if rw:
        out += ("\n<i>«свой» — кэф самого игрока, «соп.» — кэф соперника: "
                "андердожьи роли на tennisratio заданы именно ценой соперника.</i>")
    return out


def format_elo_telegram(row_dict: dict) -> str:
    r = row_dict
    lines = [
        f"Elo        {r['elo']}   (#{r['elo_rank']})",
        f"hElo хард  {r['helo']}   (#{r['helo_rank']})",
        f"cElo грунт {r['celo']}   (#{r['celo_rank']})",
        f"gElo трава {r['gelo']}   (#{r['gelo_rank']})",
        f"yElo       {r['yelo']}   (#{r['yelo_rank']})  "
        f"{r['yelo_wins']}-{r['yelo_losses']}",
        f"Пик        {r['peak_elo']} ({r['peak_month']})",
        f"ATP        #{r['atp_rank']}",
    ]
    return f"<b>{_e(r['player'])}</b>\n<pre>{_e(chr(10).join(lines))}</pre>"
