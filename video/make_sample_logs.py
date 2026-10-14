#!/usr/bin/env python3
"""Generate SAMPLE Claude Code session logs for previewing the walkthrough video.

This is synthetic data, NOT anyone's real usage. It exists so the recording
pipeline can be tested on a machine that has no Claude logs (CI, a cloud
sandbox). The final video should be recorded against real logs:

    python video/build_video.py            # reads ~/.claude/projects

Usage:
    python video/make_sample_logs.py OUT_DIR [--days 80] [--seed 7]
"""
import argparse
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

PROJECTS = [
    # dir-name Claude Code would create, cwd, branches, typical prompts
    ("-home-dev-checkout-service", "/home/dev/checkout-service", ["main", "feat/retry-payments", "fix/cart-rounding"],
     ["Add idempotency keys to the payment retry path", "Why does the cart total round differently on the invoice?",
      "Write tests for the refund webhook handler", "Refactor the pricing module into smaller functions",
      "Investigate the flaky order-status integration test"]),
    ("-home-dev-mobile-app", "/home/dev/mobile-app", ["main", "feat/offline-sync", "chore/deps"],
     ["Implement offline queue for outgoing messages", "Fix the crash when the app resumes from background",
      "Update the navigation to the new design", "Add unit tests for the sync reducer"]),
    ("-home-dev-data-pipeline", "/home/dev/data-pipeline", ["main", "feat/dedupe-job", "fix/timezone-bug"],
     ["Dedupe events by (user_id, event_id) before the aggregation step", "The daily rollup is off by one day near midnight",
      "Add a backfill command with a dry-run flag", "Profile why the nightly job takes 40 minutes"]),
    ("-home-dev-docs-site", "/home/dev/docs-site", ["main", "docs/api-reference"],
     ["Document the new webhook events", "Fix broken anchors in the API reference", "Add a migration guide for v2"]),
    ("-home-dev-internal-tools", "/home/dev/internal-tools", ["main", "feat/slack-bot"],
     ["Build a Slack command that posts the deploy summary", "Explore how the permissions service caches roles"]),
]

TITLES = {
    "feature": ["Add retry-safe payment idempotency", "Implement offline message queue", "Build Slack deploy summary command",
                "Add backfill command with dry-run", "Create webhook events reference"],
    "bugfix": ["Fix cart rounding mismatch on invoices", "Fix crash on resume from background", "Fix timezone off-by-one in rollup",
               "Fix flaky order status test"],
    "refactor": ["Refactor pricing module", "Refactor sync reducer"],
    "docs": ["Document webhook events", "Write v2 migration guide"],
    "explore": ["Explore permissions service caching", "Investigate slow nightly job"],
}

FILES = {
    "-home-dev-checkout-service": ["src/payments/retry.py", "src/cart/totals.py", "src/pricing/rules.py", "src/webhooks/refund.py",
                                    "tests/test_retry.py", "tests/test_totals.py"],
    "-home-dev-mobile-app": ["src/sync/queue.ts", "src/sync/reducer.ts", "src/nav/Router.tsx", "src/screens/Inbox.tsx",
                              "src/sync/reducer.test.ts"],
    "-home-dev-data-pipeline": ["jobs/dedupe.py", "jobs/rollup.py", "cli/backfill.py", "tests/test_dedupe.py"],
    "-home-dev-docs-site": ["docs/api/webhooks.md", "docs/guides/migrate-v2.md", "docs/api/index.md"],
    "-home-dev-internal-tools": ["bot/commands/deploy.js", "bot/lib/slack.js", "bot/commands/deploy.test.js"],
}

VERIFY_CMDS = ["pytest -q", "pytest tests/ -x", "npm test", "npm run lint", "ruff check .", "tsc --noEmit", "npm run build",
               "python -m unittest -v"]
OTHER_CMDS = ["git status", "git diff --stat", "ls -la", "git log --oneline -5", "python scripts/check_env.py", "git add -A",
              "git commit -m 'wip'"]

ASST_TEXT = "Done. I made the change and checked the result."


class Writer:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.n = 0

    def put(self, obj):
        self.f.write(json.dumps(obj) + "\n")

    def close(self):
        self.f.close()


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def session(w, rng, proj, cwd, branches, prompts, files, start, skill, quality, uses_new):
    """Write one session. `skill` (0..1) drives how fluent the habits are, so the
    dashboard shows improvement over time. `quality` = chance a verify step runs."""
    sid = str(uuid.uuid4())
    t = start
    branch = rng.choice(branches)
    kind = rng.choices(["feature", "bugfix", "refactor", "docs", "explore"], [34, 28, 12, 10, 16])[0]
    if proj.endswith("docs-site"):
        kind = rng.choice(["docs", "docs", "feature"])
    title = rng.choice(TITLES[kind])
    version = "2.1.296"

    def base(**kw):
        d = {"sessionId": sid, "timestamp": iso(t), "cwd": cwd, "version": version, "gitBranch": branch,
             "entrypoint": "cli", "isSidechain": False}
        d.update(kw)
        return d

    model = rng.choices(["claude-opus-5", "claude-sonnet-5", "claude-haiku-5"],
                        [0.30 + 0.25 * skill, 0.55 - 0.15 * skill, 0.15 - 0.10 * skill])[0]

    def asst(tool=None, text=None, stop=None, out=None, side=False):
        nonlocal t
        w.n += 1
        mid = "m%d" % w.n
        content = []
        if tool:
            content.append({"type": "tool_use", "id": "tu%d" % w.n, "name": tool[0], "input": tool[1]})
        if text:
            content.append({"type": "text", "text": text})
        # cache behaviour improves with longer, warmer sessions
        usage = {"input_tokens": rng.randint(4, 60),
                 "output_tokens": out if out is not None else rng.randint(120, 1800),
                 "cache_creation_input_tokens": rng.randint(0, 2500) if rng.random() < 0.18 else rng.randint(0, 200),
                 "cache_read_input_tokens": rng.randint(18000, 140000)}
        e = base(type="assistant", uuid=mid, requestId="r%d" % w.n,
                 message={"id": mid, "model": model, "stop_reason": stop or ("tool_use" if tool else "end_turn"),
                          "content": content, "usage": usage})
        e["isSidechain"] = side
        if rng.random() < 0.35:
            e["thinkingDurationMs"] = rng.randint(1500, 22000)
            e["effort"] = rng.choice(["low", "medium", "high"])
        w.put(e)
        return "tu%d" % w.n

    def result(tid, ok=True, text="ok", side=False):
        nonlocal t
        r = {"type": "tool_result", "tool_use_id": tid, "content": text}
        if not ok:
            r["is_error"] = True
        e = base(type="user", message={"role": "user", "content": [r]})
        e["isSidechain"] = side
        if rng.random() < 0.5:
            e["permissionDecision"] = {"decision": "allow", "source": rng.choice(["user", "rule", "mode"])}
        w.put(e)

    def tick(lo=1, hi=9):
        nonlocal t
        t += timedelta(seconds=rng.uniform(lo, hi))

    w.put({"type": "permission-mode", "permissionMode": "default", "sessionId": sid})
    if rng.random() < 0.15 + 0.5 * skill and uses_new:
        w.put({"type": "permission-mode", "permissionMode": "plan", "sessionId": sid})
        w.put({"type": "permission-mode", "permissionMode": "acceptEdits", "sessionId": sid})
    if rng.random() < 0.25 + 0.4 * skill:
        w.put(base(type="user", message={"role": "user",
                    "content": "<command-name>/init</command-name>" if rng.random() < 0.2
                    else "<command-name>/compact</command-name>"}))
        tick()

    n_turns = rng.randint(2, 6)
    for turn in range(n_turns):
        tick(3, 40)
        # prompt quality: more specific and longer as skill rises
        base_prompt = rng.choice(prompts)
        if turn == 0:
            text = base_prompt
            if rng.random() < skill:
                text += (". Context: the relevant code is in %s. Acceptance: existing tests still pass and add one for the new behaviour."
                         % rng.choice(files))
        else:
            r = rng.random()
            if r < 0.18 * (1.1 - skill):
                text = rng.choice(["No, that's not what I meant - keep the public API the same",
                                   "Actually revert that and only change the helper",
                                   "Wrong file, it's in the other module"])
            else:
                text = rng.choice(["Looks good, now handle the empty-input case", "Also update the docs for this",
                                   "Can you explain why you chose that approach?", "Now run the full suite",
                                   "Nice. Tighten the error message"])
        w.put(base(type="user", message={"role": "user", "content": text}))
        if rng.random() < 0.05:
            tick(2, 6)
            w.put(base(type="user", message={"role": "user", "content": "[Request interrupted by user]"}))
            continue

        # exploration
        for _ in range(rng.randint(1, 5)):
            tick()
            tool = rng.choice(["Read", "Read", "Grep", "Glob"])
            inp = {"file_path": "/x/" + rng.choice(files)} if tool == "Read" else {"pattern": "def |class "}
            tid = asst(tool=(tool, inp))
            tick(0.3, 2)
            result(tid)
        if rng.random() < 0.10 + 0.25 * skill:
            tick()
            tid = asst(tool=("WebSearch", {"query": "python retry idempotency best practice"}))
            tick(1, 4)
            result(tid)
        if uses_new and rng.random() < 0.07 + 0.2 * skill:
            tick()
            tid = asst(tool=("Agent", {"subagent_type": rng.choice(["Explore", "general-purpose", "Plan"]),
                                       "prompt": "Research how this is wired"}))
            for _ in range(rng.randint(3, 8)):
                tick(0.5, 3)
                t2 = asst(tool=("Read", {"file_path": "/x/" + rng.choice(files)}), side=True)
                result(t2, side=True)
            result(tid)
        if uses_new and rng.random() < 0.05 + 0.2 * skill:
            tick()
            tid = asst(tool=("TodoWrite", {"todos": [{"content": "step", "status": "pending"}]}))
            result(tid)
        if uses_new and rng.random() < 0.03 + 0.12 * skill:
            tick()
            tid = asst(tool=("Skill", {"skill": "review"}))
            result(tid)
        if uses_new and rng.random() < 0.02 + 0.10 * skill:
            tick()
            tid = asst(tool=("mcp__github__get_pull_request", {"number": rng.randint(10, 400)}))
            result(tid)

        # building
        wrote = kind != "explore" and rng.random() < 0.82
        if wrote:
            for _ in range(rng.randint(1, 4)):
                tick(2, 14)
                f = rng.choice(files)
                n = rng.randint(4, 60 + int(60 * skill))
                body = "\n".join("    x = %d" % i for i in range(n))
                if rng.random() < 0.25:
                    tid = asst(tool=("Write", {"file_path": "/x/" + f, "content": body}), out=rng.randint(600, 3200))
                else:
                    tid = asst(tool=("Edit", {"file_path": "/x/" + f, "old_string": "x", "new_string": body}),
                               out=rng.randint(300, 2200))
                tick(0.4, 2)
                result(tid)
            # verification
            if rng.random() < quality:
                tick(2, 8)
                cmd = rng.choice(VERIFY_CMDS)
                tid = asst(tool=("Bash", {"command": cmd}))
                tick(2, 40)
                fail = rng.random() < 0.22
                result(tid, ok=not fail, text="Exit code 1\nFAILED test" if fail else "passed")
                if fail:
                    tick(2, 10)
                    tid = asst(tool=("Edit", {"file_path": "/x/" + rng.choice(files), "old_string": "x",
                                              "new_string": "    y = 1\n    z = 2"}))
                    result(tid)
                    tick(2, 8)
                    tid = asst(tool=("Bash", {"command": cmd}))
                    tick(2, 30)
                    result(tid)
        if rng.random() < 0.5:
            tick()
            tid = asst(tool=("Bash", {"command": rng.choice(OTHER_CMDS)}))
            tick(0.5, 5)
            result(tid, ok=rng.random() > 0.06, text="ok")
        tick(1, 5)
        asst(text=ASST_TEXT, stop="end_turn", out=rng.randint(150, 900))

    if rng.random() < 0.55 + 0.3 * skill:
        w.put({"type": "system", "subtype": "stop_hook_summary", "sessionId": sid, "timestamp": iso(t)})
    w.put({"type": "ai-title", "sessionId": sid, "aiTitle": title})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--days", type=int, default=80)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    writers = {}
    for pdir, *_ in PROJECTS:
        d = os.path.join(a.out_dir, pdir)
        os.makedirs(d, exist_ok=True)
        writers[pdir] = Writer(os.path.join(d, "history.jsonl"))
    for back in range(a.days, -1, -1):
        day = today - timedelta(days=back)
        progress = 1 - back / a.days                       # 0 -> 1 across the window
        skill = 0.15 + 0.75 * progress                     # habits improve over time
        quality = 0.12 + 0.70 * progress                   # verification habit grows
        if day.weekday() >= 5 and rng.random() < 0.7:
            continue                                       # mostly rest on weekends
        if rng.random() < 0.12:
            continue
        for _ in range(rng.choice([1, 2, 2, 3, 4])):
            pdir, cwd, branches, prompts = rng.choice(PROJECTS)
            hour = rng.choices([9, 10, 11, 13, 14, 15, 16, 17, 20, 22, 23], [6, 9, 8, 6, 9, 8, 6, 4, 3, 2, 1])[0]
            start = day.replace(hour=hour, minute=rng.randint(0, 50))
            if start > datetime.now().astimezone():
                continue
            session(writers[pdir], rng, pdir, cwd, branches, prompts, FILES[pdir], start, skill, quality,
                    uses_new=progress > 0.25)
    for w in writers.values():
        w.close()
    print("wrote sample logs to", a.out_dir)


if __name__ == "__main__":
    main()
