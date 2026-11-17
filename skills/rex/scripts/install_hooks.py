#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install independent Rex hooks without changing Python hooks or host trust."""
import argparse
import json
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_types import HookEvent
from question_model import FLAG_ENV


class QuestionEscalation(StrEnum):
    OFF = "off"
    ON = "on"

    @property
    def environment_value(self) -> str:
        return "1" if self is QuestionEscalation.ON else "0"


@dataclass(frozen=True, slots=True)
class HookSpec:
    script: str
    event: HookEvent
    matcher: str | None
    timeout: int


CODEX_QUESTION_TOOL_MATCHER = "^(request_user_input|request_user_input_async)$"
CLAUDE_QUESTION_TOOL_MATCHER = "^AskUserQuestion$"
BROADEN_POST_TOOL_USE = frozenset({"codex_sessions.py", "claude_sessions.py"})


def write(path, content):
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".agent-tooling-backup-" + time.strftime("%Y%m%d-%H%M%S")))
    temporary = path.with_name(path.name + ".agent-tooling.tmp")
    temporary.write_text(content)
    temporary.replace(path)
    return True


def marked_block(path, name, text):
    before = path.read_text() if path.exists() else ""
    begin, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    addition = f"{begin}\n{text.strip()}\n{end}"
    if begin in before and end in before:
        after = re.sub(re.escape(begin) + r".*?" + re.escape(end), lambda _: addition, before, flags=re.DOTALL)
    else:
        after = before.rstrip() + ("\n\n" if before.strip() else "") + addition + "\n"
    write(path, after)


def owned_handler(command, script, home, root):
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 3 or "hook" not in words[2:]:
        return False
    paths = [Path(word) for word in words if word.startswith("/")]
    path = next((candidate for candidate in paths if candidate.name == script), None)
    if path is None:
        return False
    if path.name != script or not path.is_absolute():
        return False
    return (path.parent == root or path.parent == home / "Developer/tools/rex-visible"
            or path.parent == Path(__file__).resolve().parent
            or str(path.parent).endswith(("/skills/python-tooling/scripts/rex", "/skills/rex/scripts")))


def _hook_specs(codex):
    specs = []
    visible = (HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT, HookEvent.PRE_TOOL_USE)
    specs.extend(HookSpec("rex_visible.py", event,
                          "Bash|exec_command|shell|shell_command" if event == HookEvent.PRE_TOOL_USE else None,
                          5) for event in visible)
    script = "codex_sessions.py" if codex else "claude_sessions.py"
    events = ((HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT, HookEvent.SUBAGENT_START,
               HookEvent.SUBAGENT_STOP, HookEvent.STOP, HookEvent.SESSION_END, HookEvent.POST_TOOL_USE,
               HookEvent.PERMISSION_REQUEST) if codex else
              (HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT, HookEvent.SUBAGENT_START,
               HookEvent.SUBAGENT_STOP, HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE, HookEvent.STOP,
               HookEvent.STOP_FAILURE, HookEvent.SESSION_END, HookEvent.TEAMMATE_IDLE,
               HookEvent.TASK_CREATED, HookEvent.TASK_COMPLETED, HookEvent.NOTIFICATION,
               HookEvent.PERMISSION_REQUEST, HookEvent.POST_MODEL_SWITCH))
    for event in events:
        timeout = 3 if event == HookEvent.SESSION_END else (20 if event == HookEvent.SUBAGENT_START else 5)
        # PostToolUse remains a broad lifecycle observer; only these two scripts
        # participate in the legacy matcher broadening migration below.
        specs.append(HookSpec(script, event, None, timeout))
    if codex:
        specs.append(HookSpec(script, HookEvent.PRE_TOOL_USE, CODEX_QUESTION_TOOL_MATCHER, 10))
    agent = "codex" if codex else "claude"
    matcher = CODEX_QUESTION_TOOL_MATCHER if codex else CLAUDE_QUESTION_TOOL_MATCHER
    specs.extend((HookSpec("question_hooks.py", HookEvent.PRE_TOOL_USE, matcher, 60 if not codex else 10),
                  HookSpec("question_hooks.py", HookEvent.POST_TOOL_USE, matcher, 10),
                  HookSpec("question_hooks.py", HookEvent.USER_PROMPT_SUBMIT, None, 5),
                  HookSpec("question_hooks.py", HookEvent.STOP, None, 5),
                  HookSpec("question_hooks.py", HookEvent.SESSION_END, None, 5),
                  HookSpec("question_hooks.py", HookEvent.SUBAGENT_STOP, None, 5)))
    if not codex:
        specs.append(HookSpec("question_hooks.py", HookEvent.PERMISSION_REQUEST, matcher, 60))
    return [(spec, agent if spec.script == "question_hooks.py" else None) for spec in specs]


def _command(spec, root, uv, agent, question_escalation: QuestionEscalation = QuestionEscalation.OFF):
    words = ["env", f"{FLAG_ENV}={question_escalation.environment_value}",
             uv, "run", "--script", str(root / spec.script), "hook"]
    if spec.script == "question_hooks.py":
        words.extend(("--agent", agent))
    return shlex.join(words)


def _matches_spec(group, spec):
    return group.get("matcher") == spec.matcher


def _apply_spec(data, spec, root, home, uv, agent, sibling_matchers=None,
                question_escalation: QuestionEscalation = QuestionEscalation.OFF):
    groups = data.setdefault("hooks", {}).setdefault(spec.event.value, [])
    command = _command(spec, root, uv, agent, question_escalation)
    matches = []
    sibling_matchers = sibling_matchers or {spec.matcher}
    # A script/event pair can own several matcher groups. Move only handlers
    # that belong to this spec; leave mixed groups and their matchers intact.
    for group in list(groups):
        owned = [handler for handler in group.get("hooks", [])
                 if owned_handler(handler.get("command", ""), spec.script, home, root)]
        if not owned:
            continue
        if _matches_spec(group, spec):
            matches.extend((group, handler) for handler in owned)
        elif group.get("matcher") in sibling_matchers:
            continue
        else:
            owned_ids = {id(handler) for handler in owned}
            remaining = [handler for handler in group.get("hooks", []) if id(handler) not in owned_ids]
            if remaining:
                group["hooks"] = remaining
            else:
                groups.remove(group)
    if matches:
        keeper_group, keeper = matches[0]
        keeper["command"] = command
        keeper["timeout"] = spec.timeout
        # De-duplicate exact owned handlers in this matcher's group.
        keepers = keeper_group.get("hooks", [])
        seen_keeper = False
        for handler in list(keepers):
            if owned_handler(handler.get("command", ""), spec.script, home, root):
                if not seen_keeper:
                    seen_keeper = True
                else:
                    duplicate_index = next(index for index, candidate in enumerate(keepers)
                                           if candidate is handler)
                    keepers.pop(duplicate_index)
        for group, handler in matches[1:]:
            match_index = next((index for index, candidate in enumerate(group.get("hooks", []))
                                if candidate is handler), None)
            if match_index is not None:
                group["hooks"].pop(match_index)
            if not group.get("hooks"):
                groups.remove(group)
        return
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": spec.timeout}],
                   **({"matcher": spec.matcher} if spec.matcher is not None else {})})


def rex_hooks(data, root, home, uv, codex, question_escalation: QuestionEscalation = QuestionEscalation.OFF):
    all_specs = _hook_specs(codex)
    for spec, agent in all_specs:
        # The legacy observer behavior intentionally makes these scripts global
        # PostToolUse observers, while question_hooks retain exact ownership.
        if spec.event == HookEvent.POST_TOOL_USE and spec.script in BROADEN_POST_TOOL_USE:
            groups = data.setdefault("hooks", {}).setdefault(spec.event.value, [])
            for group in list(groups):
                owned = [handler for handler in group.get("hooks", [])
                         if owned_handler(handler.get("command", ""), spec.script, home, root)]
                if owned and group.get("matcher"):
                    if len(owned) == len(group["hooks"]):
                        group.pop("matcher")
                    else:
                        group["hooks"] = [handler for handler in group["hooks"] if handler not in owned]
                        groups.append({"hooks": owned})
        sibling_matchers = {other.matcher for other, _ in all_specs
                            if other.script == spec.script and other.event == spec.event}
        _apply_spec(data, spec, root, home, uv, agent, sibling_matchers, question_escalation)


def _has_enabled_owned_hook(data, root, home, codex):
    scripts = {spec.script for spec, _ in _hook_specs(codex)}
    return any(
        f"{FLAG_ENV}=1" in shlex.split(handler.get("command", ""))
        for groups in data.get("hooks", {}).values()
        for group in groups
        for handler in group.get("hooks", [])
        if any(owned_handler(handler.get("command", ""), script, home, root) for script in scripts)
    )


def install(home, agents, question_escalation: QuestionEscalation = QuestionEscalation.OFF):
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required; install uv and ensure it is on PATH")
    uv = str(Path(uv).absolute())
    source = Path(__file__).resolve().parent
    runtime = home / ".local/share/agent-tooling/rex"
    if source != runtime.resolve():
        # Include the callback used by the existing lifecycle handlers; no extra hooks.
        shutil.copytree(source, runtime, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".state", "*.pyc"))
    changed = []
    downgrades_question_escalation = False
    for agent in agents:
        codex = agent == "codex"
        path = home / (".codex/hooks.json" if codex else ".claude/settings.json")
        data = json.loads(path.read_text()) if path.exists() else {}
        if (question_escalation is QuestionEscalation.OFF
                and _has_enabled_owned_hook(data, runtime, home, codex)):
            downgrades_question_escalation = True
        rex_hooks(data, runtime, home, uv, codex, question_escalation)
        if write(path, json.dumps(data, indent=2) + "\n"):
            changed.append(str(path))
        instructions = home / (".codex/AGENTS.md" if codex else ".claude/CLAUDE.md")
        marked_block(instructions, "rex-visible", "## Visible terminal work\n\nRun important commands in the existing main agent Rex session. Reuse its tabs and owned panes; never create task or team sessions for visibility. Before important terminal work, read " + str(runtime / "POLICY.md") + " and follow its runner, layout, verification, and retention rules. Keep quick reads in ordinary tools. Preserve normal permissions and explicitly requested machines. Access other machines through their configured Rex connection directly, never SSH.")
        if codex:
            marked_block(instructions, "codex-coordination", "## Conversational agent coordination\n\nUse direct tools for small deterministic work. Delegate bounded work to fresh focused workers with compact context; escalate models when evidence requires it. Read " + str(runtime / "COORDINATION.md") + " once before coordinating, then consult changed sections. Keep ownership clear and views in the main agent Rex session.")
        else:
            marked_block(instructions, "claude-coordination", "## Conversational agent coordination\n\nCoordinate the user's objective using Claude's native Agent and SendMessage tools when available. Before delegation, background work, or session coordination, read " + str(runtime / "COORDINATION.md") + ". Keep clear ownership and use the existing main agent Rex session for read-only activity views. Preserve normal permissions and host trust.")

    return {"runtime": str(runtime), "changed": changed,
            "question_escalation": question_escalation.value,
            "GROKBOT_QE": question_escalation.environment_value,
            "downgrades_question_escalation": downgrades_question_escalation,
            "hook_review": "Review new or changed hooks in the host's /hooks menu. This installer never changes hook trust or tool permissions."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", nargs="+", choices=("codex", "claude-code"), default=["codex", "claude-code"])
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    parser.add_argument("--question-escalation", type=QuestionEscalation,
                        choices=list(QuestionEscalation), default=QuestionEscalation.OFF,
                        help="Operator-controlled question escalation for installed hooks (default: off).")
    args = parser.parse_args()
    receipt = install(args.home.expanduser().resolve(), args.agents, args.question_escalation)
    if receipt["downgrades_question_escalation"]:
        print("Warning: reinstalling without --question-escalation changes existing GROKBOT_QE=1 hooks to GROKBOT_QE=0.", file=sys.stderr)
    print(json.dumps(receipt, indent=2))
