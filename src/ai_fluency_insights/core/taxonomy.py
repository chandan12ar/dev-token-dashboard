"""Tool and command taxonomy: which tools read, write or run, and how shell commands are classified."""

import os
import re

CODE_TOOLS_WRITE = {"Write", "Create"}          # tools whose 'content' is new code
CODE_TOOLS_EDIT = {"Edit", "StrEditReplace"}    # tools whose 'new_string' is new code

# tool classification for the exploration-vs-building split
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
              "NotebookRead", "TodoRead", "ToolSearch"}
WRITE_TOOLS = CODE_TOOLS_WRITE | CODE_TOOLS_EDIT | {"MultiEdit", "NotebookEdit"}
CMD_TOOLS = {"Bash", "PowerShell"}   # command runs — the verification proxy for diligence

# Claude Code features tracked on the "My Adoption" tab. tool name -> feature id;
# MCP tools are matched by their "mcp__" prefix, slash commands / plan-mode
# snapshots / CLAUDE.md are detected separately (see parse_entry, build_adoption).
TOOL_FEATURES = {"EnterPlanMode": "plan", "ExitPlanMode": "plan",
                 "Agent": "subagents", "Task": "subagents",
                 "WebSearch": "web", "WebFetch": "web", "Skill": "skills",
                 "Bash": "verify", "PowerShell": "verify",
                 "TodoWrite": "tasks", "TaskCreate": "tasks", "TaskUpdate": "tasks"}
# ---- log-derived signals for the Flow / Quality / Value tabs -----------------
EXIT_RE = re.compile(r"Exit code (-?\d+)")
TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?)/|(^|/)test_[^/]*$|_test\.[A-Za-z]+$|\.(test|spec)\.[A-Za-z]+$",
    re.IGNORECASE)
SECRET_PATH_RE = re.compile(
    r"(^|/)\.env(\.[\w-]+)?$|\.pem$|(^|/)id_(rsa|ed25519)$|(^|/)credentials(\.\w+)?$"
    r"|(^|/)secrets?\.(json|ya?ml|toml)$", re.IGNORECASE)
# What a shell command was for. Patterns are matched against each *invoked*
# command (see _cmd_segments), never against heredoc bodies or quoted text, so a
# script that merely mentions "pytest" or "--no-verify" does not count.
CMD_KIND_RES = [
    ("test", re.compile(
        r"(pytest|py\.test|unittest|nosetests|tox|nox|jest|vitest|mocha|karma|rspec|phpunit|ctest)\b"
        r"|go\s+test\b|cargo\s+test\b|(npm|yarn|pnpm|bun)\s+(run\s+)?test\b"
        r"|(mvnw?|gradlew?)\b.*\btest\b|dotnet\s+test\b|make\s+test\b"
        r"|(python[\d.]*\s+)?(\S*/)?manage\.py\s+test\b|playwright\s+test\b|cypress\s+run\b",
        re.IGNORECASE)),
    ("lint", re.compile(
        r"(eslint|ruff|flake8|pylint|mypy|pyright|golangci-lint|rubocop|stylelint|shellcheck)\b"
        r"|tsc\b.*--noEmit|prettier\b.*--check|black\b.*--check"
        r"|(npm|yarn|pnpm)\s+(run\s+)?(lint|typecheck)\b|cargo\s+clippy\b|make\s+lint\b",
        re.IGNORECASE)),
    ("build", re.compile(
        r"(tsc|webpack|rollup|esbuild|cmake)\b|vite\s+build\b|gradlew?\s+(build|assemble)\b"
        r"|mvnw?\b.*\b(package|compile|install)\b|cargo\s+build\b|go\s+build\b|dotnet\s+build\b"
        r"|make\b|(build|compileall|py_compile)\b|(npm|yarn|pnpm)\s+(run\s+)?build\b"
        r"|node\s+--check\b", re.IGNORECASE)),
]
# commands worth surfacing as a safety signal (category only is stored, never the command)
RISKY_CMD_RES = [
    ("force_push", re.compile(r"git\s+push\b.*(--force\b|--force-with-lease|\s-f\b)")),
    ("hard_reset", re.compile(r"git\s+reset\s+--hard\b")),
    ("git_clean", re.compile(r"git\s+clean\s+-[a-zA-Z]*f")),
    ("recursive_delete", re.compile(r"rm\s+-[a-zA-Z]*[rR]")),
    ("pipe_to_shell", re.compile(r"(curl|wget)\b.*\|\s*(sudo\s+)?(ba|z)?sh\b")),
    ("skip_hooks", re.compile(r"git\s+(commit|push)\b.*--no-verify\b")),
    ("wide_permissions", re.compile(r"chmod\s+(-R\s+)?777\b")),
    ("sql_drop", re.compile(r"(psql|mysql|sqlite3|sqlcmd)\b.*\bdrop\s+(table|database)\b", re.IGNORECASE)),
]
SUBCMD_TOOLS = {"git", "npm", "yarn", "pnpm", "docker", "cargo", "go", "pip", "pip3", "kubectl",
                "gh", "make", "uv", "poetry", "bun"}
_LEAD_WORDS = ("sudo", "time", "env", "nohup", "command", "exec")
_RUNNER_RE = re.compile(
    r"^(?:npx\s+|bunx\s+|uvx\s+|uv\s+run\s+|poetry\s+run\s+|pipenv\s+run\s+|bundle\s+exec\s+"
    r"|python[\d.]*\s+-m\s+|\./|(?:\S*/)?node_modules/\.bin/)")


def _cmd_segments(cmd):
    """The commands a shell string actually invokes, normalised: the text after a
    heredoc marker is dropped, then it is split on && || ; and newlines, and
    leading env assignments, sudo/time/nohup and runners (npx, uv run, python -m) are stripped."""
    if not isinstance(cmd, str):
        return []
    m = re.search(r"<<-?\s*['\"]?\w+['\"]?", cmd)
    if m:
        nl = cmd.find("\n", m.end())
        cmd = cmd[:nl] if nl != -1 else cmd
    out = []
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        seg = seg.strip().lstrip("({").strip()
        while True:
            toks = seg.split(None, 1)
            if toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in _LEAD_WORDS):
                seg = toks[1] if len(toks) > 1 else ""
                continue
            m2 = _RUNNER_RE.match(seg)
            if m2 and m2.end() < len(seg):
                seg = seg[m2.end():]
                continue
            break
        if seg:
            out.append(seg)
    return out


def classify_cmd(cmd) -> str:
    """'test' | 'lint' | 'build' | 'other' for the commands a shell string invokes."""
    segs = _cmd_segments(cmd)
    for kind, rx in CMD_KIND_RES:
        if any(rx.match(sg) for sg in segs):
            return kind
    return "other"


def risky_categories(cmd):
    segs = _cmd_segments(cmd)
    return [cat for cat, rx in RISKY_CMD_RES if any(rx.match(sg) for sg in segs)]


def cmd_family(cmd) -> str:
    """Short label for what a shell command ran (e.g. 'git push', 'pytest', 'python')."""
    segs = [sg for sg in _cmd_segments(cmd) if not re.match(r"^cd(\s|$)", sg)]
    if not segs:
        return "shell"
    toks = segs[0].split()
    first = os.path.basename(toks[0].strip("'\"")) or "shell"
    if re.match(r"^python[\d.]*$", first):
        return "python"
    if first in ("npm", "yarn", "pnpm", "bun") and len(toks) > 2 and toks[1] == "run" \
            and re.match(r"^[\w:-]+$", toks[2]):
        return first + " run " + toks[2]
    if first in SUBCMD_TOOLS and len(toks) > 1 and re.match(r"^[a-z][\w-]*$", toks[1]):
        return first + " " + toks[1]
    return first[:24]


# id, label, short description, how-to-start tip, usefulness weight for the coach
ADOPTION_FEATURES = [
    ("memory", "Project memory", "A CLAUDE.md that teaches Claude your project",
     "Run /init in a project so Claude learns your conventions once, instead of every session.", 58),
    ("verify", "Run & verify", "Claude runs commands, tests and builds",
     "Ask Claude to run your tests or build after it edits, so it can check its own work.", 60),
    ("plan", "Plan mode", "Agree on an approach before any edits",
     "Press Shift+Tab to switch to plan mode and let Claude propose an approach before it touches files.", 55),
    ("subagents", "Subagents", "Helpers that research or review in parallel",
     "Say \"use a subagent to research this\" to keep your main conversation clean.", 50),
    ("skills", "Skills", "Reusable workflows Claude can pick up",
     "Package something you repeat weekly as a skill so Claude does it the same way every time.", 45),
    ("hooks", "Hooks", "Automatic checks that run on Claude Code events",
     "Add a hook in your settings to run your formatter or tests automatically after Claude edits.", 48),
    ("tasks", "Task lists", "Claude tracks multi-step work in a checklist",
     "For a big job, ask Claude to write a task list first and tick items off as it goes.", 38),
    ("mcp", "Connected tools", "MCP servers for GitHub, docs, databases...",
     "Connect a tool you already use with `claude mcp add` so Claude can work with it directly.", 42),
    ("slash", "Slash commands", "Shortcuts like /compact, /clear, /init",
     "Type / to see the shortcuts. /compact and /clear keep long sessions fast and focused.", 40),
    ("web", "Web research", "Looking up docs and errors online",
     "Ask Claude to look up the docs or an error message instead of pasting it in yourself.", 35),
]
