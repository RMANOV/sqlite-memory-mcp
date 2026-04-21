# Publishing Guide — sqlite-memory-mcp v3.5.0

Day-by-day calendar for the v3.5.0 press cycle. Adapted from the
SmartKey v0.5.0 press playbook.

---

## Sequencing principles

1. Short-form first (X, LinkedIn, HN). These generate the initial discovery
   spike and surface early feedback.
2. Long-form second (dev.to, Medium). They reward discussion that is
   already underway from short-form posts.
3. Reddit staggered across different subreddits on different days. Never
   post identical content to multiple subs in the same day — Reddit's
   anti-spam detection flags that pattern.
4. Twitter thread goes out after the X Article so the thread can point
   back to the Article for depth.

---

## Day 0 — 2026-04-21 (today, publish day)

| Window | Channel | File | Notes |
|---|---|---|---|
| Morning | X Article | `2026-04-21-x-article-premium-memory-war-v3.md` | Post as an X Article (long-form), not a regular tweet |
| +15 min | X teaser tweet | (one-liner) | "Premium AI won't be won by bigger models. It will be won by memory discipline." + link to the X Article |
| +60 min | LinkedIn | `2026-04-21-linkedin-premium-memory-war-v3.publish.txt` | Plain-text copy-paste version. Put repo link in the FIRST COMMENT on the post, not in the body. 3 hashtags max. |
| +2 hours | Hacker News | `2026-04-21-hn-submission.md` | Submit around 10:00 UTC for best US morning visibility. Post from your real account. |

---

## Day 1 — 2026-04-22

| Window | Channel | File | Notes |
|---|---|---|---|
| Morning | Twitter thread | `2026-04-21-twitter-thread-premium-memory-war.md` | 8 tweets, post as a native thread (not cross-post). Tag tweet 1 as the root. |
| Afternoon | Reddit r/LocalLLaMA | `2026-04-21-reddit-premium-memory-war.md` (LocalLLaMA section) | Flair "Resources" or "Discussion". Answer questions in thread for first 4 hours. |

---

## Day 2 — 2026-04-23

| Window | Channel | File | Notes |
|---|---|---|---|
| Morning | dev.to | `2026-04-21-devto-premium-memory-war.md` | Flip `published: false` to `true` in the front matter. Or paste into the dev.to editor directly. Add cover image if possible. |

---

## Day 3 — 2026-04-24

| Window | Channel | File | Notes |
|---|---|---|---|
| Afternoon | Reddit r/selfhosted | `2026-04-21-reddit-premium-memory-war.md` (selfhosted section) | Different angle than r/LocalLLaMA — lead with cross-machine bridge sync. |

---

## Day 4 — 2026-04-25

| Window | Channel | File | Notes |
|---|---|---|---|
| Morning | Medium | `2026-04-21-medium-premium-memory-war.md` | Target a Medium publication if possible (Better Programming, The Startup). Cold posts get less reach. |

---

## Day 5 — 2026-04-26

| Window | Channel | File | Notes |
|---|---|---|---|
| Morning | Reddit r/programming | `2026-04-21-reddit-premium-memory-war.md` (r/programming section) | Architecture angle — "putting the gate in the public-core repo". Good discussion bait. |

---

## Day 6–7 — engagement

- Reply to comments across all channels within 24 hours.
- Track metrics: stars/forks on GitHub, comment engagement per channel.
- If HN submission hit front page: watch the CI load.
- If Reddit drove traffic: evaluate install drop-off on README Quick Start.
- Log outcomes into sqlite-kb as a follow-up observation on the
  "sqlite-memory-mcp v3.5.0 Launch 2026-04-21" entity.

---

## Cross-channel rules

- Never post the same exact text to two channels in the same day.
- Always include the same final tagline across channels to build recall:
  "Brief me. Warn me. Shape the room. Lock the wrong door."
- LinkedIn: no markdown, link in first comment, 3 hashtags max.
- Twitter: hashtags only in the last tweet.
- Dev.to: YAML front matter, ASCII diagrams where they help.
- Medium: lead with personal narrative, not the feature list.
- HN: short body (under 200 words), leave technical gaps for discussion.
- Reddit: tailor per subreddit, stagger across days.

---

## Competitive note

OyaAIProd (oya.ai, 6000+ customers at $1K/mo, uses DuckDB-based memory)
forked this repo on 2026-03-29 for pure research/benchmarking. Expect
their audience to intersect with this launch, especially on
LinkedIn/SDR-adjacent channels. The comparison table in the Reddit
piece and the HN discussion prompt both explicitly invite the
DuckDB-vs-SQLite conversation. That is intentional — we would rather
host the comparison than duck it.
