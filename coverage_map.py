"""Read a map of pristine test executions. Nothing here reads sample answers."""

import ast
import gzip
import json
import re
from functools import lru_cache

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def apply_hunks(source, hunk):
    original = source.splitlines()
    output, removed, added = [], [], []
    pos = 0
    for line in hunk:
        match = HUNK.match(line)
        if match:
            start = int(match[1]) if match[2] == "0" else int(match[1]) - 1
            if start < pos or start > len(original):
                raise ValueError("hunk outside original source")
            output.extend(original[pos:start])
            pos = start
        elif line.startswith((" ", "-")):
            if pos >= len(original) or original[pos] != line[1:]:
                raise ValueError("base source mismatch")
            if line[0] == " ":
                output.append(original[pos])
            else:
                removed.append(pos + 1)
            pos += 1
        elif line.startswith("+"):
            added.append(len(output) + 1)
            output.append(line[1:])
    return "\n".join(output + original[pos:]), removed, added


def code_scopes(code):
    import types

    signature = [
        (
            code.co_qualname,
            code.co_argcount,
            code.co_kwonlyargcount,
            code.co_flags,
            code.co_varnames,
            code.co_cellvars,
            code.co_freevars,
        )
    ]
    for child in code.co_consts:
        if isinstance(child, types.CodeType):
            signature.extend(code_scopes(child))
    return signature


@lru_cache(maxsize=64)
def analyze(source):
    tree = ast.parse(source, type_comments=True)
    compiled = compile(tree, "<patch>", "exec")
    nodes = list(ast.walk(tree))
    size = lambda n: n.end_lineno - n.lineno
    functions = sorted(
        (n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        key=size,
    )
    statements = sorted((n for n in nodes if isinstance(n, ast.stmt)), key=size)
    return ast.dump(tree), code_scopes(compiled), functions, statements, tree


def statement_header(statement):
    # The condition of a multiline if/while starts before its suite. A change
    # to boolean short-circuiting may affect every visit to this header.
    end = statement.end_lineno
    suite = getattr(statement, "body", None)
    if isinstance(suite, list) and suite:
        end = max(statement.lineno, suite[0].lineno - 1)
    return range(statement.lineno, end + 1)


def bounded_statements(locations, functions, statements):
    result = []
    for line in locations:
        owner = next(
            (f for f in functions if f.body[0].lineno <= line <= f.end_lineno), None
        )
        stmt = next((s for s in statements if line in statement_header(s)), None)
        if (
            owner is None
            or stmt is None
            or isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ):
            return []
        result.append((owner.name, stmt))
    return result


def class_constants(tree):
    # Restrict module-level narrowing to values in a named class assignment.
    result = {}
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for stmt in cls.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                for line in range(stmt.lineno, stmt.end_lineno + 1):
                    result[line] = (cls.name, stmt.targets[0].id, stmt.value)
    return result


def literal_shape(node):
    if isinstance(node, ast.Constant):
        return ("literal", type(node.value).__name__)
    if isinstance(node, ast.AST):
        return (
            type(node).__name__,
            tuple((k, literal_shape(v)) for k, v in ast.iter_fields(node)),
        )
    if isinstance(node, list):
        return tuple(map(literal_shape, node))
    return node


class Profile:
    @classmethod
    def load(cls, path, rows):
        if not path.exists():
            return None
        with gzip.open(path, "rt") as fp:
            return cls(json.load(fp), rows)

    def __init__(self, data, rows):
        self.sources, self.lines = data["sources"], data["lines"]
        self.shared, self.reads = data["shared"], data["reads"]
        self.dialects = data.get("dialects", {})
        self.direct_reads = data.get("direct_reads", self.reads)
        self.lambdas = data.get("lambda_hits", {})
        self.attribute_readers = data.get("attribute_readers", {})
        self.operands = data.get("operands", {})
        self.expr_types = data.get("expr_types", {})
        ids = {r["id"]: r["number"] for r in rows}
        self.all = set(ids.values())
        self.lookup = [ids.get(n) for n in data["nodes"]]
        self.unknown = self.all - {ids[n] for n in data["passed"] if n in ids}
        self.rows = rows

    def decode(self, masks):
        combined = 0
        for mask in masks:
            combined |= int(mask, 16)
        chosen = self.unknown.copy()
        while combined:
            bit = combined & -combined
            i = self.lookup[bit.bit_length() - 1]
            if i is not None:
                chosen.add(i)
            combined ^= bit
        return chosen

    def equivalent_comparisons(self, tree, updated, old_shape, name, candidates):
        # Check the actual patch. Type coverage is recorded on the unmodified
        # comparison and contains no precomputed predictions for edits.
        before = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)]
        after = [n for n in ast.walk(updated) if isinstance(n, ast.Compare)]
        if len(before) != len(after):
            return candidates
        pairs = {
            (ast.Is, ast.Eq),
            (ast.Eq, ast.Is),
            (ast.IsNot, ast.NotEq),
            (ast.NotEq, ast.IsNot),
        }
        changes = [
            (a, b)
            for a, b in zip(before, after)
            if len(a.ops) == len(b.ops) == 1
            and (type(a.ops[0]), type(b.ops[0])) in pairs
        ]
        if not changes:
            return candidates
        saved = [b.ops for a, b in changes]
        try:
            for a, b in changes:
                b.ops = a.ops
            if ast.dump(updated) != old_shape:
                return candidates
        finally:
            for (_, b), ops in zip(changes, saved):
                b.ops = ops
        retained = self.unknown.copy()
        for a, _ in changes:
            site = f"{name}:{a.lineno}:{a.col_offset}"
            left = self.operands.get(site + "/left")
            if not left:
                return candidates
            rhs = a.comparators[0]
            if isinstance(rhs, ast.Constant) and rhs.value is None:
                right = {"none": format((1 << len(self.lookup)) - 1, "x")}
            else:
                right = self.operands.get(site + "/right")
            if not right:
                return candidates
            uncertain = 0
            observed = 0
            for x, mask in left.items():
                for y, other in right.items():
                    overlap = int(mask, 16) & int(other, 16)
                    observed |= overlap
                    safe = (
                        (x in ("none", "object") and y in ("none", "object"))
                        or (x == "builtin" and y == "none")
                        or (y == "builtin" and x == "none")
                        or (x == y and x.startswith("str-enum:"))
                    )
                    if not safe:
                        uncertain |= overlap
            # A test reaching the surrounding statement without type evidence
            # stays selected. This also covers incomplete instrumentation.
            proven = self.decode([format(observed & ~uncertain, "x")]) - self.unknown
            retained.update(candidates - proven)
        return retained

    @lru_cache(maxsize=128)
    def dialect_family(self, stem):
        if stem not in self.dialects:
            return None
        masks = [self.dialects[stem]]
        for prefix in (
            "sqlglot/dialects/",
            "sqlglot/generators/",
            "sqlglot/parsers/",
            "sqlglot/typing/",
        ):
            masks.extend(self.lines.get(prefix + stem + ".py", {}).values())
        selected = self.decode(masks)
        selected.update(
            r["number"]
            for r in self.rows
            if r["file"].endswith("/test_" + stem + ".py")
        )
        return selected

    def select(self, path, hunk):
        name = str(path)
        if path.suffix != ".py":
            mask = self.reads.get(name)
            return (
                self.decode([mask])
                if mask and 0 not in self.shared.get(name, [])
                else None
            )
        source = self.sources.get(name)
        if source is None:
            return None
        try:
            patched, old, new = apply_hunks(source, hunk)
            shape, scopes, funcs, stmts, tree = analyze(source)
            after, new_scopes, new_funcs, new_stmts, new_tree = analyze(patched)
        except (ValueError, SyntaxError, RecursionError):
            return self.all
        if shape == after:
            return self.decode([self.direct_reads.get(name, "0")])
        if not old or not new or scopes != new_scopes:
            return None

        # An in-class dispatch lambda runs later than its dictionary initializer.
        # Separate calls of that code object from execution of the class body.
        def enclosing_lambda(root, lines):
            return next(
                (
                    n
                    for n in ast.walk(root)
                    if isinstance(n, ast.Lambda)
                    and all(n.body.lineno <= i <= n.body.end_lineno for i in lines)
                ),
                None,
            )

        if self.lambdas:
            lhs, rhs = enclosing_lambda(tree, old), enclosing_lambda(new_tree, new)
            if lhs and rhs and ast.dump(lhs.args) == ast.dump(rhs.args):
                patch_body = rhs.body
                try:
                    rhs.body = lhs.body
                    same = ast.dump(new_tree) == shape
                finally:
                    rhs.body = patch_body
                if same and not set(old).intersection(self.shared.get(name, ())):
                    return self.decode([self.lambdas.get(f"{name}:{lhs.lineno}", "0")])
                if same and lhs.body.lineno > lhs.lineno:
                    return self.decode([self.lambdas.get(f"{name}:{lhs.lineno}", "0")])
        a = bounded_statements(old, funcs, stmts)
        b = bounded_statements(new, new_funcs, new_stmts)
        if a and b and {(f, type(s)) for f, s in a} == {(f, type(s)) for f, s in b}:
            lines = {n for _, s in a for n in statement_header(s)}
            if lines.intersection(self.shared.get(name, ())):
                return None
            covered = self.lines.get(name, {})
            candidates = self.decode(
                [covered[str(n)] for n in lines if str(n) in covered]
            )
            if self.operands and candidates:
                return self.equivalent_comparisons(
                    tree, new_tree, shape, name, candidates
                )
            return candidates
        if len(path.parts) == 3 and path.parts[0] == "sqlglot":
            left, right = class_constants(tree), class_constants(new_tree)
            if all(n in left for n in old) and all(n in right for n in new):
                original = {
                    (left[n][0], left[n][1], literal_shape(left[n][2])) for n in old
                }
                changed = {
                    (right[n][0], right[n][1], literal_shape(right[n][2])) for n in new
                }
                if original == changed:
                    if path.parts[1] == "expressions" and all(
                        cls in self.expr_types and attr == "arg_types"
                        for cls, attr, _ in original
                    ):
                        return self.decode(
                            [self.expr_types[cls] for cls, _, _ in original]
                        )
                    if (
                        path.parts[1] in ("parsers", "generators", "typing", "dialects")
                        and path.stem != "dialect"
                    ):
                        family = self.dialect_family(path.stem)
                        attrs = {attr for _, attr, _ in original}
                        if (
                            family is not None
                            and attrs <= self.attribute_readers.keys()
                        ):
                            return family & self.decode(
                                [self.attribute_readers[k] for k in attrs]
                            )
                        return family
        return None
