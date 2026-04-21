# sqlite-memory-mcp-premium-template

Public-safe bootstrap template for a **separate private premium repo**.

This template is intentionally safe to keep in the OSS repo:

- no private keys
- no customer entitlements
- no premium business logic
- no proprietary retrieval/ranking rules

## Purpose

Use this template to start a private repository that implements:

- `register_premium_extensions(...)`
- premium-only MCP tools
- private connector logic
- premium governance / ACL / ingestion workflows

## Expected host

This template is meant to run next to the public `sqlite-memory-mcp` repo and use:

- `premium_runtime.py`
- `premium_contract.py`

## Minimal flow

1. Create a new **private** repo.
2. Copy this template into that repo.
3. Implement premium-only tools under `sqlite_memory_premium/`.
4. Point `SQLITE_MEMORY_PREMIUM_ENTRYPOINT` to the private module.
5. Load through the gated public runtime only.

## Important boundary

Do not move secrets, signatures, entitlement issuance, or real premium logic into the public repo.
