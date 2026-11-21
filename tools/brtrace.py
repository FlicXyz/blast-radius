import sys, os, json, builtins, io, pathlib
mon = sys.monitoring
TOOL = 4
ROOT = None
lines = {"import": set(), "test": set()}
cur = lines["import"]
opened = set()
classes = set()
extra = {}
_fc = {}

def keep(fn):
    r = _fc.get(fn)
    if r is None:
        p = os.path.realpath(fn) if fn else ""
        r = p[len(ROOT):] if p.startswith(ROOT) else False
        _fc[fn] = r
    return r

def on_line(code, line):
    f = keep(code.co_filename)
    if f is not False:
        cur.add((f, line if code.co_flags & 1 else -line))
    return mon.DISABLE

def on_start(code, offset):
    # one-line functions/lambdas get no LINE event for their only line
    f = keep(code.co_filename)
    if f is not False and code.co_flags & 1:
        cur.add((f, code.co_firstlineno))
    return mon.DISABLE

def phase(name):
    global cur
    cur = lines[name]
    mon.restart_events()

def _wrap_init(cls):
    orig = cls.__init__
    def __init__(self, *a, **k):
        classes.add(type(self))
        return orig(self, *a, **k)
    cls.__init__ = __init__

_patched = set()
class _Finder:
    """After sqlglot modules load, wrap base-class __init__s once."""
    def find_spec(self, *a, **k):
        return None

tokens = set()
words = set()
def _wrap_token(cls):
    orig = cls.__init__
    def __init__(self, token_type, text, *a, **k):
        tokens.add(token_type.name)
        if isinstance(text, str) and len(text) <= 40:
            words.add(text.upper())
        return orig(self, token_type, text, *a, **k)
    cls.__init__ = __init__

def patch_bases():
    targets = [("sqlglot.expressions.core", "Expr"), ("sqlglot.tokens", "Tokenizer"),
               ("sqlglot.parser", "Parser"), ("sqlglot.generator", "Generator"),
               ("sqlglot.dialects.dialect", "Dialect"), ("sqlglot.tokenizer_core", "TokenizerCore")]
    m = sys.modules.get("sqlglot.tokenizer_core")
    if m is not None and hasattr(m, "Token") and "Token" not in _patched:
        _wrap_token(m.Token); _patched.add("Token")
    for m, c in targets:
        if (m, c) in _patched: continue
        mod = sys.modules.get(m)
        if mod is not None and hasattr(mod, c):
            _wrap_init(getattr(mod, c)); _patched.add((m, c))

_orig_import = builtins.__import__
def _imp(*a, **k):
    r = _orig_import(*a, **k)
    if len(_patched) < 7: patch_bases()
    return r

def start(root):
    global ROOT
    ROOT = os.path.realpath(root) + "/"
    _open = builtins.open
    def rec_open(file, *a, **k):
        if isinstance(file, (str, os.PathLike)):
            f = keep(os.path.abspath(os.fspath(file)))
            if f: opened.add(f)
        return _open(file, *a, **k)
    builtins.open = rec_open; io.open = rec_open
    import gzip
    _gopen = gzip.open
    def g(filename, *a, **k):
        if isinstance(filename, (str, os.PathLike)):
            f = keep(os.path.abspath(os.fspath(filename)))
            if f: opened.add(f)
        return _gopen(filename, *a, **k)
    gzip.open = g
    _rt = pathlib.Path.read_text
    def rt(self, *a, **k):
        f = keep(str(self.absolute()))
        if f: opened.add(f)
        return _rt(self, *a, **k)
    pathlib.Path.read_text = rt
    builtins.__import__ = _imp
    mon.use_tool_id(TOOL, "brtrace")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)

def dump(path):
    out = {"import": sorted(lines["import"]), "test": sorted(lines["test"]),
           "opened": sorted(opened), "tokens": sorted(tokens), "words": sorted(words),
           "classes": sorted({f"{m.__module__}:{m.__qualname__}" for c in classes for m in c.__mro__}),
           **extra}
    with open(path + ".tmp", "w") as fh:
        json.dump(out, fh)
    os.rename(path + ".tmp", path)
