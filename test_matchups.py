"""Проверка диагностики загрузки матчапов на подставных ответах."""
import ast, logging, sys, types
logging.basicConfig(level=logging.DEBUG, format="  %(levelname)s: %(message)s")

# Вытаскиваем _get_matchup_list из bot_merged без импорта всего файла
src = open("bot_merged.py", encoding="utf-8").read()
start = src.index("        def _get_matchup_list(session, url, timeout=15):")
end = src.index("        def _download_matchups():")
code = "\n".join(l[8:] for l in src[start:end].split("\n"))
ns = {"log": logging.getLogger("pin")}
exec(compile(code, "bot_merged", "exec"), ns)
get = ns["_get_matchup_list"]

class Resp:
    def __init__(self, code, payload=None, bad_json=False):
        self.status_code, self._p, self._bad = code, payload, bad_json
    def json(self):
        if self._bad:
            raise ValueError("Expecting value: line 1 column 1")
        return self._p

class Sess:
    def __init__(self, resp=None, exc=None):
        self._r, self._e = resp, exc
    def get(self, *a, **k):
        if self._e:
            raise self._e
        return self._r

cases = [
    ("200 со списком из 62 матчей", Sess(Resp(200, [{"id": i} for i in range(62)])),
     lambda d, c, w: len(d) == 62 and c == 200 and w == ""),
    ("200, но список пуст", Sess(Resp(200, [])),
     lambda d, c, w: d == [] and c == 200),
    ("401 — ключ не принят", Sess(Resp(401)),
     lambda d, c, w: d is None and c == 401),
    ("403 — блокировка", Sess(Resp(403)),
     lambda d, c, w: d is None and c == 403),
    ("таймаут", Sess(exc=TimeoutError("timed out")),
     lambda d, c, w: d is None and c is None and "TimeoutError" in w),
    ("прокси отвалился", Sess(exc=OSError("Tunnel connection failed: 502")),
     lambda d, c, w: d is None and "OSError" in w and "502" in w),
    ("ответ не JSON (заглушка Cloudflare)", Sess(Resp(200, bad_json=True)),
     lambda d, c, w: d is None and "не JSON" in w),
    ("dict вместо списка", Sess(Resp(200, {"matchups": [{"id": 1}, {"id": 2}]})),
     lambda d, c, w: len(d) == 2),
]
ok = 0
for name, sess, check in cases:
    d, c, w = get(sess, "http://x")
    good = check(d, c, w)
    ok += good
    shown = f"{len(d)} матчей" if isinstance(d, list) else "None"
    print(("OK  " if good else "FAIL"), f"{name:<38} -> {shown:<12} код={c} {w[:50]!r}")
print(f"\n{ok}/{len(cases)}")
sys.exit(0 if ok == len(cases) else 1)
