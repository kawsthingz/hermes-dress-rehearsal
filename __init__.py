"""Dress Rehearsal: run a task with every side effect recorded instead of executed,
review the exact script, strike what you don't want, then replay it with no model in the loop."""

from __future__ import annotations

import logging
import threading

from . import rehearsal as core

logger = logging.getLogger(__name__)

_STATE_KEY = "rehearsal"
_lock = threading.Lock()

USAGE = (
    "Dress Rehearsal\n"
    "  /rehearse on          record the next task instead of running its side effects\n"
    "  /rehearse             show the recorded script with diffs and risk flags\n"
    "  /rehearse drop 2,4-6  strike steps     /rehearse keep 4  restore a step\n"
    "  /rehearse go          run the kept steps exactly as recorded\n"
    "  /rehearse off         discard the rehearsal"
)


def _session_cwd() -> str | None:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except Exception:
        import os
        return os.getcwd()


def _replay_dispatch(ctx, name: str, args: dict):
    """Dispatch one approved step. Hermes refuses to overwrite a file the task has not read in full,
    so read it first; replay already confirmed the file is byte-identical to what was rehearsed."""
    import json
    import os
    path = args.get("path") if name in {"write_file", "patch"} else None
    if path and os.path.isfile(os.path.expanduser(path)):
        offset = None
        for _ in range(50):  # page through large files; read_file pages at 2000 lines
            req = {"path": path} if offset is None else {"path": path, "offset": offset}
            try:
                page = json.loads(ctx.dispatch_tool("read_file", req))
            except (TypeError, ValueError):
                break
            offset = page.get("next_offset") if isinstance(page, dict) else None
            if not offset:
                break
    return ctx.dispatch_tool(name, args)


class _Store:
    """Rehearsal state lives in ctx.state so the hook process and the command process agree."""

    def __init__(self, ctx):
        self.ctx = ctx

    def load(self) -> core.Rehearsal:
        try:
            return core.Rehearsal.from_dict(self.ctx.state.get(_STATE_KEY, default=None))
        except Exception:
            logger.exception("dress-rehearsal: unreadable state, starting clean")
            return core.Rehearsal()

    def save(self, r: core.Rehearsal) -> None:
        self.ctx.state.set(_STATE_KEY, r.to_dict())


def register(ctx):
    store = _Store(ctx)

    def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
        args = args or {}
        with _lock:
            r = store.load()
            if not r.armed or not core.should_record(tool_name, args):
                return None
            r.cwd = _session_cwd() or r.cwd  # the agent's folder, read where the agent runs
            step = r.record(tool_name, args)
            store.save(r)
        return {"action": "block", "message": core.RECORDED_NOTE.format(n=step.n)}

    def pre_llm_call(**kwargs):
        if store.load().armed:
            return {"context": core.AGENT_BRIEF}
        return None

    def rehearse(raw_args: str = "", **kwargs) -> str:
        words = (raw_args or "").strip().split(maxsplit=1)
        verb = words[0].lower() if words else "show"
        rest = words[1] if len(words) > 1 else ""
        with _lock:
            r = store.load()
            if verb in {"on", "start"}:
                r.start(_session_cwd())
                store.save(r)
                return ("Dress Rehearsal is on. Give Hermes the task. Reads run for real; every change is "
                        "recorded, not executed. Type /rehearse to review.")
            if verb in {"off", "cancel", "discard"}:
                n = len(r.steps)
                store.save(core.Rehearsal())
                return f"Dress Rehearsal off. Discarded {n} recorded steps; nothing ran."
            if verb in {"show", "script", "list"}:
                return core.render(r)
            if verb in {"drop", "keep"}:
                nums = core.parse_numbers(rest)
                if not nums:
                    return f"Usage: /rehearse {verb} 2,4-6"
                hit = r.drop(nums) if verb == "drop" else r.keep(nums)
                store.save(r)
                return core.render(r, with_diffs=False) if hit else f"No steps changed for {rest}."
            if verb in {"go", "run", "approve"}:
                if not r.live_steps():
                    return "Nothing to run."
                r.armed = False  # replay must reach the real tools
                store.save(r)
                approved = r.script_hash()
                done, total, failed = r.replay(lambda name, a: _replay_dispatch(ctx, name, a))
                store.save(r if failed else core.Rehearsal())
                head = f"Ran {done} of {total} approved steps (script {approved})."
                if failed:
                    return (f"{head}\nStopped at step {failed.n}: {failed.result}\n"
                            f"Steps after it did not run. /rehearse shows the state; /rehearse go resumes.")
                return head + " Rehearsal closed."
            return USAGE

    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_command("rehearse", handler=rehearse,
                         description="Rehearse a task: record side effects, review, then run exactly that",
                         args_hint="[on|go|drop N|keep N|off]")
