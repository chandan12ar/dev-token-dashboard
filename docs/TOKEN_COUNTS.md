# Why Our Token Count Differs From the Official Claude Code Dashboard

The Claude Code desktop app has a built-in stats panel (the "Overview /
Models" cards on the home screen). Its **Total tokens** number is much higher
than the input/output totals this dashboard shows — on the same machine, on
the same day. Both read the **same local session logs**, so the difference is
purely in *how* the log lines are counted. This document explains the
difference and why our method is the accurate one.

## The observed discrepancy (2026-07-08, this machine)

| Metric | Official app panel | This dashboard | Verdict |
|---|---|---|---|
| Sessions | 4 | 4 | identical |
| Messages | 496 | 476 | different filters (we exclude meta entries, interruptions, command output) |
| Total tokens | **283.2k** | **~83k** input+output | explained below |

## Root cause: duplicate usage blocks in the logs

When Claude streams a response, Claude Code writes **several JSONL lines for
that single API response** — roughly one per content block (text, tool call,
etc.). Every one of those lines carries a copy of the **same** `usage` object
(same `message.id`, same `requestId`, same token numbers).

So for one API response that cost, say, 1,200 output tokens, the log may
contain 3 lines each saying "1,200 output tokens". Sum every line and you
count those tokens three times.

## The proof

We recomputed the totals directly from the raw logs both ways
(`compare_counts.py` style analysis, run the same morning as the screenshot):

| Counting method | input + output tokens |
|---|---|
| Sum **every** log line (no deduplication) | **294,667** |
| Count each API response **once** (dedup by `message.id` + `requestId`) | **83,213** |

The official panel showed **283.2k** — matching the *no-deduplication* sum
almost exactly (the small gap is just log growth between the screenshot at
09:56 and the recount minutes later). The deduplicated figure is ~3.5× lower.

Conclusion: the official panel's "Total tokens" sums the usage block on every
log line. This dashboard counts each API response exactly once.

## Why our count is the correct one

1. **It matches what the API actually processed.** Each `usage` object
   describes one API response. That response happened once — counting its
   tokens once is the ground truth; counting it per-log-line is an artifact
   of how streaming gets journaled to disk.
2. **It's what billing would say.** If you compared against real Anthropic
   API usage reports, the per-response (deduplicated) numbers are the ones
   that line up. The inflated sum corresponds to no real quantity — you were
   never charged (or rate-limited) for those tokens more than once.
3. **It's the established community practice.** `ccusage`, the most widely
   used third-party Claude Code usage tool, deduplicates the same way
   (by message ID + request ID) for exactly this reason.
4. **It's internally consistent.** Our per-day, per-model, and per-project
   splits all use the same rule, so they sum to the totals. A mixed or
   inflated count would break down the moment you cross-check the parts
   against the whole.

## What the official panel is still good for

Sessions, active days, streaks, and peak hour are fine — those don't depend
on token counting. Treat its "Total tokens" as a rough activity indicator,
not an accounting figure.

## One more thing ours shows that the official panel folds away

The overwhelming majority of real token volume is **cache reads** — context
replayed from prompt cache at ~10% of the input price (≈28M cache-read tokens
on this machine vs ~83k fresh input+output). This dashboard reports cache
read/write separately, plus a cache-hit %, because lumping them in with fresh
tokens would misrepresent both the volume and the cost.
