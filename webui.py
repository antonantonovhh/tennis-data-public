"""Общая обвязка для веб-панелей: стили, вёрстка таблиц, сервер, доступ.

Вынесено отдельно, потому что панелей две — по одной на каждого бота — и
данные у них разные, а всё остальное одинаковое. Дублировать триста строк
CSS и обработчик запросов ради этого не стоит.
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import random
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Зона, в которой показываем время на панелях. Внутри всё считается и
# хранится в UTC — сдвиг только на выводе, чтобы сортировки и сравнения
# «матч уже начался?» не поехали.
#
# Время начала матча с tennisratio тоже UTC — проверено сверкой с Pinnacle
# (Baez — Hurkacz и Sakellaridis — Van Assche совпали до минуты), поэтому
# его можно переводить тем же сдвигом, что и наши отметки.
try:
    TZ_OFFSET_H = float(os.environ.get("DASH_TZ_OFFSET", "4"))
except ValueError:
    TZ_OFFSET_H = 4.0
TZ = timezone(timedelta(hours=TZ_OFFSET_H))
TZ_LABEL = f"UTC{TZ_OFFSET_H:+.0f}".replace("+0", "+").replace("-0", "-")


def to_local(dt: datetime | None) -> datetime | None:
    """UTC-время (или наивное, считаем его UTC) -> зона показа."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def fmt_when(raw: str) -> str:
    """Время начала матча «24.08. 15:00» (UTC) -> та же строка в зоне показа.

    Разбирать умеет parse_when из обходчика — он же знает про переход через
    год. Если строка не разобралась, отдаём как есть: лучше показать сырое
    значение, чем потерять его.
    """
    if not raw:
        return ""
    try:
        from tennisratioall.results import parse_when  # noqa: PLC0415
        t = parse_when(str(raw))
    except Exception:  # noqa: BLE001
        t = None
    if not t:
        return str(raw)
    return to_local(t).strftime("%d.%m %H:%M")


def fmt_stamp(raw) -> str:
    """Наша отметка времени (ISO-строка или unix) -> «08-23 09:18» в зоне показа."""
    if raw in (None, ""):
        return ""
    dt = None
    if isinstance(raw, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(raw), timezone.utc)
        except (ValueError, OSError):
            return ""
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return str(raw)
    return to_local(dt).strftime("%m-%d %H:%M")

log = logging.getLogger("webui")

# Подсказка на графике. Своё вместо библиотеки: панель отдаёт обычный
# BaseHTTPRequestHandler, тащить ради тултипа CDN незачем, а весь код —
# полторы сотни строк без зависимостей. Ловим движение по всему полю графика
# и ищем ближайшую точку по X: попадать курсором в кружок радиусом 2.6px
# невозможно, а именно так вёл себя нативный <title>.
CHART_JS = """
(function(){
 function init(box){
  if(box.__wired) return; box.__wired=1;
  var cfg; try{ cfg=JSON.parse(box.getAttribute('data-chart')); }catch(e){ return; }
  var svg=box.querySelector('svg');
  var pane=box.querySelector('.chartscroll');
  if(!svg||!pane||!cfg.x||!cfg.x.length) return;
  // нативные подсказки убираем: с живым скриптом они дублировали бы плашку
  Array.prototype.forEach.call(svg.querySelectorAll('title'),
                               function(t){ t.parentNode.removeChild(t); });
  var guide=svg.querySelector('.guide');
  var dots=svg.querySelectorAll('.hi');
  var tip=box.querySelector('.tip');
  function hide(){ box.classList.remove('on'); }
  function move(ev){
   var r=svg.getBoundingClientRect();
   if(!r.width) return;
   var sx=(ev.clientX-r.left)*cfg.vw/r.width;
   var best=0, bd=Infinity;
   for(var i=0;i<cfg.x.length;i++){
    var d=Math.abs(cfg.x[i]-sx);
    if(d<bd){ bd=d; best=i; }
   }
   var gx=cfg.x[best];
   guide.setAttribute('x1',gx); guide.setAttribute('x2',gx);
   var html='<div class="day">'+cfg.labels[best]+'</div>';
   for(var s=0;s<cfg.series.length;s++){
    var se=cfg.series[s];
    dots[s].setAttribute('cx',gx);
    dots[s].setAttribute('cy',se.y[best]);
    html+='<div class="row"><i style="background:'+se.color+'"></i>'
        + se.name+'<b>'+se.txt[best]+'</b></div>';
   }
   tip.innerHTML=html;
   box.classList.add('on');
   // Координаты считаем от коробки, а не от окна. Разница r.left-br.left
   // уже учитывает прокрутку, поэтому px — отступ от ВИДИМОГО левого края,
   // в нём и удобно прижимать плашку к границам.
   var br=pane.getBoundingClientRect();
   var px=r.left-br.left+gx*r.width/cfg.vw;
   var py=r.top-br.top+cfg.series[0].y[best]*r.height/cfg.vh;
   var left=px+14;
   if(left+tip.offsetWidth > pane.clientWidth-6) left=px-tip.offsetWidth-14;
   if(left<6) left=6;
   // ...а вот CSS-свойство left у абсолютного элемента отсчитывается от
   // СОДЕРЖИМОГО контейнера, которое при прокрутке уехало. Без scrollLeft
   // на узком экране плашка улетала на сотни пикселей за границу экрана.
   tip.style.left=(left+pane.scrollLeft)+'px';
   tip.style.top=Math.max(4, py-tip.offsetHeight-14)+'px';
  }
  // Мышь и палец ведут себя по-разному. У мыши уход курсора — это конец
  // просмотра, подсказку надо убрать. А палец «уходит» сразу же, как только
  // его подняли: на телефоне подсказка гасла ровно в тот момент, когда её
  // собирались прочитать. Поэтому после касания держим её до тех пор, пока
  // не тронут что-то в стороне от графика.
  var touched = false;
  svg.addEventListener('pointermove', function(ev){
   if(ev.pointerType === 'touch') touched = true;
   move(ev);
  });
  svg.addEventListener('pointerdown', function(ev){
   if(ev.pointerType === 'touch') touched = true;
   move(ev);
  });
  svg.addEventListener('pointerleave', function(ev){
   if(ev.pointerType && ev.pointerType !== 'mouse') return;
   hide();
  });
  // Касание мимо графика — закрыть. Слушаем на всём документе, но гасим
  // только свою подсказку и только если её показывали пальцем.
  document.addEventListener('pointerdown', function(ev){
   if(touched && !svg.contains(ev.target)) hide();
  }, true);
 }
 function wire(){
  Array.prototype.forEach.call(
   document.querySelectorAll('.chartbox[data-chart]'), init);
 }
 if(document.readyState!=='loading') wire();
 else document.addEventListener('DOMContentLoaded',wire);
})();
"""

# Соседние панели: порт, «семья», подпись. Хост не зашит — берётся из
# заголовка Host самого запроса, поэтому ссылки работают и по внешнему IP,
# и через ssh-туннель на localhost, и вообще откуда угодно.
#
# Семья — это код, который панель запускает. 8800 и 8802 — один и тот же
# dashboard.py, набор путей у них совпадает до символа, поэтому переход
# между ними сохраняет открытую вкладку: «Исходы ATP» → «Исходы WTA».
# У бота (8801) страницы свои (`/live`, `/days`, а `/picks` нет вовсе),
# так что туда и оттуда — всегда на корень, иначе ссылка вела бы в 404.
PEERS = [
    ("8801", "bot", "Бот"),
    ("8800", "tra", "ATP"),
    ("8802", "tra", "WTA"),
]

# Фильтры содержимого, которые переезжают вместе со страницей на соседнюю
# панель: смотрели ATP за неделю — на WTA попадёте тоже за неделю. Список
# закрытый, потому что переносить можно только то, что на соседе значит ровно
# то же самое; токен сюда не входит — он у каждой панели подставляется свой.
CARRY = ("period", "status")

# Статистика рассылки на bet-hub. Внешняя, токен туда не подставляем.
BETHUB_STATS = os.environ.get(
    "BETHUB_STATS_URL",
    "https://new.bet-hub.com/sub/280140/atpten/stats/all")


def peer_bar(host: str, port: int, token: str, path: str = "/",
             params: dict | None = None) -> str:
    """Ссылки на соседние панели — строчным блоком в шапке.

    Стоят рядом с `nav`, но приглушены и отделены чертой: вкладки слева
    переключают страницы этой панели, а эти ссылки уводят на другой сайт,
    и путать их не должно.

    Открытая страница и её фильтры (CARRY) переносятся на соседнюю панель,
    но только внутри одной семьи (см. PEERS): у ATP и WTA код общий, пути и
    смысл фильтров совпадают, а на боте своя разметка страниц. Свой порт в
    PEERS не находится, если панель подняли на нестандартном (DASH_PORT=9000
    при отладке) — тогда семья пустая, ни с кем не совпадает, и все ссылки
    ведут на корень, как раньше.
    """
    mine = {p: fam for p, fam, _ in PEERS}.get(str(port), "")
    tail = "".join(f"&{k}={urllib.parse.quote(v)}"
                   for k, v in (params or {}).items())
    out = []
    for p, fam, label in PEERS:
        here = str(port) == p
        # Не своя семья — ни путь, ни фильтры не переносим: на боте таких
        # страниц нет, а «за неделю» без страницы смысла не имеет.
        dest, qs = (path, tail) if mine and fam == mine else ("/", "")
        url = f"http://{host}:{p}{dest}?token={urllib.parse.quote(token)}{qs}"
        cls = "on" if here else ""
        out.append(f'<a class="{cls}" href="{e(url)}">{e(label)}</a>')
    if BETHUB_STATS:
        out.append(f'<a href="{e(BETHUB_STATS)}" target="_blank" '
                   f'rel="noopener">bet-hub ↗</a>')
    return ('<span class="peers"><span class="lbl">Панели:</span>'
            + "".join(out) + "</span>")


CSS = """
:root{--bg:#0f1419;--card:#1a2027;--line:#2a333d;--tx:#dfe6ec;--dim:#8c9aa8;
--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--acc:#60a5fa}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);
display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
/* Названия панелей разной длины («tennisratio ставки по кнопке» против
   «tennisratioATP поиск ценности»), из-за чего вкладки начинались на разном
   месте и при переходе по ссылкам шапка дёргалась. Ширина взята с запасом
   к самому длинному заголовку — 220px у WTA. */
header h1{min-width:232px}
h1 span{color:var(--dim);font-weight:400;font-size:13px;margin-left:6px}
nav a{color:var(--dim);text-decoration:none;margin-right:14px}
/* Ширина под самый широкий набор вкладок: без неё блок «Панели» начинался бы
   сразу за вкладками и уезжал на десятки пикселей при переходе между
   панелями. Прижимать его к правому краю нельзя — тогда шапке нужны все
   1280px, и на окне поуже она ломается на две строки.
   Было 318px под пять вкладок (307px), затем 378px под шесть. 30.08.2026 у
   обходчика появилась седьмая, «Закрытие», — запас поднят снова. Добавляете
   вкладку — правьте и это число, иначе шапка снова начнёт дёргаться. */
header nav{min-width:458px}
nav a.on,nav a:hover{color:var(--acc)}
main{padding:18px;max-width:1200px;margin:0 auto}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 16px;min-width:130px;flex:1}
.card .k{color:var(--dim);font-size:12px;text-transform:uppercase;
letter-spacing:.04em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;
margin-bottom:22px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);
white-space:nowrap}
th{color:var(--dim);font-weight:500;font-size:12px;text-transform:uppercase;
letter-spacing:.04em}
tr:last-child td{border-bottom:none}
tr:hover td{background:#1f2830}
.num{text-align:right;font-variant-numeric:tabular-nums}
/* Моноширинные цифры: иначе с каждой минутой ширина отметки менялась бы на
   пиксель-другой и тянула за собой прижатый к ней блок «Панели». */
.stamp{color:var(--dim);font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.dim{color:var(--dim)}
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;
margin-top:6px}
.bar i{display:block;height:100%;background:var(--ok)}
h2{font-size:15px;margin:22px 0 10px;font-weight:600}
.note{color:var(--dim);font-size:13px;margin:-10px 0 16px;max-width:70ch}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
background:var(--line);color:var(--dim)}
/* Ссылки на соседние панели живут в шапке, справа от вкладок. Приглушены
   сильнее, чем nav: вкладка переключает страницу, а эта ссылка уводит на
   другой сайт — разницу видно по насыщенности и по разделительной черте. */
.peers{display:inline-flex;gap:11px;flex-wrap:wrap;align-items:baseline;
font-size:12px;padding-left:10px;border-left:1px solid var(--line)}
.peers .lbl{color:#5d6b78}
.peers a{color:var(--dim);text-decoration:none;white-space:nowrap}
.peers a:hover{color:var(--acc)}
.peers a.on{color:var(--tx);font-weight:600}
.chartbox{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 14px 8px;margin-bottom:22px}
/* Прокручивается только полотно: будь overflow на всей коробке, вместе с
   графиком уезжала бы и легенда, а её текст обрезался бы по краю */
.chartscroll{overflow-x:auto;position:relative}
/* min-width: на узком экране график прокручивается вбок, а не сжимается —
   при сжатии подписи осей уезжают в нечитаемые 4px */
.chart{display:block;width:100%;min-width:520px;height:auto;cursor:crosshair}
/* Направляющая и крупная точка проявляются только под курсором */
.guide,.hi{opacity:0;pointer-events:none}
.chartbox.on .guide,.chartbox.on .hi{opacity:1}
.tip{position:absolute;display:none;pointer-events:none;z-index:5;
background:var(--bg);border:1px solid var(--line);border-radius:8px;
padding:7px 10px;font-size:12px;white-space:nowrap;
box-shadow:0 6px 18px rgba(0,0,0,.5)}
.chartbox.on .tip{display:block}
.tip .day{color:var(--dim);font-size:11px}
.tip .row{display:flex;align-items:center;gap:6px;margin-top:4px}
.tip .row i{width:9px;height:9px;border-radius:50%;flex:none}
.tip .row b{margin-left:8px;font-variant-numeric:tabular-nums}
.legend{display:flex;gap:16px;align-items:center;flex-wrap:wrap;
margin-top:6px;font-size:12px;color:var(--dim)}
.legend .lg{display:inline-flex;align-items:center;gap:6px;color:var(--tx)}
.legend i{width:10px;height:10px;border-radius:50%;display:inline-block}
@media(max-width:640px){td,th{padding:6px}main{padding:10px}
table{font-size:12px}
/* на узком экране шапка переносится: фиксированная ширина заголовка и
   прижим вправо только мешают, а черта слева повисает в пустоте */
header h1{min-width:0}header nav{min-width:0}
.peers{padding-left:0;border-left:none;gap:9px}}
"""


def load_env(here: str) -> None:
    """Подхватывает .env рядом — панель запускают и руками, и службой."""
    path = os.path.join(here, ".env")
    if not os.path.exists(path):
        return
    for raw in open(path, encoding="utf-8"):
        line = raw.strip().removeprefix("export ")
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


# ------------------------------------------------------------------ вёрстка
def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def num(v, default=None):
    """Число из строки CSV или JSON, понимает и точку, и запятую."""
    if v in (None, ""):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except ValueError:
        return default


def money(v) -> str:
    v = num(v, 0.0)
    cls = "ok" if v > 0 else ("bad" if v < 0 else "dim")
    return f'<span class="{cls}">{v:+,.0f}</span>'.replace(",", " ")


def pct(v, good=None) -> str:
    if v is None:
        return '<span class="dim">—</span>'
    cls = ""
    if good is not None:
        cls = "ok" if v >= good else "bad"
    return f'<span class="{cls}">{v:.1f}%</span>'


def bar(part, whole) -> str:
    w = (part / whole * 100) if whole else 0
    return f'<div class="bar"><i style="width:{w:.0f}%"></i></div>'


def _nice_step(span: float, target: int = 4) -> float:
    """Шаг сетки «круглым числом»: 1, 2, 2.5, 5 на порядок величины.

    Без этого подписи оси выходят вида «1373», и глазом по ним не считать.
    """
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _short_num(v: float) -> str:
    """Подпись оси: 12 400 -> «12,4k», мелочь — как есть."""
    a = abs(v)
    if a >= 10_000:
        return f"{v / 1000:+.0f}k".replace("+0k", "0")
    if a >= 1000:
        return f"{v / 1000:+.1f}k".replace(".0k", "k")
    return f"{v:+.0f}" if v else "0"


def line_chart(series, labels, *, height: int = 240, hint: str = "") -> str:
    """Линейный график накопленным итогом. Чистый SVG, без библиотек.

    series — [(имя, цвет, [значения])], все ряды одной длины с labels.
    labels — подписи по X (даты); показываем не все, иначе слипаются.

    Цвета берём переменными CSS (`var(--ok)`), чтобы график жил в той же
    палитре, что и остальная панель, и не расходился с ней при правках темы.
    Точки снабжены <title> — это нативная всплывающая подсказка браузера,
    так что наводка мышью работает без единой строчки JS.
    """
    series = [(n, c, list(v)) for n, c, v in series if v]
    if not series or len(labels) < 2:
        return '<p class="dim">Пока нечего рисовать: нужно хотя бы два дня.</p>'

    W, H = 900, height
    L, R, T, B = 58, 14, 16, 30           # поля под подписи осей
    iw, ih = W - L - R, H - T - B

    flat = [v for _, _, vals in series for v in vals]
    lo_v, hi_v = min(flat + [0.0]), max(flat + [0.0])
    step = _nice_step(hi_v - lo_v)
    lo = math.floor(lo_v / step) * step
    hi = math.ceil(hi_v / step) * step
    if hi == lo:
        hi = lo + step

    n = len(labels)

    def x(i: int) -> float:
        return L + (iw * i / (n - 1) if n > 1 else 0)

    def y(v: float) -> float:
        return T + ih - (v - lo) / (hi - lo) * ih

    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
           f'preserveAspectRatio="xMidYMid meet">']

    # горизонтальная сетка с подписями
    ticks, t = [], lo
    while t <= hi + step / 2:
        ticks.append(round(t, 6))
        t += step
    for tv in ticks:
        yy = y(tv)
        zero = abs(tv) < 1e-9
        # нулевая линия сплошная и толще: относительно неё и читают график
        dash = '' if zero else 'stroke-dasharray="3 4" '
        out.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L + iw}" y2="{yy:.1f}" '
                   f'stroke="var(--line)" stroke-width="{2 if zero else 1}" '
                   f'{dash}/>')
        out.append(f'<text x="{L - 8}" y="{yy + 4:.1f}" text-anchor="end" '
                   f'fill="var(--dim)" font-size="11">{e(_short_num(tv))}</text>')

    # подписи дат: не больше семи, иначе наезжают друг на друга. Последнюю
    # показываем всегда, но если она села вплотную к предыдущей — заменяем
    # её, а не дописываем рядом.
    every = max(1, math.ceil(n / 7))
    shown = list(range(0, n, every))
    if shown[-1] != n - 1:
        if n - 1 - shown[-1] < every * 0.6:
            shown[-1] = n - 1
        else:
            shown.append(n - 1)
    for i in shown:
        out.append(f'<text x="{x(i):.1f}" y="{T + ih + 20}" '
                   f'text-anchor="middle" fill="var(--dim)" '
                   f'font-size="11">{e(labels[i])}</text>')

    cfg = {"vw": W, "vh": H, "x": [round(x(i), 1) for i in range(n)],
           "labels": list(labels), "series": []}

    for name, colour, vals in series:
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
        out.append(f'<polyline fill="none" stroke="{colour}" '
                   f'stroke-width="2" stroke-linejoin="round" '
                   f'stroke-linecap="round" points="{pts}"/>')
        for i, v in enumerate(vals):
            # <title> — запасной вариант: если JS отключён, нативная подсказка
            # браузера всё равно покажет сумму. При живом JS её снимают, иначе
            # поверх нашей плашки через секунду вылезала бы вторая.
            money_txt = f"{v:+,.0f}".replace(",", " ")
            out.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.6" '
                       f'fill="{colour}"><title>{e(labels[i])} · {e(name)}: '
                       f'{e(money_txt)}</title></circle>')
        cfg["series"].append({
            "name": name, "color": colour,
            "y": [round(y(v), 1) for v in vals],
            "txt": [f"{v:+,.0f}".replace(",", " ") for v in vals]})

    # Направляющая и жирные точки живут в разметке всегда, но скрыты, — так
    # скрипту не нужно ничего создавать, только двигать.
    out.append(f'<line class="guide" y1="{T}" y2="{T + ih}" x1="0" x2="0" '
               f'stroke="var(--dim)" stroke-width="1" stroke-dasharray="3 4"/>')
    for _, colour, _ in series:
        out.append(f'<circle class="hi" r="5" cx="0" cy="0" fill="{colour}" '
                   f'stroke="var(--card)" stroke-width="2"/>')
    out.append("</svg>")

    legend = " ".join(
        f'<span class="lg"><i style="background:{c}"></i>{e(n_)}</span>'
        for n_, c, _ in series)
    note = f'<span class="dim">{e(hint)}</span>' if hint else ""
    data = html.escape(json.dumps(cfg, ensure_ascii=False), quote=True)
    return (f'<div class="chartbox" data-chart="{data}">'
            f'<div class="chartscroll">{"".join(out)}<div class="tip"></div></div>'
            f'<div class="legend">{legend}{note}</div></div>')


def cards(items) -> str:
    return ('<div class="cards">' + "".join(
        f'<div class="card"><div class="k">{e(k)}</div>'
        f'<div class="v">{v}</div></div>' for k, v in items) + "</div>")


def table(head, rows) -> str:
    if not rows:
        return '<p class="dim">Пусто.</p>'
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                 for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>"


def links(base: str, options, current: str, token: str, param: str = "period"):
    """Строка-переключатель: «за сегодня · за неделю · …»"""
    out = []
    for key, label in options:
        colour = "#60a5fa" if key == current else "#8c9aa8"
        out.append(f'<a href="{base}?{param}={key}&token='
                   f'{urllib.parse.quote(token)}" style="color:{colour};'
                   f'text-decoration:none">{e(label)}</a>')
    return " · ".join(out)


# ------------------------------------------------------------------ сервер
def serve(*, title: str, subtitle: str, routes: dict, token: str,
          host: str, port: int, refresh: int = 120) -> None:
    """Поднимает панель. routes: путь -> (функция, ключ вкладки, имя вкладки)."""

    def page(body: str, active: str, host: str = "",
             path: str = "/", params: dict | None = None) -> bytes:
        nav = "".join(
            f'<a class="{"on" if k == active else ""}" '
            f'href="{u}?token={urllib.parse.quote(token)}">{e(n)}</a>'
            for u, (_, k, n) in routes.items())
        now = to_local(datetime.now(timezone.utc)).strftime(
            f"%d.%m %H:%M {TZ_LABEL}")
        return (f"<!doctype html><html lang=ru><head><meta charset=utf-8>"
                f"<meta name=viewport content='width=device-width,"
                f"initial-scale=1'>"
                f"<meta http-equiv=refresh content={refresh}>"
                f"<title>{e(title)}</title><style>{CSS}</style></head><body>"
                f"<header><h1>{e(title)}<span>{e(subtitle)}</span></h1>"
                f"<nav>{nav}</nav>"
                f"{peer_bar(host, port, token, path, params)}"
                f"<span class=stamp style='margin-left:auto'>{now}</span>"
                f"</header>"
                f"<main>{body}</main>"
                f"<script>{CHART_JS}</script></body></html>").encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "tennis-dash"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(parsed.query)
            # compare_digest — чтобы по времени ответа нельзя было подбирать
            # токен посимвольно
            if not secrets.compare_digest((q.get("token") or [""])[0], token):
                self._send(403, b"403", "text/plain; charset=utf-8")
                return
            route = routes.get(parsed.path)
            if not route:
                self._send(404, b"404", "text/plain; charset=utf-8")
                return
            fn, active, _ = route
            try:
                body = fn(q)
            except Exception as exc:  # noqa: BLE001
                import traceback
                log.error("страница %s упала:\n%s", parsed.path,
                          traceback.format_exc())
                body = (f'<h2>Ошибка</h2><p class="dim">{e(type(exc).__name__)}'
                        f': {e(exc)}</p>')
            # Хост берём из запроса: панель открывают и по внешнему IP,
            # и через туннель на 127.0.0.1 — ссылки должны вести туда же,
            # откуда пришли, иначе с туннеля они уводят в никуда.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            carried = {k: q[k][0] for k in CARRY if (q.get(k) or [""])[0]}
            self._send(200, page(body, active,
                                 host or os.environ.get("DASH_PUBLIC_IP", ""),
                                 parsed.path, carried))

    srv = ThreadingHTTPServer((host, port), Handler)
    ip = os.environ.get("DASH_PUBLIC_IP", "<ip сервера>")
    print("=" * 62)
    print(f"  {title}: http://{ip}:{port}/?token={token}")
    if host == "0.0.0.0":
        print("  Слушает на всех адресах. Если панель нужна только вам,")
        print(f"  надёжнее DASH_HOST=127.0.0.1 и туннель:")
        print(f"    ssh -L {port}:127.0.0.1:{port} root@{ip}")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


def cluster_ci(groups, iters: int = 2000, seed: int = 7):
    """95% интервал для среднего, с ресэмплингом ГРУПП, а не значений.

    groups — список списков: одна группа = один матч, внутри его ставки.
    Ресэмплить надо матчи: на один матч приходится несколько ставок с общим
    исходом и общим движением линии, и обычный интервал «по n ставкам»
    считал бы их независимыми. Он выходит уже настоящего в полтора-два
    раза — то есть врёт ровно в сторону «перевес есть». Та же логика, что
    в check_edge.py.

    Живёт здесь, а не в панели, потому что webui — единственный модуль,
    общий для всех трёх панелей: иначе пришлось бы держать две копии.

    Зерно фиксировано: одни и те же данные обязаны давать один и тот же
    ответ, иначе числами нельзя пользоваться.
    """
    groups = [g for g in groups if g]
    if len(groups) < 2:
        return None, None
    rnd = random.Random(seed)
    n = len(groups)
    got = []
    for _ in range(iters):
        vals = [v for _ in range(n) for v in groups[rnd.randrange(n)]]
        if vals:
            got.append(sum(vals) / len(vals))
    if not got:
        return None, None
    got.sort()
    return got[int(len(got) * 0.025)], got[int(len(got) * 0.975)]
