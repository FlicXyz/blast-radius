"""Records, per test, the operands every single-operator comparison in sqlglot/ sees.

Each test runs in a freshly forked child (cold imports and caches, as for the line
coverage map). sqlglot's modules are loaded through a finder that rewrites every
`a OP b` comparison into `_brcmp_(site, a, OP, b)`, which notes an encoding of the two
operands and returns `a OP b` unchanged.

usage: opcov.py <sqlglot root> <out.jsonl> <test ids file> [shard k/n]
"""
import ast, enum, importlib.abc, importlib.machinery, importlib.util, json, os, signal, sys, time

ROOT = os.path.realpath(sys.argv[1]) + "/"
OUT = os.path.abspath(sys.argv[2])
ids = [l.strip() for l in open(sys.argv[3]) if l.strip()]
if len(sys.argv) > 4:
    k, n = map(int, sys.argv[4].split("/"))
    ids = ids[k::n]
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["SKIP_INTEGRATION"] = "0"
import builtins, operator, unittest, pickle, concurrent.futures  # noqa
import duckdb, numpy, pandas, pandas.testing  # noqa

OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
       ast.Is: "is", ast.IsNot: "is not"}
FN = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt,
      ">=": operator.ge, "is": operator.is_, "is not": operator.is_not}
CAP = 64
REC = {}            # site -> set of encoded pairs, or None when unknown
UNK = object()
_plain_eq = {object.__eq__, type.__eq__}


def enc(x, depth=0):
    if x is None or isinstance(x, bool):
        return x
    t = type(x)
    if t in (int, float, str):
        return [t.__name__[0], x] if t is not float or x == x else UNK
    if isinstance(x, enum.Enum):
        if type(x).__eq__ not in (enum.Enum.__eq__, int.__eq__, str.__eq__) and not isinstance(x, (int, str)):
            return UNK
        v = x.value
        mix = "i" if isinstance(x, int) else "s" if isinstance(x, str) else ""
        return ["e", f"{t.__module__}.{t.__qualname__}", x.name, mix, v if isinstance(v, (int, str)) else None]
    if t in (tuple, frozenset) and depth == 0 and len(x) <= 8:
        items = [enc(i, 1) for i in x]
        if any(i is UNK for i in items):
            return UNK
        return ["t" if t is tuple else "z", items]
    if isinstance(x, type) and type(x).__eq__ in _plain_eq:
        return ["c", f"{x.__module__}.{x.__qualname__}"]
    return UNK


def _brcmp_(site, a, op, b):
    r = FN[op](a, b)
    s = REC.get(site, ())
    if s is not None:
        if s == ():
            s = REC[site] = set()
        if op in ("is", "is not"):
            # identity: only the outcome and what the operands are matters
            ea, eb = enc(a), enc(b)
            if ea is UNK or eb is UNK:
                if a is None or b is None:
                    # x is None: all that matters is that x is some other object
                    ea = ea if ea is not UNK else ["o"]
                    eb = eb if eb is not UNK else ["o"]
                else:
                    REC[site] = None
                    return r
        else:
            ea, eb = enc(a), enc(b)
            if ea is UNK or eb is UNK:
                REC[site] = None
                return r
        s.add(json.dumps([ea, eb]))
        if len(s) > CAP:
            REC[site] = None
    return r


builtins._brcmp_ = _brcmp_


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(func=ast.Name("_brcmp_", ast.Load()),
                        args=[ast.Constant(site), node.left, ast.Constant(OPS[type(node.ops[0])]),
                              node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        src = self.get_data(self.path).decode("utf-8")
        rel = os.path.relpath(self.path, ROOT)
        tree = Rewriter(rel).visit(ast.parse(src))
        ast.fix_missing_locations(tree)
        return compile(tree, self.path, "exec", dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        spec.loader = Loader(fullname, spec.origin)
        return spec


class SyncPool:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


sys.meta_path.insert(0, Finder())
concurrent.futures.ProcessPoolExecutor = SyncPool
out = open(OUT, "a")
for tid in ids:
    r, w = os.pipe()
    t0 = time.time()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        ok = None
        try:
            path, cls, meth = tid.split("::")
            mod = importlib.import_module(path[:-3].replace("/", "."))
            res = unittest.TestResult()
            signal.alarm(900)
            unittest.TestSuite([getattr(mod, cls)(meth)]).run(res)
            ok = res.wasSuccessful()
        except BaseException as e:  # noqa
            ok = repr(e)[:200]
        data = {s: (sorted(v) if v is not None else None) for s, v in REC.items() if v != ()}
        with os.fdopen(w, "w") as fh:
            json.dump({"id": tid, "ok": ok, "sites": data}, fh)
        os._exit(0)
    os.close(w)
    with os.fdopen(r) as fh:
        txt = fh.read()
    os.waitpid(pid, 0)
    if txt:
        out.write(txt + "\n"); out.flush()
        print(f"{time.time() - t0:.1f}s {tid} ok={json.loads(txt)['ok']}", flush=True)
    else:
        print(f"FAILED {tid}", flush=True)
