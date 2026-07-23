"""Offline, fail-fast Leiden projection for derived memory threads.

This is deliberately a CLI, not an MCP tool.  Normal agents cannot trigger
clustering.  The default is a dry run; ``--persist`` activates a derived weak
signal even with zero human labels and reports that cold-start state as
unvalidated.
"""

from __future__ import annotations

import argparse
import json

from db_utils import DB_PATH, get_conn
from memory_thread_clustering import (
    DEFAULT_PRIMARY_RESOLUTION,
    DEFAULT_RESOLUTIONS,
    DEFAULT_SEED,
    run_memory_thread_projection,
)
from link_suggestions import evaluate_link_suggestions
from schema import init_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a deterministic offline Leiden memory-thread projection."
    )
    parser.add_argument("--db", default=DB_PATH, help="SQLite memory database path.")
    parser.add_argument(
        "--resolution",
        action="append",
        type=float,
        dest="resolutions",
        help="Resolution to test; repeat for multiple values.",
    )
    parser.add_argument(
        "--primary-resolution",
        type=float,
        default=DEFAULT_PRIMARY_RESOLUTION,
        help="Resolution stored as the active projection.",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--no-vector",
        action="store_true",
        help="Build the sparse graph without optional vector edges.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Persist derived weak-signal memberships (default: dry run).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    init_db(args.db)
    resolutions = tuple(args.resolutions or DEFAULT_RESOLUTIONS)
    # Graph construction and multi-seed Leiden runs are read-heavy and can
    # take tens of seconds on the live database.  The projection explicitly
    # releases that read snapshot before acquiring a short IMMEDIATE writer
    # transaction for the derived rows.
    with get_conn(args.db) as conn:
        report = run_memory_thread_projection(
            conn,
            resolutions=resolutions,
            primary_resolution=args.primary_resolution,
            seed=args.seed,
            include_vector=not args.no_vector,
            persist=args.persist,
            restart_transaction_before_persist=args.persist,
        )
        report["link_metrics"] = evaluate_link_suggestions(conn, k=5)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
