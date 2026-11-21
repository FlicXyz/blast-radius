"""Isolated per-test coverage at the pinned sqlglot commit.

Each test runs in a freshly forked child (sqlglot not yet imported), so caches
are cold and lazy imports are attributed to the test that triggers them.
Records: lines executed (import phase + test phase), files opened, and which
sqlglot classes were instantiated (Expr/Tokenizer/Parser/Generator/Dialect).
"""
import sys, os, json, builtins, io, time, signal
if __name__ == "__main__":
    ROOT = os.path.realpath(sys.argv[1]) + "/"
    OUTDIR = os.path.abspath(sys.argv[2])
    ids = [l.strip() for l in open(sys.argv[3]) if l.strip()]
    os.environ["BR_COV_DIR"] = OUTDIR
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    import unittest, sqlite3, csv, gzip, decimal, datetime, tempfile, concurrent.futures, multiprocessing, unittest.mock
    import duckdb, numpy, pandas, pandas.testing
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import brtrace

    for tid in ids:
        t0 = time.time()
        pid = os.fork()
        if pid == 0:
            try:
                os.environ["BR_TEST"] = tid.replace("/", "_").replace(":", "_")
                os.environ["BR_TEST_ID"] = tid
                brtrace.start(ROOT)
                path, cls, meth = tid.split("::")
                modname = path[:-3].replace("/", ".")
                import importlib
                mod = importlib.import_module(modname)
                brtrace.phase("test")
                suite = unittest.TestSuite([getattr(mod, cls)(meth)])
                res = unittest.TestResult()
                signal.alarm(600)
                suite.run(res)
                brtrace.extra["ok"] = res.wasSuccessful()
                brtrace.extra["errors"] = [str(e[1])[:300] for e in res.errors + res.failures]
            except BaseException as e:
                brtrace.extra["crash"] = repr(e)
            brtrace.dump(os.path.join(OUTDIR, os.environ["BR_TEST"] + ".main.json"))
            os._exit(0)
        os.waitpid(pid, 0)
        print(f"{time.time()-t0:.2f} {tid}", flush=True)
