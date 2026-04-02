# DOC-002 — Task Tray and Operator Surfaces

**Intent:** да се оцени най-рисковият human-facing writer в системата. Tray-ът е удобен, но точно затова е опасен: може да произвежда валидни, но нежелани writes.

**Primary anchors:** `task_tray.py:184`, `task_tray.py:1506`, `tray_dialogs.py:1178`, `tray_dialogs.py:1468`, `tray_dialogs.py:2168`, `tray_sync.py:194`, `tray_sync.py:253`, `tray_filters.py:285`

---

## 1. Startup Semantics

### 1.1 Hidden writes

- [ ] `DOC-002/1.1/1` — Потвърди, че startup / refresh не прави hidden section mutations за terminal tasks.
- [ ] `DOC-002/1.1/2` — `promote_due_today()` трябва да мести само валидните active tasks, не archived/cancelled residue.

### 1.2 Background activity

- [ ] `DOC-002/1.2/1` — Periodic pull и background audit не трябва да изглеждат като operator edit.
- [ ] `DOC-002/1.2/2` — Ако има hidden write, то трябва да е auditably tagged с tool name, не да изглежда като human save.

---

## 2. Edit and Save Paths

### 2.1 Reader -> Edit flow

- [ ] `DOC-002/2.1/1` — Провери, че `TaskReaderDialog -> EditTaskDialog -> update_task/apply_attachment_changes` не rewrite-ва полета без смисъл.
- [ ] `DOC-002/2.1/2` — Full-form save path трябва да е съвместим с event-backed mutation authority.

### 2.2 Description / notes safety

- [ ] `DOC-002/2.2/1` — Големи `description`/`notes` не трябва да се смаляват без auto-heal или ясна блокировка.
- [ ] `DOC-002/2.2/2` — Reader/edit UX трябва да оставя видим trace, когато bridge copy е върната локално като recovery action.

---

## 3. Filters and Project Taxonomy

### 3.1 Project chips

- [ ] `DOC-002/3.1/1` — `mapping-studio` и `mapping_studio` не трябва да създават различни visual project universes.
- [ ] `DOC-002/3.1/2` — Include/exclude project filtering трябва да сравнява canonical names, не raw string variants.

### 3.2 Persisted UI state

- [ ] `DOC-002/3.2/1` — `tab_views` в `QSettings` и bridge `ui_profiles` трябва да нормализират `project` filters симетрично.
- [ ] `DOC-002/3.2/2` — Host A не трябва да инжектира legacy alias-и обратно в host B чрез shared profile restore.

---

## 4. Attachments as Operator Surface

### 4.1 CRUD semantics

- [ ] `DOC-002/4.1/1` — Add/remove attachment трябва да е атомично от гледна точка на metadata + bytes + task touch.
- [ ] `DOC-002/4.1/2` — Reader и edit dialog трябва да отварят attachment по resolve path, а не по stale assumption.

### 4.2 Manual checks

- [ ] `DOC-002/4.2/1` — Open attachment на втора машина след bridge pull.
- [ ] `DOC-002/4.2/2` — Remove attachment на една машина и провери дали другата не държи zombie metadata.

---

## 5. Strategic Questions

1. Ако операторът очаква задача в `Inbox`, а background logic я е преместила в `Today`, има ли достатъчно видим trace?
2. Ако една задача е невидима заради project filter alias drift, колко бързо системата self-heal-ва?
3. Ако edit dialog сериализира whole form, а не delta, има ли достатъчно evidence кое е било човешка промяна и кое collateral rewrite?

---

## 6. Re-Run Triggers

- всеки commit по `task_tray.py`, `tray_dialogs.py`, `tray_sync.py`, `tray_filters.py`;
- нови attachment features;
- нови background workers;
- UX bug reports от типа “създадох, но не виждам”.
