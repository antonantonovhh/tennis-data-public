"""Состояние с тремя видами pending: живой, отвалившийся, исчерпавший попытки."""
import os, sys, json, types, re as _re
D = "/tmp/q"; os.makedirs(D, exist_ok=True)
for f in os.listdir(D): os.remove(D + "/" + f)
os.environ.update(TRA_STATE=f"{D}/state.json", TRA_RESULTS=f"{D}/res.jsonl",
                  TRA_VALUE_CSV=f"{D}/v.csv", TRA_PICKS_CSV=f"{D}/p.csv",
                  TRA_LOG_CSV=f"{D}/l.csv")
sys.path.insert(0, ".")
src = open("bot_merged.py", encoding="utf-8").read()
stub = types.ModuleType("bot_merged")
exec(compile(src[src.index("def _parse_score_sets"):src.index("def parse_te_last_matches")],
             "bot_merged", "exec"), stub.__dict__)
stub.__dict__["re"] = _re; stub.remove_accents = lambda s: s
sys.modules["bot_merged"] = stub

json.dump({"meta": {}, "entries": {
    "zhivoy-match":  {"status": "pending", "attempts": 0, "first_seen": "2026-08-22T09:00:00+00:00",
                      "summary": {"when": "24.08. 14:00"}},
    "staryy-match":  {"status": "pending", "attempts": 1, "first_seen": "2026-08-18T09:00:00+00:00",
                      "summary": {"when": "18.08. 12:00"}},
    "ischerpal":     {"status": "pending", "attempts": 3, "first_seen": "2026-08-19T09:00:00+00:00",
                      "summary": {"when": "19.08. 12:00"}},
    "budushchiy":    {"status": "pending", "attempts": 0, "first_seen": "2026-08-22T09:00:00+00:00",
                      "summary": {"when": "25.08. 10:00"}},
    "gotovyy":       {"status": "done", "attempts": 1, "first_seen": "2026-08-22T09:00:00+00:00"},
}}, open(f"{D}/state.json", "w"))

from tennisratioall.scanner import MatchRef
import tennisratioall.scanner as sc
# афиша: только один из четырёх pending в ней есть
sc.discover = lambda: [MatchRef(slug="zhivoy-match", p1="A", p2="B", when="24.08. 14:00")]

import tennisratioall_run as run
for argv in (["--diag-queue"], ["--diag-queue", "--prune"]):
    print("\n" + "#" * 64)
    print("$ tennisratioall_run.py " + " ".join(argv))
    print("#" * 64)
    sys.argv = ["tennisratioall_run.py"] + argv
    try:
        run.main()
    except SystemExit:
        pass

left = json.load(open(f"{D}/state.json"))["entries"]
print("\nосталось в состоянии:", sorted(left))
