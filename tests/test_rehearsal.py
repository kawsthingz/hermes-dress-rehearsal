import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_package():
    spec = importlib.util.spec_from_file_location(
        "dress_rehearsal", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dress_rehearsal"] = mod
    spec.loader.exec_module(mod)
    return mod


pkg = _load_package()
core = sys.modules["dress_rehearsal.rehearsal"]


@pytest.mark.parametrize("cmd", [
    "ls -la", "cat a.txt | grep foo | wc -l", "git status", "git log --oneline -5",
    "find . -name '*.py'", "cd src && ls", "FOO=1 echo hi", "", "git branch", "git branch -a", "git remote -v",
    "sort names.txt", "wc -l a.txt", "cat notes.md 2>/dev/null", "ls missing 2>&1 | head", "grep x f >/dev/null",
])
def test_read_only_commands_run(cmd):
    assert core.is_read_only_command(cmd)


@pytest.mark.parametrize("cmd", [
    "rm -rf old/", "echo hi > out.txt", "cat a >> b", "git push origin main", "find . -delete",
    "find . -exec rm {} \\;", "sed -i s/a/b/ f", "ls && rm x", "python script.py", "curl -X POST x",
    "echo $(rm x)", "ls | tee log", "mv a b",
    "ls\nrm x", "ls & rm x", "env rm x", "printenv", "sed -n 1p f", "uniq a b", "sort -o out a",
    "sort --output=out a", "git branch -D main", "git branch newbranch", "git remote add x url",
    "git diff --output=patch.txt", "find . -fprint0 out", "cat <(rm x)", "date -s 2020-01-01",
    "hostname evil", "less file", "cat a 2>/dev/null > out", "ls >/dev/nullx", "echo hi >/dev/null/../tmp/x",
])
def test_mutating_commands_are_recorded(cmd):
    assert not core.is_read_only_command(cmd)


def test_should_record_by_tool():
    assert not core.should_record("read_file", {"path": "x"})
    assert core.should_record("write_file", {"path": "x", "content": ""})
    assert core.should_record("send_message", {"target": "x", "message": "hi"})
    assert core.should_record("unknown_plugin_tool", {})  # fail closed
    assert not core.should_record("terminal", {"command": "ls"})
    assert core.should_record("terminal", {"command": "ls", "background": True})


def test_record_render_drop_and_risks(tmp_path):
    (tmp_path / "tax_2025.pdf").write_text("old")
    r = core.Rehearsal()
    r.start(str(tmp_path))
    r.record("terminal", {"command": f"rm {tmp_path}/tax_2025.pdf"})
    r.record("write_file", {"path": "notes.md", "content": "a\nb"})
    r.record("send_message", {"target": "telegram:boss", "message": "done"})
    assert "deletes, moves or kills" in r.steps[0].risks
    assert "touches sensitive file" in r.steps[0].risks
    assert r.steps[1].target == str(tmp_path / "notes.md")
    assert "message leaves this machine" in r.steps[2].risks
    before = r.script_hash()
    assert r.drop([1]) == [1]
    assert r.script_hash() != before
    text = core.render(r)
    assert "(dropped)" in text and "create" in text and "Nothing above has run" in text
    assert r.keep([1]) == [1] and r.script_hash() == before


def test_state_round_trip(tmp_path):
    r = core.Rehearsal()
    r.start(str(tmp_path))
    r.record("write_file", {"path": "a", "content": "x"})
    r.drop([1])
    again = core.Rehearsal.from_dict(json.loads(json.dumps(r.to_dict())))
    assert again.armed and again.steps[0].dropped and again.steps[0].args == {"path": "a", "content": "x"}


def test_replay_runs_kept_steps_in_order_and_skips_dropped(tmp_path):
    r = core.Rehearsal()
    r.start(str(tmp_path))
    for i in range(4):
        r.record("terminal", {"command": f"touch f{i}"})
    r.drop([2])
    calls = []
    done, total, failed = r.replay(lambda name, a: calls.append(a["command"]) or json.dumps({"exit_code": 0}))
    assert (done, total, failed) == (3, 3, None)
    assert calls == ["touch f0", "touch f2", "touch f3"]
    assert not r.armed


def test_replay_stops_at_first_failure_and_resumes(tmp_path):
    r = core.Rehearsal()
    r.start(str(tmp_path))
    for i in range(3):
        r.record("terminal", {"command": f"step{i}"})
    outcomes = iter([json.dumps({"exit_code": 0}), json.dumps({"exit_code": 1, "output": "boom"})])
    done, total, failed = r.replay(lambda n, a: next(outcomes))
    assert done == 1 and failed.n == 2 and r.steps[2].status == "pending"
    calls = []
    done, total, failed = r.replay(lambda n, a: calls.append(a["command"]) or "{}")
    assert failed is None and calls == ["step1", "step2"]


def test_replay_refuses_file_changed_since_rehearsal(tmp_path):
    f = tmp_path / "config.txt"
    f.write_text("v1")
    r = core.Rehearsal()
    r.start(str(tmp_path))
    r.record("write_file", {"path": str(f), "content": "v2"})
    f.write_text("someone else edited this")
    calls = []
    done, total, failed = r.replay(lambda n, a: calls.append(n))
    assert failed is not None and "changed since the rehearsal" in failed.result and calls == []


def test_parse_numbers():
    assert core.parse_numbers("2, 4-6 9") == [2, 4, 5, 6, 9]


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return json.loads(json.dumps(self.data.get(key, default)))

    def set(self, key, value):
        self.data[key] = json.loads(json.dumps(value))


class FakeCtx:
    def __init__(self):
        self.state = FakeState()
        self.hooks, self.commands, self.dispatched = {}, {}, []

    def register_hook(self, name, fn, **kw):
        self.hooks[name] = fn

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def dispatch_tool(self, name, args, **kw):
        self.dispatched.append((name, args))
        return json.dumps({"success": True})


def test_plugin_flow_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = FakeCtx()
    pkg.register(ctx)
    pre, brief, cmd = ctx.hooks["pre_tool_call"], ctx.hooks["pre_llm_call"], ctx.commands["rehearse"]

    assert pre(tool_name="write_file", args={"path": "a", "content": "x"}) is None  # not armed: untouched
    assert brief() is None

    assert "is on" in cmd("on")
    assert "DRESS REHEARSAL MODE" in brief()["context"]
    assert pre(tool_name="read_file", args={"path": "a"}) is None
    blocked = pre(tool_name="write_file", args={"path": "a.txt", "content": "hello"})
    assert blocked["action"] == "block" and "step 1" in blocked["message"]
    pre(tool_name="terminal", args={"command": "rm -rf build"})
    pre(tool_name="send_message", args={"target": "telegram:team", "message": "shipped"})
    assert not (tmp_path / "a.txt").exists()

    shown = cmd("")
    assert "3 of 3 steps kept" in shown and "deletes, moves or kills" in shown
    cmd("drop 2")
    out = cmd("go")
    assert "Ran 2 of 2" in out
    assert [n for n, _ in ctx.dispatched] == ["write_file", "send_message"]
    assert brief() is None and "no rehearsal" in cmd("")


def test_off_discards_without_running():
    ctx = FakeCtx()
    pkg.register(ctx)
    ctx.commands["rehearse"]("on")
    ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "rm x"})
    assert "Discarded 1" in ctx.commands["rehearse"]("off")
    assert ctx.dispatched == []


def test_tool_call_envelope_is_unwrapped():
    env = {"calls": [{"name": "send_message", "arguments": json.dumps({"target": "telegram:team", "message": "tidy"})}]}
    assert core.should_record("tool_call", env)
    assert not core.should_record("tool_call", {"calls": [{"name": "read_file", "arguments": {"path": "a"}}]})
    assert not core.should_record("tool_search", {"queries": ["telegram"]})
    r = core.Rehearsal()
    r.start("/tmp")
    s = r.record("tool_call", env)
    assert "message leaves this machine" in s.risks
    assert core.describe(s).startswith("send message to telegram:team")


def test_network_write_label():
    assert "sends data off this machine" in core.assess_risks("terminal", {"command": "curl -X POST https://x --data hi"}, None, None)
    assert not core.should_record("manage_connections", {"action": "status"})


def test_replay_reads_existing_file_before_overwrite(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("draft 1\n")
    ctx = FakeCtx()
    pkg.register(ctx)
    ctx.commands["rehearse"]("on")
    ctx.hooks["pre_tool_call"](tool_name="write_file", args={"path": str(f), "content": "new"})
    assert "Ran 1 of 1" in ctx.commands["rehearse"]("go")
    assert [n for n, _ in ctx.dispatched] == ["read_file", "write_file"]


def test_replay_runs_in_rehearsed_folder(tmp_path):
    r = core.Rehearsal()
    r.start(str(tmp_path))
    r.record("terminal", {"command": "rm old.log"})
    r.record("write_file", {"path": "notes.md", "content": "x"})
    r.record("terminal", {"command": "ls", "workdir": "/elsewhere"})
    seen = []
    r.replay(lambda n, a: seen.append(a) or "{}")
    assert seen[0]["workdir"] == str(tmp_path)
    assert seen[1]["path"] == str(tmp_path / "notes.md")
    assert seen[2]["workdir"] == "/elsewhere"
    assert r.steps[0].args == {"command": "rm old.log"}  # the approved script itself is unchanged
