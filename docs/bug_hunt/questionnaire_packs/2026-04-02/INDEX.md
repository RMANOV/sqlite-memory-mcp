# SQLite Memory Bug Hunt Pack — 2026-04-02

**Pack ID:** `BH-PACK-2026-04-02-SQLITE-MEMORY`

Това е първият стратегически pack за `sqlite_memory`, който гледа repo-то като memory substrate, не само като набор от Python файлове.

## Документи

1. `DOC-001` — sync, causal ordering, bridge guarantees
2. `DOC-002` — task tray and operator surfaces
3. `DOC-003` — memory governance, retrieval, audit
4. `DOC-004` — SPOF and operational resilience

## Най-полезният reading order

1. `DOC-001`, за да стане ясно къде е authority chain-ът.
2. `DOC-004`, за да се видят singletons и recovery choke points.
3. `DOC-002`, за да се оцени най-рисковият human-facing write surface.
4. `DOC-003`, за да се провери дали storage correctness не създава retrieval illusion.

## Какъв type findings търсим

- silent data loss
- stale state winning over newer authority
- bootstrap/recovery holes
- operator paths, които правят скрити writes
- project/filter drift
- retrieval confidence without evidence
- audit loops, които не са достатъчно автономни
- operational singletons, които могат да спрат cross-machine continuity

## When This Pack Must Be Reused

- след sync-related commit cluster;
- след schema / merge / audit промени;
- след tray UI write-path промени;
- преди release tag;
- след реален incident между Windows/Fedora или друга двойка машини.
