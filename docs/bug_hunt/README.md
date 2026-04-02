# SQLite Memory Bug Hunt Tracking Hub

Цел: едно място в repo-то за bug-hunt questionnaires, strategic risk baselines, SPOF анализ и run traceability във времето.

Това repo не е типичен UI-only продукт. `sqlite_memory` е memory substrate с няколко едновременно активни слоя:

- един физически SQLite state store;
- event/field history и audit/governance слой;
- bridge sync към git repo за cross-machine propagation;
- tray GUI, hooks и background workers;
- няколко MCP server entry points върху една и съща база.

Затова bug hunt-ът тук трябва да проверява не само "има ли бъг", а:

1. къде е authority-то;
2. къде може да се загуби или изкриви state;
3. как се възстановява системата след частичен провал;
4. кои зависимости са SPOF-и и как се наблюдават.

## Tracking Model

Най-практичната форма тук е същият хибриден модел като в `reports_generator`, но адаптиран за distributed state:

1. `Markdown` е source of truth за questionnaires, checklists и SPOF reasoning.
2. `MANIFEST.json` на ниво questionnaire pack държи машинно-четима карта на документите.
3. `RUN_REGISTRY.jsonl` е append-only централен регистър на run-овете.
4. Всеки реален run има собствена папка с:
   - `run_result.json`
   - `checkpoints.jsonl`
   - `summary.md`
5. `DOC-004` е задължителен при всеки сериозен run, защото този repo има operational singletons, които не личат от unit tests.

## Структура

```text
docs/bug_hunt/
├─ questionnaire_packs/
│  └─ 2026-04-02/
│     ├─ MANIFEST.json
│     ├─ INDEX.md
│     ├─ DOC-001_SYNC_CAUSAL_BRIDGE_GUARANTEES.md
│     ├─ DOC-002_TASK_TRAY_AND_OPERATOR_SURFACES.md
│     ├─ DOC-003_MEMORY_GOVERNANCE_RETRIEVAL_AUDIT.md
│     └─ DOC-004_SPOF_OPERATIONAL_RESILIENCE.md
└─ runs/
   ├─ RUN_REGISTRY.jsonl
   ├─ _TEMPLATE_RUN_FOLDER/
   │  ├─ run_result.json
   │  ├─ checkpoints.jsonl
   │  └─ summary.md
   └─ RUN-2026-04-02_static-baseline/
      ├─ run_result.json
      ├─ checkpoints.jsonl
      └─ summary.md
```

## Recommended Run Order

1. `DOC-001` — sync, merge ordering, bridge export/import, recovery.
2. `DOC-002` — tray startup semantics, filters, operator write paths, attachments.
3. `DOC-003` — provenance, retrieval contract, audit cadence, contradiction handling.
4. `DOC-004` — SPOF inventory, failure drills, residual risk.

Това не е случайна подредба. Ако `DOC-001` е счупен, останалите могат да изглеждат коректни локално, но да се разпаднат cross-machine.

## When to Rerun

Пусни нов run задължително след:

- промени по `db_utils.py`, `schema.py`, `bridge_sync_worker.py`, `bridge_server.py`;
- промени по tray write paths: `task_tray.py`, `tray_dialogs.py`, `tray_sync.py`, `tray_filters.py`;
- промени по `memory_audit.py`, `context_packer.py`, `claim_graph.py`, `intel_server.py`;
- нови attachment, bridge, hook или recovery механизми;
- всяка серия от sync инциденти между машини;
- преди release tag или по-голям rollout към друга машина.

## What Must Stay Traceable Over Time

Минимално пази в `RUN_REGISTRY.jsonl`:

- `run_id`
- `pack_id`
- `started_at`
- `ended_at`
- `operator`
- `method`
- `docs_touched`
- `checkpoints_checked`
- `open_risks`
- `manual_followups`
- `spofs_reviewed`
- `status`

Минимално пази в `checkpoints.jsonl` на run ниво:

- timestamp
- doc reference
- status
- finding summary
- file / function anchor
- fixed yes/no
- commit ref ако finding-ът е вече mitigation, а не open issue

## Current Scope Boundary

Този пакет е само за `sqlite_memory` repo. Не е обща паметна система, не е bridge repo, не е machine-specific ops notebook. Точно затова отделният baseline run тук ползва:

- git history на repo-то;
- текущата кодова структура;
- текущото live поведение на DB-facing paths;
- и отделен SPOF анализ.
