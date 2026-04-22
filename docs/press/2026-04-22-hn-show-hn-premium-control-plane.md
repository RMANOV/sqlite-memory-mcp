# Hacker News submission — 2026-04-22

## Title

Show HN: sqlite-memory-mcp – local-first MCP memory with a gated premium runtime

## URL

https://github.com/RMANOV/sqlite-memory-mcp

## Body

sqlite-memory-mcp is a local-first memory server for MCP-speaking agents,
backed by SQLite with WAL, FTS5, optional sqlite-vec hybrid search, and a
cross-machine bridge sync.

The part I think HN may find interesting is the premium boundary design.

The OSS repo now ships the public airlock:

- signed entitlement verification
- signed artifact-manifest verification
- signed control-policy verification
- local revoke/audit tables
- remote issuer URL hooks
- a boot path that mounts a separate private runtime only after the gate passes

The premium logic itself stays outside the OSS tree, and the new control-plane
repo issues the signed runtime documents over HTTP.

So the question is not really “how do you hide premium code?”, but whether you
can make the trust boundary explicit enough that the public repo remains
auditable while the commercial layer stays operationally gated.

Two things I would especially like technical feedback on:

1. Is putting the gate in the public-core repo a net positive for auditability,
   or does it create a worse long-term attack surface than a more opaque model?
2. For agent memory, is SQLite + WAL + FTS5 + optional sqlite-vec still the
   right local-first base, or would you move to DuckDB / a dedicated vector DB
   once the control-plane layer enters the picture?

## Notes

- Not a press release.
- Use the repo as the original source.
- Keep the title plain. No hype words, no editorializing.
