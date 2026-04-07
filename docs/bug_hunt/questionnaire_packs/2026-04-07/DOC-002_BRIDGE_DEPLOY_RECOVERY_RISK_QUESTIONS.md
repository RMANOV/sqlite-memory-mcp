# DOC-002 — Bridge, Deploy and Recovery Risk Questions

**Intent:** къс high-yield филтър за bridge/export/import/deploy topology.

## Questions

- [ ] Новият artifact влиза ли и в export, и в import, и в bootstrap recovery?
- [ ] Има ли deterministic filename/id encoding за unsafe identifiers?
- [ ] Temp/lock/generated файловете имат ли уникални имена и cleanup path?
- [ ] Preflight различава ли safe generated debris от user edits?
- [ ] Fresh DB restore от bridge-only възстановява ли тази промяна?
- [ ] Tombstone / archived / cancelled / legacy cases пазят ли се?
- [ ] Pages/site publish path наистина ли има нужда от този artifact?
- [ ] Има ли файл, който може да надхвърли platform size limit или да направи deploy-а non-portable?
- [ ] Publish dir stage-ва ли само runtime-needed files, а не целия repo root?
- [ ] При bridge outage локалната истина оцелява ли, а при recovery shared truth възстановима ли е?
- [ ] Имаш ли поне един targeted test за export/import roundtrip или live smoke reasoning?
- [ ] Ако промяната активира и deploy, и recovery risk едновременно, задължителен е follow-up drill.

## Stop Rule

Не merge-вай bridge-related change, ако не можеш да отговориш:

> “Какво точно ще има в bridge repo-то, какво ще се publish-не и как ще се възстанови на чиста машина?”
