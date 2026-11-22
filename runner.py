"""Runs chosen sqlglot tests against a patched tree, in a forked child.

The pinned checkout is read-only, so a patch is applied by writing the changed
files under /tmp and pointing imports and file reads at them:

* patched modules are served by a meta-path finder,
* patched data files (fixtures) are served by wrapping open()/gzip.open().

Each child imports sqlglot fresh, so nothing from one commit leaks into the
next. Results stream back over a pipe one test at a time, so a child that is
killed (hang, crash) still reports everything it finished.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
import pickle
import signal
import sys
import time
import traceback

SRC = None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class _PatchedLoader(importlib.machinery.SourceFileLoader):
    """Loads a module from its real path (so __file__ is unchanged) but with the patched source."""

    def __init__(self, fullname, path, source):
        super().__init__(fullname, path)
        self._source = source

    def get_data(self, path):
        if path == self.path:
            return self._source
        return super().get_data(path)

    def get_code(self, fullname):
        return compile(self._source, self.path, "exec", dont_inherit=True)


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, modules):
        self.modules = modules  # module name -> (real path, patched bytes or None if deleted, is_package)

    def find_spec(self, fullname, path=None, target=None):
        hit = self.modules.get(fullname)
        if hit is None:
            return None
        real, source, is_pkg = hit
        if source is None:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        loader = _PatchedLoader(fullname, real, source)
        return importlib.util.spec_from_file_location(
            fullname, real, loader=loader,
            submodule_search_locations=[os.path.dirname(real)] if is_pkg else None,
        )


class _SyncPool:
    """Stands in for ProcessPoolExecutor: runs tasks in-process (with a pickle
    round trip, like the real pool) to avoid spawning interpreters."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future
        f = Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            r = fn(*a, **k)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, **kw):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()
        return False


class _Timeout(BaseException):
    pass


class _StopRun(BaseException):
    pass


STATE = {"failures": 0, "stop_after": 1, "lazy": False, "filters": {}, "filter": None, "cont": set()}


EXEC_FILTERED = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_sqls",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": "tpch_sqls",
}


def _filter_fixtures(mod):
    """While a test with a case filter runs, its fixture loop sees only those cases."""
    orig = mod.load_sql_fixture_pairs

    def pairs(filename):
        keep = STATE["filter"]
        for meta, sql, expected in orig(filename):
            if keep is None or sql in keep:
                yield meta, sql, expected

    mod.load_sql_fixture_pairs = pairs
    mod._br_filtered = True

# Class fixtures that only build data a few tests read: built on first use instead.
# Applies only when nothing the fixture runs is patched (the selector checks), so it
# behaves exactly as in the pinned tree, just later or never.
LAZY_SETUP = {("tests/test_executor.py", "TestExecutor")}


def _lazy_class_setup(cls):
    import inspect
    import re
    orig_setup = cls.__dict__.get("setUpClass")
    orig_tear = cls.__dict__.get("tearDownClass")
    if not isinstance(orig_setup, classmethod):
        return
    names = set(re.findall(r"\bcls\.(\w+)\s*=", inspect.getsource(orig_setup.__func__)))
    if not names:
        return
    state = {"loaded": False}

    class Lazy:
        def __init__(self, name):
            self.name = name

        def __get__(self, obj, owner):
            load()
            return getattr(cls, self.name)

    def load():
        if state["loaded"]:
            return
        state["loaded"] = True
        for n in names:
            if isinstance(cls.__dict__.get(n), Lazy):
                delattr(cls, n)
        orig_setup.__func__(cls)

    def setUpClass(c):
        state["loaded"] = False
        for n in names:
            setattr(cls, n, Lazy(n))

    def tearDownClass(c):
        if state["loaded"] and isinstance(orig_tear, classmethod):
            orig_tear.__func__(cls)
        for n in names:
            if isinstance(cls.__dict__.get(n), Lazy):
                delattr(cls, n)

    cls.setUpClass = classmethod(setUpClass)
    cls.tearDownClass = classmethod(tearDownClass)


def _alarm(signum, frame):
    raise _Timeout("test timed out")


def _purge():
    for name in list(sys.modules):
        if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
            del sys.modules[name]


def _child(src, patched_files, deleted, plan, wfd, import_checks, purge=True, stop_after=1, lazy=False):
    STATE["stop_after"] = stop_after
    STATE["lazy"] = lazy
    """plan: list of (test_id, timeout_s). Writes JSON lines to wfd."""
    global SRC
    SRC = src
    out = os.fdopen(wfd, "w", buffering=1)

    def emit(**kw):
        out.write(json.dumps(kw) + "\n")
        out.flush()

    os.dup2(2, 1)  # nothing a test prints may reach our stdout
    sys.stdout = sys.stderr
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(src)
    if src not in sys.path:
        sys.path.insert(0, src)

    # patched python modules
    mods = {}
    data = {}
    for rel, tmp in patched_files.items():
        if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            name = rel[:-3]
            is_pkg = name.endswith("/__init__")
            if is_pkg:
                name = name[: -len("/__init__")]
            with open(tmp, "rb") as fh:
                mods[name.replace("/", ".")] = (os.path.join(src, rel), fh.read(), is_pkg)
        data[os.path.join(src, rel)] = tmp
    for rel in deleted:
        if rel.endswith(".py"):
            name = rel[:-3]
            if name.endswith("/__init__"):
                name = name[: -len("/__init__")]
            mods[name.replace("/", ".")] = (os.path.join(src, rel), None, False)
        data[os.path.join(src, rel)] = None
    if purge or any(m in sys.modules for m in mods):
        _purge()
    if mods:
        sys.meta_path.insert(0, _PatchedFinder(mods))
    if data:
        import builtins
        import gzip

        def redirect(p):
            if isinstance(p, (str, os.PathLike)):
                ap = os.path.abspath(os.fspath(p))
                if ap in data:
                    if data[ap] is None:
                        raise FileNotFoundError(2, "No such file or directory", os.fspath(p))
                    return data[ap]
            return p

        _open = builtins.open
        def open_(file, *a, **k):
            return _open(redirect(file), *a, **k)
        builtins.open = open_
        io.open = open_
        _gopen = gzip.open
        def gopen(filename, *a, **k):
            return _gopen(redirect(filename), *a, **k)
        gzip.open = gopen
        import pathlib
        _rt = pathlib.Path.read_text
        _rb = pathlib.Path.read_bytes
        _po = pathlib.Path.open
        pathlib.Path.read_text = lambda self, *a, **k: _rt(pathlib.Path(redirect(self)), *a, **k)
        pathlib.Path.read_bytes = lambda self, *a, **k: _rb(pathlib.Path(redirect(self)), *a, **k)
        pathlib.Path.open = lambda self, *a, **k: _po(pathlib.Path(redirect(self)), *a, **k)

    import concurrent.futures
    concurrent.futures.ProcessPoolExecutor = _SyncPool
    import unittest

    signal.signal(signal.SIGALRM, _alarm)

    # import checks: does the patched code import at all?
    for m in import_checks:
        try:
            signal.setitimer(signal.ITIMER_REAL, 20)
            importlib.import_module(m)
            signal.setitimer(signal.ITIMER_REAL, 0)
            emit(kind="import", module=m, ok=True)
        except BaseException as e:  # noqa
            signal.setitimer(signal.ITIMER_REAL, 0)
            emit(kind="import", module=m, ok=False, err=repr(e)[:300])

    must = {e[0] for e in plan if len(e) > 2 and e[2]}
    STATE["filters"] = {e[0]: set(e[3]) for e in plan if len(e) > 3 and e[3] is not None}
    # a failure at one of these does not stop the run: what follows is cheaper to run than to list
    STATE["cont"] = {e[0] for e in plan if len(e) > 4 and e[4]}
    done_ids = set()
    stopped = _run_groups([(e[0], e[1]) for e in plan], emit, done_ids)
    if stopped:
        emit(kind="stopped")
        # sentinels: classmates of slow tests, whose results decide whether the slow
        # tests are listed. They run to the end whatever else failed.
        rest = [(e[0], e[1]) for e in plan if e[0] in must and e[0] not in done_ids]
        if rest:
            STATE["stop_after"] = 10 ** 9
            _run_groups(rest, emit, done_ids)
    emit(kind="done")
    out.close()
    os._exit(0)


def _run_groups(plan, emit, done_ids):
    """Runs (test_id, timeout) pairs grouped by class; returns True if stopped early."""
    import unittest
    groups = {}
    for tid, to in plan:
        path, cls, meth = tid.split("::")
        groups.setdefault((path, cls), []).append((meth, to, tid))

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.failed = set()

        def _id(self, test):
            return getattr(test, "_br_id", None)

        def startTest(self, test):
            super().startTest(test)
            STATE["filter"] = getattr(test, "_br_filter", None)
            attr = EXEC_FILTERED.get(getattr(test, "_br_id", None))
            if attr and STATE["filter"] is not None:
                # TPC tests: cut the class's query list down to the selected queries
                setattr(test, attr, [q for q in getattr(type(test), attr) if q[1] in STATE["filter"]])
            to = getattr(test, "_br_timeout", None)
            if to:
                signal.setitimer(signal.ITIMER_REAL, to)
            test._br_t0 = time.process_time()

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            STATE["filter"] = None
            super().stopTest(test)
            tid = self._id(test)
            if tid:
                done_ids.add(tid)
                emit(kind="test", id=tid, ok=tid not in self.failed,
                     cpu=round(time.process_time() - getattr(test, "_br_t0", time.process_time()), 3))
                if tid in self.failed:
                    STATE["failures"] += 1
                    if STATE["failures"] >= STATE["stop_after"] and tid not in STATE["cont"]:
                        raise _StopRun()

        def _fail(self, test, err):
            tid = self._id(test)
            if tid:
                self.failed.add(tid)
            else:
                # class/module fixture error: every test of the group fails
                emit(kind="fixture_error", desc=str(test)[:200], err=str(err[1])[:200])
                self.fixture_failed = True

        def addError(self, test, err):
            super().addError(test, err)
            self._fail(test, err)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._fail(test, err)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                tid = self._id(test)
                if tid:
                    self.failed.add(tid)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            tid = self._id(test)
            if tid:
                self.failed.add(tid)

    for (path, clsname), items in groups.items():
        modname = path[:-3].replace("/", ".")
        try:
            signal.setitimer(signal.ITIMER_REAL, 30)
            mod = importlib.import_module(modname)
            signal.setitimer(signal.ITIMER_REAL, 0)
            cls = getattr(mod, clsname)
            if STATE["filters"] and hasattr(mod, "load_sql_fixture_pairs") and not getattr(mod, "_br_filtered", False):
                _filter_fixtures(mod)
            if STATE["lazy"] and (path, clsname) in LAZY_SETUP and not getattr(cls, "_br_lazy", False):
                _lazy_class_setup(cls)
                cls._br_lazy = True
        except BaseException as e:  # noqa
            signal.setitimer(signal.ITIMER_REAL, 0)
            for meth, to, tid in items:
                done_ids.add(tid)
                emit(kind="test", id=tid, ok=False, err="import: " + repr(e)[:200])
            continue
        tests = []
        for meth, to, tid in items:
            try:
                t = cls(meth)
            except BaseException as e:  # noqa
                done_ids.add(tid)
                emit(kind="test", id=tid, ok=False, err="init: " + repr(e)[:200])
                continue
            t._br_id = tid
            t._br_timeout = to
            t._br_filter = STATE["filters"].get(tid)
            tests.append(t)
        suite = unittest.TestSuite(tests)
        res = Result()
        res.fixture_failed = False
        stop = False
        try:
            suite.run(res)
        except _StopRun:
            stop = True
        except BaseException as e:  # noqa
            emit(kind="group_error", err=repr(e)[:200])
        if res.fixture_failed:
            emit(kind="group_failed", tests=[tid for _, _, tid in items])
            done_ids.update(tid for _, _, tid in items)
            stop = True
        if stop:
            return True
        # class cleanups/teardown of the module
        try:
            unittest.suite._call_if_exists(res, "_restoreStdout")
        except Exception:
            pass
    return False


def start(src, patched_files, deleted, plan, import_checks, purge=True, stop_after=1, lazy=False):
    """Forks a child running plan; returns (pid, read file)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            _child(src, patched_files, deleted, plan, w, import_checks, purge, stop_after, lazy)
        except BaseException:  # noqa
            traceback.print_exc()
        os._exit(1)
    os.close(w)
    os.set_blocking(r, False)
    return pid, r


def preload():
    """Third-party and stdlib modules the tests import, loaded once before forking."""
    for m in ("unittest", "unittest.mock", "json", "csv", "gzip", "sqlite3", "decimal", "datetime",
              "tempfile", "concurrent.futures", "multiprocessing", "doctest", "tracemalloc", "gc",
              "textwrap", "itertools", "functools", "re", "typing", "dataclasses", "enum",
              "collections", "copy", "pickle", "logging", "math", "uuid", "pathlib", "inspect"):
        try:
            importlib.import_module(m)
        except Exception:
            pass


def preload_sqlglot(src):
    """Imports the unpatched sqlglot core once, for commits whose patched modules it doesn't load."""
    if src not in sys.path:
        sys.path.insert(0, src)
    cwd = os.getcwd()
    try:
        os.chdir(src)
        import sqlglot  # noqa: F401
        import sqlglot.optimizer  # noqa: F401
    except BaseException:  # noqa
        _purge()
        return False
    finally:
        os.chdir(cwd)
    return True


def preload_heavy():
    for m in ("numpy", "pandas", "pandas.testing", "duckdb"):
        try:
            importlib.import_module(m)
        except Exception:
            pass
