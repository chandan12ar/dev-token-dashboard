# Release checklist

Two routines. Run the first when Claude Code changes under you; run the second before you publish a new version of
this dashboard. The mechanical steps are automated by `tools/release_check.py`; the judgement steps are listed by hand.

```bash
python tools/release_check.py                 # reads your own logs, runs the tests, prints PASS / WARN / FAIL per check
python tools/release_check.py --dir PATH      # logs from another Claude Code version or machine
python tools/release_check.py --skip-tests    # only the log checks
```

Exit code 0 means no FAIL. A WARN is something to read and decide on, not a pass.

## A. A new Claude Code version shows up

Do this when Data Health shows a new version chip, a "Log format changes" banner, an unverified-version finding, or
when you simply update Claude Code and want to know the dashboard still reads it correctly.

1. **Collect logs written by the new version** (a few real sessions: edits, a failing command, a subagent, a long one).
2. `python tools/release_check.py --dir <logs>`. Read every line.
   - **FAIL on log format** means a field the dashboard reads went missing or changed type. Open Data Health, "Log format
     changes", and fix the adapter (`_adapt_claude_code_2`, or add a `_adapt_claude_code_3` when the format forks;
     see [CANONICAL_SCHEMA.md](CANONICAL_SCHEMA.md) §4).
   - **WARN on new fields**: look at the paths. A new field can be a new signal worth reading, or noise. Nothing breaks.
   - **FAIL / WARN on prices or cross-check**: the numbers no longer match what Claude Code recorded about itself
     (price change, new model, new multiplier, or a meaning change). See [COST_MODEL.md](COST_MODEL.md) §6.
3. `python tools/schema_census.py --text --dir <logs>` and compare with [SCHEMA_NOTES.md](SCHEMA_NOTES.md). Note
   what changed, including anything the sentinel cannot see (a field that kept its type but changed meaning).
4. If the format is unchanged and everything passes: grow the baseline
   (`python tools/schema_census.py --write-manifest schema/claude_code_known.json`), then add the version to the
   table in [COMPATIBILITY.md](COMPATIBILITY.md) with the evidence. The release check fails if these two disagree.
   Also write down what the logs did **not** exercise (compaction, subagents, MCP calls, `cost-state`), so "verified"
   is never read as more than it is. To see that, compare the new version with the old one field by field: a long
   list of fields "absent in the new version" is usually just features nobody used in a short session.
5. Run the test suite. If a deliberate behaviour change moved the golden snapshot, regenerate it on purpose
   (`AFI_REGEN_GOLDEN=1`) and read the diff first.
6. Add a line to the changelog in [LOG_RESILIENCE_ROADMAP.md](LOG_RESILIENCE_ROADMAP.md).

If you have no logs from the new version, do **not** add it to the matrix. Leave it unverified.

## B. Before publishing a dashboard release

1. `python tools/release_check.py` passes on your own logs (no FAIL; read each WARN).
2. Prices: compare `PRICING` with Anthropic's published table (platform.claude.com/docs/en/about-claude/pricing)
   and update the date in [COST_MODEL.md](COST_MODEL.md) §7. Data Health "Model prices" should read ok.
3. Docs: if you changed a metric, update [METRICS.md](METRICS.md) and the catalog entry together; if you changed the log
   handling, update [ARCHITECTURE.md](ARCHITECTURE.md) and [SCHEMA_NOTES.md](SCHEMA_NOTES.md).
4. Privacy: nothing new leaves the machine and nothing new is written to disk without an opt-in. The archive stays
   opt-in and command-line only ([SETUP.md](../SETUP.md) section 3c).
5. Fresh-clone check: clone, run `python ai_fluency_insights.py --dump` and the tests with nothing else installed.
6. Update the compatibility table if the environments tested changed (for example a Windows run).

## C. When to run which

| Trigger | Run |
|---|---|
| Claude Code auto-updated and the dashboard looks off | A |
| Banner "Claude Code's log format has changed" | A |
| Anthropic announces a price or model change | B step 2, then A step 2 |
| You are about to tag a release | B |
| Nothing changed | nothing; the running dashboard watches for drift by itself |
