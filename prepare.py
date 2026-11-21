"""Build the immutable baseline map. Keep a usable selector if profiling is unavailable."""

import ast
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

REV = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
OUT = Path("/opt/out")
ROOT = Path("/tmp/sqlglot")
SHARDS = 3


def merge(parts, root):
    data = dict(
        nodes=parts[0]["nodes"],
        passed=set(),
        lines={},
        reads={},
        direct_reads={},
        lambda_hits={},
        shared={},
        dialects={},
        expr_types={},
        operands={},
    )
    for part in parts:
        if data["nodes"] != part["nodes"]:
            continue
        data["passed"].update(part["passed"])
        for field in ("reads", "dialects", "expr_types", "direct_reads", "lambda_hits"):
            for key, val in part.get(field, {}).items():
                data[field][key] = data[field].get(key, 0) | int(val, 16)
        for site, values in part.get("operands", {}).items():
            target = data["operands"].setdefault(site, {})
            for kind, mask in values.items():
                target[kind] = target.get(kind, 0) | int(mask, 16)
        for name, values in part["lines"].items():
            table = data["lines"].setdefault(name, {})
            for line, mask in values.items():
                table[line] = table.get(line, 0) | int(mask, 16)
        for name, lines in part["shared"].items():
            data["shared"].setdefault(name, set()).update(lines)
    data["sources"] = {
        str(p.relative_to(root)): p.read_text(encoding="utf-8")
        for folder in ("sqlglot", "tests")
        for p in (root / folder).rglob("*.py")
    }
    # Index uppercase attribute reads on original executed lines. Mark a
    # setting as shared when a metaclass or module can derive cached values.
    reader_masks = {}
    escaping = set()
    for name, source in data["sources"].items():
        parsed = ast.parse(source)
        covered = data["lines"].get(name, {})
        shared = data["shared"].get(name, set())
        for n in ast.walk(parsed):
            if (
                isinstance(n, ast.Attribute)
                and n.attr.isupper()
                and isinstance(n.ctx, ast.Load)
            ):
                reader_masks[n.attr] = reader_masks.get(n.attr, 0) | covered.get(
                    str(n.lineno), 0
                )
                if n.lineno in shared:
                    escaping.add(n.attr)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in (
                "__init_subclass__",
                "__new__",
            ):
                escaping.update(
                    x.attr for x in ast.walk(n) if isinstance(x, ast.Attribute)
                )
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in ("getattr", "hasattr")
                and len(n.args) >= 2
            ):
                key = n.args[1]
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    escaping.add(key.value)
    data["attribute_readers"] = {
        k: format(v, "x") for k, v in reader_masks.items() if k not in escaping
    }
    # Some consumers access a class directly without constructing an instance.
    # Add baseline coverage of explicit class-name uses, including test code.
    types = data["expr_types"]
    for name, source in data["sources"].items():
        covered = data["lines"].get(name, {})
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Attribute):
                symbol = node.attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                symbol = node.id
            else:
                continue
            if symbol in types:
                types[symbol] |= covered.get(str(node.lineno), 0)
    data["operands"] = {
        s: {k: format(v, "x") for k, v in vs.items()}
        for s, vs in data["operands"].items()
    }
    data["passed"] = sorted(data["passed"])
    for field in ("reads", "dialects", "expr_types", "direct_reads", "lambda_hits"):
        data[field] = {k: format(v, "x") for k, v in data[field].items()}
    data["lines"] = {
        f: {k: format(v, "x") for k, v in rows.items()}
        for f, rows in data["lines"].items()
    }
    data["shared"] = {f: sorted(ns) for f, ns in data["shared"].items()}
    return data


def build():
    started = time.monotonic()
    archive = Path("/tmp/pinned.tgz")
    urllib.request.urlretrieve(
        f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{REV}", archive
    )
    with tarfile.open(archive) as tar:
        tar.extractall("/tmp", filter="data")
    Path(f"/tmp/sqlglot-{REV}").rename(ROOT)
    deps = [
        "pytest",
        "pytest-timeout",
        "numpy",
        "pandas",
        "duckdb",
        "python-dateutil",
        "pytz",
        "PyYAML",
        "Jinja2",
        "typing_extensions",
        "tzdata",
        "setuptools",
        "setuptools-scm",
    ]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "--no-cache-dir",
            "--retries",
            "1",
            "-c",
            "/opt/requirements.lock",
            *deps,
        ],
        check=True,
        timeout=130,
    )
    deadline = started + 390
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT),
        SKIP_INTEGRATION="0",
        RWX_TRACE_POOL="1",
        PYTHON_CPU_COUNT="2",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    workers = []
    for i in range(SHARDS):
        output = Path(f"/tmp/profile-{i}.gz")
        checkout = Path(f"/tmp/worktree-{i}")
        shutil.copytree(ROOT, checkout)
        proc = subprocess.Popen(
            [
                sys.executable,
                "/opt/build_profile.py",
                str(output),
                str(i),
                str(SHARDS),
                str(deadline),
            ],
            cwd=checkout,
            env=dict(env, PYTHONPATH=str(checkout)),
        )
        workers.append((proc, output))
    parts = []
    for proc, output in workers:
        try:
            proc.wait(timeout=max(1, started + 425 - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.returncode == 0 and output.exists():
            with gzip.open(output, "rt") as fp:
                parts.append(json.load(fp))
    if not parts:
        raise RuntimeError("No profiling shard completed")
    data = merge(parts, ROOT)
    with gzip.open(OUT / "partial.gz", "wt", compresslevel=3) as fp:
        json.dump(data, fp, separators=(",", ":"))
    (OUT / "partial.gz").replace(OUT / "profile.json.gz")
    print(f"Profiled {len(data['passed'])} baseline tests", flush=True)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    try:
        build()
    except Exception as exc:
        print(f"Using catalog fallback: {exc}", file=sys.stderr)
