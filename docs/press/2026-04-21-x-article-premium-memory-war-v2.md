> Legacy draft. Kept for tone/history reference only. This version predates the
> shipped `password_protected_views` premium surface. Use the `v3` draft for
> current publishing.

# The premium AI battle is moving away from "more model" and toward "better memory under pressure."

That shift is already visible.

Claude Code and Codex are making raw coding power cheaper and more common.
So the premium layer has to move somewhere harder to commoditize.

My bet: **operational memory**.

Not archive memory.
Not "chat history."
Memory that can:

- brief an operator fast
- catch drift early
- shape the working surface around urgency

That is why the premium direction around
[`sqlite-memory-mcp`](https://github.com/RMANOV/sqlite-memory-mcp) gets
interesting.

The repo is explicit about the boundary:

- OSS ships the SQLite memory core, premium airlock, entitlement contract, and
  tray hooks
- private premium logic stays behind a gated runtime

That honesty matters.

## The 3 premium surfaces I would push first

### `instant_briefing`

The anti-cold-start layer.

Before the mail or meeting, it should answer:

- who is this
- what changed
- what was promised
- what is open
- what is risky

### `commitment_radar`

The anti-drift layer.

It should surface:

- commitments
- blockers
- stale follow-ups
- silent threads
- deadline pressure

### `custom_design_tab`

The anti-generic layer.

This is where premium memory becomes an operating surface instead of a pile of
retrieved notes:

- custom grouping
- custom sorting
- operator-specific views
- client/risk/mailbox-focused working layouts

Truth boundary:

- `instant_briefing`, `commitment_radar`, and `custom_design_tab` are current
  documented premium surfaces
- `password-protected premium views` are the next commercial layer I would add
  on top, not a claim that they already ship in the OSS repo

## The actual moat

When everyone can generate, the moat is no longer raw intelligence.
It becomes:

- memory quality
- timing
- prioritization
- selective exposure

In short:

**Brief me. Warn me. Shape the room.**

Full catalog:

- Repo: https://github.com/RMANOV/sqlite-memory-mcp
- Premium packs: https://github.com/RMANOV/sqlite-memory-mcp?tab=readme-ov-file#premium-feature-packs
- Premium features: https://github.com/RMANOV/sqlite-memory-mcp?tab=readme-ov-file#feature-level-premium-surfaces
