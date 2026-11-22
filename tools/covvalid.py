"""Per-call coverage for dialect tests: each Validator.validate_* call is a case keyed by its SQL.

Each fixture case is one parse_and_optimize call (run in-process, as the selector's
runner does) plus its checks under subTest(title). Lines executed while a case is
current are attributed to that case's SQL; everything else is the test's base.

usage: covcase.py <sqlglot root> <out.json> <test ids file>
"""
import sys, os, json, time, signal, pickle, contextlib

ROOT = os.path.realpath(sys.argv[1]) + "/"
OUT = os.path.abspath(sys.argv[2])
ids = [l.strip() for l in open(sys.argv[3]) if l.strip()]
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["SKIP_INTEGRATION"] = "0"
import unittest, concurrent.futures, importlib
import duckdb, numpy, pandas, pandas.testing  # noqa


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
TOOL = 5
_fc = {}


def keep(fn):
    r = _fc.get(fn)
    if r is None:
        p = os.path.realpath(fn) if fn else ""
        r = p[len(ROOT):] if p.startswith(ROOT) and "/tests/" not in p[len(ROOT) - 1:] else False
        _fc[fn] = r
    return r


results = {}
for tid in ids:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        base = set()
        cases = {}
        state = {"cur": base}

        def on_line(code, line):
            f = keep(code.co_filename)
            if f is not False and code.co_flags & 1:
                state["cur"].add((f, line))
            return mon.DISABLE

        def on_start(code, offset):
            f = keep(code.co_filename)
            if f is not False and code.co_flags & 1:
                state["cur"].add((f, code.co_firstlineno))
            return mon.DISABLE

        def switch(target):
            state["cur"] = target
            mon.restart_events()

        mon.use_tool_id(TOOL, "covcase")
        mon.register_callback(TOOL, mon.events.LINE, on_line)
        mon.register_callback(TOOL, mon.events.PY_START, on_start)
        mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
        path, cls, meth = tid.split("::")
        mod = importlib.import_module(path[:-3].replace("/", "."))
        V = importlib.import_module("tests.dialects.test_dialect").Validator

        def wrap(name):
            orig = getattr(V, name)

            def f(self, sql, *a, **k):
                if not isinstance(sql, str):
                    return orig(self, sql, *a, **k)
                prev = state["cur"]
                switch(cases.setdefault(sql, set()))
                try:
                    return orig(self, sql, *a, **k)
                finally:
                    switch(prev)
            setattr(V, name, f)

        for name in ("validate_identity", "validate_all", "validate_transpile"):
            wrap(name)

        switch(base)
        suite = unittest.TestSuite([getattr(mod, cls)(meth)])
        res = unittest.TestResult()
        signal.alarm(900)
        suite.run(res)
        out = {"ok": res.wasSuccessful(), "base": sorted(base),
               "cases": {s: sorted(v) for s, v in cases.items()}}
        with os.fdopen(w, "w") as fh:
            json.dump(out, fh)
        os._exit(0)
    os.close(w)
    t0 = time.time()
    with os.fdopen(r) as fh:
        data = fh.read()
    os.waitpid(pid, 0)
    try:
        results[tid] = json.loads(data)
        print(f"{time.time() - t0:.1f}s {tid} ok={results[tid]['ok']} cases={len(results[tid]['cases'])}", flush=True)
    except ValueError:
        print(f"FAILED {tid}", flush=True)
json.dump(results, open(OUT, "w"))
