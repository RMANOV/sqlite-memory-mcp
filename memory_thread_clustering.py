"""Fail-fast offline Leiden projection for memory threads.

This module builds a sparse, auditable graph and stores only derived community
memberships.  It never creates task↔entity links.  An active projection is only
a weak ``same_community`` signal in ``link_suggestions``; it does not create a
new agent-facing stream or expand task digests.  The projection may start with
zero human labels so usefulness can be tested immediately; zero-label output is
reported as unvalidated rather than being mistaken for measured quality.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from db_utils import fts_query, now_iso, tokenize_for_similarity
from link_suggestions import LINK_MODEL_VERSION, decision_progress, normalize_phrase

CLUSTER_MODEL_VERSION = "memory-threads-leiden-v1"
DEFAULT_RESOLUTIONS = (0.6, 1.0, 1.4, 2.0)
DEFAULT_SEED = 1729
DEFAULT_SEED_OFFSETS = (0, 101, 307)
DEFAULT_PRIMARY_RESOLUTION = 1.0

Node = tuple[str, str]
EdgeKey = tuple[Node, Node]


@dataclass
class SparseMemoryGraph:
    """Deterministic weighted graph plus per-edge provenance receipts."""

    edges: dict[EdgeKey, float] = field(default_factory=dict)
    reasons: dict[EdgeKey, set[str]] = field(default_factory=lambda: defaultdict(set))

    @staticmethod
    def _key(left: Node, right: Node) -> EdgeKey:
        if left == right:
            raise ValueError("self-loop is not a memory-thread edge")
        return (left, right) if left < right else (right, left)

    def add(self, left: Node, right: Node, weight: float, reason: str) -> None:
        if left == right or not math.isfinite(weight) or weight <= 0:
            return
        key = self._key(left, right)
        self.edges[key] = max(self.edges.get(key, 0.0), min(float(weight), 1.0))
        self.reasons[key].add(reason)

    @property
    def nodes(self) -> list[Node]:
        return sorted({node for edge in self.edges for node in edge})


def _safe_task_fts_query(text: str) -> str:
    tokens = sorted(tokenize_for_similarity(text))[:64]
    return fts_query(" ".join(tokens)) if tokens else ""


def _task_text(row: sqlite3.Row) -> str:
    return " ".join(
        str(value)
        for value in (row["title"], row["description"], row["notes"])
        if value
    )


def _add_structural_edges(
    conn: sqlite3.Connection,
    graph: SparseMemoryGraph,
    tasks: list[sqlite3.Row],
    entities: list[sqlite3.Row],
) -> None:
    task_ids = {str(row["id"]) for row in tasks}
    entity_ids = {int(row["id"]) for row in entities}

    for row in conn.execute(
        "SELECT task_id, entity_id FROM task_entity_links ORDER BY task_id, entity_id"
    ):
        if str(row["task_id"]) in task_ids and int(row["entity_id"]) in entity_ids:
            graph.add(
                ("task", str(row["task_id"])),
                ("entity", str(row["entity_id"])),
                1.0,
                "explicit_task_entity_link",
            )

    for row in tasks:
        if row["parent_id"] and str(row["parent_id"]) in task_ids:
            graph.add(
                ("task", str(row["id"])),
                ("task", str(row["parent_id"])),
                1.0,
                "task_parent",
            )

    for row in conn.execute(
        "SELECT from_id, to_id, relation_type FROM relations "
        "ORDER BY from_id, to_id, relation_type"
    ):
        if int(row["from_id"]) in entity_ids and int(row["to_id"]) in entity_ids:
            graph.add(
                ("entity", str(row["from_id"])),
                ("entity", str(row["to_id"])),
                0.9,
                f"entity_relation:{row['relation_type']}",
            )

    for row in tasks:
        project = normalize_phrase(row["project"])
        if project:
            graph.add(
                ("task", str(row["id"])),
                ("project", project),
                0.35,
                "same_project_hub",
            )
    for row in entities:
        project = normalize_phrase(row["project"])
        if project:
            graph.add(
                ("entity", str(row["id"])),
                ("project", project),
                0.35,
                "same_project_hub",
            )

    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provenance_links'"
    ).fetchone():
        return
    for row in conn.execute(
        "SELECT subject_kind, subject_ref, source_kind, source_ref "
        "FROM provenance_links "
        "WHERE subject_kind IN ('task', 'entity') "
        "ORDER BY subject_kind, subject_ref, source_kind, source_ref"
    ):
        subject_kind = str(row["subject_kind"])
        subject_ref = str(row["subject_ref"])
        if subject_kind == "task" and subject_ref not in task_ids:
            continue
        if subject_kind == "entity":
            try:
                if int(subject_ref) not in entity_ids:
                    continue
            except ValueError:
                continue
        source_ref = f"{row['source_kind']}:{row['source_ref']}"
        graph.add(
            (subject_kind, subject_ref),
            ("source", source_ref),
            0.55,
            "shared_provenance_source",
        )


def _add_lexical_task_edges(
    conn: sqlite3.Connection,
    graph: SparseMemoryGraph,
    tasks: list[sqlite3.Row],
    *,
    top_k: int,
    minimum_jaccard: float,
) -> None:
    task_by_rowid = {int(row["rowid"]): row for row in tasks}
    tokens_by_id = {
        str(row["id"]): set(tokenize_for_similarity(_task_text(row))) for row in tasks
    }
    for task in tasks:
        task_id = str(task["id"])
        left_tokens = tokens_by_id[task_id]
        if not left_tokens:
            continue
        query = _safe_task_fts_query(_task_text(task))
        if not query:
            continue
        try:
            candidates = conn.execute(
                "SELECT rowid, rank FROM tasks_fts WHERE tasks_fts MATCH ? "
                "AND rowid != ? ORDER BY rank, rowid LIMIT ?",
                (query, int(task["rowid"]), top_k * 4),
            ).fetchall()
        except sqlite3.Error:
            return
        accepted = 0
        for candidate in candidates:
            other = task_by_rowid.get(int(candidate["rowid"]))
            if other is None:
                continue
            other_id = str(other["id"])
            if task_id >= other_id:
                continue
            right_tokens = tokens_by_id[other_id]
            union = left_tokens | right_tokens
            jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
            if jaccard < minimum_jaccard:
                continue
            graph.add(
                ("task", task_id),
                ("task", other_id),
                min(0.75, 0.25 + 0.5 * jaccard),
                f"lexical_jaccard:{jaccard:.4f}",
            )
            accepted += 1
            if accepted >= top_k:
                break


def _add_vector_task_edges(
    conn: sqlite3.Connection,
    graph: SparseMemoryGraph,
    tasks: list[sqlite3.Row],
    *,
    top_k: int,
    maximum_distance: float,
) -> dict[str, Any]:
    try:
        from vec_search import load_vec
    except ImportError:
        return {"enabled": False, "reason": "sqlite_vec_unavailable", "edges": 0}
    if not load_vec(conn):
        return {"enabled": False, "reason": "sqlite_vec_unavailable", "edges": 0}
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_embeddings'"
    ).fetchone()
    if table is None:
        return {"enabled": False, "reason": "task_embeddings_missing", "edges": 0}

    task_by_rowid = {int(row["rowid"]): str(row["id"]) for row in tasks}
    before = len(graph.edges)
    for rowid, task_id in sorted(task_by_rowid.items()):
        embedding_row = conn.execute(
            "SELECT embedding FROM task_embeddings WHERE rowid = ?", (rowid,)
        ).fetchone()
        if embedding_row is None:
            continue
        try:
            neighbors = conn.execute(
                "SELECT rowid, distance FROM task_embeddings "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance, rowid",
                (embedding_row["embedding"], top_k + 1),
            ).fetchall()
        except sqlite3.Error as exc:
            return {
                "enabled": False,
                "reason": f"vector_query_failed:{exc}",
                "edges": len(graph.edges) - before,
            }
        for neighbor in neighbors:
            other_rowid = int(neighbor["rowid"])
            other_id = task_by_rowid.get(other_rowid)
            if other_id is None or other_id == task_id or task_id >= other_id:
                continue
            distance = float(neighbor["distance"])
            if distance > maximum_distance:
                continue
            similarity = max(0.0, 1.0 - distance / maximum_distance)
            graph.add(
                ("task", task_id),
                ("task", other_id),
                0.35 + 0.45 * similarity,
                f"vector_distance:{distance:.4f}",
            )
    return {"enabled": True, "reason": None, "edges": len(graph.edges) - before}


def build_sparse_memory_graph(
    conn: sqlite3.Connection,
    *,
    lexical_top_k: int = 3,
    minimum_jaccard: float = 0.15,
    include_vector: bool = True,
    vector_top_k: int = 5,
    maximum_vector_distance: float = 0.35,
) -> tuple[SparseMemoryGraph, dict[str, Any]]:
    """Build a bounded sparse graph; never uses all-pairs comparisons."""
    tasks = conn.execute(
        "SELECT rowid, id, title, description, notes, project, parent_id "
        "FROM tasks WHERE status != 'cancelled' ORDER BY id"
    ).fetchall()
    entities = conn.execute(
        "SELECT id, name, project FROM entities ORDER BY id"
    ).fetchall()
    graph = SparseMemoryGraph()
    _add_structural_edges(conn, graph, tasks, entities)
    structural_edges = len(graph.edges)
    _add_lexical_task_edges(
        conn,
        graph,
        tasks,
        top_k=max(0, min(int(lexical_top_k), 10)),
        minimum_jaccard=max(0.0, min(float(minimum_jaccard), 1.0)),
    )
    lexical_edges = len(graph.edges) - structural_edges
    vector_report = {"enabled": False, "reason": "disabled", "edges": 0}
    if include_vector:
        vector_report = _add_vector_task_edges(
            conn,
            graph,
            tasks,
            top_k=max(1, min(int(vector_top_k), 20)),
            maximum_distance=max(0.01, min(float(maximum_vector_distance), 2.0)),
        )
    return graph, {
        "task_count": len(tasks),
        "entity_count": len(entities),
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "structural_edges": structural_edges,
        "lexical_edges": lexical_edges,
        "vector": vector_report,
    }


def _adjusted_rand_agreement(left: list[int], right: list[int]) -> float:
    """Adjusted Rand agreement in O(n), corrected for chance partitions."""
    if len(left) != len(right):
        raise ValueError("partition sizes differ")
    n = len(left)
    if n < 2:
        return 1.0
    left_counts = Counter(left)
    right_counts = Counter(right)
    joint_counts = Counter(zip(left, right, strict=True))

    def choose2(value: int) -> int:
        return value * (value - 1) // 2

    same_both = float(sum(choose2(value) for value in joint_counts.values()))
    same_left = float(sum(choose2(value) for value in left_counts.values()))
    same_right = float(sum(choose2(value) for value in right_counts.values()))
    total = choose2(n)
    if total == 0:
        return 1.0
    expected = same_left * same_right / total
    maximum = 0.5 * (same_left + same_right)
    denominator = maximum - expected
    if denominator == 0:
        return 1.0 if left == right else 0.0
    return (same_both - expected) / denominator


def _run_leiden(
    graph: SparseMemoryGraph,
    *,
    resolutions: tuple[float, ...],
    seed: int,
) -> dict[str, Any]:
    try:
        import igraph as ig
        import leidenalg
    except ImportError as exc:
        raise RuntimeError(
            "Leiden dependencies unavailable; install the 'community' optional extra"
        ) from exc

    nodes = graph.nodes
    node_index = {node: index for index, node in enumerate(nodes)}
    edge_keys = sorted(graph.edges)
    igraph = ig.Graph(
        n=len(nodes),
        edges=[(node_index[left], node_index[right]) for left, right in edge_keys],
        directed=False,
    )
    weights = [graph.edges[key] for key in edge_keys]
    memberships: dict[str, list[int]] = {}
    seed_stability: dict[str, float] = {}
    community_counts: dict[str, int] = {}
    seeds = tuple(seed + offset for offset in DEFAULT_SEED_OFFSETS)

    for resolution in resolutions:
        key = f"{resolution:.6g}"
        runs: list[list[int]] = []
        for run_seed in seeds:
            partition = leidenalg.find_partition(
                igraph,
                leidenalg.RBConfigurationVertexPartition,
                weights=weights,
                resolution_parameter=float(resolution),
                seed=int(run_seed),
            )
            runs.append([int(value) for value in partition.membership])
        memberships[key] = runs[0]
        seed_stability[key] = round(
            sum(_adjusted_rand_agreement(runs[0], other) for other in runs[1:])
            / max(1, len(runs) - 1),
            6,
        )
        community_counts[key] = len(set(runs[0]))

    resolution_keys = list(memberships)
    cross_resolution: dict[str, float] = {}
    for left, right in zip(resolution_keys, resolution_keys[1:], strict=False):
        cross_resolution[f"{left}->{right}"] = round(
            _adjusted_rand_agreement(memberships[left], memberships[right]), 6
        )
    return {
        "nodes": nodes,
        "memberships": memberships,
        "seed_stability": seed_stability,
        "cross_resolution_stability": cross_resolution,
        "community_counts": community_counts,
        "seeds": seeds,
    }


def run_memory_thread_projection(
    conn: sqlite3.Connection,
    *,
    resolutions: tuple[float, ...] = DEFAULT_RESOLUTIONS,
    primary_resolution: float = DEFAULT_PRIMARY_RESOLUTION,
    seed: int = DEFAULT_SEED,
    include_vector: bool = True,
    persist: bool = False,
    restart_transaction_before_persist: bool = False,
) -> dict[str, Any]:
    """Run the offline projection, including the zero-label fail-fast phase.

    ``restart_transaction_before_persist`` is intended for the standalone CLI:
    it releases the long read snapshot after graph computation, then acquires a
    short IMMEDIATE writer transaction only for replacing the derived rows.
    """
    progress = decision_progress(conn)
    validation_state = (
        "unvalidated_cold_start"
        if int(progress["qualified_total"]) == 0
        else "observed_human_labels"
    )
    if not progress["gate"]["ready"]:
        return {
            "ok": False,
            "error": "INSUFFICIENT_REVIEW_LABELS",
            "validation_state": validation_state,
            "label_progress": progress,
            "mutated": False,
        }
    cleaned_resolutions = tuple(
        sorted(
            {
                round(float(value), 6)
                for value in resolutions
                if math.isfinite(float(value)) and float(value) > 0
            }
        )
    )
    if not cleaned_resolutions:
        raise ValueError("at least one positive finite resolution is required")
    primary_key = f"{float(primary_resolution):.6g}"
    if primary_key not in {f"{value:.6g}" for value in cleaned_resolutions}:
        raise ValueError("primary_resolution must be present in resolutions")

    graph, graph_report = build_sparse_memory_graph(conn, include_vector=include_vector)
    if not graph.edges:
        return {
            "ok": False,
            "error": "EMPTY_GRAPH",
            "validation_state": validation_state,
            "label_progress": progress,
            "graph": graph_report,
            "mutated": False,
        }
    projection = _run_leiden(
        graph,
        resolutions=cleaned_resolutions,
        seed=int(seed),
    )
    stability = {
        "seed": projection["seed_stability"],
        "cross_resolution": projection["cross_resolution_stability"],
        "community_counts": projection["community_counts"],
        "seeds": list(projection["seeds"]),
    }
    run_id = str(uuid.uuid4())
    if persist:
        if restart_transaction_before_persist:
            if conn.in_transaction:
                conn.execute("COMMIT")
            conn.execute("BEGIN IMMEDIATE")
        now = now_iso()
        conn.execute("UPDATE link_community_runs SET active = 0 WHERE active = 1")
        conn.execute(
            "INSERT INTO link_community_runs "
            "(run_id, model_version, algorithm, seed, resolutions_json, "
            "primary_resolution, stability_json, label_count, node_count, "
            "edge_count, created_at, active) "
            "VALUES (?, ?, 'leiden', ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                run_id,
                f"{CLUSTER_MODEL_VERSION}+{LINK_MODEL_VERSION}",
                int(seed),
                json.dumps(cleaned_resolutions),
                primary_key,
                json.dumps(stability, sort_keys=True, separators=(",", ":")),
                int(progress["qualified_total"]),
                int(graph_report["node_count"]),
                int(graph_report["edge_count"]),
                now,
            ),
        )
        rows: list[tuple[Any, ...]] = []
        for resolution, membership in projection["memberships"].items():
            global_stability = float(projection["seed_stability"][resolution])
            for node, community_id in zip(projection["nodes"], membership, strict=True):
                rows.append(
                    (
                        run_id,
                        resolution,
                        node[0],
                        node[1],
                        int(community_id),
                        global_stability,
                    )
                )
        conn.executemany(
            "INSERT INTO link_community_memberships "
            "(run_id, resolution, node_kind, node_ref, community_id, stability_score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        if restart_transaction_before_persist:
            conn.execute("COMMIT")
            conn.execute("BEGIN")

    return {
        "ok": True,
        "run_id": run_id,
        "persisted": persist,
        "mutated": persist,
        "model_version": CLUSTER_MODEL_VERSION,
        "validation_state": validation_state,
        "label_progress": progress,
        "graph": graph_report,
        "stability": stability,
        "primary_resolution": primary_key,
    }


def active_task_threads(
    conn: sqlite3.Connection, task_ids: list[str]
) -> dict[str, Any]:
    """Return active derived thread IDs for an existing task list."""
    if not task_ids:
        return {"run_id": None, "threads": {}, "by_task": {}}
    run = conn.execute(
        "SELECT run_id, primary_resolution FROM link_community_runs "
        "WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return {"run_id": None, "threads": {}, "by_task": {}}
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        "SELECT node_ref, community_id, stability_score "
        "FROM link_community_memberships WHERE run_id = ? AND resolution = ? "
        "AND node_kind = 'task' "
        f"AND node_ref IN ({placeholders}) ORDER BY community_id, node_ref",
        [run["run_id"], run["primary_resolution"], *task_ids],
    ).fetchall()
    by_task = {str(row["node_ref"]): int(row["community_id"]) for row in rows}
    threads: dict[int, list[str]] = defaultdict(list)
    for task_id, community_id in by_task.items():
        threads[community_id].append(task_id)
    return {
        "run_id": str(run["run_id"]),
        "resolution": str(run["primary_resolution"]),
        "threads": {str(key): value for key, value in sorted(threads.items())},
        "by_task": by_task,
    }


__all__ = [
    "CLUSTER_MODEL_VERSION",
    "DEFAULT_RESOLUTIONS",
    "SparseMemoryGraph",
    "active_task_threads",
    "build_sparse_memory_graph",
    "run_memory_thread_projection",
]
