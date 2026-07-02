# Private Premium Extension Contract

> Operator / private-runtime documentation — not part of the public
> external-facing (DIANA) capability claim-set. See
> the [`README` optional private-runtime boundary](../../README.md#optional-private-runtime-boundary).

This file defines the minimal integration contract for a separate private repo.

## Goal

The public repo stays OSS and safe to share.
The private repo contains premium logic and is loaded only through the gated runtime boundary.

## Required public-side contract

- Public loader: `premium_runtime.py`
- Public contract module: `premium_contract.py`
- Runtime env var for the private entrypoint:
  - `SQLITE_MEMORY_PREMIUM_ENTRYPOINT`

## Supported private entrypoint shapes

- `package.module`
- `package.module:register`
- `C:\path\to\premium_plugin.py`
- `C:\path\to\premium_plugin.py::register`

## Recommended private function signature

```python
from premium_contract import PremiumMountContext, PremiumRegistrationResult

def register_premium_extensions(
    mcp,
    *,
    server_name: str | None = None,
    mount_context: PremiumMountContext | None = None,
) -> PremiumRegistrationResult:
    ...
```

## Guaranteed inputs from the public runtime

- `server_name`
- `mount_context.contract_version`
- `mount_context.feature_id`
- `mount_context.machine_id`
- `mount_context.config`

## Minimal private repo skeleton

```python
from premium_contract import PREMIUM_RUNTIME_CONTRACT_VERSION

def register_premium_extensions(mcp, *, server_name=None, mount_context=None):
    if mount_context is None:
        raise RuntimeError("mount_context required")
    if mount_context.contract_version != PREMIUM_RUNTIME_CONTRACT_VERSION:
        raise RuntimeError("contract mismatch")

    # mount premium tools or sub-MCP here
    return {
        "mounted": True,
        "contract_version": mount_context.contract_version,
        "extension_name": "sqlite-memory-premium",
        "features": ["acl_rbac", "partner_digest"],
    }
```

## Boundary rule

The private repo must not assume direct access to secrets from the public repo.
Entitlements, approval tokens, and public-key material stay outside git and are injected at runtime.
