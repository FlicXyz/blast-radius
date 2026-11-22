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
        # a file is known rewritten if the recording loaded it or saw any of its sites
        for src in list(d.get("loaded", ())) + [x.rsplit(":", 2)[0] for x in list(d["sites"]) + list(d.get("dicts", {}))]:
            per_file.setdefault(src, {"sets": [], "sid": {}, "sites": {}})["wrapped"] = True
        for site, keys in d.get("dicts", {}).items():
            src = site.rsplit(":", 2)[0]
            pf = per_file.setdefault(src, {"sets": [], "sid": {}, "sites": {}})
            pf.setdefault("dsets", []); pf.setdefault("did", {}); pf.setdefault("dicts", {})
            if keys is None:
                key = "u"
            else:
                k = tuple(keys)
                if k not in pf["did"]:
                    pf["did"][k] = len(pf["dsets"]); pf["dsets"].append([json.loads(x) for x in keys])
                key = str(pf["did"][k])
            pf["dicts"].setdefault(site, {}).setdefault(key, []).append(ti)
with gzip.open(os.path.join(outdir, "tests.json.gz"), "wt") as fh:
    json.dump(tests, fh)
for src, pf in per_file.items():
    with gzip.open(os.path.join(outdir, src.replace("/", "__") + ".json.gz"), "wt", compresslevel=9) as fh:
        json.dump({"sets": pf["sets"], "sites": pf["sites"], "dsets": pf.get("dsets", []),
                   "dicts": pf.get("dicts", {}), "wrapped": pf.get("wrapped", False)}, fh, separators=(",", ":"))
print(len(tests), "tests", len(per_file), "files", file=sys.stderr)
