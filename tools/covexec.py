"""Per-query coverage for TestExecutor's TPC tests, in the covcase.py output format.

Each query runs alone in a fresh fork, with the test's query list cut down to it (the
selector's runner cuts it the same way); the base is the run with an empty list, which
also covers the class fixture.

usage: covexec.py <sqlglot root> <out.json>
"""
import sys, os, json, pickle, importlib, unittest, concurrent.futures, signal, time

ROOT = os.path.realpath(sys.argv[1]) + "/"
OUT = os.path.abspath(sys.argv[2])
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["SKIP_INTEGRATION"] = "0"
import duckdb, numpy, pandas, pandas.testing  # noqa

TESTS = {"test_execute_tpcds": "tpcds_sqls", "test_execute_tpch": "tpch_sqls", "test_optimized_tpch": "tpch_sqls"}


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

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


concurrent.futures.ProcessPoolExecutor = SyncPool
mon = sys.monitoring
_fc = {}


def keep(fn):
    r = _fc.get(fn)
    if r is None:
        p = os.path.realpath(fn) if fn else ""
        rel = p[len(ROOT):] if p.startswith(ROOT) else ""
        r = rel if rel.startswith("sqlglot/") else False
        _fc[fn] = r
    return r


def run(meth, attr, only):
    """Lines (function code) executed by the test when its query list is `only`."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        lines = set()

        def on_line(code, line):
            f = keep(code.co_filename)
            if f is not False and code.co_flags & 1:
                lines.add((f, line))
            return mon.DISABLE

        def on_start(code, offset):
            f = keep(code.co_filename)
            if f is not False and code.co_flags & 1:
                lines.add((f, code.co_firstlineno))
            return mon.DISABLE

        mon.use_tool_id(5, "covexec")
        mon.register_callback(5, mon.events.LINE, on_line)
        mon.register_callback(5, mon.events.PY_START, on_start)
        mon.set_events(5, mon.events.LINE | mon.events.PY_START)
        mod = importlib.import_module("tests.test_executor")
        cls = mod.TestExecutor
        cls.setUpClass()
        t = cls(meth)
        allq = getattr(cls, attr)
        setattr(t, attr, [q for q in allq if q[1] in only] if only is not None else [])
        res = unittest.TestResult()
        signal.alarm(600)
        t.run(res)
        out = {"ok": res.wasSuccessful(), "lines": sorted(lines),
               "sqls": [q[1] for q in allq]}
        with os.fdopen(w, "w") as fh:
            json.dump(out, fh)
        os._exit(0)
    os.close(w)
    with os.fdopen(r) as fh:
        data = json.loads(fh.read())
    os.waitpid(pid, 0)
    return data


results = {}
for meth, attr in TESTS.items():
    tid = f"tests/test_executor.py::TestExecutor::{meth}"
    t0 = time.time()
    base = run(meth, attr, None)
    cases = {}
    ok = base["ok"]
    for sql in base["sqls"]:
        d = run(meth, attr, {sql})
        ok = ok and d["ok"]
        cases[sql] = d["lines"]
    results[tid] = {"ok": ok, "base": base["lines"], "cases": cases}
    print(f"{time.time() - t0:.1f}s {tid} ok={ok} cases={len(cases)}", flush=True)
json.dump(results, open(OUT, "w"))
