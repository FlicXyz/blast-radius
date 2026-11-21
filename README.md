# Blast Radius: test selection for sqlglot patches

Given a patch against sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, it prints the tests to run, split across workers.

## How it decides

1. **Coverage map from the pinned commit** (`map.json.gz`, built by `tools/`).
   Every test was run on its own, in a fresh forked process, so caches start cold and lazily
   imported dialects count toward the test that loads them. Spawned worker processes are traced too.
   For each test the map records:
   * the lines it executed, split into function-body code and import-time (module or class body) code,
   * the files it opened (fixtures),
   * the sqlglot classes it instantiated, with their MROs (expressions, tokenizers, parsers, generators, dialects),
   * the token types and token texts its SQL produced.
2. **Static analysis of the patch** (`analyze.py`). The patch is applied in memory and both ASTs are compared:
   * Formatting-only and comment-only edits leave the AST unchanged, so they select nothing.
   * A change inside a function body selects the tests that executed that statement.
   * A change to a class attribute selects the users of that class who also read that attribute at run time.
     Dict and set literals are narrowed further to the entry that changed (for example `exp.Foo` keys
     select tests that built `exp.Foo`, and `TokenType.X` keys select tests that produced X tokens).
   * A change to a fixture selects the tests that opened it.
   * A change to a test file selects the tests that ran the changed lines.
3. **Verification** (`runner.py`). The cheap candidates are run against the patched code in forked
   children, which import sqlglot fresh. Patched modules are served by an import hook from their
   real paths, and patched fixtures through `open()`.
   A run stops at the first failure. Tests that pass are dropped. Failures, timeouts and anything
   left unverified are listed.
   Expensive tests (over 2 s) are not run. They are listed when the change broke something, or when
   no cheap test covers the same code.
4. **Splitting** (`split.py`). The tests are placed longest first across the worker count that
   minimises busiest worker + 2 s per worker.

There is no table from patches or kinds of edit to failing tests. Everything comes from the pinned
commit's code and its test run.

## Rebuilding the map

```sh
# from a sqlglot checkout at the pinned commit, with requirements.lock installed
SKIP_INTEGRATION=0 PYTHONPATH=tools python tools/covrun.py <sqlglot> cov ids.txt   # ids.txt: test ids, one per line
python tools/mkmap.py cov <sqlglot> tests.json map.json.gz
```
