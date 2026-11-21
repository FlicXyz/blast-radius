"""Combine per-test coverage dumps into the committed map (map.json.gz)."""
import json, glob, sys, gzip, collections, os, ast
covdir, srcroot, testsjson, out = sys.argv[1:5]
tests = [t["id"] for t in json.load(open(testsjson))["tests"]]
idx = {t: i for i, t in enumerate(tests)}
def key(tid): return tid.replace("/", "_").replace(":", "_")
k2i = {key(t): i for t, i in idx.items()}
lines = collections.defaultdict(set); opened = collections.defaultdict(set)
classes = collections.defaultdict(set); tokens = collections.defaultdict(set); words = collections.defaultdict(set)
for fn in glob.glob(os.path.join(covdir, "*.json")):
    base = os.path.basename(fn)
    k = base.split(".py__")[0] + ".py__" + base.split(".py__")[1].split(".")[0]
    i = k2i.get(k)
    if i is None:
        print("unknown", base, file=sys.stderr); continue
    d = json.load(open(fn))
    for f, l in d.get("import", []) + d.get("test", []):
        if f.startswith(("sqlglot/", "tests/")): lines[(f, l)].add(i)
    for f in d.get("opened", []): opened[f].add(i)
    for c in d.get("classes", []): classes[c].add(i)
    for tk in d.get("tokens", []): tokens[tk].add(i)
    for w in d.get("words", []): words[w].add(i)
# PY_START on class bodies / modules (older tracer) recorded their first line as runtime: drop those
for f in sorted({f for f, _ in lines}):
    p = os.path.join(srcroot, f)
    if not f.endswith(".py") or not os.path.exists(p): continue
    tree = ast.parse(open(p).read())
    heads = {1} | {n.lineno for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    for h in heads:
        lines.pop((f, h), None)
# names referenced on runtime lines
names = collections.defaultdict(set)
for f in sorted({f for f, _ in lines}):
    p = os.path.join(srcroot, f)
    if not os.path.exists(p) or not f.endswith(".py"): continue
    tree = ast.parse(open(p).read())
    flines = {}
    for node in ast.walk(tree):
        n = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else None
        if n is None: continue
        s = lines.get((f, node.lineno))
        if s: names[n] |= s
sets = {}; setlist = []
def sid(s):
    k = tuple(sorted(s))
    if k not in sets: sets[k] = len(setlist); setlist.append(list(k))
    return sets[k]
L = collections.defaultdict(dict)
for (f, l), s in lines.items(): L[f][l] = sid(s)
N = len(tests)
res = {"tests": tests, "lines": L,
       "opened": {f: sid(s) for f, s in opened.items()},
       "classes": {c: sid(s) for c, s in classes.items()},
       "tokens": {c: sid(s) for c, s in tokens.items()},
       "words": {c: sid(s) for c, s in words.items() if len(s) < N * 0.5},
       "names": {c: sid(s) for c, s in names.items()},
       }
res["sets"] = setlist
with gzip.open(out, "wt", compresslevel=9) as fh: json.dump(res, fh, separators=(",", ":"))
print("sets", len(setlist), "lines", len(lines), "classes", len(classes), "tokens", len(tokens), "words", len(res["words"]), "names", len(names), file=sys.stderr)
