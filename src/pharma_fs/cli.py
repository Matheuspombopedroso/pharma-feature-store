"""Command-line entry point: ``pharma-fs <command>``.

Commands map one-to-one to pipeline steps so each can be run on its own:

* ``generate``     write raw events + feature tables (fixed seed)
* ``apply``        register Feast definitions (like ``feast apply``)
* ``materialize``  incremental materialization to Redis (``--end`` optional)
* ``setup``        all three, in order (used by docker compose)
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from pharma_fs import config
from pharma_fs.data_gen import generate_raw, write_raw
from pharma_fs.features import build_feature_tables, write_feature_tables
from pharma_fs.materialize import run_incremental
from pharma_fs.store import apply_definitions, default_store


def cmd_generate() -> None:
    """Generate raw events and feature tables under ``config.DATA_DIR``."""
    t0 = time.perf_counter()
    raw = generate_raw()
    write_raw(raw)
    for name, df in raw.tables().items():
        print(f"raw/{name}: {len(df):,} rows")
    tables = build_feature_tables(raw)
    write_feature_tables(tables)
    for name, df in tables.items():
        print(f"features/{name}: {len(df):,} rows")
    print(f"generate: {time.perf_counter() - t0:.1f}s -> {config.DATA_DIR}")


def cmd_apply() -> None:
    """Register entities and feature views in the registry."""
    apply_definitions(default_store())
    print("apply: definitions registered")


def cmd_materialize(end: str | None) -> None:
    """Run incremental materialization, optionally up to an ISO timestamp."""
    end_dt = None
    if end:
        end_dt = datetime.fromisoformat(end)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
    res = run_incremental(default_store(), end_dt)
    print(f"materialize: up to {res.end_date.isoformat()} in {res.seconds:.2f}s")
    for name, (start, stop) in sorted(res.intervals.items()):
        print(f"  {name:28s} {start} -> {stop}")


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to a command."""
    parser = argparse.ArgumentParser(prog="pharma-fs")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    sub.add_parser("apply")
    m = sub.add_parser("materialize")
    m.add_argument("--end", help="ISO timestamp (UTC if naive); default: simulation end")
    sub.add_parser("setup")
    args = parser.parse_args(argv)

    if args.cmd == "generate":
        cmd_generate()
    elif args.cmd == "apply":
        cmd_apply()
    elif args.cmd == "materialize":
        cmd_materialize(args.end)
    elif args.cmd == "setup":
        cmd_generate()
        cmd_apply()
        cmd_materialize(None)


if __name__ == "__main__":
    main()
