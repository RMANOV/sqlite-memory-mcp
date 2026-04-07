# DOC-001 — Write Authority and Mutation Risk Questions

**Intent:** кратък pre-change филтър за всичко, което може да пише state.

## Questions

- [ ] Новият path минава ли през authoritative mutation API, а не през direct row update?
- [ ] Създава ли `memory_events` за write-а?
- [ ] Поддържа ли `task_field_versions` / equivalent field history, когато това е релевантно?
- [ ] Ако merge/import path пипа същия state, precedence rule-ът изрично ли е описан?
- [ ] Има ли опасност raw row стойност да победи по-нов event/field authority?
- [ ] Ако change-ът е CLI / hook / helper / migration, спазва ли същите invariants като tray/server path-а?
- [ ] Има ли regression test за exact write corridor, не само за крайния резултат?
- [ ] Ако write path-ът е batch, journal-ва ли divergence/conflict вместо да overwrite-ва silently?
- [ ] Ако пипаш timestamps/order, сравними ли са cross-machine, не само локално?
- [ ] Ако change-ът е “малък convenience write”, сигурен ли си, че няма да стане hidden authority bypass?
- [ ] Ако отговорът на поне 2 въпроса е “не знам”, този change не е ready.

## Stop Rule

Ако path-ът пише state, но не можеш с едно изречение да кажеш:

> “Кой е authority source и къде се записва history?”

спри и върни промяната към design mode.
