"""Conservative additions for lifecycle hooks, shared fixture reads and unused worker time."""
import ast
from pathlib import Path
from functools import lru_cache
from coverage_map import apply_hunks, analyze

HOOKS = {"setUpClass", "tearDownClass", "setUpModule", "tearDownModule", "setUp", "tearDown"}


@lru_cache(maxsize=256)
def import_signature(source):
    tree = ast.parse(source)
    return tuple(ast.dump(node) for node in ast.walk(tree)
                 if isinstance(node, (ast.Import, ast.ImportFrom)))


def class_consumers(catalog, name, cls=None):
    return {r["number"] for r in catalog.rows if r["file"] == name and
            (cls is None or r["id"].split("::")[1:2] == [cls])}


def guard(catalog, profile, path, hunks):
    name = str(path)
    if path.suffix != ".py":
        return set()
    old = profile.sources.get(name)
    if old is None:
        return set()
    try:
        new, deleted, added = apply_hunks(old, hunks)
        old_tree = analyze(old)[-1]
        new_tree = analyze(new)[-1]
    except (ValueError, SyntaxError, RecursionError):
        return set(catalog.all)
    if import_signature(old) != import_signature(new):
        # A collection error marks every numbered test in its file as failing.
        if path.parts[0] == "tests" and path.name.startswith("test_"):
            return class_consumers(catalog,name)
        return set(catalog.all)
    extra = set()
    if path.parts[0] == "tests":
        for tree, changed in ((old_tree,deleted),(new_tree,added)):
            for node in ast.walk(tree):
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in HOOKS:
                    if any(node.lineno <= n <= node.end_lineno for n in changed):
                        owners = [cls.name for cls in ast.walk(tree) if isinstance(cls,ast.ClassDef) and node in cls.body]
                        extra.update(class_consumers(catalog,name,owners[0] if owners else None))
    return extra


def fill(catalog, required, related):
    # For nonempty predictions, insure cheap tests against hidden dependencies.
    # Allow at most 350 additional milliseconds per existing worker's makespan,
    # with the same worker count. Prioritize tests from the affected suites.
    bins = [list(worker) for worker in catalog.workers(required)]
    if not bins:
        return bins
    loads = [sum(catalog.duration[i] for i in worker) for worker in bins]
    ceiling = max(loads) + 350
    for candidates in ((related - required), (catalog.all - required - related)):
        for i in sorted(candidates, key=lambda i:(catalog.duration[i],i)):
            w = loads.index(min(loads))
            if loads[w] + catalog.duration[i] > ceiling:
                break
            bins[w].append(i)
            loads[w] += catalog.duration[i]
    return bins
