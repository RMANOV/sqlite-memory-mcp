# Premium boundary wiring (operator guide)

This guide tells an operator how to wire a signed-and-gated premium runtime into
the public host. No private keys, customer secrets, or proprietary logic live in
this repo. That separation is deliberate — it is Echelon 0 of the anti-fork
strategy ("product boundary moat") and the reason a copy of this OSS tree alone
cannot run premium features.

Premium / airlock is an **optional operator / private-runtime boundary** on the
advanced / optional path — not part of the core memory loop. See
the [`README` operator section](../../README.md#optional-private-runtime-boundary)
for where it sits in the public operator map.

## What the host enforces

The public host runtime enforces, in `premium_runtime.evaluate_feature_gate`:

1. Signed **entitlement** (Ed25519, issuer key). See
   [`docs/premium/entitlement.schema.json`](../premium/entitlement.schema.json).
2. Signed **artifact manifest** for `private_extension_runtime` (separate key).
   See [`docs/premium/artifact_manifest.schema.json`](../premium/artifact_manifest.schema.json).
3. Signed **control-plane policy** (separate key). Cache is verified on every
   read; older `issued_at` than cached → rollback rejected. See
   [`docs/premium/control_plane_policy.schema.json`](../premium/control_plane_policy.schema.json).
4. Remote fetch uses **HTTPS only, no redirects** (`_NoRedirectHandler`). Plain
   `http://` is refused.
5. Owner-approval token hash must match for features marked
   `requires_owner_approval=True` (e.g. `private_extension_runtime`).
6. Entitlement `not_before` / `expires_at` compared in epoch seconds (no
   string-sort bug for `Z`-suffixed timestamps).
7. Entitlements must carry an explicit `machine_ids` binding by default; a
   copied signed entitlement without the current `MACHINE_ID` is denied.
8. `private_extension_runtime` always requires a signed artifact manifest even
   when non-runtime premium feature checks are allowed to proceed without one.
9. All decisions audit-logged in `premium_gate_audit`.

`premium_security_config.json` turns these checks on. `control_plane_required=true`
means: no valid signed policy → deny. Do not flip this to `false` in production.

## Canonical signing payload (exact bytes the host verifies)

`premium_runtime._canonical_signed_payload` produces:

```python
json.dumps(payload_minus_signature, ensure_ascii=False, sort_keys=True,
           separators=(",", ":")).encode("utf-8")
```

Any signer that does not reproduce this byte sequence will be rejected. The
issuer CLI in the private tree is the reference signer.

## Env vars the host reads

| Env var | What it points at |
|--------|-------------------|
| `SQLITE_MEMORY_PREMIUM_ENTITLEMENT_PATH` / `_JSON` / `_URL` | signed entitlement document |
| `SQLITE_MEMORY_PREMIUM_ARTIFACT_MANIFEST_PATH` / `_JSON` / `_URL` | signed artifact manifest |
| `SQLITE_MEMORY_PREMIUM_POLICY_PATH` / `_JSON` / `_URL` | signed control-plane policy |
| `SQLITE_MEMORY_PREMIUM_PUBLIC_KEY` | Ed25519 public key (base64 raw or PEM) for entitlement |
| `SQLITE_MEMORY_PREMIUM_ARTIFACT_PUBLIC_KEY` | Ed25519 public key for manifest |
| `SQLITE_MEMORY_PREMIUM_POLICY_PUBLIC_KEY` | Ed25519 public key for control-plane policy |
| `SQLITE_MEMORY_PREMIUM_ENTRYPOINT` | `package.mod:func` or `/path/to/file.py::func` |
| `SQLITE_MEMORY_PREMIUM_INSTALLATION_SALT` | per-install randomness for forensic fingerprint |
| `SQLITE_MEMORY_OWNER_APPROVAL` | plaintext owner-approval token (hash must match entitlement) |

Precedence for docs: inline JSON env → path env → URL env.

## Private issuer tree (not in this repo)

A reference private issuer tree lives at `~/.sqlite-memory-premium/` on
operator machines:

```
~/.sqlite-memory-premium/
  keys/           # Ed25519 secret keys (0600, never leave the machine)
  docs/           # signed entitlement / manifest / policy JSON
  extension/      # private premium extension package
  bin/issue_premium.py  # keygen + signer CLI
  env.sh          # exports env vars for public host to consume
```

Keys are role-separated (`issuer`, `artifact`, `policy`) to limit blast radius
if a single key is compromised. Rotation: delete the `.sk` file, re-run
`init-keys`, re-mint documents, re-issue `env.sh`.

## Verifying the wiring

After sourcing `env.sh`, a positive-path smoke test:

```python
import sqlite3, tempfile, schema, premium_runtime as pr
tmp = tempfile.mktemp(suffix=".db"); schema.init_db(tmp)
conn = sqlite3.connect(tmp)
v = pr.evaluate_feature_gate(conn, feature_id="acl_rbac")
assert v["allowed"], v
```

A negative-path smoke test (tamper one field of the entitlement JSON without
re-signing) must return `signature_invalid:InvalidSignature`. If it does not,
something is wrong with the env-var wiring or the public-key material.

## URL-based remote fetch (Phase B)

Every `_PATH` env var above has a parallel `_URL` variant
(`*_ENTITLEMENT_URL`, `*_ARTIFACT_MANIFEST_URL`, `*_POLICY_URL`) consumed by
the same gate. Runtime precedence is inline JSON → path → URL, but operators
can leave `_PATH` unset and point the client at an HTTPS issuer service.

The fetcher enforces:
- HTTPS only (plain `http://` refused at `premium_runtime.py:603`)
- No redirects (`_NoRedirectHandler` — 3xx raises, never follows to another host)
- Signatures verified *before* the doc enters any cache or gate decision
- Rollback guard: a fresh fetch whose `issued_at` is older than the cached
  copy is rejected (`premium_runtime._cache_control_plane_policy`)

The standalone HTTPS issuer service lives outside this OSS repo (see the
private tree under `~/.sqlite-memory-mcp-control-plane/` on operator machines).
Its role: hold signing keys, serve signed documents with monotonic
`doc_version` counters, and mint short-lived per-installation entitlements.
None of its state is required here.

## What this wiring is *not*

This is Phase A + Phase B of the anti-fork strategy: signed artifacts with
an optional remote issuer service. Phase C (remote-authoritative execution
for advanced_ranking / partner_digest / chief_of_staff_queries) and Phase D
(managed release channels, customer rings, kill switches) are future work.
See the internal strategy note "Corporate fork defense research and echeloned
strategy" (2026-04-22) for the full roadmap.

The OSS boundary in this repo is sufficient to prove that a copy of the code
without the corresponding signed artifacts cannot execute premium features. It
is not sufficient — alone — against an organization willing to run its own
parallel signing infrastructure. That is why authority, policy, and update
discipline are the real moats, not code obfuscation.

## DIANA / public-material boundary

Premium / airlock is **excluded as a named feature from external-facing (DIANA)
material**. It is an internal operator / private-runtime boundary and is kept out
of the main external claim-set. The only exception is when a diligence question
specifically asks about extension governance — in which case it can be described
as the optional, signed, revocable operator boundary it is, not as a headline
capability. This boundary is recorded in the frozen claim-set in
the [`README` external claim boundary](../../README.md#external-claim-boundary-frozen-claim-set).
