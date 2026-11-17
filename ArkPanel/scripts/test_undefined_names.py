"""Catch calls to functions that do not exist.

Written after `_finish_selfupdate` spent weeks calling `_restarter_available()`,
a name that was never defined anywhere. Python only raises NameError when the
line actually runs, and that line only ran after an update had already written
its files - so every agent self-update wrote the new code, then died before
triggering the restart, and reported "check failed" into a log nobody was
reading. The agents sat on OLD CODE and the panel kept saying so.

pyflakes would catch this, but it is not installed here and pip is locked down,
so this does the one check that mattered: every bare name that gets CALLED must
be defined somewhere sensible - in the module, imported, a builtin, or a local.

Deliberately narrow. It does not try to be a type checker; it tries to stop
exactly the failure above from shipping twice.
"""
import ast
import builtins
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTINS = set(dir(builtins))

# Checked because these are what actually run in production. Test scripts are
# exercised by being run; the long-lived services are not.
TARGETS = ["agent.py", "panel.py", "ArkPanel.py"] + [
    os.path.join("core", f) for f in sorted(os.listdir(os.path.join(BASE, "core")))
    if f.endswith(".py")
]


def defined_names(tree: ast.AST) -> set:
    """Everything a call could legitimately resolve to, module-wide.

    Gathered without scope analysis on purpose: a name bound anywhere in the
    file is treated as fine. That under-reports (a genuinely out-of-scope call
    slips through) but never cries wolf, and a checker people switch off
    because it nags is worth nothing.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            for arg in getattr(node, "args", ast.arguments(
                    posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])).args:
                names.add(arg.arg)
            args = getattr(node, "args", None)
            if args:
                for arg in list(args.posonlyargs) + list(args.kwonlyargs):
                    names.add(arg.arg)
                for extra in (args.vararg, args.kwarg):
                    if extra:
                        names.add(extra.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.comprehension,)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def called_names(tree: ast.AST):
    """(name, lineno) for every plain `foo(...)` call.

    Attribute calls like `mod.foo()` are skipped - resolving those means
    knowing what `mod` is at runtime, which is a different and much larger job.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            yield node.func.id, node.func.lineno


def main() -> int:
    problems = []
    for rel in TARGETS:
        path = os.path.join(BASE, rel)
        if not os.path.isfile(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        known = defined_names(tree) | BUILTINS
        seen = set()
        for name, line in called_names(tree):
            if name in known or (name, rel) in seen:
                continue
            seen.add((name, rel))
            problems.append(f"{rel}:{line}  calls undefined {name}()")

    for line in problems:
        print("  FAIL ", line)
    print(f"\n{len(TARGETS)} file(s) checked, {len(problems)} undefined call(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
