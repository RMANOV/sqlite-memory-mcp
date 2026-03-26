# Public Knowledge & Release Notifications — Implementation Plan

> Historical note: this plan predates the current micro-server split. File references to `server.py` are historical and now typically map to `schema.py`, `bridge_server.py`, `collab_server.py`, `session_server.py`, or shared helpers in `db_utils.py`.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable marking entities as public/searchable, expose a public search tool, export public knowledge via bridge, and auto-create GitHub releases on the public repo for discoverability.

**Architecture:** Add `visibility` column to `entities` table (private/public). New `set_entity_visibility` tool toggles it. New `search_public_knowledge` tool does FTS5 search filtered to public entities. `bridge_push` exports public entities as `public_knowledge` key in shared.json. After push, `gh release create` on public repo with knowledge summary. `bridge_pull` routes incoming `public_knowledge` through existing `pending_shared_entities` staging.

**Tech Stack:** Python 3.12, SQLite FTS5, FastMCP, `gh` CLI, subprocess

---

### Task 1: Migration — Add `visibility` column to entities

**Files:**
- Modify: `server.py:116-125` (schema definition)
- Modify: `server.py:273-374` (migrations list)

**Step 1: Add visibility to _SCHEMA_SQL**

In `server.py` line 124, add the visibility column after `origin`:

```python
# In _SCHEMA_SQL, entities table definition (line 116-125):
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT    UNIQUE NOT NULL,
    entity_type TEXT    NOT NULL,
    project     TEXT    DEFAULT NULL,
    shared_by   TEXT    DEFAULT NULL,
    origin      TEXT    DEFAULT 'local',
    visibility  TEXT    DEFAULT 'private',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
```

**Step 2: Add migration entry**

Append to `_MIGRATIONS` list (after line 373):

```python
# v0.7.0: visibility column for public/private entities
(
    "SELECT 1 FROM pragma_table_info('entities') WHERE name='visibility'",
    "ALTER TABLE entities ADD COLUMN visibility TEXT DEFAULT 'private'",
    "entities.visibility column (public/private)",
),
# v0.7.0: visibility index
(
    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_entities_visibility'",
    "CREATE INDEX idx_entities_visibility ON entities(visibility)",
    "idx_entities_visibility index",
),
```

**Step 3: Verify migration runs**

Run: `cd /home/rmanov/sqlite-memory-mcp && python3 -c "import server; server._init_db(); print('OK')"`
Expected: "Migration applied: entities.visibility column" + "Migration applied: idx_entities_visibility index" + "OK"

**Step 4: Verify column exists in DB**

Run: `sqlite3 ~/.claude/memory/memory.db "PRAGMA table_info(entities);" | grep visibility`
Expected: row with `visibility|TEXT|||0` and default `'private'`

**Step 5: Commit**

```bash
cd /home/rmanov/sqlite-memory-mcp
git add server.py
git commit -m "feat: add visibility column to entities (v0.7.0)"
```

---

### Task 2: New tool — `set_entity_visibility`

**Files:**
- Modify: `server.py` — add new tool after `open_nodes` (around line 900)

**Step 1: Add VISIBILITY_LEVELS constant**

In `db_utils.py` or at top of server.py near other constants:

```python
VISIBILITY_LEVELS = ("private", "public")
```

**Step 2: Implement the tool**

```python
@mcp.tool()
def set_entity_visibility(
    entity_names: list[str],
    visibility: str = "public",
) -> str:
    """Set visibility of entities. 'public' makes them searchable by all instances.
    'private' (default for all entities) restricts to owner only.

    Args:
        entity_names: List of entity names to update.
        visibility: 'public' or 'private'.
    """
    if visibility not in VISIBILITY_LEVELS:
        return json.dumps({"error": f"Invalid visibility: {visibility}. Must be one of {VISIBILITY_LEVELS}"})

    now = _now()
    updated = 0
    not_found = []
    with _get_conn() as conn:
        for name in entity_names:
            cur = conn.execute(
                "UPDATE entities SET visibility = ?, updated_at = ? WHERE name = ?",
                (visibility, now, name),
            )
            if cur.rowcount > 0:
                updated += 1
            else:
                not_found.append(name)

    logger.info("set_entity_visibility: %d updated to %s", updated, visibility)
    result = {"updated": updated, "visibility": visibility}
    if not_found:
        result["not_found"] = not_found
    return json.dumps(result)
```

**Step 3: Test manually**

Run: `cd /home/rmanov/sqlite-memory-mcp && python3 -c "
import server
server._init_db()
# Create test entity
server.create_entities([{'name': 'test-vis-entity', 'entityType': 'Test', 'observations': ['test obs']}])
# Set public
result = server.set_entity_visibility(['test-vis-entity'], 'public')
print(result)
# Verify in DB
import sqlite3
conn = sqlite3.connect(server.DB_PATH)
row = conn.execute('SELECT visibility FROM entities WHERE name=\"test-vis-entity\"').fetchone()
print(f'DB visibility: {row[0]}')
# Cleanup
server.delete_entities(['test-vis-entity'])
"`
Expected: `{"updated": 1, "visibility": "public"}` + `DB visibility: public`

**Step 4: Commit**

```bash
git add server.py
git commit -m "feat: add set_entity_visibility tool"
```

---

### Task 3: New tool — `search_public_knowledge`

**Files:**
- Modify: `server.py` — add new tool after `set_entity_visibility`

**Step 1: Implement the search tool**

```python
@mcp.tool()
def search_public_knowledge(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
) -> str:
    """Search publicly available knowledge across all instances.

    Returns entities marked as visibility='public', ranked by FTS5 BM25 relevance.

    Args:
        query: Free-text search query.
        entity_type: Optional filter by entity type.
        limit: Max results (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    fts_q = _fts_query(query)

    with _get_conn() as conn:
        sql = (
            "SELECT f.rowid, f.name, f.entity_type, f.rank "
            "FROM memory_fts f "
            "JOIN entities e ON f.rowid = e.id "
            "WHERE f MATCH ? AND e.visibility = 'public'"
        )
        params: list = [fts_q]

        if entity_type:
            sql += " AND e.entity_type = ?"
            params.append(entity_type)

        sql += " ORDER BY f.rank LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        results = []
        for r in rows:
            eid = r["rowid"]
            obs = conn.execute(
                "SELECT content FROM observations WHERE entity_id = ? ORDER BY id",
                (eid,),
            ).fetchall()
            ent = conn.execute(
                "SELECT project, origin, shared_by FROM entities WHERE id = ?",
                (eid,),
            ).fetchone()
            entity = {
                "name": r["name"],
                "entityType": r["entity_type"],
                "observations": [o["content"] for o in obs],
            }
            if ent and ent["project"]:
                entity["project"] = ent["project"]
            results.append(entity)

    logger.info("search_public_knowledge: query=%r matched=%d", query, len(results))
    return json.dumps({"entities": results, "query": query, "total_results": len(results)})
```

**Step 2: Test manually**

Run: `cd /home/rmanov/sqlite-memory-mcp && python3 -c "
import server
server._init_db()
# Create and make public
server.create_entities([{'name': 'public-test-kb', 'entityType': 'KnowledgeBase', 'observations': ['OODA loop framework for decisions']}])
server.set_entity_visibility(['public-test-kb'], 'public')
# Search — should find it
r1 = server.search_public_knowledge('OODA')
print('Found:', r1)
# Set private — should NOT find it
server.set_entity_visibility(['public-test-kb'], 'private')
r2 = server.search_public_knowledge('OODA')
print('After private:', r2)
# Cleanup
server.delete_entities(['public-test-kb'])
"`
Expected: First search returns 1 result, second returns 0.

**Step 3: Commit**

```bash
git add server.py
git commit -m "feat: add search_public_knowledge tool with FTS5 filtering"
```

---

### Task 4: Modify `create_entities` to accept `visibility` parameter

**Files:**
- Modify: `server.py:523-570` (create_entities function)

**Step 1: Add visibility to INSERT**

In `create_entities`, accept optional `visibility` from entity dict:

```python
# Around line 535, after project extraction:
visibility = ent.get("visibility", "private")
if visibility not in VISIBILITY_LEVELS:
    visibility = "private"

# Modify the INSERT at line 538-543:
cur = conn.execute(
    "INSERT OR IGNORE INTO entities "
    "(name, entity_type, project, visibility, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    (name, etype, project, visibility, now, now),
)
```

**Step 2: Test**

Run: `cd /home/rmanov/sqlite-memory-mcp && python3 -c "
import server, sqlite3
server._init_db()
server.create_entities([{'name': 'born-public', 'entityType': 'Test', 'observations': ['test'], 'visibility': 'public'}])
conn = sqlite3.connect(server.DB_PATH)
row = conn.execute('SELECT visibility FROM entities WHERE name=\"born-public\"').fetchone()
print(f'visibility: {row[0]}')
server.delete_entities(['born-public'])
"`
Expected: `visibility: public`

**Step 3: Commit**

```bash
git add server.py
git commit -m "feat: create_entities accepts optional visibility parameter"
```

---

### Task 5: Modify `bridge_push` — export public_knowledge

**Files:**
- Modify: `server.py:2308-2555` (bridge_push function)

**Step 1: Collect public entities**

After the existing entity export (around line 2350), add public knowledge collection:

```python
# After entities_out is built (line 2350), collect public entities
public_rows = conn.execute(
    "SELECT id, name, entity_type, project, created_at, updated_at "
    "FROM entities WHERE visibility = 'public' ORDER BY name",
).fetchall()

public_knowledge = []
for pe in public_rows:
    obs = conn.execute(
        "SELECT content, created_at FROM observations "
        "WHERE entity_id = ? ORDER BY id",
        (pe["id"],),
    ).fetchall()
    public_knowledge.append({
        "name": pe["name"],
        "entityType": pe["entity_type"],
        "project": pe["project"],
        "observations": [
            {"content": o["content"], "createdAt": o["created_at"]}
            for o in obs
        ],
        "publishedAt": pe["updated_at"],
    })
```

**Step 2: Add public_knowledge to payload**

After `team_manifest` in the payload dict (around line 2405):

```python
payload["public_knowledge"] = public_knowledge
```

**Step 3: Add `public_knowledge` to known_keys**

In the `known_keys` set (line 2455-2466), add `"public_knowledge"`:

```python
known_keys = {
    "version", "pushed_at", "machine_id", "owner",
    "entities", "relations", "tasks",
    "shared_tasks", "shared_knowledge", "public_knowledge",
    "team_manifest",
}
```

**Step 4: Add public knowledge count to result**

After the result dict (line 2545-2554), add:

```python
if public_knowledge:
    result["public_knowledge"] = len(public_knowledge)
```

**Step 5: Commit**

```bash
git add server.py
git commit -m "feat: bridge_push exports public_knowledge to shared.json"
```

---

### Task 6: Modify `bridge_pull` — import public_knowledge via staging

**Files:**
- Modify: `server.py:2559-2800` (bridge_pull function)

**Step 1: Handle incoming public_knowledge**

After the existing `shared_knowledge` staging block (around line 2840), add:

```python
# Stage public_knowledge for review (same flow as shared_knowledge)
pub_knowledge = payload.get("public_knowledge", [])
staged_public = 0
repo_owner = payload.get("owner", "unknown")
for pk in pub_knowledge:
    pname = pk.get("name")
    if not pname:
        continue
    obs_json = json.dumps(pk.get("observations", []), ensure_ascii=False)
    phash = _source_hash(
        pname, pk.get("entityType", ""), pk.get("observations", [])
    )
    # Public knowledge: accept from any known collaborator (not trust-gated)
    collab = conn.execute(
        "SELECT github_user FROM collaborators WHERE github_user = ?",
        (repo_owner,),
    ).fetchone()
    if not collab:
        logger.info("bridge_pull: skipping public knowledge from non-collaborator %s", repo_owner)
        continue

    conn.execute(
        "INSERT OR IGNORE INTO pending_shared_entities "
        "(name, entity_type, project, observations, priority, "
        "shared_by, source_hash, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pname,
            pk.get("entityType", "unknown"),
            pk.get("project"),
            obs_json,
            "medium",
            repo_owner,
            phash,
            now,
        ),
    )
    staged_public += 1
```

**Step 2: Include staged_public count in result**

Add to the result dict at the end of bridge_pull:

```python
if staged_public:
    result["staged_public_knowledge"] = staged_public
```

**Step 3: Commit**

```bash
git add server.py
git commit -m "feat: bridge_pull stages incoming public_knowledge for review"
```

---

### Task 7: Auto-create GitHub release on bridge_push

**Files:**
- Modify: `server.py:2528-2555` (end of bridge_push, after git push)

**Step 1: Add release creation after successful push**

After `push_result` check (line 2535), add release logic:

```python
# Create GitHub release on public repo if public knowledge was pushed
if pushed and public_knowledge:
    try:
        # Build release notes from public knowledge names
        knowledge_names = [pk["name"] for pk in public_knowledge]
        tag = f"knowledge-{_now()[:10]}-{len(public_knowledge)}pk"
        title = f"Knowledge Export: {len(public_knowledge)} public entities"
        body_lines = ["## Public Knowledge Entities\n"]
        for pk in public_knowledge:
            obs_count = len(pk.get("observations", []))
            body_lines.append(f"- **{pk['name']}** ({pk['entityType']}) — {obs_count} observations")
        body = "\n".join(body_lines)

        release_result = subprocess.run(
            [
                "gh", "release", "create", tag,
                "--repo", "RMANOV/sqlite-memory-mcp",
                "--title", title,
                "--notes", body,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if release_result.returncode == 0:
            result["release_created"] = tag
            logger.info("bridge_push: created release %s on public repo", tag)
        else:
            logger.warning("bridge_push: release creation failed: %s", release_result.stderr)
    except Exception as exc:
        logger.warning("bridge_push: release creation error: %s", exc)
```

**Step 2: Test with dry run**

Run: `gh release list --repo RMANOV/sqlite-memory-mcp --limit 3` to verify `gh` auth works.
Expected: list of recent releases (or empty if none)

**Step 3: Commit**

```bash
git add server.py
git commit -m "feat: auto-create GitHub release on public repo after bridge_push"
```

---

### Task 8: Update `bridge_status` to show public knowledge count

**Files:**
- Modify: `server.py:2858-2941` (bridge_status function)

**Step 1: Add public entity counts**

In bridge_status, after querying local entities, add:

```python
public_count = conn.execute(
    "SELECT COUNT(*) FROM entities WHERE visibility = 'public'"
).fetchone()[0]
```

And include in the result:

```python
result["public_entities"] = public_count
```

**Step 2: Commit**

```bash
git add server.py
git commit -m "feat: bridge_status shows public entity count"
```

---

### Task 9: Integration test — full publish → push → release flow

**Step 1: Create test entity and make public**

```bash
cd /home/rmanov/sqlite-memory-mcp && python3 -c "
import server
server._init_db()
server.create_entities([{
    'name': 'OODA-Decision-Framework',
    'entityType': 'Framework',
    'observations': [
        'Observe-Orient-Decide-Act loop for rapid decision making',
        'Orient phase: classify routine vs complex vs critical',
        'Decide phase: pick robust action acceptable in all scenarios',
    ],
    'visibility': 'public',
}])
print('Entity created')
r = server.search_public_knowledge('OODA')
print('Search result:', r)
"
```

**Step 2: Run bridge_push and verify**

```bash
python3 -c "
import server, json
server._init_db()
r = json.loads(server.bridge_push())
print(json.dumps(r, indent=2))
"
```

Expected: result includes `"public_knowledge": 1` and optionally `"release_created": "knowledge-..."`.

**Step 3: Verify shared.json has public_knowledge**

```bash
python3 -c "
import json
d = json.load(open('/home/rmanov/.claude/memory/bridge/shared.json'))
pk = d.get('public_knowledge', [])
print(f'Public knowledge items: {len(pk)}')
for p in pk:
    print(f'  - {p[\"name\"]} ({p[\"entityType\"]})')
"
```

**Step 4: Verify GitHub release (if gh auth works)**

```bash
gh release list --repo RMANOV/sqlite-memory-mcp --limit 1
```

**Step 5: Cleanup test entity**

```bash
python3 -c "import server; server._init_db(); server.set_entity_visibility(['OODA-Decision-Framework'], 'private'); print('Reverted to private')"
```

**Step 6: Final commit**

```bash
git add -A
git commit -m "feat: complete public knowledge + release notification system (v0.7.0)"
```
