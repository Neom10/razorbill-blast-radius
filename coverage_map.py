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
            start = int(match[1]) if match[2] == "0" else int(match[1])-1
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
                removed.append(pos+1)
            pos += 1
        elif line.startswith("+"):
            added.append(len(output)+1)
            output.append(line[1:])
    return "\n".join(output+original[pos:]), removed, added


@lru_cache(maxsize=64)
def analyze(source):
    tree = ast.parse(source)
    funcs = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                   key=lambda f: f.end_lineno-f.lineno)
    return ast.dump(tree), funcs


class Profile:
    @classmethod
    def load(cls, path, rows):
        if not path.exists():
            return None
        with gzip.open(path, "rt") as fp:
            data = json.load(fp)
        return cls(data, rows)

    def __init__(self, data, rows):
        self.sources = data["sources"]
        self.lines = data["lines"]
        self.reads = data["reads"]
        self.shared = data["shared"]
        ids = {r["id"]:r["number"] for r in rows}
        self.all = set(ids.values())
        self.lookup = [ids.get(node) for node in data["nodes"]]
        self.unknown = self.all - {ids[n] for n in data["passed"] if n in ids}

    def decode(self, masks):
        combined = 0
        for mask in masks:
            combined |= int(mask, 16)
        chosen = self.unknown.copy()
        while combined:
            bit = combined & -combined
            i = self.lookup[bit.bit_length()-1]
            if i is not None:
                chosen.add(i)
            combined ^= bit
        return chosen

    def select(self, path, hunk):
        name = str(path)
        if path.suffix != ".py":
            mask = self.reads.get(name)
            return self.decode([mask]) if mask and name not in self.shared else None
        if name not in self.sources:
            return None
        try:
            source = self.sources[name]
            patched, old, new = apply_hunks(source, hunk)
            old_ast, old_funcs = analyze(source)
            new_ast, new_funcs = analyze(patched)
            compile(patched, name, "exec")
        except (SyntaxError, ValueError, RecursionError):
            return self.all
        if old_ast == new_ast:
            return self.decode([self.reads.get(name, "0")]) if name not in self.shared else None
        if not old or not new:
            return None
        # Only narrow edits that stay in the same function bodies on both sides.
        def owners(lines, funcs):
            result = []
            for line in lines:
                f = next((f for f in funcs if f.body[0].lineno <= line <= f.end_lineno), None)
                if f is None:
                    return []
                result.append(f)
            return result
        before, after = owners(old, old_funcs), owners(new, new_funcs)
        if not before or not after or {f.name for f in before} != {f.name for f in after}:
            return None
        covered = self.lines.get(name, {})
        shared = set(self.shared.get(name, []))
        masks = []
        for f in set(before):
            for n in range(f.body[0].lineno, f.end_lineno+1):
                if n in shared:
                    return None
                if str(n) in covered:
                    masks.append(covered[str(n)])
        return self.decode(masks)
