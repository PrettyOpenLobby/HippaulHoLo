#!/usr/bin/env python3
"""Split one flat service module into a package, one module per concern.

A generalisation of OpenLobby's split_core.py (which cut responders.py into
services/core/), used for CrystalHoLo's jangame.py and janhourou.py. It is
deterministic: the same source and the same map give the same package.

    python split_jan_pkg.py --src services/jangame.py --map split_jangame_map.txt \\
        --package jantable --out services/jantable --facade services/jangame.py
    python split_jan_pkg.py --src ... --map ... --package ... --check   # report only
    python split_jan_pkg.py --src ... --map ... --package ... --out ... --facade ... --verify
                                      # the tree is exactly what source + map give

The source is the FLAT module (the file as it was before the split, plus any
change ported into it since); take it from git history when the facade has
already replaced it: `git show <commit>:services/jangame.py > flat.py`.

How it works
- Every top-level statement of the source is assigned to one module of the
  package by the map (defs and module globals by NAME; unnamed statements by
  a `~prefix` of their first line). The comment block above a statement
  travels with it, so section banners and per-function commentary survive.
- A class can be split too: `Class.member` entries move a method (or a class
  attribute) into another module, where it lands in a mixin class named by a
  `=Class MixinName` line of that module's section. The class keeps its name,
  its home module and every member not moved, and inherits the mixins, so
  `Class.member` and `instance.member` resolve exactly as before.
- Inside a moved statement, every reference to a top-level name that lives in
  ANOTHER module is rewritten to `<module>.<name>`. Scope analysis follows
  Python's rules (function scopes nest, class bodies do not). A `global X`
  for an X that moved elsewhere is dropped and the function's uses of X are
  qualified the same way, so its writes land in the owning module.
  `globals()["X"]` becomes `<owner>.X`; for an imported name, which every
  module holds a copy of, it becomes `<facade>.X`, and the facade forwards
  the write to each copy.
- Import-like statements (plain imports, and `try: import x / except: x = None`
  blocks) go to <package>/deps.py; a module that uses such a name gets the
  plain import, or `from .deps import x` for the optional ones.
- The old module becomes a facade: `import jangame` keeps working for every
  tool and test, reads AND writes (`jangame.WATCH_FILE = "0"`) are forwarded
  to the owning module, and `python jangame.py ...` still runs the old CLI.

Map syntax
    @title <first line of the package docstring>
    [module] one-line docstring
    name name name                  top-level names this module owns
    ~prefix                         an unnamed statement whose first line starts so
    !# --- banner                   a section banner (and the blank line after it) that
                                    moves to this module's next statement in source order
    =Class MixinName                this module holds a mixin of Class
    Class.member Class.member       members of Class moved into that mixin
    [__facade__]                    statements that stay in the facade (by ~prefix)
"""
import argparse
import ast
import collections
import io
import os
import re
import sys
import textwrap


# --------------------------------------------------------------------------- #
# Source model
# --------------------------------------------------------------------------- #
class Stmt:
    __slots__ = ("node", "names", "kind", "first", "last", "span_first", "text", "module",
                 "members", "header_end", "cls")

    def __init__(self, node, names, kind, first, last):
        self.node = node
        self.names = names        # names this statement binds (top level, or in its class)
        self.kind = kind          # def | assign | import | optimport | other
        self.first = first        # first line of the statement proper
        self.last = last
        self.span_first = None    # first line including the comment block above
        self.text = None
        self.module = None
        self.members = None       # for a ClassDef: its body statements as Stmts
        self.header_end = None    # for a ClassDef: last line of the `class ...:` header
        self.cls = None           # for a class member: the class name


def bound_targets(target):
    out = []
    for n in ast.walk(target):
        if isinstance(n, ast.Name):
            out.append(n.id)
    return out


def classify(node):
    """(names, kind) for a top-level statement."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name], "def"
    if isinstance(node, ast.Assign):
        names = []
        for t in node.targets:
            names += bound_targets(t)
        return names, "assign"
    if isinstance(node, ast.AnnAssign):
        return bound_targets(node.target), "assign"
    if isinstance(node, ast.AugAssign):
        return [], "other"
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in node.names], "import"
    if isinstance(node, ast.Try):
        body_imports = all(isinstance(b, (ast.Import, ast.ImportFrom)) for b in node.body)
        if body_imports and node.handlers:
            names = []
            for b in node.body:
                names += [(a.asname or a.name).split(".")[0] for a in b.names]
            return names, "optimport"
    return [], "other"


def first_line_of(node):
    first = node.lineno
    for d in getattr(node, "decorator_list", []):
        first = min(first, d.lineno)
    return first


def load_source(path):
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    stmts = []
    for node in tree.body:
        names, kind = classify(node)
        stmts.append(Stmt(node, names, kind, first_line_of(node), node.end_lineno))
    prev_end = 0
    for s in stmts:
        s.span_first = prev_end + 1
        s.text = "".join(lines[s.span_first - 1:s.last])
        prev_end = s.last
        if isinstance(s.node, ast.ClassDef):
            node = s.node
            s.header_end = max([node.lineno] + [b.end_lineno for b in node.bases]
                               + [k.value.end_lineno for k in node.keywords])
            s.members = []
            mprev = s.header_end
            for b in node.body:
                names, kind = classify(b)
                m = Stmt(b, names, kind, first_line_of(b), b.end_lineno)
                m.span_first = mprev + 1
                m.text = "".join(lines[m.span_first - 1:m.last])
                m.cls = node.name
                mprev = m.last
                s.members.append(m)
    trailing = "".join(lines[prev_end:])
    return text, lines, tree, stmts, trailing


# --------------------------------------------------------------------------- #
# The map
# --------------------------------------------------------------------------- #
def read_map(path):
    """({module: {"doc", "names", "members", "prefixes", "mixins"}}, title) in file order."""
    mods = collections.OrderedDict()
    title = None
    cur = None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("@title "):
            title = line[len("@title "):].strip()
            continue
        m = re.match(r"^\[([A-Za-z_][A-Za-z0-9_]*)\]\s*(.*)$", line)
        if m:
            cur = m.group(1)
            mods[cur] = {"doc": m.group(2).strip(), "names": set(), "members": set(),
                         "prefixes": [], "mixins": {}, "banners": []}
            continue
        if cur is None:
            raise SystemExit(f"{path}: entry before any [module] header: {line!r}")
        if line.startswith("!"):
            mods[cur]["banners"].append(line[1:].rstrip())
        elif line.startswith("~"):
            mods[cur]["prefixes"].append(line[1:].rstrip())
        elif line.startswith("="):
            cls, mixin = line[1:].split()
            mods[cur]["mixins"][cls] = mixin
        else:
            for tok in line.split():
                if "." in tok:
                    mods[cur]["members"].add(tuple(tok.split(".", 1)))
                else:
                    mods[cur]["names"].add(tok)
    return mods, title


def assign_modules(stmts, mods, report):
    owner = {}
    member_owner = {}
    for mod, spec in mods.items():
        for n in spec["names"]:
            if n in owner:
                raise SystemExit(f"map: {n} listed in both {owner[n]} and {mod}")
            owner[n] = mod
        for key in spec["members"]:
            if key in member_owner:
                raise SystemExit(f"map: {'.'.join(key)} listed in both {member_owner[key]} and {mod}")
            member_owner[key] = mod
    facade_prefixes = ['if __name__ == "__main__":'] + mods.get("__facade__", {}).get("prefixes", [])
    unmapped = []
    seen_names = set()
    for s in stmts:
        seen_names |= set(s.names)
        first_line = s.text.splitlines()[s.first - s.span_first].rstrip() if s.text else ""
        if s.kind in ("import", "optimport"):
            s.module = "deps"
            continue
        if any(first_line.startswith(p) for p in facade_prefixes):
            s.module = "__facade__"
            continue
        hit = None
        for mod, spec in mods.items():
            if mod != "__facade__" and any(first_line.startswith(p) for p in spec["prefixes"]):
                hit = mod
                break
        if hit is None and s.names:
            owners = {owner.get(n) for n in s.names}
            owners.discard(None)
            if len(owners) > 1:
                raise SystemExit(f"L{s.first}: names {s.names} map to several modules {owners}")
            if owners:
                hit = owners.pop()
        if hit is None:
            if s.kind == "other" and isinstance(s.node, ast.Expr) and isinstance(
                    getattr(s.node, "value", None), ast.Constant) and s.first <= 2:
                s.module = "__facade__"      # the module docstring
                continue
            unmapped.append(s)
            continue
        s.module = hit
        if s.members is not None:
            for m in s.members:
                homes = {member_owner.get((s.names[0], n)) for n in m.names}
                homes.discard(None)
                if len(homes) > 1:
                    raise SystemExit(f"L{m.first}: {s.names[0]} members {m.names} map to "
                                     f"several modules {homes}")
                m.module = homes.pop() if homes else hit
    for n in owner:
        if n not in seen_names:
            report.append(f"MAP NAME NOT IN SOURCE: {n} ({owner[n]})")
    classes = {s.names[0]: s for s in stmts if s.members is not None}
    for (cls, n), mod in member_owner.items():
        if cls not in classes or not any(n in m.names for m in classes[cls].members):
            report.append(f"MAP NAME NOT IN SOURCE: {cls}.{n} ({mod})")
    return owner, unmapped


# --------------------------------------------------------------------------- #
# Scope analysis
# --------------------------------------------------------------------------- #
def declared_global(scope_node):
    """Names a function declares `global` (directly, not in nested scopes)."""
    out = set()

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(c, ast.Global):
                out.update(c.names)
            walk(c)
    if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        walk(scope_node)
    return out


def local_bindings(scope_node):
    """Names bound directly in this function/lambda/comprehension scope."""
    bound = set()
    args = getattr(scope_node, "args", None)
    if args is not None:
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            bound.add(a.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    if isinstance(scope_node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        for g in scope_node.generators:
            bound |= set(bound_targets(g.target))

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(c.name)
                for d in c.decorator_list:
                    walk(d)
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in c.args.defaults + c.args.kw_defaults:
                        if d is not None:
                            walk(d)
                continue
            if isinstance(c, ast.Lambda):
                for d in c.args.defaults + c.args.kw_defaults:
                    if d is not None:
                        walk(d)
                continue
            if isinstance(c, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                # the first iterable is evaluated in the enclosing scope
                walk(c.generators[0].iter)
                continue
            if isinstance(c, ast.Name) and isinstance(c.ctx, (ast.Store, ast.Del)):
                bound.add(c.id)
            elif isinstance(c, ast.ExceptHandler) and c.name:
                bound.add(c.name)
            elif isinstance(c, (ast.Import, ast.ImportFrom)):
                for a in c.names:
                    bound.add((a.asname or a.name).split(".")[0])
            elif isinstance(c, ast.NamedExpr):
                bound.add(c.target.id)
            elif isinstance(c, ast.MatchAs) and c.name:
                bound.add(c.name)
            elif isinstance(c, ast.MatchStar) and c.name:
                bound.add(c.name)
            elif isinstance(c, ast.MatchMapping) and c.rest:
                bound.add(c.rest)
            walk(c)

    if isinstance(scope_node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        for g in scope_node.generators:
            for cond in g.ifs:
                walk(cond)
            if g is not scope_node.generators[0]:
                walk(g.iter)
        if isinstance(scope_node, ast.DictComp):
            walk(scope_node.key)
            walk(scope_node.value)
        else:
            walk(scope_node.elt)
    else:
        walk(scope_node)
    # `global X` makes X the module's, however the function assigns it
    return bound - declared_global(scope_node)


def global_name_refs(stmt_node):
    """Yield every Name node in the statement that resolves to MODULE scope."""
    out = []

    def visit(n, fn_scopes, in_class):
        # fn_scopes: list of sets of names bound in enclosing FUNCTION scopes
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for d in getattr(c, "decorator_list", []):
                    visit_expr(d, fn_scopes)
                for d in c.args.defaults + c.args.kw_defaults:
                    if d is not None:
                        visit_expr(d, fn_scopes)
                for a in c.args.posonlyargs + c.args.args + c.args.kwonlyargs:
                    if a.annotation is not None:
                        visit_expr(a.annotation, fn_scopes)
                if getattr(c, "returns", None) is not None:
                    visit_expr(c.returns, fn_scopes)
                inner = local_bindings(c)
                body = c.body if isinstance(c.body, list) else [c.body]
                for b in body:
                    visit(b, fn_scopes + [inner], False)
                    if isinstance(b, ast.Name):
                        resolve(b, fn_scopes + [inner])
                continue
            if isinstance(c, ast.ClassDef):
                visit_class(c, fn_scopes)
                continue
            if isinstance(c, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                visit(c.generators[0].iter, fn_scopes, in_class)
                if isinstance(c.generators[0].iter, ast.Name):
                    resolve(c.generators[0].iter, fn_scopes)
                inner = local_bindings(c)
                scopes = fn_scopes + [inner]
                parts = [g.ifs for g in c.generators] + [[g.iter] for g in c.generators[1:]]
                elts = [c.key, c.value] if isinstance(c, ast.DictComp) else [c.elt]
                for group in parts + [elts]:
                    for p in group:
                        visit(p, scopes, False)
                        if isinstance(p, ast.Name):
                            resolve(p, scopes)
                continue
            if isinstance(c, ast.Name):
                resolve(c, fn_scopes)
            visit(c, fn_scopes, in_class)

    def visit_expr(node, fn_scopes):
        if isinstance(node, ast.Name):
            resolve(node, fn_scopes)
        else:
            visit(node, fn_scopes, False)

    def resolve(name_node, fn_scopes):
        for sc in fn_scopes:
            if name_node.id in sc:
                return
        out.append(name_node)

    def visit_class(c, fn_scopes):
        for d in c.decorator_list:
            visit_expr(d, fn_scopes)
        for b in c.bases + [k.value for k in c.keywords]:
            visit_expr(b, fn_scopes)
        # names assigned directly in the class body are class attributes: they
        # shadow module names for the body's own statements, not for methods
        class_bound = set()
        for b in c.body:
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                class_bound.add(b.name)
                continue
            for n in ast.walk(b):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    class_bound.add(n.id)
        for b in c.body:
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                visit(ast.Module(body=[b], type_ignores=[]), fn_scopes, False)
            else:
                visit(b, fn_scopes + [class_bound], True)
                if isinstance(b, ast.Name):
                    resolve(b, fn_scopes + [class_bound])

    if isinstance(stmt_node, ast.Name):
        out.append(stmt_node)
        return out
    if isinstance(stmt_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        # the statement IS a function: its body runs in its own local scope
        for d in getattr(stmt_node, "decorator_list", []):
            visit_expr(d, [])
        for d in stmt_node.args.defaults + stmt_node.args.kw_defaults:
            if d is not None:
                visit_expr(d, [])
        for a in stmt_node.args.posonlyargs + stmt_node.args.args + stmt_node.args.kwonlyargs:
            if a.annotation is not None:
                visit_expr(a.annotation, [])
        if getattr(stmt_node, "returns", None) is not None:
            visit_expr(stmt_node.returns, [])
        inner = local_bindings(stmt_node)
        for b in stmt_node.body:
            visit(b, [inner], False)
            if isinstance(b, ast.Name):
                resolve(b, [inner])
        return out
    if isinstance(stmt_node, ast.ClassDef):
        visit_class(stmt_node, [])
        return out
    visit(stmt_node, [], False)
    return out


def def_time_exprs(node):
    """Expressions a def/class statement evaluates when it executes."""
    exprs = list(getattr(node, "decorator_list", []))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        exprs += [d for d in node.args.defaults + node.args.kw_defaults if d is not None]
        exprs += [a.annotation for a in node.args.posonlyargs + node.args.args
                  + node.args.kwonlyargs if a.annotation is not None]
        if node.returns is not None:
            exprs.append(node.returns)
    return exprs


def names_in(exprs):
    out = []
    for e in exprs:
        for n in ast.walk(e):
            if isinstance(n, ast.Name):
                out.append(n)
    return out


# --------------------------------------------------------------------------- #
# Rewriting
# --------------------------------------------------------------------------- #
def statement_edits(node, module, owner, lines, import_names, report, facade_name,
                    class_scope=frozenset()):
    """Edits that qualify cross-module names in one statement node.

    Returns (edits, deps, used_imports, needs_facade, drops) where edits are
    (lineno, col, end_col, new) and drops are whole lines to delete (a `global`
    statement whose every name moved elsewhere)."""
    edits = []
    deps = set()
    used_imports = set()
    needs_facade = False
    drops = set()
    for nm in global_name_refs(node):
        ident = nm.id
        if ident in class_scope:
            continue
        if ident in import_names:
            used_imports.add(ident)
            continue
        mod = owner.get(ident)
        if mod is None or mod == module:
            continue
        line = lines[nm.lineno - 1]
        if line[nm.col_offset:nm.end_col_offset] != ident:
            report.append(f"L{nm.lineno}:{nm.col_offset} position mismatch for {ident} "
                          f"(f-string?) -- fix by hand: {line.strip()[:80]}")
            continue
        edits.append((nm.lineno, nm.col_offset, nm.end_col_offset, f"{mod}.{ident}"))
        deps.add(mod)
    # `global X` where X moved to another module: the declaration goes, the uses
    # were qualified above (local_bindings no longer counts X as local)
    for g in ast.walk(node):
        if not isinstance(g, ast.Global):
            continue
        keep = [n for n in g.names if owner.get(n) in (None, module)]
        if len(keep) == len(g.names):
            continue
        if g.lineno != g.end_lineno:
            report.append(f"L{g.lineno}: multi-line global statement -- fix by hand")
            continue
        line = lines[g.lineno - 1]
        if keep:
            edits.append((g.lineno, g.col_offset, g.end_col_offset, "global " + ", ".join(keep)))
        elif line.strip() == line[g.col_offset:g.end_col_offset].strip():
            drops.add(g.lineno)
        else:
            report.append(f"L{g.lineno}: global statement shares its line -- fix by hand")
    # globals()["X"]: the old module's namespace is the facade now
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name) and sub.value.func.id == "globals"
                and not sub.value.args):
            key = sub.slice
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                report.append(f"L{sub.lineno}: globals()[<non-constant>] -- fix by hand")
                continue
            if sub.lineno != sub.end_lineno:
                report.append(f"L{sub.lineno}: multi-line globals()[...] -- fix by hand")
                continue
            if key.value not in owner:
                report.append(f"L{sub.lineno}: globals()[{key.value!r}] is not a name of the module")
                continue
            home = owner[key.value]
            if home == module:
                continue                    # still this module's own global
            if home == "deps":
                # an imported name has a copy in every module that uses it; the
                # facade's write reaches them all
                edits.append((sub.lineno, sub.col_offset, sub.end_col_offset,
                              f"{facade_name}.{key.value}"))
                needs_facade = True
            else:
                edits.append((sub.lineno, sub.col_offset, sub.end_col_offset,
                              f"{home}.{key.value}"))
                deps.add(home)
    return edits, deps, used_imports, needs_facade, drops


def apply_edits(lines, first, last, edits, drops=(), skip=()):
    """Text of lines first..last (1-based, inclusive) with edits applied,
    dropping `drops` lines and every line inside a `skip` (lo, hi) range."""
    by_line = collections.defaultdict(list)
    for ln, c0, c1, new in edits:
        by_line[ln].append((c0, c1, new))
    out = []
    for ln in range(first, last + 1):
        if ln in drops or any(lo <= ln <= hi for lo, hi in skip):
            continue
        s = lines[ln - 1]
        for c0, c1, new in sorted(by_line.get(ln, ()), reverse=True):
            s = s[:c0] + new + s[c1:]
        out.append(s)
    return "".join(out)


def wrap_call(head, items, tail, width=99):
    """`head` + items joined by ", " + `tail`, continued under the opening
    parenthesis when it does not fit on one line."""
    one = head + ", ".join(items) + tail
    if len(one) <= width:
        return one
    pad = " " * len(head)
    out, cur = [], head
    for i, it in enumerate(items):
        piece = it + (", " if i < len(items) - 1 else tail)
        if cur.strip() and cur != head and len(cur) + len(piece.rstrip()) > width:
            out.append(cur.rstrip())
            cur = pad
        cur += piece
    out.append(cur)
    return "\n".join(out)


def module_docstring(doc):
    if not doc:
        return ""
    if len(doc) + 6 <= 99:
        return '"""' + doc + '"""\n'
    return '"""' + textwrap.fill(doc, 76) + '\n"""\n'


def import_lines_for(used, import_stmts, optional_names):
    """Emit the import statements a module needs, in the head's order."""
    plain = []
    optional = sorted(n for n in used if n in optional_names)
    for names, text, is_from in import_stmts:
        want = [n for n in names if n in used and n not in optional_names]
        if not want:
            continue
        if len(names) > 1:
            if is_from:
                mod = re.match(r"\s*from\s+(\S+)\s+import", text).group(1)
                plain.append(f"from {mod} import {', '.join(sorted(want))}\n")
            else:
                plain.append("".join(f"import {n}\n" for n in names if n in want))
        else:
            plain.append(text.strip() + "\n")
    out = "".join(plain)
    if optional:
        out += f"from .deps import {', '.join(optional)}\n"
    return out


FACADE_TEMPLATE = '''{docstring}
# The code lives in the `{package}` package, one module per concern
# ({package}/__init__.py lists them). This module is the entry point and a
# compatibility facade: `import {facade}` still resolves every name, reading
# or writing, to the module that owns it, so the services, tools and tests
# written against the single-file layout keep working unchanged.
{head_imports}import types  # noqa: E402

{head}
# ONE COPY OF THIS MODULE. `python {facade}.py` runs it as `__main__`; a later
# `import {facade}` (from the package's own selftest, say) must find this copy
# rather than load a second one.
if __name__ == "__main__":
    sys.modules.setdefault("{facade}", sys.modules[__name__])

from {package} import (  # noqa: E402
{module_imports}
)

# Which {package} module owns each top-level name of the old {facade}.py.
_OWNERS = {{
{owners}
}}
_MODULES = {{
{modules_dict}
}}


class _Facade(types.ModuleType):
    """`{facade}.<name>` reads and writes go to the owning {package} module."""

    def __getattr__(self, name):
        mod = _OWNERS.get(name)
        if mod is None:
            raise AttributeError(f"module '{facade}' has no attribute {{name!r}}")
        return getattr(_MODULES[mod], name)

    def __setattr__(self, name, value):
        mod = _OWNERS.get(name)
        if mod is None:
            super().__setattr__(name, value)
            return
        setattr(_MODULES[mod], name, value)
        if mod == "deps":
            # an imported name is a copy in every module that imported it;
            # a patch has to reach each copy
            for other in _MODULES.values():
                if other is not _MODULES["deps"] and hasattr(other, name):
                    setattr(other, name, value)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_OWNERS))


sys.modules[__name__].__class__ = _Facade
{tail}'''


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--package", required=True, help="package name, e.g. jantable")
    ap.add_argument("--out")
    ap.add_argument("--facade")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="generate into a scratch directory and compare with --out/--facade")
    args = ap.parse_args(argv)
    if args.verify:
        return verify(args)

    text, lines, tree, stmts, trailing = load_source(args.src)
    facade_name = os.path.splitext(os.path.basename(args.facade or args.src))[0]

    mods, title = read_map(args.map)
    if "deps" not in mods:
        mods["deps"] = {"doc": "Imports shared by the package's modules; an optional one "
                               "is None when it is not installed.",
                        "names": set(), "members": set(), "prefixes": [], "mixins": {},
                        "banners": []}
    report = []
    owner, unmapped = assign_modules(stmts, mods, report)
    mods.pop("__facade__", None)

    # import-like names and their statements
    import_stmts = []       # (names, text, is_from)
    optional_names = set()
    import_names = set()
    for s in stmts:
        if s.kind == "import":
            import_stmts.append((s.names, "".join(lines[s.first - 1:s.last]),
                                 isinstance(s.node, ast.ImportFrom)))
            import_names |= set(s.names)
        elif s.kind == "optimport":
            optional_names |= set(s.names)
            import_names |= set(s.names)
    for n in import_names:
        owner.setdefault(n, "deps")

    # section banners that head a statement of another module move to this
    # module's next statement, with the blank line that follows them
    banner_drops = set()
    banner_prepend = collections.defaultdict(str)
    for mod, spec in mods.items():
        for prefix in spec.get("banners", []):
            hits = [i + 1 for i, ln in enumerate(lines) if ln.startswith(prefix)]
            if len(hits) != 1:
                report.append(f"BANNER {prefix!r}: {len(hits)} matching lines, want 1")
                continue
            ln = hits[0]
            # a banner above a top-level statement, or above a class member
            seq = stmts
            idx = next((i for i, st in enumerate(stmts) if st.span_first <= ln < st.first), None)
            if idx is None:
                for st in stmts:
                    if st.members and st.span_first <= ln <= st.last:
                        seq = st.members
                        idx = next((i for i, m in enumerate(seq)
                                    if m.span_first <= ln < m.first), None)
                        break
            if idx is None:
                report.append(f"BANNER {prefix!r}: L{ln} is not in a comment block")
                continue
            target = next((st for st in seq[idx:] if st.module == mod), None)
            if target is None:
                report.append(f"BANNER {prefix!r}: no {mod} statement follows it")
                continue
            if target is seq[idx]:
                continue
            take = [ln] + ([ln + 1] if ln < len(lines) and not lines[ln].strip() else [])
            banner_drops.update(take)
            banner_prepend[id(target)] += "".join(lines[t - 1] for t in take)

    def with_banner(stmt, text):
        banner = banner_prepend.get(id(stmt))
        return "\n\n" + banner + text.lstrip("\n") if banner else text
    for s in unmapped:
        report.append(f"UNMAPPED L{s.first}-{s.last} {s.kind} {s.names or ''}: "
                      f"{lines[s.first - 1].rstrip()[:90]}")

    # the checks a split class needs: members that reference each other at
    # class level must stay together, and a name may be defined once
    split_classes = {}
    for s in stmts:
        if s.members is None or s.module in (None, "__facade__"):
            continue
        moved = [m for m in s.members if m.module != s.module]
        if not moved:
            continue
        cls = s.names[0]
        split_classes[cls] = s
        class_bound = collections.defaultdict(set)
        for m in s.members:
            for n in m.names:
                class_bound[n].add(m.module)
        for n, where in class_bound.items():
            if len(where) > 1:
                report.append(f"SPLIT CLASS {cls}: {n} defined in several modules {where}")
        for m in s.members:
            level = names_in(def_time_exprs(m.node)) if m.kind == "def" else names_in([m.node])
            for nm in level:
                if nm.id in class_bound and class_bound[nm.id] != {m.module}:
                    report.append(f"SPLIT CLASS {cls}: L{nm.lineno} class-level use of {nm.id} "
                                  f"crosses modules")
            for sub in ast.walk(m.node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id == "super" and m.module != s.module:
                    report.append(f"SPLIT CLASS {cls}: L{sub.lineno} super() in a moved member")
                if isinstance(sub, ast.Attribute) and sub.attr.startswith("__") \
                        and not sub.attr.endswith("__") and m.module != s.module:
                    report.append(f"SPLIT CLASS {cls}: L{sub.lineno} name-mangled {sub.attr}")
        for m in moved:
            if cls not in mods[m.module]["mixins"]:
                report.append(f"SPLIT CLASS {cls}: module {m.module} has no `={cls} Mixin` line")

    # build module bodies
    order = list(mods)
    order.remove("deps")
    order.insert(0, "deps")
    bodies = collections.OrderedDict((m, []) for m in order)
    mod_deps = collections.defaultdict(set)
    mod_imports = collections.defaultdict(set)
    mod_facade = set()
    units = []              # (module, node) of every function body, for the shadow check
    import_time = collections.defaultdict(set)
    facade_head = []
    facade_tail = []
    stats = collections.Counter()

    def note_import_time(module, refs, class_scope=frozenset()):
        for nm in refs:
            if nm.id in class_scope:
                continue
            mod = owner.get(nm.id)
            if mod and mod not in (module, "deps") and nm.id not in import_names:
                import_time[module].add(f"{mod}.{nm.id}")

    for s in stmts:
        if s.module is None:
            continue
        if s.module == "__facade__":
            (facade_tail if lines[s.first - 1].startswith("if __name__")
             else facade_head).append(s)
            continue
        if s.kind in ("import", "optimport"):
            bodies["deps"].append("".join(lines[s.span_first - 1:s.last]))
            continue
        if s.names and s.names[0] in split_classes and s is split_classes[s.names[0]]:
            cls = s.names[0]
            host = s.module
            moved = [m for m in s.members if m.module != host]
            kept = [m for m in s.members if m.module == host]
            edits, deps, used, fac, drops = statement_edits(
                s.node, host, owner, lines, import_names, report, facade_name)
            skip = [(m.span_first, m.last) for m in moved]
            edits = [e for e in edits if not any(lo <= e[0] <= hi for lo, hi in skip)]
            # what the host still needs is what its kept lines reference
            deps = {new.split(".")[0] for _l, _c0, _c1, new in edits
                    if not new.startswith(("global ", facade_name + "."))}
            used = {nm.id for nm in global_name_refs(s.node) if nm.id in import_names
                    and not any(lo <= nm.lineno <= hi for lo, hi in skip)}
            fac = any(new.startswith(facade_name + ".") for _l, _c0, _c1, new in edits)
            mixin_mods = []
            for m in moved:
                if m.module not in mixin_mods:
                    mixin_mods.append(m.module)
            bases = [f"{mm}.{mods[mm]['mixins'][cls]}" for mm in mixin_mods]
            old_bases = [ast.get_source_segment(text, b) for b in s.node.bases]
            if old_bases != ["object"]:
                bases += old_bases
            header_line = lines[s.node.lineno - 1]
            new_header, n = re.subn(r"^(\s*class\s+%s)\s*(\([^)]*\))?\s*:" % re.escape(cls),
                                    lambda mt: wrap_call(mt.group(1) + "(", bases, "):"),
                                    header_line)
            if n != 1 or s.header_end != s.node.lineno:
                report.append(f"SPLIT CLASS {cls}: header is not one plain line -- fix by hand")
            host_text = with_banner(s, apply_edits(
                lines, s.span_first, s.last, edits, drops | banner_drops, skip))
            # swap in the new header (its line survived: it is never inside a member span)
            host_text = host_text.replace(header_line, new_header, 1)
            bodies[host].append(host_text)
            mod_deps[host] |= deps | set(mixin_mods)
            mod_imports[host] |= used
            if fac:
                mod_facade.add(host)
            stats[host] += 1
            for mm in mixin_mods:
                note_import_time(host, [ast.Name(id=mods[mm]["mixins"][cls])], ())
                import_time[host].add(f"{mm}.{mods[mm]['mixins'][cls]}")
            class_names = {n for m in s.members for n in m.names}
            for m in kept:
                if m.kind == "def":
                    units.append((host, m.node))
                    note_import_time(host, names_in(def_time_exprs(m.node)), class_names)
                else:
                    note_import_time(host, names_in([m.node]), class_names)
            note_import_time(host, names_in(s.node.bases + s.node.decorator_list))
            # the mixins, each at this class's position in its module
            for mm in mixin_mods:
                mixin = mods[mm]["mixins"][cls]
                parts = [f"class {mixin}:\n",
                         f'    """Part of `{cls}` ({host}.py), which inherits it.\n\n',
                         textwrap.fill(mods[mm]["doc"], 79, initial_indent="    ",
                                       subsequent_indent="    ") + '\n    """\n']
                for m in moved:
                    if m.module != mm:
                        continue
                    scope = class_names if m.kind != "def" else frozenset()
                    e2, d2, u2, f2, dr2 = statement_edits(
                        m.node, mm, owner, lines, import_names, report, facade_name, scope)
                    if m.kind == "def":
                        # decorators and defaults run in the class body, where
                        # the class's own names shadow the module's
                        at = {(x.lineno, x.col_offset) for x in names_in(def_time_exprs(m.node))
                              if x.id in class_names}
                        e2 = [e for e in e2 if (e[0], e[1]) not in at]
                    member_text = apply_edits(lines, m.span_first, m.last, e2,
                                              dr2 | banner_drops)
                    if banner_prepend.get(id(m)):
                        member_text = "\n" + banner_prepend[id(m)] + member_text.lstrip("\n")
                    parts.append(member_text)
                    mod_deps[mm] |= d2
                    mod_imports[mm] |= u2
                    if f2:
                        mod_facade.add(mm)
                    stats[mm] += 1
                    if m.kind == "def":
                        units.append((mm, m.node))
                        note_import_time(mm, names_in(def_time_exprs(m.node)), class_names)
                    else:
                        note_import_time(mm, names_in([m.node]), class_names)
                bodies[mm].append("\n\n" + "".join(parts))
            continue
        edits, deps, used, fac, drops = statement_edits(
            s.node, s.module, owner, lines, import_names, report, facade_name)
        bodies[s.module].append(with_banner(s, apply_edits(
            lines, s.span_first, s.last, edits, drops | banner_drops)))
        mod_deps[s.module] |= deps
        mod_imports[s.module] |= used
        if fac:
            mod_facade.add(s.module)
        stats[s.module] += 1
        if s.kind == "def":
            units.append((s.module, s.node))
            note_import_time(s.module, names_in(def_time_exprs(s.node)))
            if isinstance(s.node, ast.ClassDef):
                class_names = {n for m in s.members for n in m.names}
                note_import_time(s.module, names_in(s.node.bases + s.node.decorator_list))
                for m in s.members:
                    exprs = def_time_exprs(m.node) if m.kind == "def" else [m.node]
                    note_import_time(s.module, names_in(exprs), class_names)
        else:
            note_import_time(s.module, global_name_refs(s.node))

    # module-name collisions: a local variable named like a module the
    # function references, or a module name that is also a top-level name there
    for module, fnode in units:
        for fn in ast.walk(fnode):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                loc = local_bindings(fn)
                clash = loc & (mod_deps[module] | ({facade_name} if module in mod_facade else set()))
                for c in clash:
                    refs = {owner.get(n.id) for n in global_name_refs(fn)
                            if owner.get(n.id) not in (module, None)}
                    if c in refs or c == facade_name:
                        report.append(f"L{fn.lineno}: local {c!r} in {module}."
                                      f"{getattr(fn, 'name', '<lambda>')} shadows module {c} "
                                      f"that the function references")
    own_top = collections.defaultdict(set)
    for n, m in owner.items():
        own_top[m].add(n)
    for m in order:
        clash = (mod_deps[m] | ({facade_name} if m in mod_facade else set())) & \
            (own_top[m] | mod_imports[m])
        for c in clash:
            report.append(f"module {m}: imports module {c} but also binds a top-level {c}")
    if facade_name in owner:
        report.append(f"the facade's own name {facade_name} is a top-level name")

    # cycles among import-time deps
    def reaches(a, b, seen=None):
        seen = seen or set()
        for c in {x.split(".")[0] for x in import_time.get(a, ())}:
            if c == b or (c not in seen and reaches(c, b, seen | {c})):
                return True
        return False
    for a, bs in import_time.items():
        for b in {x.split(".")[0] for x in bs}:
            if reaches(b, a):
                report.append(f"IMPORT-TIME CYCLE {a} <-> {b}")

    print(f"statements: {len(stmts)}  modules: {len(bodies)}  unmapped: {len(unmapped)}")
    for m, c in stats.most_common():
        print(f"  {c:>4}  {m}  (uses: {', '.join(sorted(mod_deps[m]))})")
    if import_time:
        print("import-time dependencies:")
        for a, bs in import_time.items():
            print(f"  {a} -> {', '.join(sorted(bs))}")
    if report:
        print("\nREPORT (%d):" % len(report))
        for r in report:
            print("  " + r)
    if args.check or not args.out:
        return
    fatal = ("IMPORT-TIME CYCLE", "UNMAPPED", "SPLIT CLASS", "MAP NAME NOT IN SOURCE", "BANNER")
    if unmapped or any(r.startswith(fatal) or "shadows module" in r or "fix by hand" in r
                       or "position mismatch" in r or "binds a top-level" in r
                       or "not a name of the module" in r or "facade's own name" in r
                       for r in report):
        raise SystemExit("refusing to write: fix the report first")

    os.makedirs(args.out, exist_ok=True)
    # a split class's home module is imported first by the package, so its
    # mixin modules (which import it back) can be imported in any order
    hosts = sorted({s.module for s in split_classes.values()}, key=order.index)
    for m in order:
        spec = mods[m]
        buf = io.StringIO()
        buf.write(module_docstring(spec["doc"]))
        if m == "deps":
            buf.write("".join(bodies["deps"]))
        else:
            imp = import_lines_for(mod_imports[m], import_stmts, optional_names)
            if m in mod_facade:
                imp += f"import {facade_name}  # the facade: its writes reach every copy\n"
            buf.write(imp)
            deps = sorted(mod_deps[m])
            if deps:
                buf.write(wrap_call("from . import (", deps, ")") + "\n"
                          if len("from . import " + ", ".join(deps)) > 99
                          else "from . import " + ", ".join(deps) + "\n")
            body = "".join(bodies[m])
            if not body.startswith("\n"):
                buf.write("\n")
            buf.write(body)
        content = buf.getvalue()
        if not content.endswith("\n"):
            content += "\n"
        with open(os.path.join(args.out, m + ".py"), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    width = max(len(m) for m in order) + 5
    with open(os.path.join(args.out, "__init__.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write('"""%s\n\n' % (title or f"The {args.package} package, one module per concern."))
        for m in order:
            f.write(f"    {m + '.py':<{width}} {mods[m]['doc']}\n")
        f.write(f"\n{facade_name}.py (one directory up) is the entry point and the "
                f"compatibility\nfacade over these modules. Generated from the flat "
                f"{facade_name}.py by\ntools/split/split_jan_pkg.py and its map.\n\"\"\"\n")
        for h in hosts:
            f.write(f"from . import {h}  # noqa: F401  (a split class's home loads before its parts)\n")
    if args.facade:
        doc = facade_head[0] if facade_head and isinstance(facade_head[0].node, ast.Expr) else None
        docstring = "".join(lines[doc.span_first - 1:doc.last]) if doc else ""
        rest = [s for s in facade_head if s is not doc]
        # names the facade's own statements use, bound for them explicitly
        alias = {m: f"_{m}" for m in order}
        need_imports = set()
        head_txt = []
        for s in rest:
            head_txt.append("".join(lines[s.first - 1:s.last]))
            need_imports |= {n.id for n in global_name_refs(s.node) if n.id in import_names}
        tail_txt = []
        for s in facade_tail:
            edits = []
            for nm in global_name_refs(s.node):
                if nm.id in import_names:
                    need_imports.add(nm.id)
                elif nm.id in owner:
                    edits.append((nm.lineno, nm.col_offset, nm.end_col_offset,
                                  f"{alias[owner[nm.id]]}.{nm.id}"))
            tail_txt.append(apply_edits(lines, s.first, s.last, edits))
        need_imports.add("sys")
        head_imports = ""
        for names, txt, _is_from in import_stmts:
            want = [n for n in names if n in need_imports]
            if want and not _is_from:
                head_imports += "".join(f"import {n}\n" for n in want) if len(names) > 1 \
                    else txt.strip() + "\n"
        facade = FACADE_TEMPLATE.format(
            docstring=docstring.rstrip("\n"),
            package=args.package,
            facade=facade_name,
            head_imports=head_imports,
            head="".join(head_txt),
            module_imports="\n".join(f"    {m} as {alias[m]}," for m in order),
            owners="\n".join(f"    {n!r}: {m!r}," for n, m in sorted(owner.items())),
            modules_dict="\n".join(f"    {m!r}: {alias[m]}," for m in order),
            tail=("\n" + "\n".join(t.rstrip("\n") + "\n" for t in tail_txt)) if tail_txt else "",
        )
        with open(args.facade, "w", encoding="utf-8", newline="\n") as f:
            f.write(facade)
    print("written:", args.out, "and", args.facade)


def verify(args):
    """Regenerate into a scratch directory and compare with the tree: the
    committed package must be exactly what the flat source and the map give."""
    import tempfile
    scratch = tempfile.mkdtemp(prefix="split-verify-")
    out = os.path.join(scratch, os.path.basename(os.path.normpath(args.out)))
    fac = os.path.join(scratch, os.path.basename(args.facade))
    argv = ["--src", args.src, "--map", args.map, "--package", args.package,
            "--out", out, "--facade", fac]
    main(argv)

    def same(a, b):
        # a Windows checkout may hold CRLF; the generator writes LF
        try:
            return (open(a, "rb").read().replace(b"\r\n", b"\n")
                    == open(b, "rb").read().replace(b"\r\n", b"\n"))
        except OSError:
            return False
    bad = []
    names = sorted(set(os.listdir(out)) | {n for n in os.listdir(args.out) if n.endswith(".py")})
    for n in names:
        if not same(os.path.join(out, n), os.path.join(args.out, n)):
            bad.append(os.path.join(args.out, n))
    if not same(fac, args.facade):
        bad.append(args.facade)
    if bad:
        print("DIFFERS from what the flat source and the map give:")
        for b in bad:
            print("  " + b)
        return 1
    print(f"verified: {args.out} and {args.facade} are exactly the generated package")
    return 0


if __name__ == "__main__":
    sys.exit(main())
