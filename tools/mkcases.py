"""Compacts covcase.py output: per test, the fixture SQLs and, for each line that only
some cases run (not the base, not every case), which cases run it."""
import json, gzip, sys
raw = json.load(open(sys.argv[1]))
sets, sid = [], {}
out = {"tests": {}, "sets": sets}
for tid, d in raw.items():
    if not d["ok"]:
        continue
    sqls = sorted(d["cases"])
    idx = {s: i for i, s in enumerate(sqls)}
    base = {f"{f}:{l}" for f, l in d["base"]}
    by_line = {}
    for s, lines in d["cases"].items():
        for f, l in lines:
            by_line.setdefault(f"{f}:{l}", set()).add(idx[s])
    lines = {}
    for k, cs in by_line.items():
        if k in base or len(cs) == len(sqls):
            continue
        key = tuple(sorted(cs))
        if key not in sid:
            sid[key] = len(sets); sets.append(list(key))
        lines[k] = sid[key]
    out["tests"][tid] = {"sqls": sqls, "lines": lines}
    print(tid, len(sqls), "discriminating lines", len(lines), "of", len(by_line), file=sys.stderr)
with gzip.open(sys.argv[2], "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
