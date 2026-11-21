"""Collect a line/fixture map with CPython monitoring and the organizer's pytest."""
import gzip
import json
import os
from pathlib import Path
import sys
from collections import defaultdict
from functools import lru_cache

import pytest

ROOT = Path.cwd()
PREFIX = str(ROOT) + os.sep
monitor = sys.monitoring
tool = monitor.COVERAGE_ID
active = 0
nodes, passed = [], set()
unknown = set()
hits = defaultdict(dict)
reads = defaultdict(int)
shared = defaultdict(set)


@lru_cache(maxsize=32768)
def relative(filename):
    path = os.path.abspath(filename)
    if path.startswith(PREFIX):
        name = path[len(PREFIX):]
        if name.startswith(("sqlglot/", "tests/")):
            return name
    return ""


def trace_line(code, line):
    name = relative(code.co_filename)
    if name:
        if active:
            lines = hits[name]
            lines[line] = lines.get(line, 0) | active
        else:
            shared[name].add(line)
    return monitor.DISABLE


def audit(event, args):
    if event == "open" and isinstance(args[0], str):
        name = relative(args[0])
        if name:
            if active:
                reads[name] |= active
            else:
                shared[name].add(0)


class Plugin:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        global active
        nodes.append(item.nodeid)
        active = 1 << (len(nodes)-1)
        monitor.restart_events()
        try:
            yield
        finally:
            active = 0

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            unknown.add(report.nodeid)
        elif report.when == "call" and report.passed:
            passed.add(report.nodeid)


def main():
    out = Path(sys.argv[1])
    monitor.use_tool_id(tool, "profile")
    monitor.register_callback(tool, monitor.events.LINE, trace_line)
    sys.addaudithook(audit)
    monitor.set_events(tool, monitor.events.LINE)
    try:
        result = pytest.main(["-q", "--disable-warnings", "--continue-on-collection-errors", "tests"],
                             plugins=[Plugin()])
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)
    if result not in (0, 1) or not passed:
        raise RuntimeError(f"profiling failed (pytest {result})")
    data = dict(sources={str(p):p.read_text() for root in ("sqlglot", "tests")
                         for p in Path(root).rglob("*.py")},
                lines={name:{line:format(mask, "x") for line,mask in rows.items()}
                       for name,rows in hits.items()},
                reads={name:format(mask,"x") for name,mask in reads.items()},
                shared={name:sorted(lines) for name,lines in shared.items()},
                nodes=nodes, passed=sorted(passed-unknown))
    with gzip.open(out, "wt", compresslevel=6) as fp:
        json.dump(data, fp, separators=(",", ":"))
    print(f"{len(data['passed'])} profiled tests, {out.stat().st_size} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
