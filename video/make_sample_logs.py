"""Generate realistic, deterministic SAMPLE Claude Code session logs for the demo video.

Nothing here is real usage. The data tells a story on purpose: ten weeks ago the
developer wrote short prompts, corrected Claude a lot and rarely ran tests; over
time they adopt plan mode, tests after edits, subagents and a CLAUDE.md, so the
trend charts have something to show. Hooks, MCP tools, skills, web research and
slash commands are never used, so the coach has real next moves.

    python video/make_sample_logs.py --out /tmp/sample-claude
    python ai_fluency_insights.py --dir /tmp/sample-claude/projects
"""
import argparse
import json
import os
import random
import shutil
from datetime import datetime, timedelta, timezone

DAYS = 70

PROJECTS = [
    # name, weight, has CLAUDE.md, languages/files
    ("storefront-web", 0.32, True, ["src/cart/Cart.tsx", "src/checkout/Payment.tsx", "src/api/orders.ts",
                                     "src/components/ProductCard.tsx", "src/cart/useCart.ts"]),
    ("payments-api", 0.26, True, ["app/routes/charges.py", "app/services/refunds.py", "app/models/invoice.py",
                                   "tests/test_refunds.py", "app/webhooks/stripe.py"]),
    ("mobile-app", 0.18, False, ["lib/screens/home.dart", "lib/state/session.dart", "lib/widgets/order_tile.dart"]),
    ("data-pipeline", 0.14, False, ["etl/load_orders.py", "etl/transform.py", "dags/nightly.py", "tests/test_transform.py"]),
    ("infra", 0.10, True, ["terraform/main.tf", "k8s/deploy.yaml", ".github/workflows/ci.yml"]),
]

TITLES = {
    "feature": ["Add pagination to orders list", "Implement refund webhook handler", "Build cart summary drawer",
                "Add retry logic to nightly load", "Create invoice PDF export", "Add dark mode toggle",
                "Implement saved payment methods", "Add rate limiting to charges API"],
    "bugfix": ["Fix double charge on retry", "Fix cart total rounding error", "Debug flaky checkout test",
               "Fix crash on empty order history", "Fix timezone bug in nightly job"],
    "refactor": ["Refactor payment service into modules", "Clean up cart state hooks", "Simplify ETL transforms"],
    "docs": ["Update README setup steps", "Document refund API endpoints"],
    "explore": ["Investigate slow product search", "Understand auth middleware flow", "Review webhook retries"],
}

VAGUE = ["fix it", "make the cart work", "add tests", "why is this broken", "update the api",
         "do the refund thing", "clean this up", "it still fails"]
CLEAR = [
    "Add cursor-based pagination to GET /orders. Keep the existing response shape, add a next_cursor field, "
    "and update the tests in tests/test_orders.py. Run pytest when done.",
    "The refund webhook double-processes events when Stripe retries. Make the handler idempotent using the "
    "event id, add a regression test, then run the test suite.",
    "Plan first: we need saved payment methods in checkout. Propose the data model and API changes before "
    "editing anything, then implement step by step.",
    "Refactor useCart into smaller hooks without changing behaviour. Run the unit tests and the type check after.",
    "Investigate why product search takes 2 seconds. Use a subagent to profile the query, then suggest fixes.",
]
CORRECTIONS = ["no, keep the old response format", "actually, use the existing helper instead",
               "that's not what I meant, revert the CSS change", "don't touch the migrations"]


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


class Session:
    def __init__(self, rng, sid, project, cwd, start, model):
        self.rng, self.sid, self.project, self.cwd = rng, sid, project, cwd
        self.t, self.model = start, model
        self.entries, self.n = [], 0
        self.branch = rng.choice(["main", "feature/pagination", "fix/refunds", "feature/saved-cards"])

    def base(self, **kw):
        d = {"sessionId": self.sid, "timestamp": iso(self.t), "cwd": self.cwd, "version": "2.1.296",
             "gitBranch": self.branch, "entrypoint": "cli"}
        d.update(kw)
        return d

    def adv(self, lo, hi):
        self.t += timedelta(seconds=self.rng.uniform(lo, hi))

    def usage(self, out):
        r = self.rng
        cw = r.randint(15_000, 60_000) if self.n == 1 else (r.randint(2_000, 9_000) if r.random() < 0.3 else 0)
        return {"input_tokens": r.randint(4, 40), "output_tokens": out,
                "cache_creation_input_tokens": cw, "cache_read_input_tokens": r.randint(40_000, 180_000)}

    def prompt(self, text):
        self.adv(20, 120)
        self.entries.append(self.base(type="user", message={"role": "user", "content": text}))

    def slash(self, name):
        self.adv(5, 30)
        self.entries.append(self.base(type="user", message={"role": "user",
                            "content": "<command-name>/%s</command-name>" % name}))

    def tool(self, name, inp, fail=False, run=(1, 8), side=False):
        self.n += 1
        tid, mid = "toolu_%s_%d" % (self.sid[:8], self.n), "msg_%s_%d" % (self.sid[:8], self.n)
        self.adv(2, 14)
        e = self.base(type="assistant", uuid=mid, requestId="req_" + mid,
                      message={"id": mid, "model": self.model, "stop_reason": "tool_use",
                               "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                               "usage": self.usage(self.rng.randint(150, 2200))})
        if side:
            e["isSidechain"] = True
        self.entries.append(e)
        self.adv(*run)
        res = {"type": "tool_result", "tool_use_id": tid,
               "content": "Exit code 1\nFAILED tests/test_x.py::test_case" if fail else "ok"}
        if fail:
            res["is_error"] = True
        u = self.base(type="user", message={"role": "user", "content": [res]})
        if side:
            u["isSidechain"] = True
        self.entries.append(u)

    def end_turn(self):
        self.n += 1
        mid = "msg_%s_%d" % (self.sid[:8], self.n)
        self.adv(3, 20)
        self.entries.append(self.base(type="assistant", uuid=mid, requestId="req_" + mid,
                            message={"id": mid, "model": self.model, "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": "Done."}],
                                     "usage": self.usage(self.rng.randint(80, 600))}))


def edit_block(rng, path, big=False):
    lines = rng.randint(20, 70) if big else rng.randint(2, 16)
    body = "\n".join("    line_%d = compute(%d)" % (i, i) for i in range(lines))
    if big:
        return "Write", {"file_path": path, "content": body}
    return "Edit", {"file_path": path, "old_string": "x", "new_string": body}


def make_session(rng, sid, proj, cwd, files, start, skill):
    """skill: 0.0 (week 1 habits) .. 1.0 (today's habits)."""
    r = rng
    model = r.choices(["claude-opus-5", "claude-sonnet-5", "claude-haiku-5"],
                      [0.25 + 0.2 * skill, 0.65 - 0.15 * skill, 0.10 - 0.05 * skill])[0]
    s = Session(r, sid, proj, cwd, start, model)
    kind = r.choices(list(TITLES), [0.42, 0.24, 0.12, 0.07, 0.15])[0]
    s.entries.append({"type": "ai-title", "sessionId": sid, "aiTitle": r.choice(TITLES[kind])})
    plan = r.random() < 0.05 + 0.45 * skill
    s.entries.append({"type": "permission-mode", "sessionId": sid,
                      "permissionMode": "plan" if plan else r.choice(["default", "acceptEdits", "auto"])})
    turns = r.randint(2, 7)
    for ti in range(turns):
        if ti > 0 and r.random() < 0.35 - 0.25 * skill:
            s.prompt(r.choice(CORRECTIONS))
        else:
            s.prompt(r.choice(CLEAR) if r.random() < 0.15 + 0.7 * skill else r.choice(VAGUE))
        if r.random() < 0.08 - 0.05 * skill:
            s.adv(3, 10)
            s.entries.append(s.base(type="user", message={"role": "user", "content": "[Request interrupted by user]"}))
            continue
        if plan and ti == 0:
            s.tool("EnterPlanMode", {})
        for _ in range(r.randint(1, 4)):
            s.tool(r.choice(["Read", "Read", "Grep", "Glob"]), {"file_path": r.choice(files), "pattern": "def "})
        if r.random() < 0.1 + 0.3 * skill:
            s.tool("Agent", {"subagent_type": r.choice(["Explore", "general-purpose", "code-reviewer"]),
                             "prompt": "research"}, run=(20, 90))
            for _ in range(r.randint(2, 4)):
                s.tool("Read", {"file_path": r.choice(files)}, side=True)
        if r.random() < 0.05 + 0.25 * skill:
            s.tool("TodoWrite", {"todos": []})
        if kind != "explore":
            for _ in range(r.randint(1, 4)):
                name, inp = edit_block(r, r.choice(files), big=r.random() < 0.2)
                s.tool(name, inp, fail=r.random() < 0.04)
            if r.random() < 0.12 + 0.68 * skill:   # verify after editing
                cmd = r.choice(["pytest -q", "npm test", "npm run lint", "npm run build", "ruff check ."])
                failed = r.random() < 0.3
                s.tool("Bash", {"command": cmd}, fail=failed, run=(5, 60))
                if failed:
                    name, inp = edit_block(r, r.choice(files))
                    s.tool(name, inp)
                    s.tool("Bash", {"command": cmd}, run=(5, 50))
            elif r.random() < 0.3:
                s.tool("Bash", {"command": "git status"}, run=(1, 2))
        s.end_turn()
    return s.entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = os.path.abspath(a.out)
    shutil.rmtree(out, ignore_errors=True)
    projects_dir = os.path.join(out, "projects")
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    today = now.replace(hour=0, minute=0)
    per_project = {p[0]: [] for p in PROJECTS}
    for p in PROJECTS:   # the repos the sessions ran in; some carry project memory
        repo = os.path.join(out, "repos", p[0])
        os.makedirs(repo, exist_ok=True)
        if p[2]:
            with open(os.path.join(repo, "CLAUDE.md"), "w") as f:
                f.write("# %s\nRun the tests after every change.\n" % p[0])
    n = 0
    for back in range(DAYS, -1, -1):
        day = today - timedelta(days=back)
        skill = 0.62 * (1 - back / DAYS)
        weekend = day.weekday() >= 5
        if rng.random() < (0.15 if weekend else 0.6) * (0.75 + 0.25 * skill):
            for _ in range(rng.randint(1, 2 if weekend else 4)):
                proj = rng.choices(PROJECTS, [p[1] for p in PROJECTS])[0]
                if rng.random() < 0.18 * (1 - skill):
                    hour = rng.choice([22, 23, 0, 1])     # late nights fade as habits improve
                else:
                    hour = rng.randint(9, 18)
                start = day + timedelta(hours=hour, minutes=rng.randint(0, 59))
                if start > now:
                    start = now - timedelta(hours=rng.randint(1, 4))
                n += 1
                sid = "%08x-0000-4000-8000-%012x" % (rng.getrandbits(32), n)
                cwd = os.path.join(out, "repos", proj[0])
                per_project[proj[0]].append(make_session(rng, sid, proj[0], cwd, proj[3], start, skill))
    for proj, sessions in per_project.items():
        d = os.path.join(projects_dir, "-work-" + proj.replace("-", "_"))
        os.makedirs(d, exist_ok=True)
        for es in sessions:
            with open(os.path.join(d, es[0]["sessionId"] + ".jsonl"), "w", encoding="utf-8") as f:
                for e in es:
                    f.write(json.dumps(e) + "\n")
    print("wrote %d sample sessions to %s" % (n, projects_dir))


if __name__ == "__main__":
    main()
