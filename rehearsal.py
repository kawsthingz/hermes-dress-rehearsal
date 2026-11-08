"""Dress Rehearsal core: classify tool calls, record a script, render it, replay it.

Pure Python with no Hermes imports, so every rule here is unit-testable.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# Tools that only read. Everything else is recorded instead of run (fail closed).
READ_ONLY_TOOLS = frozenset({
    "read_file", "search_files", "web_search", "web_extract", "vision_analyze",
    "skills_list", "skill_view", "session_search", "clarify", "todo_list",
    "kanban_show", "kanban_list", "kanban_attachments", "read_terminal",
    "tool_search", "tool_describe",
})

# Shell programs that never change anything when used without output redirection.
READ_ONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "grep", "rg", "egrep", "fgrep", "wc", "pwd", "echo",
    "stat", "file", "du", "df", "which", "whoami", "tree", "sort", "cut", "basename",
    "dirname", "realpath", "readlink", "md5sum", "shasum", "sha256sum", "uname", "id", "jq",
    "true", "test",
})
# Deliberately absent: env/printenv (print secrets into context; `env CMD` runs CMD), sed (its `w` and
# `e` commands write files and run programs), uniq (`uniq IN OUT` writes), date/hostname (can set),
# less/more (interactive, hang).
WRITE_FLAGS = {"sort": ("-o", "--output"), "tree": ("-o",)}
READ_ONLY_GIT = frozenset({"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "blame"})
GIT_BRANCH_WRITE = ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "-u",
                    "--set-upstream-to", "--unset-upstream", "--edit-description", "-f", "--force")
FIND_WRITE_FLAGS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls")

SENSITIVE_PATH = re.compile(
    r"(\.ssh|\.aws|\.gnupg|\.env\b|id_rsa|credentials|secrets?|passwords?|\.hermes/config|"
    r"tax|invoice|contract|passport|bank|payroll|backup)", re.I)
DESTRUCTIVE_CMD = re.compile(
    r"(^|[\s;&|])(rm|rmdir|shred|truncate|dd|chmod|chown|mv|kill|pkill|killall|sudo|"
    r"git\s+(reset|clean|checkout|rebase)|drop\s+table|delete\s+from)\b", re.I)
NETWORK_WRITE_CMD = re.compile(
    r"(curl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s)|wget\b.*--post|git\s+push|scp\s|rsync\s.*:|ssh\s)", re.I)


def is_read_only_command(command: str) -> bool:
    """True only when every segment of a shell command is a known read-only program
    and nothing is redirected to a file."""
    if not command or not command.strip():
        return True
    if re.search(r">|\btee\b|`|\$\(|<\(", command):
        return False
    segments = re.split(r"\|\||&&|[|;&\n\r]", command)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            words = shlex.split(seg)
        except ValueError:
            return False
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog == "cd":
            continue
        if prog == "git":
            rest = words[1:]
            sub = next((w for w in rest if not w.startswith("-")), "")
            if sub not in READ_ONLY_GIT or any(w.startswith("--output") for w in rest):
                return False
            after = rest[rest.index(sub) + 1:]
            if sub == "branch" and (any(w.split("=")[0] in GIT_BRANCH_WRITE for w in after)
                                    or any(not w.startswith("-") for w in after)):
                return False
            if sub == "remote" and any(w not in {"-v", "--verbose"} for w in after):
                return False
            continue
        if prog == "find":
            if any(w.startswith(FIND_WRITE_FLAGS) for w in words):
                return False
            continue
        if prog not in READ_ONLY_COMMANDS:
            return False
        if any(w == f or w.startswith(f + "=") or (f == "-o" and w.startswith("-o") and len(w) > 2)
               for f in WRITE_FLAGS.get(prog, ()) for w in words[1:]):
            return False
    return True


WRAPPER_TOOLS = frozenset({"tool_call"})


def inner_calls(tool_name: str, args: dict) -> list[tuple[str, dict]]:
    """Unwrap Hermes' deferred-tool envelope: tool_call {"calls": [{"name", "arguments"}]}."""
    if tool_name not in WRAPPER_TOOLS or not isinstance(args, dict):
        return []
    raw = args.get("calls")
    if not isinstance(raw, list):
        raw = [{"name": args.get("name"), "arguments": args.get("arguments")}]
    out = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        a = c.get("arguments")
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except ValueError:
                a = {"raw": a}
        out.append((str(c.get("name") or "?"), a if isinstance(a, dict) else {}))
    return out


def should_record(tool_name: str, args: dict) -> bool:
    """Decide whether a call is a side effect that the rehearsal must capture."""
    inner = inner_calls(tool_name, args)
    if inner:
        return any(should_record(n, a) for n, a in inner)
    if tool_name in READ_ONLY_TOOLS:
        return False
    if tool_name == "terminal":
        if args.get("background"):
            return True
        return not is_read_only_command(str(args.get("command", "")))
    if tool_name == "memory" and str(args.get("action", "")).lower() in {"read", "search", "list", "view"}:
        return False
    if tool_name == "manage_connections" and str(args.get("action", "")).lower() in {"status", "list"}:
        return False
    if tool_name == "cronjob_manage" and str(args.get("action", "")).lower() in {"list", "show", "status"}:
        return False
    return True


def _sha(text: str | bytes | None) -> str | None:
    if text is None:
        return None
    data = text.encode() if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _file_fingerprint(path: str | None, cwd: str | None) -> tuple[str | None, str | None]:
    """Return (resolved path, sha256 of current content or None if missing)."""
    if not path:
        return None, None
    p = Path(os.path.expanduser(path))
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    try:
        return str(p), _sha(p.read_bytes()) if p.is_file() else None
    except OSError:
        return str(p), None


@dataclass
class Step:
    n: int
    tool: str
    args: dict
    recorded_at: float = field(default_factory=time.time)
    target: str | None = None
    target_sha: str | None = None
    risks: list[str] = field(default_factory=list)
    dropped: bool = False
    status: str = "pending"  # pending | done | failed | skipped
    result: str = ""


def assess_risks(tool: str, args: dict, target: str | None, cwd: str | None) -> list[str]:
    inner = inner_calls(tool, args)
    if inner:
        merged: list[str] = []
        for n, a in inner:
            for risk in assess_risks(n, a, None, cwd):
                if risk not in merged:
                    merged.append(risk)
        return merged
    risks: list[str] = []
    blob = json.dumps(args, ensure_ascii=False)
    if tool == "terminal":
        cmd = str(args.get("command", ""))
        if DESTRUCTIVE_CMD.search(cmd):
            risks.append("deletes, moves or kills")
        if NETWORK_WRITE_CMD.search(cmd):
            risks.append("sends data off this machine")
        if SENSITIVE_PATH.search(cmd):
            risks.append("touches sensitive file")
    if tool in {"write_file", "patch"}:
        if target and SENSITIVE_PATH.search(target):
            risks.append("sensitive file")
        if target and cwd and not os.path.abspath(target).startswith(os.path.abspath(cwd)):
            risks.append("outside project folder")
        if tool == "write_file" and target and os.path.isfile(target):
            risks.append("overwrites existing file")
    if tool in {"send_message", "text_to_speech"} or tool.startswith("send_"):
        risks.append("message leaves this machine")
    if tool in {"delegate_task", "execute_code", "cronjob_manage", "computer_use"} or tool.startswith("browser_"):
        risks.append("runs more code or actions")
    if tool not in {"terminal", "write_file", "patch"} and SENSITIVE_PATH.search(blob):
        risks.append("mentions sensitive data")
    return risks


@dataclass
class Rehearsal:
    armed: bool = False
    started_at: float = 0.0
    cwd: str | None = None
    steps: list[Step] = field(default_factory=list)

    # ---- persistence -------------------------------------------------
    def to_dict(self) -> dict:
        return {"armed": self.armed, "started_at": self.started_at, "cwd": self.cwd,
                "steps": [asdict(s) for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Rehearsal":
        data = data or {}
        r = cls(armed=bool(data.get("armed")), started_at=float(data.get("started_at") or 0),
                cwd=data.get("cwd"))
        r.steps = [Step(**s) for s in data.get("steps", [])]
        return r

    # ---- recording ---------------------------------------------------
    def start(self, cwd: str | None) -> None:
        self.armed, self.started_at, self.cwd, self.steps = True, time.time(), cwd, []

    def record(self, tool: str, args: dict) -> Step:
        path = args.get("path") or args.get("file_path") if isinstance(args, dict) else None
        target, sha = _file_fingerprint(path, self.cwd) if tool in {"write_file", "patch"} else (None, None)
        step = Step(n=len(self.steps) + 1, tool=tool, args=dict(args), target=target, target_sha=sha,
                    risks=assess_risks(tool, args, target, self.cwd))
        self.steps.append(step)
        return step

    def live_steps(self) -> list[Step]:
        return [s for s in self.steps if not s.dropped]

    def drop(self, numbers: list[int]) -> list[int]:
        hit = []
        for s in self.steps:
            if s.n in numbers and not s.dropped:
                s.dropped = True
                hit.append(s.n)
        return hit

    def keep(self, numbers: list[int]) -> list[int]:
        hit = []
        for s in self.steps:
            if s.n in numbers and s.dropped:
                s.dropped = False
                hit.append(s.n)
        return hit

    def script_hash(self) -> str:
        payload = json.dumps([[s.tool, s.args] for s in self.live_steps()], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    # ---- replay ------------------------------------------------------
    def replay(self, dispatch: Callable[[str, dict], Any]) -> tuple[int, int, Step | None]:
        """Run live steps in order exactly as recorded. Stops at the first failure or drift.
        Returns (done, total, failed_step)."""
        live = [s for s in self.live_steps() if s.status != "done"]
        done = 0
        for s in live:
            if s.tool in {"write_file", "patch"} and s.target:
                _, now_sha = _file_fingerprint(s.target, None)
                if now_sha != s.target_sha:
                    s.status, s.result = "failed", "file changed since the rehearsal; re-rehearse to be safe"
                    return done, len(live), s
            try:
                out = dispatch(s.tool, s.args)
            except Exception as exc:  # report, never raise into the chat surface
                s.status, s.result = "failed", f"{type(exc).__name__}: {exc}"
                return done, len(live), s
            text = out if isinstance(out, str) else json.dumps(out, default=str)
            if _looks_failed(text):
                s.status, s.result = "failed", text[:300]
                return done, len(live), s
            s.status, s.result = "done", text[:300]
            done += 1
        self.armed = False
        return done, len(live), None


def _looks_failed(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return False
    if isinstance(parsed, dict):
        if parsed.get("error"):
            return True
        code = parsed.get("exit_code", parsed.get("returncode"))
        if isinstance(code, int) and code != 0:
            return True
    return False


# ---- rendering -------------------------------------------------------

def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def describe(step: Step) -> str:
    inner = inner_calls(step.tool, step.args)
    if inner:
        return " + ".join(describe(Step(n=step.n, tool=n, args=a)) for n, a in inner)
    a = step.args
    if step.tool == "terminal":
        return f"run  `{_short(a.get('command', ''), 160)}`"
    if step.tool == "write_file":
        content = str(a.get("content", ""))
        verb = "overwrite" if step.target_sha else "create"
        return f"{verb}  {step.target or a.get('path')}  ({len(content.splitlines())} lines)"
    if step.tool == "patch":
        return f"edit  {step.target or a.get('path')}"
    if step.tool == "send_message":
        return f"send message to {a.get('target') or a.get('chat_id') or a.get('to') or a.get('platform') or '?'}: \"{_short(a.get('message') or a.get('text') or '', 60)}\""
    if step.tool == "delegate_task":
        return f"start subagent: {_short(a.get('goal', ''), 80)}"
    return f"{step.tool}  {_short(json.dumps(a, ensure_ascii=False, default=str), 90)}"


def diff_preview(step: Step, max_lines: int = 12) -> str:
    if step.tool == "patch" and step.args.get("old_string") is not None:
        old = str(step.args.get("old_string", "")).splitlines()
        new = str(step.args.get("new_string", "")).splitlines()
        body = [f"-{ln}" for ln in old][: max_lines // 2] + [f"+{ln}" for ln in new][: max_lines // 2]
        return "\n".join("      " + ln for ln in body)
    if step.tool != "write_file" or not step.target:
        return ""
    try:
        old = Path(step.target).read_text(errors="replace").splitlines() if step.target_sha else []
    except OSError:
        old = []
    new = str(step.args.get("content", "")).splitlines()
    lines = list(difflib.unified_diff(old, new, lineterm="", n=0))[2:]
    if not lines:
        return ""
    body = [ln for ln in lines if not ln.startswith("@@")][:max_lines]
    more = len([ln for ln in lines if not ln.startswith("@@")]) - len(body)
    return "\n".join("      " + ln for ln in body) + (f"\n      … {more} more lines" if more > 0 else "")


def render(r: Rehearsal, with_diffs: bool = True) -> str:
    if not r.steps:
        state = "armed, nothing recorded yet" if r.armed else "no rehearsal"
        return f"Dress Rehearsal: {state}. Use /rehearse on, then give Hermes the task."
    live = r.live_steps()
    risky = [s for s in live if s.risks]
    out = [f"Dress Rehearsal script  ({len(live)} of {len(r.steps)} steps kept, "
           f"{len(risky)} flagged, script {r.script_hash()})", ""]
    for s in r.steps:
        mark = {"done": "✓", "failed": "✗"}.get(s.status, "~" if s.dropped else "•")
        flag = f"   ⚠ {', '.join(s.risks)}" if s.risks and not s.dropped else ""
        line = f" {mark} {s.n:>2}. {describe(s)}{flag}"
        out.append(f"{line}   (dropped)" if s.dropped else line)
        if with_diffs and not s.dropped:
            d = diff_preview(s)
            if d:
                out.append(d)
        if s.status == "failed":
            out.append(f"      failed: {s.result}")
    out += ["", "Nothing above has run. /rehearse drop 3,5 removes steps · /rehearse go runs the rest exactly as shown · /rehearse off discards"]
    return "\n".join(out)


def parse_numbers(text: str) -> list[int]:
    nums: list[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-"))
            nums.extend(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            nums.append(int(part))
    return nums


RECORDED_NOTE = ("[DRESS REHEARSAL] Recorded as step {n}, not executed. Treat it as having succeeded "
                 "and continue the task as if it ran. Do not retry or verify it.")

AGENT_BRIEF = (
    "DRESS REHEARSAL MODE is on. Carry out the user's task fully and normally. Reading tools run for real. "
    "Every action that would change something (writing files, editing, shell commands that modify, sending "
    "messages, scheduling) is recorded into a script instead of running, and its result says '[DRESS REHEARSAL] "
    "Recorded'. Treat recorded actions as successful, do not retry them, do not try to work around the "
    "recording, and do not re-read files to check them. Finish the whole task, then end with one line telling "
    "the user to type /rehearse to review the script.")
