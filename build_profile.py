"""Profile the unchanged checkout; split collection deterministically across processes."""

import type_coverage
import concurrent.futures
import pickle
import tracemalloc
from contextlib import AbstractContextManager
import gzip
import json
import os
import sys
import time
import zlib
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path.cwd()
PREFIX = str(ROOT) + os.sep
monitor = sys.monitoring
tool = monitor.COVERAGE_ID
active = 0
hits = defaultdict(dict)
lambda_hits = defaultdict(int)
direct_reads = defaultdict(int)
reads = defaultdict(int)
dialects = defaultdict(int)
expr_types = defaultdict(int)
seen_types = set()
seen_dialects = set()
shared = defaultdict(set)


@lru_cache(maxsize=32768)
def relative(filename):
    path = os.path.abspath(filename)
    if path.startswith(PREFIX):
        name = path[len(PREFIX) :]
        if name.startswith(("sqlglot/", "tests/")):
            return name
    return ""


def trace_line(code, line):
    name = relative(code.co_filename)
    if name and not tracemalloc.is_tracing():
        if active:
            if code.co_name == "<lambda>":
                lambda_hits[f"{name}:{code.co_firstlineno}"] |= active
            by_line = hits[name]
            by_line[line] = by_line.get(line, 0) | active
        else:
            shared[name].add(line)
    return monitor.DISABLE


def audit(event, args):
    if event == "open" and isinstance(args[0], str):
        name = relative(args[0])
        if name:
            if active:
                reads[name] |= active
                if name.endswith(".py"):
                    frame = sys._getframe(1)
                    while frame:
                        module = frame.f_globals.get("__name__", "")
                        if module.startswith(
                            (
                                "importlib.",
                                "_frozen_importlib",
                                "type_coverage",
                                "_pytest.assertion.rewrite",
                            )
                        ):
                            break
                        if module.startswith(
                            ("tests.", "sqlglot.", "inspect", "linecache")
                        ):
                            direct_reads[name] |= active
                            break
                        frame = frame.f_back
            else:
                shared[name].add(0)


def start():
    monitor.use_tool_id(tool, "rwx-baseline")
    monitor.register_callback(tool, monitor.events.LINE, trace_line)
    sys.addaudithook(audit)
    monitor.set_events(tool, monitor.events.LINE)


def switch(mask):
    global active
    active = mask
    type_coverage.switch(mask)
    seen_types.clear()
    seen_dialects.clear()
    monitor.restart_events()


def stop():
    monitor.set_events(tool, 0)
    monitor.register_callback(tool, monitor.events.LINE, None)
    monitor.free_tool_id(tool)


@lru_cache(None)
def dialect_modules(cls):
    return tuple(
        c.__module__.rsplit(".", 1)[-1]
        for c in cls.__mro__
        if c.__module__.startswith("sqlglot.dialects.")
    )


class TracedPool(AbstractContextManager):
    """Trace task code and its serialization within the originating baseline test."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = fn(*args, **kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, *args, **kwargs):
        pass

    def __exit__(self, *args):
        self.shutdown()


def observe_types():
    from sqlglot import exp

    base = exp.Expr
    pending = [base]
    known = set()
    while pending:
        cls = pending.pop()
        if cls not in known:
            known.add(cls)
            expr_types[cls.__name__] |= 0
            pending.extend(cls.__subclasses__())
    original = base.__init__

    def traced_init(self, *args, **kwargs):
        cls = type(self)
        if active and cls not in seen_types:
            seen_types.add(cls)
            for parent in cls.__mro__:
                if parent in known:
                    expr_types[parent.__name__] |= active
        original(self, *args, **kwargs)

    base.__init__ = traced_init


class Plugin:
    def __init__(self, shard, shards, deadline):
        self.shard, self.shards, self.deadline = shard, shards, deadline
        self.passed, self.failed = set(), set()
        self.positions = {}

    def pytest_collection_modifyitems(self, items):
        self.positions = {
            node: i for i, node in enumerate(sorted({item.nodeid for item in items}))
        }
        items[:] = [
            item
            for item in items
            if zlib.crc32(item.nodeid.encode()) % self.shards == self.shard
        ]

    def pytest_collection_finish(self, session):
        self.session = session
        from sqlglot.dialects.dialect import Dialect

        original = Dialect.__dict__.get("get_or_raise")
        if isinstance(original, classmethod):
            observe_types()

            def observe(cls, *args, **kwargs):
                result = original.__func__(cls, *args, **kwargs)
                kind = type(result)
                if active and kind not in seen_dialects:
                    seen_dialects.add(kind)
                    for name in dialect_modules(kind):
                        dialects[name] |= active
                return result

            Dialect.get_or_raise = classmethod(observe)

    def pytest_runtest_logstart(self, nodeid, location):
        switch(1 << self.positions[nodeid])

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        switch(0)
        if time.monotonic() > self.deadline:
            self.session.shouldstop = "Reserve time to export the baseline map"


def dump(path, plugin):
    payload = dict(
        nodes=list(plugin.positions),
        passed=sorted(plugin.passed - plugin.failed),
        lines={
            f: {n: format(mask, "x") for n, mask in lines.items()}
            for f, lines in hits.items()
        },
        reads={f: format(mask, "x") for f, mask in reads.items()},
        direct_reads={k: format(v, "x") for k, v in direct_reads.items()},
        lambda_hits={k: format(v, "x") for k, v in lambda_hits.items()},
        operands={
            site: {kind: format(mask, "x") for kind, mask in values.items()}
            for site, values in type_coverage.table.items()
        },
        expr_types={k: format(v, "x") for k, v in expr_types.items()},
        dialects={k: format(v, "x") for k, v in dialects.items()},
        shared={f: sorted(lines) for f, lines in shared.items()},
    )
    with gzip.open(path, "wt", compresslevel=1) as fp:
        json.dump(payload, fp, separators=(",", ":"))


if __name__ == "__main__":
    import pytest

    output, shard, shards, deadline = sys.argv[1:]
    plugin = Plugin(int(shard), int(shards), float(deadline))
    if os.getenv("RWX_TRACE_POOL") == "1":
        concurrent.futures.ProcessPoolExecutor = TracedPool
    type_coverage.install()
    start()
    try:
        status = pytest.main(
            [
                "tests",
                "-q",
                "--disable-warnings",
                "--continue-on-collection-errors",
                "-p",
                "no:cacheprovider",
            ],
            plugins=[plugin],
        )
    finally:
        stop()
    if not plugin.passed:
        raise RuntimeError(f"pytest yielded no passing tests (status {status})")
    dump(output, plugin)
