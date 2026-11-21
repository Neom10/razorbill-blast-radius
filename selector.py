"""Select tests using only the mounted catalog and the independent patch text."""

import json
from coverage_map import Profile
from guard import fill, guard
import re
import sys
from collections import defaultdict
from pathlib import Path

HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
WORKER_MS = 2000


def diff_files(text):
    """Return paths, edited lines and context. Unknown patch forms use the full suite."""
    files = []
    for part in re.split(r"(?m)^diff --git ", text):
        if not part.strip():
            continue
        lines = part.splitlines()
        first = next((j for j, line in enumerate(lines) if HEADER.match(line)), None)
        if first is None:
            return None
        headers = lines[:first]
        paths = [line[6:] for line in headers if line.startswith(("--- a/", "+++ b/"))]
        if len(paths) != 2 or paths[0] != paths[1]:
            return None
        edited = []
        for line in lines[first:]:
            if line.startswith(("+", "-")):
                edited.append(line)
        if not edited:
            return None
        files.append((Path(paths[0]), edited, lines[first:]))
    return files or None


def ordinary_comments(edited, hunk):
    # Encoding declarations, type comments and tool directives may be significant.
    # Treat visible multiline strings, escaped continuations and directives cautiously.
    content = [line[1:].strip() for line in edited]
    if not all(line.startswith("# ") for line in content):
        return False
    if any(
        re.search(r"coding[:=]|type:|noqa|fmt:|pragma|doctest|pytest|ruff:", line, re.I)
        for line in content
    ):
        return False
    return not any(
        '"""' in line or "'''" in line or line.endswith(chr(92)) for line in hunk
    )


def words(name):
    return tuple(
        w.rstrip("s") for w in re.findall(r"[a-z0-9]+", name.lower()) if w != "test"
    )


def starts_with(a, b):
    return bool(b) and a[: len(b)] == b


class Catalog:
    def __init__(self, rows):
        self.rows = rows
        self.duration = {r["number"]: round(1000 * r["seconds"]) for r in rows}
        self.all = frozenset(self.duration)
        self.ordered = sorted(self.all, key=lambda i: (-self.duration[i], i))
        self.files = defaultdict(set)
        self.methods = {}
        self.directories = defaultdict(set)
        self.parents = {}
        self.cheap = {i for i in self.all if self.duration[i] <= WORKER_MS}
        for row in rows:
            i = row["number"]
            location = Path(row["file"])
            key = words(location.stem)
            self.files[key].add(i)
            self.directories[location.parent].add(i)
            self.parents[key] = location.parent
            self.methods[i] = words(row["id"].split("::")[-1].split("[")[0])
        self.schedule_cache = {}

    def affected(self, path, edits, hunk):
        stem = words(path.stem.split("__")[0])
        if path.suffix == ".py" and ordinary_comments(edits, hunk):
            return set()
        if path.parts[:2] == ("tests", "fixtures"):
            group = self.files.get(words(path.parent.name), set())
            named = {
                i
                for i, method in self.methods.items()
                if starts_with(method, stem) or starts_with(stem, method)
            }
            return named if named else (group or self.all)
        if path.parts[:1] == ("tests",) and path.suffix == ".py":
            return self.files.get(stem, self.all)
        if path.parts[:1] != ("sqlglot",) or path.suffix != ".py":
            return self.all
        file_tests = self.files.get(stem)
        # A shared package or an unpaired implementation module is a poor
        # basis for ruling out downstream tests in a different suite.
        if len(path.parts) < 3 or not file_tests or stem == words(path.parent.name):
            return self.all
        # Sibling suites in a family can overlap. Retain their inexpensive
        # tests; shared suites outside that family keep the wider allowance.
        siblings = self.directories[self.parents[stem]]
        direct = file_tests | {
            i
            for i in self.cheap
            if i not in siblings or self.duration[i] <= WORKER_MS // 16
        }
        symbols = set()
        for line in edits:
            symbols.update(words(line[1:]))
        direct.update(
            i
            for i, method in self.methods.items()
            if method
            and (
                starts_with(method, stem)
                or (len(method) > 1 and set(method) <= symbols)
            )
        )
        return direct

    def workers(self, ids):
        key = frozenset(ids)
        if key in self.schedule_cache:
            return self.schedule_cache[key]
        jobs = [i for i in self.ordered if i in key]
        if not jobs:
            return []
        total = sum(self.duration[i] for i in jobs)
        longest = self.duration[jobs[0]]
        best = None
        best_cost = float("inf")
        for k in range(1, min(8, len(jobs)) + 1):
            if max(longest, (total + k - 1) // k) + WORKER_MS * k >= best_cost:
                continue
            loads = [0] * k
            bins = [[] for _ in loads]
            for i in jobs:
                w = loads.index(min(loads))
                bins[w].append(i)
                loads[w] += self.duration[i]
            bins = [b for b in bins if b]
            cost = max(loads) + WORKER_MS * len(bins)
            if cost < best_cost:
                best, best_cost = bins, cost
        self.schedule_cache[key] = best
        return best


def run(data):
    catalog = Catalog(json.loads((data / "tests.json").read_text())["tests"])
    profile = Profile.load(Path(__file__).with_name("profile.json.gz"), catalog.rows)
    for patch in sorted((data / "commits").glob("*.patch")):
        files = diff_files(patch.read_text(errors="replace"))
        selected = set()
        related = set()
        if files:
            for path, edits, hunk in files:
                related.update(catalog.files.get(words(path.stem), ()))
                related.update(catalog.files.get(words(path.parent.name), ()))
                if profile:
                    selected.update(guard(catalog, profile, path, hunk))
                inferred = profile.select(path, hunk) if profile else None
                selected.update(
                    catalog.affected(path, edits, hunk)
                    if inferred is None
                    else inferred
                )
        else:
            selected.update(catalog.all)
        print(
            json.dumps(
                {"commit": patch.stem, "workers": fill(catalog, selected, related)},
                separators=(",", ":"),
            )
        )


if __name__ == "__main__":
    run(Path(sys.argv[1]) if len(sys.argv) == 2 else Path("/data"))
