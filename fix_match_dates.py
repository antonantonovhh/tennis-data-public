#!/usr/bin/env python3
"""Чинит поле `when` в журналах обходчика после правки разбора афиши.

Разбор карточки на tennisratio ошибался дважды: в компактной вёрстке время бралось от соседнего матча, а датой
становился ближайший заголовок со словом-месяцем — так в журнал попало
«August Holmgren 15:00», имя игрока вместо даты.

Правка в bot_merged.py действует только на новые записи. `log_match`
обновляет строку журнала, лишь когда матч заново проходит круг, а
посчитанные матчи в круг больше не попадают — их `when` останется кривым
навсегда, и веб-панель будет показывать его до конца истории.

Правильное значение берётся с живой афиши: у матчей, которые ещё висят на
ней, дата и время теперь читаются точно. Матчи, ушедшие с афиши, восстановить
неоткуда — они только перечисляются в отчёте.

ВАЖНО: обходчик держит состояние в памяти и сохраняет файл целиком, поэтому
правку `state.json` при живой службе он затрёт своей копией через считанные
минуты. Порядок такой:

    systemctl stop tennisratioall
    python3 fix_match_dates.py --apply
    systemctl start tennisratioall

По умолчанию ничего не пишет, только показывает, что изменится:

    python3 fix_match_dates.py                  # сухой прогон, ATP
    python3 fix_match_dates.py --apply          # записать (с бэкапом)
    python3 fix_match_dates.py --tour wta --apply

Бэкапы кладутся рядом с файлами: <имя>.bak-<дата>.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _pick_tour_early() -> None:
    """Ставит TRA_TOUR из --tour ДО импорта пакета.

    Пути к состоянию и журналам считаются на импорте store.py, а argparse
    отрабатывает позже — тот же приём, что в tennisratioall_run.py. Без него
    прогон с --tour wta молча открыл бы мужские файлы.
    """
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        val = None
        if a.startswith("--tour="):
            val = a.split("=", 1)[1]
        elif a == "--tour" and i + 1 < len(argv):
            val = argv[i + 1]
        if val:
            os.environ["TRA_TOUR"] = val.strip().lower()
            return


_pick_tour_early()

# bot_merged на импорте сам подхватывает .env и делает sys.exit() без токенов
import bot_merged  # noqa: E402

from tennisratioall.journal import (LOG_CSV, LOG_FIELDS, PICK_FIELDS,  # noqa: E402
                                    PICKS_CSV, VALUE_CSV, VALUE_FIELDS,
                                    _read, _write)
from tennisratioall.results import parse_when  # noqa: E402
from tennisratioall.store import RESULTS_FILE, STATE_FILE, TOUR  # noqa: E402

# «24.08.», «24.08. 16:30», «4.8 9:00» — то, как время матча пишет афиша.
# Всё остальное в поле `when` — мусор вроде «August Holmgren 15:00».
SHAPE = re.compile(r"^\d{1,2}\.\d{1,2}\.?(\s+\d{1,2}:\d{2})?$")

STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


def crawler_running() -> str:
    """Имя запущенного юнита обходчика, иначе пустая строка.

    Ловим ровно ту ошибку, на которой я уже обжёгся: правку state.json при
    живой службе она затирает своей копией из памяти, и через десять минут
    кривые даты возвращаются как ни в чём не бывало.
    """
    unit = "tennisratioall" if TOUR == "atp" else f"tennisratioall-{TOUR}"
    try:
        got = subprocess.run(["systemctl", "is-active", unit],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""          # не systemd (Windows, контейнер) — проверять нечем
    return unit if got.stdout.strip() == "active" else ""


def board_dates() -> dict:
    """slug → правильные дата и время с живой афиши."""
    raw = bot_merged.parse_matches({}, {}, TOUR)
    return {slug: (data.get("date") or "").strip()
            for slug, data in raw.items() if (data.get("date") or "").strip()}


# Slug строится из имён игроков, поэтому у повторной встречи той же пары он
# тот же самый: daniil-glinka-vs-pedro-martinez есть и в записи от 21.08, и
# на сегодняшней афише. Слепо переписать дату с афиши значило бы испортить
# историю, поэтому близость по времени обязательна.
NEAR_HOURS = 36


def _near(cur: str, good: str) -> bool:
    """Один ли это матч. Сбой давал сдвиг на часы, а не на дни."""
    a, b = parse_when(cur), parse_when(good)
    if not a or not b:
        return False
    return abs((b - a).total_seconds()) <= NEAR_HOURS * 3600


def verdict(slug: str, cur: str, board: dict, resolved: bool = False):
    """(новое значение, пометка) или (None, пометка) если чинить нечем."""
    cur = (cur or "").strip()
    good = board.get(slug)
    if not good:
        if SHAPE.match(cur):
            return None, "уже верно"
        return None, "не с чем сверить (матч ушёл с афиши)"
    if good == cur:
        return None, "уже верно"
    if resolved:
        # Закрытый матч — это история, и совпадение slug тут означает
        # повторную встречу пары, а не ту же самую игру.
        return None, "матч уже закрыт, не трогаю"
    if not cur or not SHAPE.match(cur):
        # Мусор вида «August Holmgren 15:00» или пустая ячейка: терять
        # нечего, ставим то, что говорит афиша.
        return good, "исправлено"
    if _near(cur, good):
        # Время, утащенное у соседа по блоку, по виду не отличить от
        # настоящего: «24.08. 16:30» вместо «24.08. 21:00» выглядит нормально.
        return good, "исправлено"
    return None, f"похоже на другую встречу той же пары (в журнале {cur})"


def fix_csv(path: str, fields: list, board: dict, apply: bool) -> dict:
    rows = _read(path, fields)
    if not rows:
        return {"файл": os.path.basename(path), "строк": 0}
    changed, lost, kept, samples = 0, 0, [], []
    for r in rows:
        done = bool((r.get("resolved_at") or "").strip()
                    or (r.get("winner") or "").strip())
        new, note = verdict(r.get("slug", ""), r.get("when", ""), board, done)
        if new is not None:
            if len(samples) < 5:
                samples.append((r.get("slug", ""), r.get("when", ""), new))
            r["when"] = new
            changed += 1
        elif note.startswith("похоже") or note.startswith("матч уже"):
            kept.append((r.get("slug", ""), note))
        elif note.startswith("не с чем"):
            # Пустая ячейка — это «никогда не заполняли», а не испорченная
            # дата: в value_bets.csv поле `when` пустое у всех строк искони.
            if (r.get("when") or "").strip():
                lost += 1
    if changed and apply:
        shutil.copy2(path, f"{path}.bak-{STAMP}")
        _write(path, fields, rows)
    return {"файл": os.path.basename(path), "строк": len(rows),
            "исправлено": changed, "не восстановить": lost,
            "не тронуто": kept, "примеры": samples}


def fix_state(path: str, board: dict, apply: bool) -> dict:
    if not os.path.exists(path):
        return {"файл": os.path.basename(path), "строк": 0}
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"файл": os.path.basename(path), "ошибка": str(exc)}
    entries = data.get("entries") or {}
    changed, lost, kept, samples = 0, 0, [], []
    for slug, e in entries.items():
        for holder in ("summary", "rec"):
            box = e.get(holder)
            if not isinstance(box, dict) or "when" not in box:
                continue
            new, note = verdict(slug, box.get("when", ""), board)
            if new is not None:
                if len(samples) < 5:
                    samples.append((slug, box.get("when", ""), new))
                box["when"] = new
                changed += 1
            elif note.startswith("похоже") or note.startswith("матч уже"):
                kept.append((slug, note))
            elif note.startswith("не с чем"):
                if (box.get("when") or "").strip():
                    lost += 1
    if changed and apply:
        shutil.copy2(path, f"{path}.bak-{STAMP}")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    return {"файл": os.path.basename(path), "строк": len(entries),
            "исправлено": changed, "не восстановить": lost,
            "не тронуто": kept, "примеры": samples}


def fix_jsonl(path: str, board: dict, apply: bool) -> dict:
    if not os.path.exists(path):
        return {"файл": os.path.basename(path), "строк": 0}
    out, changed, lost, samples, total = [], 0, 0, [], 0
    kept = []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        total += 1
        try:
            rec = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        new, note = verdict(rec.get("slug", ""), rec.get("when", ""), board)
        if new is not None:
            if len(samples) < 5:
                samples.append((rec.get("slug", ""), rec.get("when", ""), new))
            rec["when"] = new
            changed += 1
        elif note.startswith("похоже") or note.startswith("матч уже"):
            kept.append((rec.get("slug", ""), note))
        elif note.startswith("не с чем"):
            if (rec.get("when") or "").strip():
                lost += 1
        out.append(json.dumps(rec, ensure_ascii=False))
    if changed and apply:
        shutil.copy2(path, f"{path}.bak-{STAMP}")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    return {"файл": os.path.basename(path), "строк": total,
            "исправлено": changed, "не восстановить": lost,
            "не тронуто": kept, "примеры": samples}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (по умолчанию сухой прогон)")
    ap.add_argument("--tour", default=TOUR, choices=["atp", "wta"],
                    help="какой тур чинить (учитывается до импорта, см. код)")
    ap.add_argument("--force", action="store_true",
                    help="править, даже если служба обходчика запущена")
    args = ap.parse_args()

    print(f"тур: {TOUR}")
    unit = crawler_running()
    if unit and args.apply and not args.force:
        tour_arg = f" --tour {TOUR}" if TOUR != "atp" else ""
        me = os.path.basename(__file__)
        print()
        print(f"Служба {unit} запущена. Она держит состояние в памяти и "
              f"сохраняет файл целиком,")
        print("так что правку state.json затрёт через несколько минут. "
              "Остановите её:")
        print(f"    systemctl stop {unit}")
        print(f"    python3 {me} --apply{tour_arg}")
        print(f"    systemctl start {unit}")
        print("Либо повторите с --force, если понимаете, что делаете.")
        return 2
    if unit:
        print(f"(служба {unit} запущена — для --apply её надо остановить)")

    print("читаю афишу…")
    board = board_dates()
    if not board:
        print("афиша пуста — без неё сверять не с чем, прерываю")
        return 1
    print(f"на афише матчей с датой: {len(board)}\n")

    reports = [
        fix_csv(LOG_CSV, LOG_FIELDS, board, args.apply),
        fix_csv(VALUE_CSV, VALUE_FIELDS, board, args.apply),
        fix_csv(PICKS_CSV, PICK_FIELDS, board, args.apply),
        fix_state(STATE_FILE, board, args.apply),
        fix_jsonl(RESULTS_FILE, board, args.apply),
    ]

    total = 0
    for r in reports:
        if r.get("ошибка"):
            print(f"{r['файл']}: ОШИБКА {r['ошибка']}")
            continue
        if not r.get("строк"):
            print(f"{r['файл']}: пусто или нет файла")
            continue
        total += r.get("исправлено", 0)
        print(f"{r['файл']}: строк {r['строк']}, "
              f"исправлено {r.get('исправлено', 0)}, "
              f"пропущено {len(r.get('не тронуто', []))}, "
              f"не восстановить {r.get('не восстановить', 0)}")
        for slug, was, now in r.get("примеры", []):
            print(f"    {slug}\n        было {was!r} → стало {now!r}")
        for slug, note in r.get("не тронуто", [])[:5]:
            print(f"    пропущено: {slug} — {note}")

    print()
    if not args.apply:
        print(f"сухой прогон: изменилось бы {total} значений. "
              f"Записать — повторить с --apply")
    else:
        print(f"записано: {total} значений, бэкапы с суффиксом .bak-{STAMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
