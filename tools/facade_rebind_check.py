#!/usr/bin/env python3
"""Prove that rebinding a name through the jangame / janhourou facades still
reaches the code that runs.

    python tools/facade_rebind_check.py        # fail on any rebinding a facade does not forward
    python tools/facade_rebind_check.py -v     # also list every rebinding found

services/jangame.py and services/janhourou.py are facades over the jantable
and janworld packages (tools/split/ generates both). Reads of
`jangame.<name>` go to the module that owns the name. That is not enough for
the code that REBINDS a name to fake something or to turn a switch,

    jh.log = lambda *a, **k: None          # tests/test_jan_reserve_limits.py
    janhourou.LIVE_ROOMS = _live_rooms     # services/jantitle.py
    jangame.TRACE = lambda m: log(m)       # janworld/dispatch.py

because a write that landed on the facade alone would leave every caller
inside the package reading the old value: nothing would raise, and a test
would keep passing while testing nothing. The facades therefore forward
writes to the owning module, and for an imported name (a copy in every module
that imported it) to every copy. This tool checks that promise against the
rebindings the tree actually makes: it sets a sentinel through the facade and
reads it back through the owner and every package module holding the name.

Aliases matter (`import janhourou as jh`, `from .deps import jangame`), so
this walks the AST for the names bound to each facade rather than grepping.
"""
import argparse
import ast
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
SCAN_DIRS = ("tools", "services", "tests")
FACADES = ("jangame", "janhourou")


def module_aliases(tree):
    """{local name: facade} for every binding of a facade module in the file."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in FACADES:
                    out[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom):
            # `from .deps import jangame` inside a package: the facade itself
            for a in node.names:
                if a.name in FACADES:
                    out[a.asname or a.name] = a.name
    return out


def rebindings():
    """[(file, line, facade, attr)] for every `<alias>.<attr> = ...` on a facade."""
    found = []
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except (SyntaxError, UnicodeDecodeError):
                    continue
                aliases = module_aliases(tree)
                if not aliases:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                        targets = [node.target]
                    else:
                        continue
                    for tgt in targets:
                        for t in ast.walk(tgt):
                            if (isinstance(t, ast.Attribute)
                                    and isinstance(t.ctx, ast.Store)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id in aliases):
                                found.append((rel, node.lineno, aliases[t.value.id], t.attr))
    return found


def forwards(F, attr):
    """Does a write of `attr` through facade F reach the owning module (and
    every module holding a copy)? Returns a reason string on failure."""
    owners = getattr(F, "_OWNERS", None)
    modules = getattr(F, "_MODULES", None)
    if owners is None or modules is None:
        return "not a facade (no _OWNERS/_MODULES)"
    home = owners.get(attr)
    if home is None:
        return "not a name of the old module; the write lands on the facade only"
    sentinel = object()
    saved = {m: vars(mod)[attr] for m, mod in modules.items() if attr in vars(mod)}
    try:
        setattr(F, attr, sentinel)
        if getattr(modules[home], attr, None) is not sentinel:
            return f"write did not reach the owner {home}"
        for m, mod in modules.items():
            if m in saved and getattr(mod, attr) is not sentinel:
                return f"{m} still holds the old copy"
        if getattr(F, attr) is not sentinel:
            return "read-back through the facade returned something else"
    finally:
        for m, old in saved.items():
            setattr(modules[m], attr, old)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, SERVICES)
    import importlib
    facades = {name: importlib.import_module(name) for name in FACADES}

    # the control: a module that holds the same tables but does NOT forward
    # must be caught, or a pass below proves nothing
    for name, F in facades.items():
        plain = types.ModuleType(name + "_control")
        plain._OWNERS, plain._MODULES = F._OWNERS, F._MODULES
        probe = next(n for n, m in sorted(F._OWNERS.items()) if m != "deps")
        if forwards(plain, probe) is None:
            print(f"  [FAIL] control: a non-forwarding copy of {name} passed the check")
            return 1
    print("control: a non-forwarding module is caught")

    found = rebindings()
    keys = sorted({(f, a) for _p, _l, f, a in found})
    print(f"{len(found)} rebinding(s) of {len(keys)} name(s) through the facades")
    failures = []
    for fac, attr in keys:
        why = forwards(facades[fac], attr)
        sites = [f"{p}:{l}" for p, l, f, a in found if (f, a) == (fac, attr)]
        if why:
            failures.append((fac, attr, why, sites))
        elif args.verbose:
            home = facades[fac]._OWNERS[attr]
            print(f"  [PASS] {fac + '.' + attr:<24} -> {home}  ({', '.join(sites)})")
    for fac, attr, why, sites in failures:
        print(f"  [FAIL] {fac}.{attr}: {why}\n         at {', '.join(sites)}")
    if failures:
        print(f"\n{len(failures)} name(s) are rebound through a facade that does not forward them")
        return 1
    print("every rebinding is forwarded to the module that runs it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
