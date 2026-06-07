"""Bridge shared.json / index.json git merge driver — tombstone-safe reconcile.

DEFENSE IN DEPTH for the recurring "resurrected deleted tasks" incident class.

The hot bridge sync path (bridge_sync_worker / bridge_server) is fast-forward
only and never lets git textually merge the generated bridge artifacts — it
reconciles at the DB layer via ``db_utils.merge_import_tasks`` (authoritative
per-field causal/LWW merge with a tombstone-union invariant). This module is the
*second* line of defense: when an EXTERNAL operation (a manual ``git pull``,
``git merge``, or ``git rebase`` run outside the sync worker) tries to textually
merge a tombstone-bearing bridge file, a naive 3-way text merge — or worse, an
``ours``/``theirs`` strategy — can drop a remote tombstone and resurrect a task
that was deleted on another machine. This is exactly the 2026-05-08 "12 tasks
resurrected" failure.

Surface targeting (the load-bearing fact)
-----------------------------------------
``shared.json`` ``tasks`` is **active-only** (the writer filters
``status NOT IN ('archived','cancelled')``). Tombstones live in ``index.json``
and the per-task ``tasks/*.json`` files, marked ``_tombstone: True``. So:

* ``shared.json``  -> row-union + per-field LWW (tombstone-union is a correct
  no-op there because tombstones never appear).
* ``index.json``   -> row-union + tombstone-union + per-field LWW.
* ``tasks/<id>.json`` (single task object) -> tombstone-union + per-field LWW.

Hard invariant (never violated)
-------------------------------
A task that is a tombstone (``_tombstone`` flag, or a terminal
archived/cancelled status) on EITHER side stays a tombstone after merge. A
tombstone is NEVER reopened to an active status by this merge. When sides
disagree, the tombstone wins regardless of timestamps.

Fail-closed
-----------
If any required input file is present but unparsable, the driver exits non-zero
and writes NOTHING. Git then leaves the path unmerged (``UU``); the bridge sync
preflight (``ensure_bridge_repo_ready``) auto-heals generated artifacts from the
DB or blocks. We never emit a half-merged / guessed payload for data-bearing
state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from db_utils import (
    MACHINE_ID,
    MERGEABLE_FIELDS,
    TASK_HIDDEN_STATUSES,
    _HLC_PACKED_MIN,
    _field_version_sort_key,
    _iso_to_epoch_ms,
    _pack_logical_clock,
    _parse_field_ts,
    git_run,
    json_dumps,
    json_loads,
    now_iso,
)

# git config section/name for the driver. Registered programmatically because
# .gitattributes ``merge=<name>`` is inert without a matching local git config
# entry (and .git/config is not version-controlled).
MERGE_DRIVER_NAME = "bridge-reconcile"
# git config key prefix. ``git config`` wants the dotted form
# ``merge.<name>.<key>`` (the quoted ``merge "<name>"`` form is the *file*
# representation, not a valid CLI key). ``bridge-reconcile`` is a clean
# subsection name (no dots), so the dotted form is unambiguous.
MERGE_DRIVER_KEY_PREFIX = f"merge.{MERGE_DRIVER_NAME}"
# Back-compat alias (file-section representation) for any external reader.
MERGE_DRIVER_SECTION = f'merge "{MERGE_DRIVER_NAME}"'

# Canonical .gitattributes lines for the bridge repo. The tombstone-bearing data
# files route through the reconcile driver; purely generated/derived files use
# git's built-in ``union`` (or are treated as binary) so they never raise
# spurious text conflicts that would block a sync. ``diff=json`` is preserved for
# human-readable diffs. These lines are merged additively into whatever
# .gitattributes already exists (existing unrelated lines are kept verbatim).
_GITATTRIBUTES_MANAGED_BLOCK_HEADER = "# >>> bridge-reconcile (managed) >>>"
_GITATTRIBUTES_MANAGED_BLOCK_FOOTER = "# <<< bridge-reconcile (managed) <<<"
_GITATTRIBUTES_MANAGED_LINES = (
    "# Tombstone-bearing data: tombstone-safe reconcile merge driver.",
    "shared.json merge=bridge-reconcile diff=json",
    "index.json merge=bridge-reconcile diff=json",
    "tasks/*.json merge=bridge-reconcile diff=json",
    "# Derived/generated artifacts: rebuilt from the DB on next export.",
    "shared.js merge=union",
    "entities_index.json merge=union diff=json",
    "entities/*.json merge=union diff=json",
    "extended_memory/*.json merge=union diff=json",
)

# Generated bridge artifacts that are safe to discard / rebuild from the DB if
# they get stuck in an unmerged (UU) state without an active sequence operation.
_GENERATED_UNMERGED_HEALABLE = frozenset(
    {
        "shared.json",
        "shared.js",
        "index.json",
        "entities_index.json",
    }
)
_GENERATED_UNMERGED_HEALABLE_DIRS = ("tasks/", "entities/", "extended_memory/")


# ── tombstone classification ────────────────────────────────────────────────


def _is_tombstone(task: dict[str, Any]) -> bool:
    """A task is a tombstone if explicitly flagged or carries a terminal status.

    Either signal is sufficient — a deletion expressed only by status (legacy
    peers) must count, and an explicit ``_tombstone`` flag must count even if a
    stale/active status field rode along in the same payload.
    """
    if task.get("_tombstone"):
        return True
    return task.get("status") in TASK_HIDDEN_STATUSES


def _field_key(task: dict[str, Any], field: str) -> tuple:
    """LWW sort key for ``field`` on ``task`` using its ``_field_ts`` metadata.

    Reuses ``db_utils._parse_field_ts`` / ``_field_version_sort_key`` so this
    driver's ordering can never drift from the authoritative DB-layer merge.
    """
    fts = task.get("_field_ts") or {}
    fallback_ts = task.get("updated_at", "") or ""
    updated_at, updated_by, updated_order, _event = _parse_field_ts(
        fts, field, fallback_ts
    )
    return _field_version_sort_key(updated_at, updated_by, updated_order)


def _task_recency_key(task: dict[str, Any]) -> tuple:
    """Row-level recency for choosing which side's non-field metadata to keep."""
    return _field_key(task, "status")


def _packed_order_of(task: dict[str, Any], field: str) -> int:
    """Return a PACKED logical clock representing ``field``'s current order.

    Legacy (unpacked, ``< _HLC_PACKED_MIN``) orders are projected onto the
    packed space using the field's ``updated_at`` so they can be compared and
    out-ranked on the same axis. This mirrors the legacy/packed bridging in
    ``db_utils._field_version_sort_key`` (legacy < any packed clock).
    """
    fts = task.get("_field_ts") or {}
    fallback_ts = task.get("updated_at", "") or ""
    updated_at, _by, updated_order, _ev = _parse_field_ts(fts, field, fallback_ts)
    order = int(updated_order or 0)
    if order >= _HLC_PACKED_MIN:
        return order
    return _pack_logical_clock(_iso_to_epoch_ms(updated_at), 0)


def _dominating_status_clock(
    ours: dict[str, Any], theirs: dict[str, Any]
) -> tuple[str, str, int]:
    """A status field-version that STRICTLY out-ranks BOTH inputs' status clocks.

    This is the invariant fix: "tombstone wins regardless of timestamp" cannot be
    encoded by copying either side's status clock — a copied clock merely *ties*
    the losing peer's clock, so on the next ``merge_import_tasks`` round-trip the
    tombstone fails to win (equal sort key) and the deletion resurrects. The
    merged tombstone must out-rank both sides so every downstream peer absorbs it.

    Returns (updated_at, updated_by, updated_order) with a packed order >
    max(both sides) and an updated_at >= max(both sides, now).
    """
    now = now_iso()
    max_packed = max(
        _packed_order_of(ours, "status"),
        _packed_order_of(theirs, "status"),
        _pack_logical_clock(_iso_to_epoch_ms(now), 0),
    )
    # +1 on a packed clock increments the counter (carrying into physical_ms on
    # overflow): strictly greater as an integer, so it dominates on the packed
    # axis where _field_version_sort_key places all packed clocks above legacy.
    dominating_order = max_packed + 1
    o_at = (ours.get("_field_ts", {}).get("status") or {})
    t_at = (theirs.get("_field_ts", {}).get("status") or {})
    candidate_ats = [
        now,
        ours.get("updated_at", "") or "",
        theirs.get("updated_at", "") or "",
        o_at.get("updated_at", "") if isinstance(o_at, dict) else "",
        t_at.get("updated_at", "") if isinstance(t_at, dict) else "",
    ]
    updated_at = max(a for a in candidate_ats if a) if any(candidate_ats) else now
    return updated_at, MACHINE_ID, dominating_order


# ── core reconcile ──────────────────────────────────────────────────────────


def reconcile_task_pair(ours: dict[str, Any], theirs: dict[str, Any]) -> dict[str, Any]:
    """Merge two versions of the SAME task (same id). Tombstone-union + per-field LWW.

    Tombstone invariant: if either side is a tombstone, the result is a tombstone
    and is never reopened. Otherwise per-field LWW picks the freshest value for
    each mergeable field.
    """
    ours_tomb = _is_tombstone(ours)
    theirs_tomb = _is_tombstone(theirs)

    if ours_tomb or theirs_tomb:
        # Tombstone-union: start from whichever side is the (more recent)
        # tombstone so its archival metadata is preserved, then mark it.
        if ours_tomb and theirs_tomb:
            base = (
                dict(ours)
                if _task_recency_key(ours) >= _task_recency_key(theirs)
                else dict(theirs)
            )
        else:
            base = dict(ours) if ours_tomb else dict(theirs)
        # Carry over a richer description/notes from the non-tombstone side only
        # when the tombstone lacks them (never resurrect status, never shrink).
        other = theirs if ours_tomb and not theirs_tomb else ours
        for field in ("description", "notes"):
            if not base.get(field) and other.get(field):
                base[field] = other[field]
        base["_tombstone"] = True
        if base.get("status") not in TASK_HIDDEN_STATUSES:
            # Explicit-flag tombstone with stale active status: canonicalize.
            base["status"] = "archived"
        # Union field-version metadata for non-status fields, then OVERWRITE the
        # status field-version with a clock that STRICTLY out-ranks both inputs.
        # Copying either side's status clock would only tie the losing peer and
        # let the deletion resurrect on the next DB-layer merge round-trip; the
        # tombstone must dominate so every peer absorbs it. (Invariant fix.)
        merged_fts = _union_field_ts(ours, theirs)
        dom_at, dom_by, dom_order = _dominating_status_clock(ours, theirs)
        merged_fts["status"] = {
            "updated_at": dom_at,
            "updated_by": dom_by,
            "updated_order": dom_order,
            "value": base["status"],
        }
        base["_field_ts"] = merged_fts
        base["updated_at"] = max(base.get("updated_at", "") or "", dom_at)
        return base

    # Neither side is a tombstone: per-field LWW.
    merged = dict(ours)
    merged_fts = _union_field_ts(ours, theirs)
    for field in MERGEABLE_FIELDS:
        in_ours = field in ours
        in_theirs = field in theirs
        if in_theirs and not in_ours:
            merged[field] = theirs[field]
            continue
        if not in_theirs:
            continue
        if _field_key(theirs, field) > _field_key(ours, field):
            merged[field] = theirs[field]
    merged["_field_ts"] = merged_fts
    # Keep the freshest top-level updated_at so downstream consumers see progress.
    merged["updated_at"] = max(
        ours.get("updated_at", "") or "", theirs.get("updated_at", "") or ""
    )
    return merged


def _union_field_ts(
    ours: dict[str, Any], theirs: dict[str, Any]
) -> dict[str, Any]:
    """Per-field union of ``_field_ts`` metadata, keeping the newer entry."""
    o_fts = ours.get("_field_ts") or {}
    t_fts = theirs.get("_field_ts") or {}
    out: dict[str, Any] = {}
    for field in set(o_fts) | set(t_fts):
        if field not in o_fts:
            out[field] = t_fts[field]
        elif field not in t_fts:
            out[field] = o_fts[field]
        else:
            out[field] = (
                t_fts[field]
                if _field_key(theirs, field) > _field_key(ours, field)
                else o_fts[field]
            )
    return out


def reconcile_task_collection(
    ours: dict[str, Any], theirs: dict[str, Any]
) -> dict[str, Any]:
    """Merge two shared.json / index.json payloads (each with a ``tasks`` list).

    * Row-union: a task present on either side is kept (by ``id``).
    * Tombstone-union + per-field LWW for tasks present on both sides.
    * Non-task top-level keys: prefer the side whose ``pushed_at`` is newer; the
      bridge sync worker fully regenerates these from the DB on the next push, so
      this only has to be self-consistent, never authoritative.
    """
    if not isinstance(ours, dict) or not isinstance(theirs, dict):
        raise ValueError("reconcile_task_collection requires dict payloads")

    ours_tasks = {t["id"]: t for t in ours.get("tasks", []) if isinstance(t, dict) and t.get("id")}
    theirs_tasks = {t["id"]: t for t in theirs.get("tasks", []) if isinstance(t, dict) and t.get("id")}

    merged_tasks: list[dict[str, Any]] = []
    for tid in sorted(set(ours_tasks) | set(theirs_tasks)):
        o = ours_tasks.get(tid)
        t = theirs_tasks.get(tid)
        if o is not None and t is not None:
            merged_tasks.append(reconcile_task_pair(o, t))
        else:
            merged_tasks.append(dict(o if o is not None else t))

    # Choose the base envelope from the newer payload (top-level metadata only).
    newer, older = (
        (ours, theirs)
        if (ours.get("pushed_at", "") or "") >= (theirs.get("pushed_at", "") or "")
        else (theirs, ours)
    )
    result = dict(newer)
    # Preserve any top-level keys present only on the older side (defensive).
    for key, value in older.items():
        if key not in result and key != "tasks":
            result[key] = value
    result["tasks"] = merged_tasks
    return result


# ── git merge driver entrypoint ─────────────────────────────────────────────


def _read_json_or_fail(path: str, *, label: str) -> dict[str, Any]:
    """Parse a JSON file. Empty/absent base is treated as ``{}`` (3-way base may
    legitimately be empty). A present-but-unparsable side is FAIL-CLOSED."""
    p = Path(path)
    if not p.exists():
        if label == "base":
            return {}
        raise ValueError(f"required merge input missing: {label} ({path})")
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        if label == "base":
            return {}
        raise ValueError(f"required merge input empty: {label} ({path})")
    try:
        data = json_loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"unparsable {label} JSON ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} JSON is not an object ({path})")
    return data


def run_merge_driver(
    base_path: str,
    ours_path: str,
    theirs_path: str,
    *,
    single_task: bool = False,
) -> int:
    """Git merge driver: reconcile ``theirs`` into ``ours`` (the %A result slot).

    Returns 0 on success (``ours_path`` overwritten with the merged result), or 1
    fail-closed WITHOUT writing anything (git leaves the path unmerged).
    """
    try:
        ours = _read_json_or_fail(ours_path, label="ours")
        theirs = _read_json_or_fail(theirs_path, label="theirs")
        _ = _read_json_or_fail(base_path, label="base")  # parsed for fail-closed validation
        if single_task:
            merged = reconcile_task_pair(ours, theirs)
        else:
            merged = reconcile_task_collection(ours, theirs)
        merged_text = json_dumps(merged)
        # Validate round-trip before clobbering %A (never write a corrupt result).
        json_loads(merged_text)
    except (ValueError, TypeError, OSError) as exc:
        sys.stderr.write(f"bridge-reconcile merge driver FAILED-CLOSED: {exc}\n")
        return 1
    # Only now is it safe to overwrite the %A (ours) slot in-place.
    Path(ours_path).write_text(merged_text, encoding="utf-8")
    return 0


# ── programmatic driver registration ────────────────────────────────────────


def is_merge_driver_registered(repo_dir: str) -> bool:
    """True when the local git config wires up the bridge-reconcile driver."""
    result = git_run(
        repo_dir, "config", "--get", f"{MERGE_DRIVER_KEY_PREFIX}.driver", timeout=10
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def register_bridge_merge_driver(repo_dir: str) -> bool:
    """Register the bridge-reconcile merge driver in the repo's local git config.

    Idempotent. ``.gitattributes`` (version-controlled) selects the driver per
    path; this writes the matching ``[merge "bridge-reconcile"]`` config (NOT
    version-controlled, so it must be (re)applied on every machine/clone).

    The driver shells back into THIS module so there is a single implementation
    of the reconcile logic. Per-task files pass ``--single-task``; the path
    placeholder ``%P`` lets us route by filename.
    """
    python = sys.executable or "python3"
    module_path = str(Path(__file__).resolve())
    # %O=base(ancestor) %A=ours(current/result) %B=theirs(other) %P=pathname.
    driver_cmd = (
        f'"{python}" "{module_path}" merge '
        f'--base %O --ours %A --theirs %B --path %P'
    )
    name_set = git_run(
        repo_dir,
        "config",
        f"{MERGE_DRIVER_KEY_PREFIX}.name",
        "bridge tombstone-safe JSON reconcile",
        timeout=10,
    )
    driver_set = git_run(
        repo_dir,
        "config",
        f"{MERGE_DRIVER_KEY_PREFIX}.driver",
        driver_cmd,
        timeout=10,
    )
    # recursive=bridge-reconcile (self): for the inner merge of a recursive
    # (criss-cross) merge that builds a virtual ancestor, reuse THIS driver
    # rather than git's text/binary fallback — so a tombstone can never be
    # dropped in the virtual-ancestor step. ``binary``/``text`` would let the
    # ancestor merge resolve without tombstone-union.
    recursive_set = git_run(
        repo_dir,
        "config",
        f"{MERGE_DRIVER_KEY_PREFIX}.recursive",
        MERGE_DRIVER_NAME,
        timeout=10,
    )
    return all(
        r.returncode == 0 for r in (name_set, driver_set, recursive_set)
    )


def _render_managed_block() -> str:
    return "\n".join(
        (
            _GITATTRIBUTES_MANAGED_BLOCK_HEADER,
            *_GITATTRIBUTES_MANAGED_LINES,
            _GITATTRIBUTES_MANAGED_BLOCK_FOOTER,
        )
    )


def is_managed_gitattributes(repo_dir: str, rel_path: str) -> bool:
    """True only for a ``.gitattributes`` that carries OUR managed block.

    Content-verified and intentionally narrow: this lets the bridge readiness
    gate allow the merge-driver's own ``.gitattributes`` seed through (so
    first-time runtime install does not block sync), WITHOUT broadening the dirty
    gate for arbitrary files. Any other path, or a ``.gitattributes`` lacking the
    managed header/footer, returns False.
    """
    rel = (rel_path or "").replace("\\", "/").strip("/")
    if rel != ".gitattributes":
        return False
    path = Path(repo_dir) / ".gitattributes"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        _GITATTRIBUTES_MANAGED_BLOCK_HEADER in text
        and _GITATTRIBUTES_MANAGED_BLOCK_FOOTER in text
    )


def ensure_gitattributes(repo_dir: str) -> bool:
    """Write/refresh the managed .gitattributes block in the bridge repo.

    Idempotent and additive: any lines outside the managed block are preserved
    verbatim; only the managed block is (re)written. Returns True when the file
    now contains the current managed block.
    """
    path = Path(repo_dir) / ".gitattributes"
    managed = _render_managed_block()
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            return False

    if (
        _GITATTRIBUTES_MANAGED_BLOCK_HEADER in existing
        and _GITATTRIBUTES_MANAGED_BLOCK_FOOTER in existing
    ):
        pre, _, rest = existing.partition(_GITATTRIBUTES_MANAGED_BLOCK_HEADER)
        _, _, post = rest.partition(_GITATTRIBUTES_MANAGED_BLOCK_FOOTER)
        new_text = pre.rstrip("\n") + ("\n\n" if pre.strip() else "") + managed + post
    else:
        prefix = existing.rstrip("\n")
        # Drop any legacy bare ``diff=json`` lines we now manage, to avoid dupes.
        kept = [
            ln
            for ln in prefix.splitlines()
            if ln.strip()
            not in {"tasks/*.json diff=json", "index.json diff=json"}
        ]
        prefix = "\n".join(kept)
        new_text = (prefix + "\n\n" if prefix.strip() else "") + managed

    new_text = new_text.rstrip("\n") + "\n"
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True


def ensure_bridge_merge_protection(repo_dir: str) -> dict[str, Any]:
    """Idempotently install the full merge-driver protection in a bridge repo.

    1. materialize the managed .gitattributes block (selects the driver per path);
    2. register the driver in the repo's local git config (makes the attribute
       actually fire — .gitattributes is inert without it).

    Returns a structured record. Never raises.
    """
    attrs_ok = ensure_gitattributes(repo_dir)
    driver_ok = register_bridge_merge_driver(repo_dir)
    staged = False
    if attrs_ok and (Path(repo_dir) / ".git").exists():
        # Stage .gitattributes so the next bridge commit propagates the managed
        # block to peers (it is not in BRIDGE_GIT_STAGE_PATHS). We do NOT commit
        # here: committing inside the readiness preflight (which runs before the
        # fast-forward step) would turn a normally fast-forwardable concurrent
        # peer-push into a stuck divergence. Instead it rides the worker's next
        # commit. ``git status --porcelain`` reports BOTH untracked (fresh repo)
        # and modified (live repo already tracks .gitattributes) states — plain
        # ``git diff`` misses the untracked case.
        st = git_run(repo_dir, "status", "--porcelain", "--", ".gitattributes", timeout=10)
        if st.returncode == 0 and st.stdout.strip():
            add = git_run(repo_dir, "add", "--", ".gitattributes", timeout=10)
            staged = add.returncode == 0
    return {
        "gitattributes": attrs_ok,
        "driver_registered": driver_ok,
        "gitattributes_staged": staged,
        "ok": attrs_ok and driver_ok,
    }


# ── stuck-UU (unmerged) auto-heal for generated artifacts ───────────────────


def _is_generated_healable(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/").strip("/")
    if rel in _GENERATED_UNMERGED_HEALABLE:
        return True
    return any(rel.startswith(prefix) for prefix in _GENERATED_UNMERGED_HEALABLE_DIRS)


def find_unmerged_generated_paths(repo_dir: str) -> tuple[list[str], list[str]]:
    """Return (generated_unmerged, user_unmerged) repo-relative paths.

    A path is "unmerged" when ``git status --porcelain`` reports a conflict
    state (XY with U, or AA/DD). This is detected independently of any active
    sequence operation so it also catches the stuck-UU-without-MERGE_HEAD class.
    """
    status = git_run(repo_dir, "status", "--porcelain", timeout=15)
    generated: list[str] = []
    user: list[str] = []
    if status.returncode != 0:
        return generated, user
    conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        xy = line[:2]
        if xy not in conflict_codes:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.replace("\\", "/").strip("/")
        if _is_generated_healable(path):
            generated.append(path)
        else:
            user.append(path)
    return generated, user


def auto_heal_unmerged_generated(repo_dir: str) -> dict[str, Any]:
    """Resolve stuck-UU generated artifacts by taking them out of conflict.

    ONLY touches generated bridge artifacts, and ONLY when no user-managed path
    is also unmerged (fail-closed: never discard user work). The resolved
    generated files are rebuilt from the DB by the next bridge export, so we
    simply ``git checkout --theirs`` then ``git add`` to clear the conflict and
    let normal export overwrite them; if checkout fails we ``git rm`` the path
    so it is regenerated cleanly.

    Returns a structured record; never raises.
    """
    generated, user = find_unmerged_generated_paths(repo_dir)
    record: dict[str, Any] = {
        "generated_unmerged": generated,
        "user_unmerged": user,
        "healed": [],
        "skipped": None,
    }
    if not generated:
        record["skipped"] = "no generated unmerged paths"
        return record
    if user:
        record["skipped"] = f"user-managed unmerged paths present: {user[:3]}"
        return record
    for path in generated:
        # Prefer theirs (remote) so an incoming tombstone is retained until the
        # DB-layer merge + export rewrites the file authoritatively.
        co = git_run(repo_dir, "checkout", "--theirs", "--", path, timeout=15)
        if co.returncode != 0:
            rm = git_run(repo_dir, "rm", "-f", "--", path, timeout=15)
            if rm.returncode != 0:
                continue
        else:
            git_run(repo_dir, "add", "--", path, timeout=15)
        record["healed"].append(path)
    return record


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge tombstone-safe JSON merge driver")
    sub = parser.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("merge", help="git merge driver entrypoint")
    m.add_argument("--base", required=True, help="%%O ancestor file")
    m.add_argument("--ours", required=True, help="%%A current/result file")
    m.add_argument("--theirs", required=True, help="%%B other file")
    m.add_argument("--path", default="", help="%%P pathname (routes single-task files)")

    sub.add_parser("register", help="register driver in local git config (cwd repo)")
    return parser


def _is_single_task_path(path: str) -> bool:
    rel = (path or "").replace("\\", "/").strip("/")
    return rel.startswith("tasks/") and rel.endswith(".json")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd == "register":
        ok = register_bridge_merge_driver(".")
        return 0 if ok else 1
    if args.cmd == "merge":
        return run_merge_driver(
            args.base,
            args.ours,
            args.theirs,
            single_task=_is_single_task_path(args.path),
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
