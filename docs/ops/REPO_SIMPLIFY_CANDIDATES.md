# Repo Simplification — Removal/Relocation Candidates (FOR OPERATOR REVIEW)

Status: **proposal only — nothing deleted, nothing moved.** This is the
conservative output of the launch-prep "clean OSS first impression" pass.
Each item below is a candidate for the operator to decide on; the executor took
no destructive action.

Method: candidates are drawn from **tracked files** (`git ls-files`) — i.e.,
what a fresh `git clone` actually shows — not from the working directory. Local
untracked cruft (logs, caches, `.wrangler/`, build artifacts) does not reach a
clone and is already handled by `.gitignore`.

Repo snapshot at analysis time: 209 tracked files. `docs/` is the bulk
(57 tracked files).

---

## Tier 1 — strongest candidates (marketing drafts in the tracked tree)

### `docs/press/` — 16 tracked files of marketing copy

Contains versioned drafts of a marketing campaign ("premium-memory-war" v1/v2/v3,
plus `.publish.txt` and per-platform variants for LinkedIn / X / Medium / Reddit
/ DevTo / HN). `docs/press/README.md` describes it as "long-form article drafts
for the premium direction."

Why a candidate:
- Marketing draft copy in a tracked OSS tree dilutes the "what is this code"
  first impression for a fresh-clone visitor.
- It conflicts with the standing operator rule that press/marketing material
  belongs in `.gitignore`, not the tracked tree. Note `.gitignore` already
  excludes the sibling `press-releases/` and `docs/press-releases-audit.md`,
  but **not** `docs/press/`.
- Several files are near-duplicate version iterations (v1/v2/v3) of the same
  piece — internal drafting history, not reference material.

Suggested operator options (pick one — executor did NOT act):
1. `git rm -r --cached docs/press/` + add `docs/press/` to `.gitignore`
   (keeps files on disk, removes from the tracked tree — matches the standing
   press-releases rule).
2. Keep only the single final/published version of each platform piece; drop the
   v1/v2 iterations.
3. Leave as-is if these drafts are intentionally public.

> NOTE: This is distinct from the 4 sci-fi launch articles currently being
> audited by another work-stream. Do not action `docs/press/` until that audit
> confirms there is no overlap.

---

## Tier 2 — internal process artifacts (low value to a fresh-clone visitor)

### `docs/bug_hunt/runs/` — QA run logs

`checkpoints.jsonl`, `run_result.json`, `summary.md` per run, plus
`RUN_REGISTRY.jsonl` and a `_TEMPLATE_RUN_FOLDER/`. These are internal
bug-hunt execution logs. The *methodology* docs (`BUG_PATTERN_ANALYSIS.md`,
the questionnaire packs) have reference value; the per-run log dumps are
process exhaust.

Suggested operator options:
1. Keep the methodology docs + questionnaire packs; gitignore only
   `docs/bug_hunt/runs/` (the log dumps).
2. Keep all (if the run history is intentionally part of the project record).

### `docs/REFLECT_AUDIT_DEMO.md`

A demo/walkthrough doc. Reference value is fine; flagged only as a candidate to
fold into the main docs if a leaner top-level `docs/` listing is wanted.

---

## Tier 3 — note, not a removal candidate (local-only, already non-tracked)

### Nested `sqlite-memory-mcp/` directory in the repo root (UNTRACKED)

The working tree has a nested `sqlite-memory-mcp/` directory, but **0 of its
files are tracked** — it contains only local `*.log` files
(`server.log`, `intel_server.log`, etc.). It will **not** appear in a fresh
clone, so it is not an OSS-impression problem. It is local cruft the operator
may wish to delete from their working copy, but there is nothing to remove from
git. `*.log` is already gitignored.

---

## Explicitly NOT touched (out of scope / owned elsewhere)

- `README.md` and its positioning section — owned by the README-positioning
  rewrite work-stream.
- `docs/articles/` sci-fi article(s) — owned by the article-audit work-stream.
- All Python source, `tests/`, `templates/`, `hooks/`, `bin/`, `examples/`,
  `systemd/`, `docs/ops/`, `docs/premium/`, `docs/plans/`, `docs/DEBATE_PROTOCOL.md`
  — load-bearing or legitimate reference docs; left untouched.

---

## Summary

The single highest-impact, lowest-risk simplification for a clean OSS first
impression is to get the `docs/press/` marketing drafts out of the **tracked**
tree (Tier 1), consistent with the existing `press-releases/` gitignore rule.
Everything else is optional polish. No file was deleted or moved by this pass.
