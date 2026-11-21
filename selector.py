"""Blast Radius selector for sqlglot patches.

For each patch:
1. Static analysis (analyze.py) maps the changed lines / class attributes /
   fixtures to the tests that could observe them, using a per-test coverage map
   built once from the pinned commit (map.json.gz).
2. The cheap candidates are actually run against the patched tree (runner.py),
   in forked children, two at a time. Tests that pass are dropped.
3. What is printed: the tests that failed, anything we could not verify
   (timeouts, crashes, deadline), and the expensive candidates when the change
   was shown to break something or nothing cheap covers the same code.
"""

import gzip
import json
import os
import select
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import analyze  # noqa: E402
import split  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", "/app/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(HERE, "map.json.gz"))
CASES = os.environ.get("BR_CASES", os.path.join(HERE, "cases.json.gz"))
TMP = os.environ.get("BR_TMP", "/tmp/br")

DEBUG = os.environ.get("BR_DEBUG")
SENTINEL_RATIO = 0.5  # run a slow test's classmates to the end when they cost under this share of it
EXPENSIVE = 2.0      # seconds: tests above this are not run, only reasoned about
PARALLEL = 2
# With slow candidates in play, keep running after a failure: changes that break
# the slow tests break many cheap ones too, so a handful of failures decides it.
STOP_AFTER_EXP = 1
CPU_FACTOR = 1.5     # assumed board CPU seconds per recorded test second
# CPU seconds of class-level fixtures (setUpClass) at the pinned commit; the
# recorded per-test seconds don't include them.
SETUP_COST = {"tests/test_executor.py::TestExecutor": 1.3}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


PRINTED = set()
ALL_COMMITS = []
ALL_MS = {}


def emergency(reason):
    """Something went badly wrong: answer every commit not yet printed with every test."""
    log(f"fallback: {reason}")
    if not ALL_MS:
        try:
            tj = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
            ALL_MS.update({t["number"]: round(t["seconds"] * 1000) for t in tj})
        except Exception:
            pass
    commits = ALL_COMMITS or sorted(f[:-6] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch"))
    _, workers = split.best_split([(v, k) for k, v in ALL_MS.items()])
    for cid in commits:
        if cid not in PRINTED:
            print(json.dumps({"commit": cid, "workers": workers}), flush=True)
            PRINTED.add(cid)


def main():
    import signal
    def on_alarm(signum, frame):
        raise TimeoutError("selector deadline")
    signal.signal(signal.SIGALRM, on_alarm)
    try:
        n = len([f for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch")])
        signal.alarm(int(max(30, 5.5 * n)))  # the batch limit is 6 s a commit
        _main()
        signal.alarm(0)
    except BaseException as e:  # noqa
        signal.alarm(0)
        emergency(repr(e))


def _main():
    t0 = time.time()
    tj = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    by_id = {t["id"]: t for t in tj}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tj}
    cdir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    ALL_COMMITS[:] = commits
    ALL_MS.update(ms)
    n = len(commits)
    deadline = t0 + max(20.0, min(5.0 * n, 0.8 * 6.0 * n))

    mp = analyze.Map(MAP)
    an = analyze.Analyzer(SRC, mp)
    to_num = [by_id[t]["number"] if t in by_id else None for t in mp.tests]
    known = {by_id[t]["number"] for t in mp.tests if t in by_id}
    unknown_tests = [t["number"] for t in tj if t["number"] not in known]

    plans = {}
    for cid in commits:
        try:
            plans[cid] = plan_commit(cid, open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", errors="surrogateescape").read(), an, mp, to_num, ms, unknown_tests)
        except Exception as e:  # noqa
            log(f"{cid}: planning failed {e!r}; listing everything")
            plans[cid] = {"final_static": set(ms), "cands": set(ms), "run": [], "imports": [], "expensive": set(), "note": "plan-failed"}
    log(f"planned {n} commits in {time.time() - t0:.1f}s")

    # run phase
    to_run = [c for c in commits if plans[c].get("run") or plans[c].get("imports")]
    if to_run:
        import runner
        runner.preload()
        # numpy/pandas/duckdb: load once here only if several children would load them
        n_heavy = sum(1 for c in to_run if any(needs_heavy(SRC, t.split("::")[0]) for t in plans[c]["run_ids"]))
        if n_heavy >= 2:
            runner.preload_heavy()
        # commits that patch nothing the core import loads can reuse an imported sqlglot
        shared = runner.preload_sqlglot(SRC)
        for c in to_run:
            p = plans[c]
            mods = set()
            for rel in list(p["patched"]) + list(p["deleted"]):
                if rel.endswith(".py"):
                    m = rel[:-3]
                    if m.endswith("/__init__"):
                        m = m[: -len("/__init__")]
                    mods.add(m.replace("/", "."))
            p["purge"] = not shared or any(m in sys.modules for m in mods)
        log(f"shared sqlglot import: {shared}, {sum(not plans[c]['purge'] for c in to_run)} commits reuse it")
        run_all(runner, to_run, plans, deadline)

    for cid in commits:
        p = plans[cid]
        try:
            final = finalize(p)
        except Exception as e:  # noqa
            log(f"{cid}: finalize failed {e!r}")
            final = set(p.get("cands", ())) | set(p.get("expensive", ()))
        final = {x for x in final if x in ms}
        cost, workers = split.best_split([(ms[x], x) for x in final])
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
        PRINTED.add(cid)
        log(f"{cid}: cand={len(p.get('cands', ()))} ran={len(p.get('run', ()))} failed={len(p.get('failed', ()))} "
            f"final={len(final)} cost={cost/1000:.1f}s cpu={p.get('cpu', 0):.2f} tcpu={p.get('tcpu', 0):.2f} purge={p.get('purge')} {p.get('note', '')}")
        if DEBUG and p.get("tlist"):
            rec = sum(ms.get(n, 0) for n in p.get("run", ())) / 1000
            log(f"   recorded={rec:.2f} top: " + "; ".join(f"{c:.2f} {t}" for c, t in sorted(p["tlist"], reverse=True)[:6]))
    log(f"total wall {time.time() - t0:.1f}s")


_heavy_cache = {}


LAZY_DEPS = ("sqlglot/executor/table.py", "tests/helpers.py", "tests/test_executor.py",
             "tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")


_cases = {}


def load_cases():
    if "d" not in _cases:
        try:
            with gzip.open(CASES, "rt") as fh:
                _cases["d"] = json.load(fh)
        except (OSError, ValueError):
            _cases["d"] = {"tests": {}, "sets": []}
    return _cases["d"]


def case_filters(res, mp, to_num):
    """num -> (SQLs of the fixture cases to run, number of cases) for candidates whose
    selection rests only on changed lines that just some of their cases execute."""
    cm = None
    out = {}
    for i in res.cands:
        tid = mp.tests[i]
        if not tid.startswith("tests/test_optimizer.py::TestOptimizer::") or to_num[i] is None:
            continue
        if cm is None:
            cm = load_cases()
        info = cm["tests"].get(tid)
        if info is None:
            continue
        sel, full = set(), False
        for tests, ranges in res.ranged:
            if i not in tests:
                continue
            if ranges is None:
                full = True
                break
            for f, lo, hi in ranges:
                for l in range(lo, hi + 1):
                    if i not in mp.line_tests(f, l, l):
                        continue
                    k = info["lines"].get(f"{f}:{l}")
                    if k is None:
                        full = True
                        break
                    sel.update(cm["sets"][k])
                if full:
                    break
            if full:
                break
        if not full:
            out[to_num[i]] = ([info["sqls"][j] for j in sorted(sel)], len(info["sqls"]))
    return out


def split_estimate(total, biggest, n):
    """Seconds that listing n tests of this total and largest duration costs (LPT bound)."""
    best = None
    for k in range(1, min(8, n) + 1):
        c = max(biggest, total / k) / 1000 + 2 * k
        best = c if best is None else min(best, c)
    return best or 0


def setup_cost(c, p):
    if lazy_ok(p) and tuple(c.split("::")) in LAZY_CLASSES:
        return 0.0
    return SETUP_COST.get(c, 0)


LAZY_CLASSES = {("tests/test_executor.py", "TestExecutor")}


def lazy_ok(p):
    """TestExecutor's fixture can be built lazily when nothing it runs is patched."""
    return not any(rel.startswith(d) for rel in list(p.get("patched", ())) + list(p.get("deleted", ())) for d in LAZY_DEPS)


def needs_heavy(src, rel):
    if rel not in _heavy_cache:
        try:
            txt = open(os.path.join(src, rel)).read()
        except OSError:
            txt = ""
        _heavy_cache[rel] = any(w in txt for w in ("import duckdb", "import pandas", "import numpy"))
    return _heavy_cache[rel]


def plan_commit(cid, text, an, mp, to_num, ms, unknown_tests):
    res, files = an.analyze(text)
    cands = {to_num[i] for i in res.cands if to_num[i] is not None}
    p = {"cands": cands, "loc": [], "imports": sorted(res.import_modules), "note": ""}
    p["import_fail"] = {m: {to_num[i] for i in s if to_num[i] is not None} for m, s in res.import_fail_tests.items()}
    # patched files for the runner
    patched, deleted = {}, []
    ok = True
    for fd in files:
        old, new = fd["old"], fd["new"]
        if new is None:
            deleted.append(old)
            continue
        if fd["binary"]:
            ok = False
            continue
        base = ""
        if old is not None:
            try:
                base = open(os.path.join(SRC, old), encoding="utf-8", errors="surrogateescape").read()
            except OSError:
                base = ""
        txt = analyze.apply_hunks(base, fd["hunks"])
        if txt is None:
            ok = False
            continue
        if old is not None and old != new:
            deleted.append(old)
        d = os.path.join(TMP, cid)
        dst = os.path.join(d, new)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(txt)
        patched[new] = dst
    p["patched"], p["deleted"] = patched, deleted
    if getattr(res, "config_change", False):
        p["note"] += " config-change"
    # likely failures first: tests covering more changed spots, from the test file named
    # after the changed module, cheap ones first; slow class fixtures last
    hits = {}
    for why, s in res.locs:
        for i in s:
            if to_num[i] is not None:
                hits[to_num[i]] = hits.get(to_num[i], 0) + 1
    stems = {os.path.basename(f).rsplit(".", 1)[0] for f in res.files_changed if f}
    def order(x):
        tid = mp_id(mp, to_num, x)
        tf = os.path.basename(tid.split("::")[0])[5:-3]
        setup = setup_cost("::".join(tid.split("::")[:2]), p)
        return (setup > 0.5, -(tf in stems), -hits.get(x, 0), ms[x])
    # fixture-driven tests whose changed lines only some fixture cases reach run just those
    filters = case_filters(res, mp, to_num)
    p["filters"] = filters
    eff = dict((x, ms[x]) for x in cands)
    for x, (keep, total) in filters.items():
        eff[x] = round(ms[x] * len(keep) / max(total, 1)) + 5
    cheap = sorted((x for x in cands if eff[x] <= EXPENSIVE * 1000), key=lambda x: order(x)[:3] + (eff[x],))
    expensive = {x for x in cands if eff[x] > EXPENSIVE * 1000}
    list_cost = split.best_split([(ms[x], x) for x in cands])[0] / 1000
    run_cost = sum(eff[x] for x in cheap) / 1000 + 0.2 + sum(
        setup_cost(c, p) for c in {"::".join(mp_id(mp, to_num, x).split("::")[:2]) for x in cheap})
    # A real edit to a fixture almost always breaks the test reading it, so running it
    # first only adds CPU: list such tests straight away.
    data_only = files and all(
        (fd["new"] or fd["old"] or "").startswith("tests/") and not (fd["new"] or fd["old"]).endswith(".py")
        for fd in files)
    if data_only and not all(_comment_only_fixture(fd) for fd in files):
        p["run"] = []
        p["final_static"] = set(cands)
        p["note"] += " fixture-list"
    elif not ok:
        p["run"] = []
        p["final_static"] = set(cands)
        p["note"] += " unappliable"
    elif cheap and run_cost > list_cost + 1.0:
        p["run"] = []
        p["final_static"] = set(cands)
        p["note"] += f" list-only(run {run_cost:.1f} > list {list_cost:.1f})"
    else:
        # in a mixed commit, the tests reading an edited fixture are listed as above;
        # only the code side is run
        fixture_static = set()
        for fd in files:
            rel = fd["new"] or fd["old"] or ""
            if rel.startswith("tests/") and not rel.endswith(".py") and not _comment_only_fixture(fd):
                for why, s in res.locs:
                    if why == f"data {rel}":
                        fixture_static |= {to_num[i] for i in s if to_num[i] is not None}
        p["run"] = [x for x in cheap if x not in fixture_static]
        p["final_static"] = fixture_static & cands
        if fixture_static:
            p["note"] += f" fixture-static({len(fixture_static)})"
    p["expensive"] = expensive
    p["run_ids"] = [mp_id(mp, to_num, x) for x in p["run"]]
    # classmates of each slow test among the cheap ones we run: if they all pass, the
    # slow test is not listed even when other tests fail. When they are cheap next to
    # the slow test, they run to the end even after the stop point.
    run_set = set(p["run"])
    cls_of = lambda x: "::".join(mp_id(mp, to_num, x).split("::")[:2])
    p["sameclass"] = {}
    must = set()
    by_cls = {}
    for e in expensive:
        c = cls_of(e)
        mates = {x for x in run_set if cls_of(x) == c}
        p["sameclass"][e] = mates
        by_cls.setdefault(c, [set(), 0])
        by_cls[c][0] |= mates
        by_cls[c][1] = max(by_cls[c][1], ms[e])
    for c, (mates, worth) in by_cls.items():
        if mates and sum(ms[x] for x in mates) / 1000 + setup_cost(c, p) < SENTINEL_RATIO * worth / 1000:
            must |= mates
    p["must"] = must
    # after a failure at position i, keep going when running the rest costs less CPU
    # than listing it would cost in simulated time
    p["cont"] = set()
    run = p["run"]
    run_sum, list_sum, list_max, n = 0, 0, 0, 0
    for i in range(len(run) - 1, -1, -1):
        if n and run_sum / 1000 * CPU_FACTOR < split_estimate(list_sum, list_max, n):
            p["cont"].add(run[i])
        x = run[i]
        run_sum += eff[x]; list_sum += ms[x]; list_max = max(list_max, ms[x]); n += 1
    p["timeouts"] = {x: 3.0 + 5.0 * ms[x] / 1000 for x in p["run"]}
    p["filters"] = {x: v for x, v in filters.items() if x in set(p["run"])}
    if p["filters"]:
        p["note"] += " cases(" + ",".join(f"{len(k)}/{n}" for k, n in p["filters"].values()) + ")"
    # locations: for the proxy rule, which expensive tests share code with cheap ones
    covered_by_cheap = True
    if expensive and p["run"]:
        covered_by_cheap = proxy_ok(res, to_num, ms)
    p["proxy_ok"] = covered_by_cheap
    return p


def _comment_only_fixture(fd):
    for _, body in fd["hunks"]:
        for tag, txt in body:
            if tag in "+-" and txt.strip() and not txt.strip().startswith("--"):
                return False
    return True


_id_of = {}


def mp_id(mp, to_num, num):
    if not _id_of:
        for i, n in enumerate(to_num):
            if n is not None:
                _id_of[n] = mp.tests[i]
    return _id_of[num]


def proxy_ok(res, to_num, ms):
    """True when every location that an expensive test covers is also covered by a cheap test."""
    for why, s in getattr(res, "locs", []):
        nums = {to_num[i] for i in s if to_num[i] is not None}
        exp = {x for x in nums if ms[x] > EXPENSIVE * 1000}
        if exp and not (nums - exp):
            return False
    return True


def run_all(runner, to_run, plans, deadline):
    tj_ms = None
    queue = list(to_run)
    active = {}  # fd -> (cid, pid, buf, started)
    for cid in to_run:
        p = plans[cid]
        p["results"] = {}
        p["imports_ok"] = {}
        p["done"] = False
    import json as _json
    while queue or active:
        while queue and len(active) < PARALLEL and time.time() < deadline:
            cid = queue.pop(0)
            p = plans[cid]
            must = p.get("must", ())
            flt = p.get("filters", {})
            plan = [(tid, p["timeouts"][num] if "timeouts" in p else None, num in must,
                     flt[num][0] if num in flt else None, num in p.get("cont", ()))
                    for tid, num in zip(p["run_ids"], p["run"])]
            pid, fd = runner.start(SRC, p["patched"], p["deleted"], plan, p["imports"], p.get("purge", True),
                                   STOP_AFTER_EXP if p.get("expensive") else 1, lazy_ok(p))
            active[fd] = [cid, pid, b"", time.time()]
        if not active:
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            for fd, (cid, pid, buf, st) in list(active.items()):
                kill(pid)
                os.close(fd)
                plans[cid]["note"] += " deadline"
                del active[fd]
            break
        rl, _, _ = select.select(list(active), [], [], min(remaining, 1.0))
        for fd in rl:
            entry = active[fd]
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                # child finished or died
                cid, pid = entry[0], entry[1]
                try:
                    _, _, ru = os.wait4(pid, 0)
                    plans[cid]["cpu"] = ru.ru_utime + ru.ru_stime
                except ChildProcessError:
                    pass
                os.close(fd)
                del active[fd]
                continue
            entry[2] += chunk
            *lines, entry[2] = entry[2].split(b"\n")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    msg = _json.loads(line)
                except ValueError:
                    continue
                handle(plans[entry[0]], msg)
    # anything still queued never ran
    for cid in queue:
        plans[cid]["note"] += " not-run"


def kill(pid):
    import signal as _s
    try:
        os.kill(pid, _s.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


def handle(p, msg):
    k = msg.get("kind")
    if k == "test":
        p["results"][msg["id"]] = msg["ok"]
        p["tcpu"] = p.get("tcpu", 0) + msg.get("cpu", 0)
        if DEBUG:
            p.setdefault("tlist", []).append((msg.get("cpu", 0), msg["id"]))
            if not msg["ok"] and msg.get("err") and p.get("_errs", 0) < 3:
                p["_errs"] = p.get("_errs", 0) + 1
                log("   ERR", msg["id"], msg["err"][:300])
    elif k == "import":
        p["imports_ok"][msg["module"]] = msg["ok"]
        if not msg["ok"]:
            p["note"] += f" import-fail:{msg['module']}"
    elif k == "group_failed":
        for tid in msg["tests"]:
            p["results"][tid] = False
        p["note"] += " fixture-fail"
    elif k == "stopped":
        p["stopped"] = True
    elif k == "done":
        p["done"] = True


def finalize(p):
    final = set(p.get("final_static", set()))
    if not p.get("run") and not p.get("imports"):
        return final
    results = p.get("results", {})
    failed = set()
    unknown = set()
    for tid, num in zip(p.get("run_ids", []), p.get("run", [])):
        r = results.get(tid)
        if r is None:
            unknown.add(num)
        elif not r:
            failed.add(num)
    p["failed"] = failed
    final |= failed | unknown
    imports_ok = p.get("imports_ok", {})
    for m in p.get("imports", []):
        if imports_ok.get(m) is False or (m not in imports_ok and not p.get("done") and not p.get("stopped")):
            final |= p["import_fail"].get(m, set())
            final |= set(p["cands"])
    exp = p.get("expensive", set())
    for e in exp:
        if not p.get("proxy_ok", True) or not p.get("run"):
            final.add(e)
            continue
        # the rule the board has confirmed: list slow tests once 3 cheap tests fail
        # (or the run could not finish) ...
        broad = len(failed) >= 3 or p.get("stopped") or unknown
        # ... except when every classmate in play ran and passed
        mates = p.get("sameclass", {}).get(e)
        if broad and (not mates or mates & (failed | unknown)):
            final.add(e)
    return final


if __name__ == "__main__":
    main()
