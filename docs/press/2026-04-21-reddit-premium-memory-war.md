# Reddit multi-subreddit post — 2026-04-21

Format per SmartKey playbook:
- Multi-subreddit with tailored angle per target
- Comparison table (memory backends / gating approaches)
- Prepared FAQ for expected questions
- Stagger submissions across days to avoid spam flags

---

## Target subreddits

| Subreddit | Angle | Suggested flair | When |
|---|---|---|---|
| r/LocalLLaMA | Local-first, no cloud, SQLite + FTS5, sqlite-vec | Discussion / Resources | Day 1 AM |
| r/selfhosted | Self-hosted agent memory, privacy, cross-machine bridge | Release | Day 3 PM |
| r/programming | Architecture — entitlement gate in public core | Architecture | Day 4 AM |
| r/MachineLearning | Agent memory design, hybrid retrieval | Project | Day 5 PM |
| r/MCP (if active) | MCP-native, 56 OSS tools, premium runtime boundary | Release | Day 1 PM |

Do NOT post all on the same day. Reddit's anti-spam detection flags identical content across subreddits in short windows.

---

## r/LocalLLaMA version

**Title:** sqlite-memory-mcp v3.5.0 — local-first MCP memory with hybrid BM25+semantic search, WAL concurrency, and a gated premium runtime

Hey r/LocalLLaMA.

I have been shipping sqlite-memory-mcp for a while as an MCP memory server
for Claude Code / Codex / anything that speaks MCP. Everything runs local,
no cloud calls, no tokens leaving the box.

The retrieval path is SQLite WAL + FTS5 + optional sqlite-vec, fused via
reciprocal rank fusion when both are available. That lets you get BM25
baseline out of the box and upgrade to hybrid semantic search by installing
one optional extra.

What landed today in v3.5.0 that might be interesting here:

- Full 56-tool OSS stack (core memory, tasks, sessions, bridge, collab,
  entity, intel) — all MCP-native, no proprietary protocol
- A premium runtime boundary where the OSS side ships the entitlement
  contract, signed-entitlement loader, audit table, and revocation table;
  the actual premium logic lives in a separate private extension
- Password-protected premium views on top of a Custom Design operator
  surface (local hash, per-session unlock)
- Cross-machine bridge sync for people running the same memory on laptop +
  desktop

Repo: https://github.com/RMANOV/sqlite-memory-mcp
v3.5.0 tag: https://github.com/RMANOV/sqlite-memory-mcp/releases/tag/v3.5.0

Happy to answer questions. Especially curious what this sub thinks about
putting the gate in the public-core repo vs hiding it in the private
extension — I have opinions but do not want to railroad the discussion.

---

## r/selfhosted version

**Title:** Local-first agent memory with cross-machine bridge sync — v3.5.0 released

Same stack, selfhosted angle:

- SQLite with WAL (concurrent safe) on local disk
- No cloud, no phone-home
- Bridge sync via git repo between machines — works over SSH or any git remote
- 56 MCP tools for agent memory, tasks, sessions, collaborators
- v3.5.0 adds premium runtime boundary and password-protected operator views

If you run Claude Code or Codex across laptop + desktop, this replaces the
"start from scratch every session" pain with durable, local, signed,
auditable memory.

Repo: https://github.com/RMANOV/sqlite-memory-mcp

---

## r/programming version

**Title:** Putting the entitlement gate in the public-core repo — a pattern for open/premium boundaries

Wrote up the architecture decision behind sqlite-memory-mcp's new premium
runtime boundary.

The premise: when you ship an open OSS core and sell a private premium
layer, the usual pattern is to hide the entitlement check inside the
premium code. That makes the OSS user experience inscrutable ("why did
this feature stop working?") and makes the gate logic itself invisible to
security review.

Alternative pattern shipped in v3.5.0:

1. The public OSS repo declares the feature registry
   (PREMIUM_FEATURES dict)
2. The public OSS repo ships the entitlement contract (dataclass), the
   signed-entitlement loader, the gate function, an audit table
   (premium_gate_audit), and a revocation table (premium_revocations)
3. The OSS-side boot hook mounts a private premium extension only after
   the gate has ruled "allowed"
4. Private extension stays private, but cannot bypass the gate — the gate
   is called before the extension is even imported

Trade-off: the shape of the premium surface is now visible to everyone,
including competitors. Net: I think that is a win for trust. Discussion
welcome.

Repo + tag: https://github.com/RMANOV/sqlite-memory-mcp/releases/tag/v3.5.0

---

## Comparison table (for any subreddit that wants it)

| Stack | Storage | Retrieval | Gating | Cross-machine |
|---|---|---|---|---|
| sqlite-memory-mcp | SQLite WAL | FTS5 + sqlite-vec (RRF fused) | Public-core gate, signed entitlements, revocable | Bridge via git repo |
| DuckDB memory servers | DuckDB | Columnar + full-text | Usually in private layer | Manual export/import |
| Cloud agent memory | Remote blob store | Managed vector DB | Cloud account | SaaS syncs |
| Local file store | JSON / files | Grep | None | File sync tools |

---

## Prepared FAQ

**Q: Why SQLite instead of DuckDB / a vector DB?**
A: WAL gives concurrent read+write for long-running agent sessions without
   locking, FTS5 is mature, and sqlite-vec covers the hybrid semantic
   search case as an optional extra. For agent memory workloads (many small
   writes, interleaved reads), SQLite outperforms DuckDB in our testing.
   For analytical queries over a snapshot, the opposite is true — different
   tool, different job.

**Q: Is this Claude-specific?**
A: No. It speaks MCP. Any MCP-speaking client works — Claude Code, Codex,
   and anything else supporting the protocol.

**Q: Do I need the premium runtime to use this?**
A: No. The OSS stack is fully functional without any premium extension.
   The premium runtime is an optional commercial layer.

**Q: If the gate is in the OSS repo, what stops me from just deleting the
   gate call?**
A: Nothing. The OSS repo is open — you can fork and remove anything. But
   then you are not running the premium extension through the audited
   boundary. The gate exists for legitimate customers who want to prove
   their access is signed, auditable, and revocable. For everyone else,
   the OSS core is MIT-licensed and complete.

**Q: Where does the password for `password_protected_views` live?**
A: Local hash only. Never transmitted. Per-session unlock, stored in
   process memory. No cloud involvement.
