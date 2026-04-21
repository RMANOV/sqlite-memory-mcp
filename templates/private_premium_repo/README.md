# sqlite-memory-mcp-premium-template

Public-safe bootstrap template for a **separate private premium repo**.

This template mirrors the resilient bootstrap layout of the real private
premium runtime, but keeps the premium business logic replaced with safe
placeholders.

This template is intentionally safe to keep in the OSS repo:

- no private keys
- no customer entitlements
- no premium connector secrets
- no proprietary ranking / governance logic

## Purpose

Use this template to start a private repository that implements:

- `register_premium_extensions(...)`
- `build_task_tray_extension(...)`
- premium-only MCP tools
- private connector logic
- premium governance / ACL / ingestion workflows
- premium tray / protected-view surfaces

## Template layout

The template mirrors the private bootstrap structure:

- `sqlite_memory_premium/app.py`
- `sqlite_memory_premium/host_api.py`
- `sqlite_memory_premium/runtime_state.py`
- `sqlite_memory_premium/schema.py`
- `sqlite_memory_premium/acl_governance.py`
- `sqlite_memory_premium/communication_memory.py`
- `sqlite_memory_premium/tray_extension.py`
- `sqlite_memory_premium/register.py`

The placeholder tools in this template are there to preserve the contract and
module boundaries, not to expose premium logic.

## Expected host

This template is meant to run next to the public `sqlite-memory-mcp` repo and
use:

- `premium_runtime.py`
- `premium_task_tray.py`
- `premium_contract.py`

## Current contract surfaces

This public-safe template tracks the current premium contract boundary for:

- `custom_design_tab`
- `password_protected_views`
- `custom_design_surface`
- `protected_operator_surface`

The template still uses placeholders, but the declared features, packs, and
optional tray-extension hook should match the public OSS contract.

## Minimal flow

1. Create a new **private** repo.
2. Copy this template into that repo.
3. Replace the placeholder tools in `sqlite_memory_premium/`.
4. Replace `tray_extension.py` with the real Custom Design / protected-view
   logic for your private runtime.
5. Point `SQLITE_MEMORY_PREMIUM_ENTRYPOINT` to the private module.
6. Load through the gated public runtime only.

## Important boundary

Do not move secrets, signatures, entitlement issuance, or real premium logic
into the public repo.
