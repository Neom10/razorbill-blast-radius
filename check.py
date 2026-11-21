"""Smoke checks for selection, schedules and the optional profile reader."""
import difflib
from pathlib import Path
from coverage_map import Profile, apply_hunks
from selector import Catalog, diff_files, ordinary_comments

rows = [{"number": i, "id": f"tests/test_example.py::test_{i}",
         "file":"tests/test_example.py", "seconds": 9} for i in range(16)]
catalog = Catalog(rows)
assert len(catalog.workers(catalog.all)) == 8
assert catalog.workers(set()) == []
assert sorted(i for w in catalog.workers(catalog.all) for i in w) == list(range(16))

source = "def f():\n    return 1\n\ndef g():\n    return 2\n"
name = "sqlglot/x.py"
profile = Profile({"nodes":[r["id"] for r in rows], "passed":[r["id"] for r in rows],
                   "sources":{name:source}, "lines":{name:{"2":"1","5":"2"}},
                   "reads":{"tests/data.sql":"4"},"shared":{}},rows)

def hunk(patched):
    diff = list(difflib.unified_diff(source.splitlines(), patched.splitlines(),
                                     "a/"+name,"b/"+name,lineterm=""))
    return diff_files("\n".join(diff))[0][2]

diff = hunk(source.replace("return 1", "return 3"))
assert apply_hunks(source,diff)[0].splitlines() == source.replace("return 1","return 3").splitlines()
assert profile.select(Path(name),diff) == {0}
assert profile.select(Path("tests/data.sql"),[]) == {2}
assert profile.select(Path(name),hunk(source.replace("f():","f(:"))) == catalog.all
assert profile.select(Path(name),hunk(source + "# ordinary prose\n")) == set()
assert not ordinary_comments(["+# type: ignore"],[])
assert diff_files("Binary files differ") is None
print("Selector, profile-reader and scheduling checks passed.")
