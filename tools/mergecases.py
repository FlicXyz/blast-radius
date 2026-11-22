"""Merges covcase.py runs made in forward and reverse case order (BR_REVERSE=1).
A line whose cases differ between the two orders depends on state an earlier case left
(caches, lazy set-up): it goes to the test's base, so a change there runs every case.

usage: mergecases.py <forward raw.json> <out raw.json> <reverse raw.json>...
"""
import json, sys
fwd = json.load(open(sys.argv[1]))
rev = {}
for f in sys.argv[3:]:
    rev.update(json.load(open(f)))
out = {}
for tid, d in fwd.items():
    r = rev.get(tid)
    if r is None or "test_executor" in tid:
        out[tid] = d
        continue
    def by_line(cs):
        m = {}
        for s, lines in cs.items():
            for f, l in lines:
                m.setdefault((f, l), set()).add(s)
        return m
    a, b = by_line(d["cases"]), by_line(r["cases"])
    base = {tuple(x) for x in d["base"]} | {tuple(x) for x in r["base"]}
    unstable = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    base |= unstable
    cases = {s: [x for x in lines if tuple(x) not in base] for s, lines in d["cases"].items()}
    out[tid] = {"ok": d["ok"] and r["ok"], "base": sorted(base), "cases": cases}
    print(f"{tid}: {len(unstable)} order-dependent lines", file=sys.stderr)
json.dump(out, open(sys.argv[2], "w"))
