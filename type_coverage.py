"""Observe operand types in baseline comparisons, never evaluating alternative code."""

import ast
import builtins
import enum
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys

active = 0
table = {}
type_cache = {}
seen = set()
BUILTINS = (int, float, complex, bool, str, bytes, tuple, list, dict, set, frozenset)


def kind(cls):
    k = type_cache.get(cls)
    if k is None:
        if cls is type(None):
            k = "none"
        elif cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
            k = "object"
        elif cls in BUILTINS:
            k = "builtin"
        elif (
            issubclass(cls, enum.Enum)
            and cls.__eq__ is str.__eq__
            and cls.__ne__ is str.__ne__
        ):
            k = "str-enum:" + cls.__module__ + "." + cls.__qualname__
        else:
            k = "other"
        type_cache[cls] = k
    return k


def record(site, value):
    if active:
        cls = type(value)
        key = (site, cls)
        if key not in seen:
            seen.add(key)
            slot = table.setdefault(site, {})
            tag = kind(cls)
            slot[tag] = slot.get(tag, 0) | active
    return value


def switch(mask):
    global active
    active = mask
    seen.clear()


class Probes(ast.NodeTransformer):
    def __init__(self, filename):
        self.filename = filename
        self.used = False

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(
            node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)
        ):
            return node
        rhs = node.comparators[0]
        if not (
            (isinstance(rhs, ast.Constant) and rhs.value is None)
            or (isinstance(rhs, ast.Attribute) and rhs.attr.isupper())
        ):
            return node
        site = f"{self.filename}:{node.lineno}:{node.col_offset}"

        def wrap(side, expression):
            probe = ast.Call(
                ast.Name("_rwx_observe_type_", ast.Load()),
                [ast.Constant(site + side), expression],
                [],
            )
            return ast.copy_location(probe, expression)

        node.left = wrap("/left", node.left)
        if not isinstance(rhs, ast.Constant):
            node.comparators[0] = wrap("/right", rhs)
        self.used = True
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, filename=self.path)
        transformed = Probes(os.path.relpath(self.path))
        transformed.visit(tree)
        if transformed.used:
            ast.fix_missing_locations(tree)
        return compile(tree, self.path, "exec", dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
            return spec
        return None


def install():
    builtins._rwx_observe_type_ = record
    sys.meta_path.insert(0, Finder())
