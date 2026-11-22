"""Builds ops/tok.json.gz from opcov.py output: which tests' tokenizers read each
TokenizerCore setting, per owning tokenizer class, plus where else sqlglot and its tests
name the class attributes those settings come from.

usage: mktok.py <sqlglot root> <out.json.gz> <opcov jsonl>...
"""
import ast, glob, gzip, json, os, sys
root = os.path.realpath(sys.argv[1])
out_path = sys.argv[2]

# Tokenizer attribute -> core setting, from Tokenizer._init_core
src = open(os.path.join(root, "sqlglot/tokens.py")).read()
tree = ast.parse(src)
attr_of, init_core = {}, None
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef) and n.name == "_init_core":
        init_core = n
        for c in ast.walk(n):
            if isinstance(c, ast.Call):
                for kw in c.keywords:
                    v = kw.value
                    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self" and v.attr.isupper():
                        attr_of[v.attr] = kw.arg
init_lines = set(range(init_core.lineno, init_core.end_lineno + 1))

# other places naming those attributes
others = {k: {"broad": False, "lines": [], "test_files": []} for k in attr_of}
for f in glob.glob(os.path.join(root, "sqlglot/**/*.py"), recursive=True) + glob.glob(os.path.join(root, "tests/**/*.py"), recursive=True):
    rel = os.path.relpath(f, root)
    try:
        t = ast.parse(open(f, encoding="utf-8").read())
    except SyntaxError:
        continue

    def walk(n, in_func):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            in_func = True
        name = None
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            name = n.id
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            name = n.attr
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            name = n.value
        if name in others and not (rel == "sqlglot/tokens.py" and getattr(n, "lineno", 0) in init_lines):
            o = others[name]
            if rel.startswith("tests/"):
                o["test_files"].append(rel)
            elif in_func:
                o["lines"].append([rel, n.lineno])
            else:
                o["broad"] = True
        for c in ast.iter_child_nodes(n):
            walk(c, in_func)
    walk(t, False)

owners, oid, definers, tests = [], {}, {}, {}
for f in sys.argv[3:]:
    for line in open(f):
        d = json.loads(line)
        if d["ok"] is not True or "tok" not in d:
            continue
        reads = []
        for o, a in d["tok"]["reads"]:
            if o not in oid:
                oid[o] = len(owners); owners.append(o)
            reads.append([oid[o], a])
        tests[d["id"]] = reads
        for o, dm in d["tok"]["definers"].items():
            definers.setdefault(o, {}).update({k: v for k, v in dm.items() if k in attr_of})
with gzip.open(out_path, "wt") as fh:
    json.dump({"attr_of": attr_of, "others": others, "owners": owners, "definers": definers, "tests": tests}, fh)
print(len(tests), "tests", len(owners), "owners", {k: (v["broad"], len(v["lines"]), len(v["test_files"])) for k, v in others.items()}, file=sys.stderr)
