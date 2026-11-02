# Cost Model: how the dashboard prices usage, and how that was checked

The "Estimated API cost" figures (cost tile, cost per 100 lines, cost per verified session, cache savings, model mix,
cost spikes) all come from one function, `price_parts()`, and one table, `PRICING`, in `ai_fluency_insights.py`.
This page records why they look the way they do. On a Pro or Max subscription none of this is a bill: it is *what
the same usage would cost at API pay-as-you-go prices*.

## 1. What was wrong, and how it was found

Phase 3 compared the dashboard's cost estimate with the dollar figure Claude Code writes into its own logs
(`cost-state` lines) and found the dashboard about 18% higher. The investigation:

1. **Read the published prices** (platform.claude.com/docs/en/about-claude/pricing, 2026-11-02) and the fields Claude
   Code logs.
2. **Priced Claude Code's own token counts with the published rules** and compared with Claude Code's own dollar figure.
   Both sides use the same tokens, so any difference is the price rules alone.

| Snapshot | Model | Claude Code recorded | Published rules | Old dashboard formula |
|---|---|---|---|---|
| 1 | Sonnet 5.5 | $1.78896 | $1.78896 (0.0000%) | $2.09 (+16.8%) |
| 2 | Sonnet 5.5 | $2.95684 | $2.95684 (0.0000%) | $3.65 (+23.4%) |
| 3 | Sonnet 5.5 | $10.38297 | $10.38297 (0.0000%) | $14.76 (+42.2%) |
| 1 to 3 | Haiku 5.5 | $0.01417 | $0.01417 (0.0000%) | $0.14 (+900%) |

The published rules reproduce Claude Code's own cost to five decimals on all six points. The old formula was wrong,
and **wrong by more the longer a session ran**, because long sessions are dominated by cache reads.

## 2. The four mistakes in the old model

| Rule | Old dashboard | Published (and now used) |
|---|---|---|
| Cache **reads** | 0.10x input for every model | 0.10x for most models; **0.05x** for Sonnet 5.5 and Opus 5.5; **0.025x** for Fable 5.1 and Mythos 5.1 |
| Cache **writes** | 1.25x input for all | **1.25x for the 5-minute cache, 2x for the 1-hour cache.** The log splits them (`usage.cache_creation.ephemeral_5m/1h_input_tokens`); every write in the measured session was 1-hour |
| Model prices | `opus` $15/$75, `fable` $15/$75, `haiku` $1/$5 | Opus 5.5 **$4/$20**, Opus 5 to 4.5 $5/$25, Opus 4.1 and older $15/$75; Fable $10/$50; Haiku 5.5 **$0.10/$0.50** (Haiku 4.5 $1/$5) |
| Model matching | substring `opus`, `sonnet`, `haiku` | version-aware (`opus-5-5` before `opus-5` before `opus`), because the price depends on the version |

For an Opus 5.5 user the old table over-stated the base prices by almost 4x. The 18% first reported was only the
Sonnet cache effect on a short session.

## 3. The rules now in use

For one response, `price_parts(rates, input, output, cache_write_5m, cache_write_1h, cache_read, speed, geo, searches)`:

```
cost = ( input·in + output·out + cache_read·in·read + write_5m·in·1.25 + write_1h·in·2.0 )
       × fast_multiplier × region_multiplier / 1,000,000  +  web_searches × $0.01
```

- **Haiku 5.5 is priced per request by prompt length**: a request whose prompt (input + cache reads + cache writes)
  is over 100,000 tokens pays $0.50 / $2.50 instead of $0.10 / $0.50, for the whole request.
- **Fast mode** (`usage.speed == "fast"`): 2x, for Opus 5.5, 5 and 4.8 only.
- **US-only inference** (`usage.inference_geo == "us"`): 1.1x, for Claude 4.6 and later.
- **Web search**: $10 per 1,000 searches (`usage.server_tool_use.web_search_requests`).
- No long-context surcharge for 4.6 and later models (the full window is standard price).
- Cache savings = what caching saved against sending the same tokens uncached, minus the premium paid for writes.

### What is verified and what is not

| | Status |
|---|---|
| Sonnet 5.5 base, 1-hour writes, 0.05x cache reads | **Verified** against Claude Code's own cost (3 snapshots, 0.0000%) |
| Haiku 5.5 base tier | **Verified** against Claude Code's own cost (1 snapshot, 0.0000%) |
| Haiku 5.5 over-100k tier, fast mode, US-only multiplier, web search | From the published table only. The measured session had none of these (all 149 responses: standard speed, global region, no web searches; most prompts were over 100k tokens, which only matters for Haiku 5.5) |
| Opus, Fable, Sonnet 4.x and older models' rows | From the published table only |
| Claude 3.x Haiku (older than 3.5) | Approximated with the Haiku 4.5 row (not on the published page) |

## 4. What the dashboard now checks for itself

Data Health shows **Model prices**:

- It reprices the token counts in Claude Code's own `cost-state` totals with our table and compares with the dollar figure
  recorded there (tolerance 1%). A difference means the price table is wrong or stale: a price changed, a model is new, or
  fast mode / US-only inference was used. Haiku 5.5 is not judged from totals (its price depends on each request).
- Any model id that matches no `PRICING` entry is priced with `DEFAULT_PRICE` ($3 / $15) and listed, with its response count.

## 5. Why the dashboard still comes out a little below Claude Code (about 2 to 3%)

Aligned to each snapshot, the new formula gives −2.7%, −2.7% and −2.3% against Claude Code's Sonnet 5.5 figure
(old formula: +12.6%, +18.8%, +38.1%). That gap is tokens Claude Code counted that never appear as assistant lines in the
transcript (measured earlier: cache reads at 94% of its figure, input tokens at 7%). It is a limit of the transcript, not
of the price table: the check in section 4, which uses Claude Code's own counts, matches exactly.

## 6. Keeping it current

Prices move. When Anthropic publishes a change:

1. Edit the matching row in `PRICING` (or add a row above the generic one for a new model).
2. If you want older days to keep the old price, add `PRICE_HISTORY["<key>"] = [("YYYY-MM-DD", old_rates)]`: days before that
   date use the old rates. No history ships, because there are no verified historical prices.
3. Open Data Health: **Model prices** should read ok. If it says a price differs from what Claude Code recorded, the row is wrong.

A separate, deliberately rough heuristic, `weighted_tokens` (used for the plan-usage estimate and toast notifications), also
weighs cache traffic, but as a stand-in for Anthropic's undisclosed quota rules, not as dollars. It was left as it was.

## 7. Source and date

Prices and multipliers: Anthropic, "Pricing", platform.claude.com/docs/en/about-claude/pricing, read 2026-11-02
(model table and footnotes, prompt caching, long-context, fast-mode, data-residency and tool-use sections). Verification data:
`cost-state` lines of one real Claude Code 2.1.316 session. Tests: `CostModelTests` (the snapshot figures above are fixtures,
numbers only) and `PriceCheckTests`.
