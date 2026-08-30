"""Нормализация имён — главная боль при склейке двух источников.

TennisAbstract: "Jan Kumstat", ключ в URL — JanKumstat
TennisRatio:    "Jan Kumstat", slug в URL — jan-kumstat
Плюс диакритика (Kumstat/Kumšt'át), инициалы, разный порядок частей.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALPHA = re.compile(r"[^a-z0-9]+")


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize(name: str) -> str:
    """'Jan Kumšt'át' -> 'jankumstat'. Ключ для сопоставления между сайтами."""
    return _NON_ALPHA.sub("", strip_accents(name).lower())


def sorted_key(name: str) -> str:
    """Ключ, устойчивый к перестановке имени и фамилии."""
    parts = _NON_ALPHA.sub(" ", strip_accents(name).lower()).split()
    return "".join(sorted(parts))


def slugify(name: str) -> str:
    """'Jan Kumstat' -> 'jan-kumstat' (формат URL на tennisratio.com)."""
    s = _NON_ALPHA.sub("-", strip_accents(name).lower())
    return s.strip("-")


# регистронезависимый вариант: _NON_ALPHA работает только по нижнему регистру
# и в camel_key съедал бы заглавные буквы ('Jan Kumstat' -> 'AnUmstat')
_NON_ALNUM_ANY = re.compile(r"[^A-Za-z0-9]+")


def camel_key(name: str) -> str:
    """'Jan Kumstat' -> 'JanKumstat'.

    Формат идентификатора игрока и у TennisAbstract (player.cgi?p=),
    и у API tennisratio (/api/player/{id}/).
    """
    parts = _NON_ALNUM_ANY.sub(" ", strip_accents(name)).split()
    return "".join(p[:1].upper() + p[1:].lower() for p in parts)


def match_name(target: str, candidates: dict[str, object]) -> object | None:
    """Ищет target среди candidates (ключи уже нормализованы через normalize()).

    Порядок: точное совпадение -> перестановка частей -> подстрока фамилии.
    """
    key = normalize(target)
    if key in candidates:
        return candidates[key]

    sk = sorted_key(target)
    by_sorted = {sorted_key(k): v for k, v in candidates.items()}
    if sk in by_sorted:
        return by_sorted[sk]

    # последняя часть = фамилия; ищем уникальное вхождение
    parts = _NON_ALPHA.sub(" ", strip_accents(target).lower()).split()
    if parts:
        surname = parts[-1]
        hits = [v for k, v in candidates.items() if surname in k]
        if len(hits) == 1:
            return hits[0]
    return None
