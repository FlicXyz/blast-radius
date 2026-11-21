"""Static analysis of a patch: which tests could observe the change.

Everything here works from the pinned sqlglot checkout plus the coverage map
built from it (map.json.gz). The result is a set of candidate tests, which the
runner then narrows down by actually running them against the patched tree.
"""

import ast
import gzip
import json
import os
import re

# ---------------------------------------------------------------- patches


def _path(p):
    p = p.strip().split("\t")[0]
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode().decode("unicode_escape")
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text):
    """Returns a list of file diffs: {old, new, hunks: [(old_start, [(tag, text)])]}."""
    files = []
    cur = None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git (\"?a/.*?\"?) (\"?b/.*\"?)$", line)
            cur = {"old": None, "new": None, "hunks": [], "binary": False, "git": True}
            if m:
                cur["old"], cur["new"] = _path(m.group(1)), _path(m.group(2))
            files.append(cur)
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "binary": False, "git": False}
                files.append(cur)
            cur["old"], cur["new"] = _path(line[4:]), _path(lines[i + 1][4:])
            i += 1
        elif line.startswith("new file mode") and cur is not None:
            cur["old"] = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur["new"] = None
        elif line.startswith("rename from ") and cur is not None:
            cur["old"] = line[len("rename from "):]
        elif line.startswith("rename to ") and cur is not None:
            cur["new"] = line[len("rename to "):]
        elif (line.startswith("Binary files") or line.startswith("GIT binary patch")) and cur is not None:
            cur["binary"] = True
        elif line.startswith("@@") and cur is not None:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                i += 1
                continue
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            o = n = 0
            i += 1
            while i < len(lines) and (o < old_len or n < new_len or (i < len(lines) and lines[i].startswith("\\"))):
                l = lines[i]
                if l.startswith("\\"):
                    body.append(("\\", ""))
                elif l.startswith("-"):
                    body.append(("-", l[1:])); o += 1
                elif l.startswith("+"):
                    body.append(("+", l[1:])); n += 1
                else:
                    body.append((" ", l[1:])); o += 1; n += 1
                i += 1
            cur["hunks"].append((old_start, body))
            continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    """Applies hunks to old_text (str). Returns new text or None if they don't apply."""
    old = old_text.split("\n")
    # old[-1] == "" when the file ends with a newline
    out = []
    pos = 0  # index into old
    for old_start, body in hunks:
        start = max(old_start - 1, 0)
        if not any(t in " -" for t, _ in body):
            start = old_start  # pure insertion: "-N,0" means after line N
        if start < pos:
            return None
        out.extend(old[pos:start])
        pos = start
        no_newline_after = None
        for j, (tag, txt) in enumerate(body):
            if tag == "\\":
                continue
            if tag in " -":
                if pos >= len(old) or old[pos] != txt:
                    return None
                pos += 1
            if tag in " +":
                out.append(txt)
        # "\ No newline at end of file" handling
        tags = [t for t, _ in body]
        if "\\" in tags:
            # find which side the marker follows
            for j, (tag, _) in enumerate(body):
                if tag == "\\" and j > 0:
                    prev = body[j - 1][0]
                    if prev in " +":
                        no_newline_after = True
                    elif prev == "-":
                        pass
            if no_newline_after and pos >= len(old) - 1:
                # new file has no trailing newline
                rest = old[pos:]
                if rest == [""]:
                    return "\n".join(out)
    out.extend(old[pos:])
    return "\n".join(out)


def change_points(hunks):
    """Old-file line numbers touched: removed lines, and pure insertion points
    (line before which text goes). Insertions that replace removed lines are
    covered by the removed lines."""
    removed, inserted = set(), []
    for old_start, body in hunks:
        ln = old_start if any(t in " -" for t, _ in body) else old_start + 1
        added = []
        touched = False  # removed lines adjacent to the current insertion block
        for tag, txt in body:
            if tag == " ":
                if added and not touched:
                    inserted.append((ln, added))
                added = []
                touched = False
                ln += 1
            elif tag == "-":
                if added:
                    touched = True
                removed.add(ln)
                touched = True
                ln += 1
            elif tag == "+":
                added.append(txt)
        if added and not touched:
            inserted.append((ln, added))
    return removed, inserted


# ---------------------------------------------------------------- map


class Map:
    def __init__(self, path):
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        self.tests = d["tests"]
        self.sets = [frozenset(s) for s in d["sets"]]
        self.lines = {f: {int(k): v for k, v in m.items()} for f, m in d["lines"].items()}
        self.opened = d["opened"]
        self.classes = d["classes"]
        self.tokens = d.get("tokens", {})
        self.words = d.get("words", {})
        self.names = d["names"]
        self.tnames = d.get("tnames", {})
        self.all = frozenset(range(len(self.tests)))
        self.by_file = {}
        for i, t in enumerate(self.tests):
            self.by_file.setdefault(t.split("::")[0], set()).add(i)

    def line_tests(self, f, lo, hi, runtime_only=True):
        m = self.lines.get(f)
        out = set()
        if not m:
            return out
        for l in range(lo, hi + 1):
            s = m.get(l)
            if s is not None:
                out |= self.sets[s]
            if not runtime_only:
                s = m.get(-l)
                if s is not None:
                    out |= self.sets[s]
        return out

    def file_tests(self, f):
        """Tests that loaded module f (executed any line of it)."""
        m = self.lines.get(f)
        out = set()
        if m:
            for s in set(m.values()):
                out |= self.sets[s]
        return out

    def get(self, table, key):
        s = getattr(self, table).get(key)
        return set(self.sets[s]) if s is not None else set()


# ---------------------------------------------------------------- AST helpers


class _BlankLambdas(ast.NodeTransformer):
    def visit_Lambda(self, node):
        self.generic_visit(node)
        node.body = ast.Constant(value="<lambda body>")
        return node


def lambda_blind_dump(text):
    return strip_dump(_BlankLambdas().visit(ast.parse(text)))


def strip_dump(tree):
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def _module_name(path):
    p = path[:-3]
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


class Scopes:
    """Index of an old file's AST: for a line, its innermost statement and scope chain."""

    def __init__(self, src, path):
        self.tree = ast.parse(src)
        self.path = path
        self.module = _module_name(path)
        self.nodes = []  # (lineno, end, node, chain)
        self._walk(self.tree, [])
        self.src_lines = src.split("\n")

    def _walk(self, node, chain):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.Lambda, ast.ExceptHandler)) or isinstance(child, ast.match_case if hasattr(ast, "match_case") else ()):
                lineno = getattr(child, "lineno", None)
                if lineno is not None:
                    start = lineno
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.decorator_list:
                        start = min(d.lineno for d in child.decorator_list)
                    self.nodes.append((start, child.end_lineno, child, chain))
            new_chain = chain + [child] if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) else chain
            self._walk(child, new_chain)

    def innermost(self, line):
        """Innermost stmt/lambda containing line, with its enclosing scope chain."""
        best = None
        for start, end, node, chain in self.nodes:
            if start <= line <= end:
                if best is None or (end - start) < (best[1] - best[0]) or ((end - start) == (best[1] - best[0]) and start >= best[0]):
                    best = (start, end, node, chain)
        return best

    def qualname(self, chain):
        names = [n.name for n in chain if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        return ".".join(names)


def _header_end(node):
    """Last line of a compound statement's header."""
    body = getattr(node, "body", None)
    if isinstance(body, list) and body:
        first = body[0]
        start = first.lineno
        if isinstance(first, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and first.decorator_list:
            start = min(d.lineno for d in first.decorator_list)
        return max(node.lineno, start - 1)
    return node.end_lineno


COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.FunctionDef,
            ast.AsyncFunctionDef, ast.ClassDef) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ()) + (
            (ast.Match,) if hasattr(ast, "Match") else ())


# ---------------------------------------------------------------- analysis


class Result:
    def __init__(self):
        self.cands = set()          # map indices of candidate tests
        self.reasons = []
        self.locs = []
        self.import_modules = set() # sqlglot modules to import-check at run time
        self.import_fail_tests = {} # module -> tests that fail if it doesn't import
        self.everything = False
        self.files_changed = []

    def add(self, tests, why):
        tests = set(tests)
        self.cands |= tests
        self.reasons.append((why, len(tests)))
        self.locs.append((why, tests))


def _atoms(node):
    """Names, attribute names and string constants mentioned in an expression."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(("name", n.id))
        elif isinstance(n, ast.Attribute):
            out.add(("attr", n.attr))
            if isinstance(n.value, ast.Name) and n.value.id == "TokenType":
                out.add(("token", n.attr))
            if isinstance(n.value, ast.Name) and n.value.id in ("exp", "expressions"):
                out.add(("expclass", n.attr))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(("str", n.value))
    return out


def _dict_entries(node):
    """For a dict/set/list literal (possibly built with ** / | / + / set()), its entries as dumps."""
    entries = {}
    if node is None:
        return None

    def visit(n):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                kd = strip_dump(k) if k is not None else "**" + strip_dump(v)
                entries[(kd, strip_dump(v))] = (k, v)
        elif isinstance(n, (ast.Set, ast.List, ast.Tuple)):
            for e in n.elts:
                if isinstance(e, ast.Starred):
                    entries[("*" + strip_dump(e), "")] = (None, None)
                else:
                    entries[(strip_dump(e), "")] = (e, None)
        elif isinstance(n, ast.BinOp):
            visit(n.left)
            visit(n.right)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("set", "frozenset", "dict", "list", "tuple", "sorted") and not n.keywords:
            for a in n.args:
                visit(a)
        else:
            entries[("?" + strip_dump(n), "")] = (None, None)

    visit(node)
    return entries


NAME_KEYED = re.compile(r"(FUNCTIONS|KEYWORDS|PARSERS|FUNC|COMMANDS|SETTINGS|_TOKENS)$")


class Analyzer:
    def __init__(self, src_root, mp):
        self.src = src_root
        self.mp = mp
        self._scopes = {}
        self._class_index = None

    def scopes(self, path, text):
        if path not in self._scopes:
            self._scopes[path] = Scopes(text, path)
        return self._scopes[path]

    def read(self, path):
        p = os.path.join(self.src, path)
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()

    # -- users of a class (anything whose MRO includes it, by module:qualname)
    def class_users(self, module, qualname):
        return self.mp.get("classes", f"{module}:{qualname}")

    def class_users_by_name(self, name):
        """Tests that instantiated a class with this name, or None if no such class was ever seen."""
        out = None
        for k, s in self.mp.classes.items():
            if k.rsplit(".", 1)[-1].split(":")[-1] == name:
                out = (out or set()) | self.mp.sets[s]
        return out

    def name_users(self, name):
        return self.mp.get("names", name)

    def atom_users(self, atom):
        kind, val = atom
        if kind == "token":
            s = self.mp.get("tokens", val) if self.mp.tokens else None
            return s if s is not None and self.mp.tokens else None
        if kind == "expclass":
            return self.class_users_by_name(val)
        if kind == "str":
            if not self.mp.words:
                return None
            v = " ".join(val.upper().split())
            if not v:
                return None
            words = [v] + re.findall(r"[A-Z0-9_$@#]+|[^A-Z0-9_$@#\s]+", v)
            out = set()
            known = False
            for w in words:
                s = self.mp.words.get(w)
                if s is not None:
                    out |= self.mp.sets[s]; known = True
            # words absent from the map are either never tokenized (nobody) or very common (everybody)
            return out if known or words else None
        return None

    # ------------------------------------------------------------------
    def analyze(self, patch_text):
        res = Result()
        files = parse_patch(patch_text)
        for fd in files:
            old, new = fd["old"], fd["new"]
            path = new or old
            res.files_changed.append(path)
            try:
                self._analyze_file(fd, res)
            except Exception as e:  # never crash: fall back to everything that loaded the file
                res.add(self._broad(path), f"error {type(e).__name__}: {e} in {path}")
        return res, files

    def _broad(self, path):
        if path and path.endswith(".py"):
            s = self.mp.file_tests(path)
            if s:
                return s
        return set(self.mp.all)

    def _analyze_file(self, fd, res):
        old, new = fd["old"], fd["new"]
        path = new or old
        if fd["binary"]:
            if path.startswith("tests/fixtures/"):
                res.add(self.mp.get("opened", path) | self._fixture_refs(path), f"binary fixture {path}")
            elif path.startswith(("sqlglot/", "tests/")):
                res.add(self._broad(path), f"binary {path}")
            return
        if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            self._analyze_py(fd, res)
            return
        if path.startswith("tests/"):
            # fixtures and other data files read by tests
            s = self.mp.get("opened", path)
            if old and new and old != new:
                s |= self.mp.get("opened", old)
            s |= self._fixture_refs(path)
            res.add(s, f"data {path}")
            return
        if path.startswith("sqlglot/"):
            # non-python file inside the package (py.typed, data)
            s = self.mp.get("opened", path)
            res.add(s, f"pkgdata {path}")
            return
        base = os.path.basename(path)
        if base in ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py"):
            # could change how pytest runs: be careful but these rarely break tests
            res.add(set(), f"config {path}")
            res.config_change = True
            return
        res.add(set(), f"ignored {path}")

    def _fixture_refs(self, path):
        """Tests whose test file mentions the fixture's name (for fixtures read at import time)."""
        base = os.path.basename(path)
        stem = base.rsplit(".", 1)[0]
        out = set()
        if self.mp.get("opened", path):
            return out
        for tf, tests in self.mp.by_file.items():
            txt = self.read(tf) or ""
            if stem and stem in txt:
                out |= tests
        return out

    # ------------------------------------------------------------------
    def _analyze_py(self, fd, res):
        old, new = fd["old"], fd["new"]
        path = new or old
        is_test = path.startswith("tests/")
        old_text = self.read(old) if old else None
        if old and old_text is None:
            # patch against a file we don't have: treat as new
            old = None
        if new is None:
            # deleted module
            res.add(self._broad(old), f"deleted {old}")
            if not is_test:
                res.import_modules.add(_module_name(old))
            return
        if old is None:
            # new module: only matters if something imports it (that change is analyzed separately)
            new_text = apply_hunks("", fd["hunks"]) or ""
            res.add(set(), f"new file {new}")
            if is_test:
                return
            res.import_modules.add(_module_name(new))
            res.import_fail_tests[_module_name(new)] = set()
            return
        if old != new:
            res.add(self._broad(old), f"renamed {old}->{new}")
            return
        new_text = apply_hunks(old_text, fd["hunks"])
        if new_text is None:
            res.add(self._broad(path), f"patch does not apply {path}")
            return
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            if is_test:
                res.add(self._test_importers(path), f"syntax error {path}")
            else:
                res.add(self._broad(path), f"syntax error {path}")
            return
        old_scopes = self.scopes(path, old_text)
        if strip_dump(old_scopes.tree) == strip_dump(new_tree):
            res.add(set(), f"no-op (comments/format) {path}")
            return
        if not is_test:
            res.import_modules.add(_module_name(path))
            res.import_fail_tests[_module_name(path)] = self.mp.file_tests(path)
        # analyze each hunk on its own when there are several, skipping no-op ones
        hunks = fd["hunks"]
        if len(hunks) > 1:
            eff = []
            for h in hunks:
                t = apply_hunks(old_text, [h])
                try:
                    if t is not None and strip_dump(ast.parse(t)) == strip_dump(old_scopes.tree):
                        continue
                except SyntaxError:
                    pass
                eff.append(h)
            hunks = eff or hunks
        removed, inserted = change_points(hunks)
        new_scopes = Scopes(new_text, path)
        # every difference sits inside lambda bodies: their callers are all that matter (computed lazily)
        self._lambda_only = None
        self._lambda_texts = (old_text, new_text)
        for ln in sorted(removed):
            self._locate(path, old_scopes, new_scopes, ln, None, res, is_test)
        for ln, added in inserted:
            if all(not a.strip() or a.strip().startswith("#") for a in added):
                # comment-only insertion: can only matter inside a string literal, which
                # shows up as a changed statement through the neighbours
                self._locate(path, old_scopes, new_scopes, ln - 1, None, res, is_test, weak=True)
                self._locate(path, old_scopes, new_scopes, ln, None, res, is_test, weak=True)
                continue
            self._locate(path, old_scopes, new_scopes, ln - 1, added, res, is_test, insert=True)
            self._locate(path, old_scopes, new_scopes, ln, added, res, is_test, insert=True)

    def _test_importers(self, path):
        out = set(self.mp.by_file.get(path, set()))
        out |= self.mp.file_tests(path)
        return out

    def _locate(self, path, osc, nsc, line, added, res, is_test, insert=False, weak=False):
        mp = self.mp
        if line < 1 or line > len(osc.src_lines):
            if is_test:
                res.add(self._test_importers(path), f"edge {path}:{line}")
            else:
                res.add(set(), f"edge {path}:{line}")
            return
        hit = osc.innermost(line)
        if hit is None:
            # module level blank/comment line
            if weak:
                return
            if is_test:
                res.add(self._test_importers(path), f"module-level {path}:{line}")
            else:
                res.add(self._module_level(path, osc, line, None, nsc), f"module-level {path}:{line}")
            return
        start, end, node, chain = hit
        func = None
        for c in reversed(chain + [node]):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # inside a function's body?
                if isinstance(c, ast.Lambda):
                    if c.lineno <= line <= c.end_lineno:
                        func = c
                        break
                elif c.body and _body_start(c) <= line <= c.end_lineno:
                    func = c
                    break
                else:
                    # def header (signature, defaults, decorators)
                    func = None
                    break
            if isinstance(c, ast.ClassDef):
                break
        top_lambda = isinstance(func, ast.Lambda) and not any(
            isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) for c in chain)
        if top_lambda:
            # a lambda in a class/module-level literal: its callers, plus whatever the
            # enclosing statement means at class/module level
            res.add(mp.line_tests(path, func.lineno, func.end_lineno), f"lambda {path}:{line}")
            if self._lambda_only is None:
                o, n = self._lambda_texts
                self._lambda_only = lambda_blind_dump(o) == lambda_blind_dump(n)
            if self._lambda_only:
                return
            stmt = None
            for st, en, nd, ch in osc.nodes:
                if isinstance(nd, ast.stmt) and st <= line <= en and (stmt is None or (en - st) < (stmt[1] - stmt[0])):
                    stmt = (st, en, nd, ch)
            if stmt is not None:
                start, end, node, chain = stmt
            func = None
        if func is not None:
            # runtime code: tests that executed the statement
            stmt = node
            if isinstance(stmt, COMPOUND) and not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                he = _header_end(stmt)
                if stmt.lineno <= line <= he:
                    lo, hi = stmt.lineno, he
                else:
                    # else:/finally: lines, blank lines between children
                    lo, hi = line, line
                    nxt = _next_stmt_line(stmt, line)
                    s = mp.line_tests(path, stmt.lineno, he)
                    if nxt:
                        s |= mp.line_tests(path, nxt, nxt)
                    s |= mp.line_tests(path, line, line)
                    res.add(s, f"runtime-branch {path}:{line}")
                    return
            elif isinstance(stmt, ast.Lambda):
                lo, hi = stmt.lineno, stmt.end_lineno
            else:
                lo, hi = start, end
            s = mp.line_tests(path, lo, hi)
            if not s and insert:
                # inserted next to code nobody ran: use the enclosing function
                s = self._func_tests(path, func)
            res.add(s, f"runtime {path}:{line} [{lo}-{hi}]")
            return
        if weak:
            return
        # class body or module level or def header
        cls = None
        for c in reversed(chain + [node]):
            if isinstance(c, ast.ClassDef):
                cls = c
                break
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # signature/decorator change: the function's callers (all ran its body)
            s = self._func_tests(path, node)
            s |= mp.line_tests(path, node.lineno, _body_start(node) - 1)
            if cls is None:
                s |= self.name_users(node.name) if not is_test else set()
            res.add(s, f"def-header {path}:{line} {node.name}")
            if is_test:
                return
            return
        if is_test:
            # test class body / module level in a test file: the file's tests (and importers)
            if cls is not None and not isinstance(node, ast.ClassDef):
                qual = osc.qualname([c for c in chain if isinstance(c, ast.ClassDef)] or [cls])
                tests = {i for i in mp.by_file.get(path, set()) if mp.tests[i].split("::")[1] == qual.split(".")[0]}
                tests |= self._subclass_tests(path, cls.name)
                res.add(tests, f"test-class-body {path}:{line}")
            else:
                res.add(self._test_importers(path), f"test-module {path}:{line}")
            return
        if cls is not None:
            res.add(self._class_level(path, osc, nsc, line, node, chain, cls), f"class-level {path}:{line} {cls.name}")
            return
        res.add(self._module_level(path, osc, line, node, nsc), f"module-level {path}:{line}")

    def _subclass_tests(self, path, clsname):
        """Test classes in other test files that inherit from clsname (e.g. Validator)."""
        out = set()
        for tf, tests in self.mp.by_file.items():
            txt = self.read(tf) or ""
            if re.search(rf"class \w+\([^)]*\b{re.escape(clsname)}\b", txt) or (tf == path):
                out |= tests
        return out

    def _func_tests(self, path, func):
        return self.mp.line_tests(path, _body_start(func), func.end_lineno)

    def _class_level(self, path, osc, nsc, line, node, chain, cls):
        mp = self.mp
        classes = [c for c in chain if isinstance(c, ast.ClassDef)]
        if isinstance(node, ast.ClassDef):
            classes = classes + [node] if node not in classes else classes
        qual = osc.qualname(classes)
        users = self.class_users(osc.module, qual)
        # Enum members, constants read through the class: also anything naming the class
        if not users:
            users = self.class_users_by_name(classes[-1].name) or set()
        users = set(users) | self.name_users(classes[-1].name) & self._loaded(path)
        if isinstance(node, ast.ClassDef):
            # class header (bases, keywords): everything using it
            # nobody instantiates it or names it at run time (e.g. typing-only classes):
            # only an import failure could matter, and the import check covers that
            return users
        targets = _targets(node)
        refined = None
        if targets:
            # attribute read by name somewhere at run time
            readers = set()
            for t in targets:
                readers |= self.name_users(t)
            # entry-level refinement for dict/set literals
            onode = _find_assign(nsc, qual, targets)
            entries_old = _dict_entries(getattr(node, "value", None))
            entries_new = _dict_entries(getattr(onode, "value", None)) if onode is not None else None
            if entries_old is not None and entries_new is not None and isinstance(getattr(node, "value", None), (ast.Dict, ast.Set, ast.List, ast.Tuple, ast.BinOp, ast.Call)):
                changed = [entries_old[k] for k in set(entries_old) - set(entries_new)] + [entries_new[k] for k in set(entries_new) - set(entries_old)]
                if changed and _same_shape(node.value, onode.value):
                    sub = set()
                    ok = True
                    for k, v in changed:
                        got = None
                        # a mapping entry only matters where its key turns up
                        if k is not None and v is not None:
                            ks = self._expr_users(path, k, strings=any(NAME_KEYED.search(t) for t in targets))
                            if ks is not None:
                                sub |= ks
                                continue
                        for part in (k, v):
                            if part is None:
                                continue
                            s = self._expr_users(path, part, strings=any(NAME_KEYED.search(t) for t in targets))
                            if s is not None:
                                got = (got or set()) | s
                        if got is None:
                            ok = False
                            break
                        sub |= got
                    if ok:
                        refined = sub
            base = users & readers if readers else users
            if refined is not None:
                # tests that look at the attribute directly (e.g. iterate over it) see every entry
                direct = set()
                for t in targets:
                    direct |= self.mp.get("tnames", t)
                return (base & refined) | (base & direct)
            return base
        return users

    def _expr_users(self, path, expr, strings=False):
        """Tests that could exercise a dict key/value or set element."""
        s = None
        atoms = _atoms(expr)
        # strongest signals first
        for a in atoms:
            if a[0] in ("token", "expclass"):
                u = self.atom_users(a)
                if u is not None:
                    s = (s or set()) | u
        if s is not None:
            return s
        if strings and isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return self.atom_users(("str", expr.value))
        if isinstance(expr, ast.Lambda):
            return self.mp.line_tests(path, expr.lineno, expr.end_lineno)
        return None

    def _loaded(self, path):
        return self.mp.file_tests(path)

    def _module_level(self, path, osc, line, node, nsc):
        mp = self.mp
        loaded = self._loaded(path)
        if node is None:
            return set()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {(a.asname or a.name).split(".")[0] for a in node.names}
            s = set()
            for n in names:
                s |= self.name_users(n)
            return (s & loaded) | set()
        if isinstance(node, ast.ClassDef):
            return self.class_users(osc.module, node.name) or loaded
        targets = _targets(node)
        if targets:
            s = set()
            for t in targets:
                s |= self.name_users(t)
            # used at import time by class bodies of this module: those classes' users
            for t in targets:
                for c in _classes_referencing(osc.tree, t):
                    s |= self.class_users(osc.module, c)
            return s
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            return loaded
        return loaded


def _body_start(func):
    first = func.body[0]
    start = first.lineno
    if isinstance(first, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and first.decorator_list:
        start = min(d.lineno for d in first.decorator_list)
    return start


def _next_stmt_line(stmt, line):
    best = None
    for n in ast.walk(stmt):
        if isinstance(n, ast.stmt) and n is not stmt and n.lineno > line:
            if best is None or n.lineno < best:
                best = n.lineno
    return best


def _targets(node):
    out = []
    if isinstance(node, ast.Assign):
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out.append(n.id)
                elif isinstance(n, ast.Attribute):
                    out.append(n.attr)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        t = node.target
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, ast.Attribute):
            out.append(t.attr)
    return out


def _find_assign(scopes, qual, targets):
    """The statement assigning `targets` inside class `qual` of the new file."""
    node = scopes.tree
    for part in qual.split("."):
        nxt = None
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.ClassDef) and c.name == part:
                nxt = c
                break
        if nxt is None:
            return None
        node = nxt
    for c in node.body:
        if _targets(c) == targets:
            return c
    return None


def _same_shape(a, b):
    """Whether two literals differ only in entries (same kind of container)."""
    return type(a) is type(b)


def _classes_referencing(tree, name):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            for st in n.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(st)):
                    out.append(n.name)
                    break
    return out
