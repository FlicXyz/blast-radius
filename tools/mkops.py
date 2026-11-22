"""Compacts opcov.py output into ops.json.gz: per comparison site, the tests grouped by
the set of operand pairs they saw ("u": tests whose operands could not be recorded)."""
import glob, gzip, json, sys
tests, sets, sid, sites = [], [], {}, {}
for f in sorted(sys.argv[2:]):
    for line in open(f):
        d = json.loads(line)
        if d["ok"] is not True:
            print("skip", d["id"], d["ok"], file=sys.stderr)
            continue
        ti = len(tests); tests.append(d["id"])
        for site, pairs in d["sites"].items():
            if pairs is None:
                key = "u"
            else:
                k = tuple(pairs)
                if k not in sid:
                    sid[k] = len(sets); sets.append([json.loads(p) for p in pairs])
                key = str(sid[k])
            sites.setdefault(site, {}).setdefault(key, []).append(ti)
with gzip.open(sys.argv[1], "wt", compresslevel=9) as fh:
    json.dump({"tests": tests, "sets": sets, "sites": sites}, fh, separators=(",", ":"))
print(len(tests), "tests", len(sets), "pair sets", len(sites), "sites", file=sys.stderr)
