# DOC-003 — Operator and Retrieval Risk Questions

**Intent:** да спре regressions от типа “има го, но не се вижда / не се намира / мести се само”.

## Questions

- [ ] Startup path-ът read-only ли е на практика, не само по намерение?
- [ ] Background cadence прави ли pull-only или случайно full sync/push?
- [ ] Новото UI поведение съвпада ли с operator expectation за `Inbox`, `Today`, `Done`, `Archived`?
- [ ] `description` остава ли primary body field във wording-а и интерфейса?
- [ ] `notes` видими ли са за четене, без да се третират като основен контент по подразбиране?
- [ ] Search path-ът намира ли remembered phrase без точен `project`/`status`/`type` контекст?
- [ ] Project/filter normalization избягва ли split project names и exact-match drift?
- [ ] Result-ът казва ли къде е match-ът (`title`, `description`, `notes`, `project`, `observation`)?
- [ ] Weak context/enrich preview скрива ли се при executor/read path?
- [ ] Ако change-ът е “само UX”, сигурен ли си, че не въвежда hidden writer или filter invisibility bug?

## Stop Rule

Ако не можеш да симулираш user фраза от паметта и да обясниш:

> “С кое действие и в кой изглед ще го намери?”

промяната не е operator-safe.
