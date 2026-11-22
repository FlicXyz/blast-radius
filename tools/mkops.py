"""Compacts opcov.py output into ops/: tests.json.gz (test ids) and one file per source
file holding, per comparison site, the tests grouped by the operand pairs they saw
("u": tests whose operands could not be recorded). Only the file a patch edits is loaded."""
import gzip, json, os, sys
outdir = sys.argv[1]
os.makedirs(outdir, exist_ok=True)
tests, per_file = [], {}
for f in sorted(sys.argv[2:]):
    for line in open(f):
        d = json.loads(line)
        if d["ok"] is not True:
            continue
        ti = len(tests); tests.append(d["id"])
        for site, pairs in d["sites"].items():
            src = site.rsplit(":", 2)[0]
            pf = per_file.setdefault(src, {"sets": [], "sid": {}, "sites": {}})
            if pairs is None:
                key = "u"
            else:
                k = tuple(pairs)
                if k not in pf["sid"]:
                    pf["sid"][k] = len(pf["sets"]); pf["sets"].append([json.loads(p) for p in pairs])
                key = str(pf["sid"][k])
            pf["sites"].setdefault(site, {}).setdefault(key, []).append(ti)
with gzip.open(os.path.join(outdir, "tests.json.gz"), "wt") as fh:
    json.dump(tests, fh)
for src, pf in per_file.items():
    with gzip.open(os.path.join(outdir, src.replace("/", "__") + ".json.gz"), "wt", compresslevel=9) as fh:
        json.dump({"sets": pf["sets"], "sites": pf["sites"]}, fh, separators=(",", ":"))
print(len(tests), "tests", len(per_file), "files", file=sys.stderr)
